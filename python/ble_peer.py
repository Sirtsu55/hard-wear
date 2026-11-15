"""Container-side BLE peer discovery, Proximity Auto-Challenge, and 1v1 Game Link.

Connects to `/app/.ble.sock` exposed by `ble_host_daemon.py` on the UNO Q host.

Compact BLE advertisement packet payload (after "HW" magic):
  bytes 0..1 : src_id   (uint16 big-endian)
  bytes 2..3 : dst_id   (uint16 big-endian, 0x0000 = broadcast)
  byte  4    : seq      (uint8 monotonic packet sequence)
  byte  5    : opcode   (uint8)
  bytes 6..  : args     (game code, move/paddle/snake bytes)

Opcodes:
  0x01 BEACON        : idle presence broadcast
  0x02 INVITE        : manual/direct challenge [game_code]
  0x03 ACCEPT        : manual accept [game_code]
  0x04 DECLINE       : decline / cancel challenge
  0x05 READY_YES     : mutual auto-challenge 'Yes' vote
  0x06 START_MATCH   : host locks game selection & starts both boards [game_code]
  0x10 GAME_MSG      : args = [game_code, msg_kind, ...]
"""

import json
import os
import socket
import threading
import time

SOCK_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".cache", "ble.sock")

OP_BEACON = 0x01
OP_INVITE = 0x02
OP_ACCEPT = 0x03
OP_DECLINE = 0x04
OP_READY_YES = 0x05
OP_START_MATCH = 0x06
OP_GAME_MSG = 0x10

GAME_CODES = {
    "pong": 2,
    "snake": 3,
    "tictactoe": 1,
}
CODE_TO_GAME = {v: k for k, v in GAME_CODES.items()}

GAME_ORDER = ["pong", "snake", "tictactoe"]
GAME_TITLES = {
    "pong": "1v1 Pong",
    "snake": "1v1 Snake",
    "tictactoe": "Tic-Tac-Toe",
}

MSG_MOVE = 1
MSG_STATE = 2
MSG_POINT = 3
MSG_SYNC = 4
MSG_QUIT = 9

PEER_STALE_SEC = 18.0
AUTO_CHALLENGE_RSSI = -82       # triggers when two badges are in proximity (~5-8m)
AUTO_CHALLENGE_COOLDOWN = 65.0  # seconds before re-prompting the same peer after No / match end


class BlePeerManager:
    def __init__(
        self,
        on_peers_changed,
        on_auto_challenge,
        on_peer_voted_yes,
        on_peer_declined,
        on_start_match,
        on_game_msg,
    ):
        self.on_peers_changed = on_peers_changed
        self.on_auto_challenge = on_auto_challenge    # (peer_id, peer_name)
        self.on_peer_voted_yes = on_peer_voted_yes    # (peer_id, peer_name, game_code_or_0)
        self.on_peer_declined = on_peer_declined      # (peer_id, peer_name)
        self.on_start_match = on_start_match          # (peer_id, peer_name, game_name, role)
        self.on_game_msg = on_game_msg                # (msg_dict)

        self.node_id = 0
        self.badge_name = "HW-????"
        self.busy = False  # True while in a match, game picker, or challenge popup

        self._sock = None
        self._lock = threading.Lock()
        self._seq = 1
        self._peers = {}       # peer_id -> {"id", "name", "rssi", "last_seen", "last_seq"}
        self._cooldowns = {}   # peer_id -> monotonic timestamp until auto-challenge suppressed
        self._active_peer_id = 0
        self._revert_timer = None
        self._running = True

        # Wi-Fi/UDP game link (relayed through the host daemon, which owns the UDP port)
        self.local_ip = os.environ.get("HOST_IP", "0.0.0.0")
        self._peer_ips = {}        # peer_id -> "a.b.c.d" (learned from BLE beacons/votes/UDP)
        self._udp_last_rx = {}     # peer_id -> last time a UDP datagram arrived from it
        self._start_acked = False
        self._start_pending = False

    @staticmethod
    def _ip_to_bytes(ip: str) -> list[int]:
        try:
            parts = [int(x) & 0xFF for x in ip.split(".")]
            return parts if len(parts) == 4 else [0, 0, 0, 0]
        except Exception:
            return [0, 0, 0, 0]

    @staticmethod
    def _bytes_to_ip(b) -> str:
        return ".".join(str(int(x)) for x in b[:4]) if len(b) >= 4 else ""

    def _remember_ip(self, peer_id: int, ip: str):
        if ip and ip != "0.0.0.0":
            with self._lock:
                self._peer_ips[peer_id] = ip

    def _udp_send(self, peer_id: int, obj: dict) -> bool:
        with self._lock:
            ip = self._peer_ips.get(peer_id)
        if not ip or not self._sock:
            return False
        self._write_daemon({"cmd": "udp", "ip": ip, "data": json.dumps(obj, separators=(",", ":"))})
        return True

    def _udp_alive(self, peer_id: int, within: float = 4.0) -> bool:
        return time.time() - self._udp_last_rx.get(peer_id, 0.0) <= within

    def start(self):
        threading.Thread(target=self._conn_loop, daemon=True).start()
        threading.Thread(target=self._prune_loop, daemon=True).start()

    def set_cooldown(self, peer_id: int, seconds: float = AUTO_CHALLENGE_COOLDOWN):
        if peer_id:
            with self._lock:
                self._cooldowns[peer_id] = time.time() + seconds

    def clear_cooldown(self, peer_id: int):
        with self._lock:
            self._cooldowns.pop(peer_id, None)

    def is_host_vs(self, peer_id: int) -> bool:
        """Deterministic tie-breaker when both badges vote Yes simultaneously."""
        return self.node_id >= peer_id

    # ---- Peer list queries -----------------------------------------------
    def get_peers(self) -> list[dict]:
        now = time.time()
        with self._lock:
            alive = [
                dict(p) for p in self._peers.values()
                if now - p["last_seen"] <= PEER_STALE_SEC
            ]
        alive.sort(key=lambda x: x["rssi"], reverse=True)
        return alive

    def peer_count(self) -> int:
        return len(self.get_peers())

    # ---- Outgoing BLE bursts ---------------------------------------------
    def _next_seq(self) -> int:
        with self._lock:
            self._seq = (self._seq % 250) + 1
            return self._seq

    def _send_adv(self, dst_id: int, opcode: int, args: list[int] = (), revert_after: float = 2.5):
        seq = self._next_seq() if opcode != OP_BEACON else 0
        pkt = bytes([
            (self.node_id >> 8) & 0xFF,
            self.node_id & 0xFF,
            (dst_id >> 8) & 0xFF,
            dst_id & 0xFF,
            seq & 0xFF,
            opcode & 0xFF,
        ] + [int(x) & 0xFF for x in list(args)[:12]])

        if opcode != OP_BEACON:
            print(f"[ble] tx op=0x{opcode:02X} to 0x{dst_id:04X} seq={seq} args={list(args)}", flush=True)
        self._write_daemon({"cmd": "adv", "hex": pkt.hex()})

        if self._revert_timer is not None:
            self._revert_timer.cancel()
            self._revert_timer = None

        if opcode != OP_BEACON and revert_after > 0:
            t = threading.Timer(revert_after, self.advertise_idle)
            t.daemon = True
            t.start()
            self._revert_timer = t

    def advertise_idle(self):
        if self.node_id == 0:
            return
        # Beacon args carry our Wi-Fi IP so peers can open the UDP game link.
        self._send_adv(0x0000, OP_BEACON, self._ip_to_bytes(self.local_ip), revert_after=0)

    def vote_yes(self, peer_id: int, preferred_game: str = ""):
        self._active_peer_id = peer_id
        gcode = GAME_CODES.get(preferred_game, 0)
        print(f"[ble] voted YES for challenge with 0x{peer_id:04X} (game={gcode})", flush=True)
        self._udp_send(peer_id, {"t": "vote", "from": self.node_id, "gcode": gcode})
        self._send_adv(peer_id, OP_READY_YES, [gcode] + self._ip_to_bytes(self.local_ip), revert_after=12.0)

    def vote_no(self, peer_id: int):
        self.set_cooldown(peer_id, AUTO_CHALLENGE_COOLDOWN)
        print(f"[ble] voted NO for challenge with 0x{peer_id:04X}", flush=True)
        self._udp_send(peer_id, {"t": "decline", "from": self.node_id})
        self._send_adv(peer_id, OP_DECLINE, [0], revert_after=2.5)

    def broadcast_start_match(self, peer_id: int, game_name: str, on_ready=None):
        """Tell the guest to start. `on_ready` (host-side begin) fires once the guest has
        acknowledged over Wi-Fi, so both boards start together; without a known peer IP or
        an ack within ~4s it fires anyway (legacy BLE behaviour)."""
        if self._start_pending:
            return
        self._start_pending = True
        self._active_peer_id = peer_id
        gcode = GAME_CODES.get(game_name, 2)
        print(f"[ble] host starting {game_name} with 0x{peer_id:04X}", flush=True)
        self._start_acked = False
        threading.Thread(target=self._udp_start_loop, args=(peer_id, game_name, on_ready), daemon=True).start()
        threading.Thread(target=self._burst_start_match, args=(peer_id, gcode), daemon=True).start()

    def _udp_start_loop(self, peer_id: int, game_name: str, on_ready=None):
        """Reliable start over Wi-Fi: re-send every 0.3s until the guest acknowledges."""
        try:
            with self._lock:
                have_ip = peer_id in self._peer_ips
            if have_ip:
                deadline = time.time() + 4.5
                next_send = 0.0
                while time.time() < deadline and not self._start_acked and self._active_peer_id == peer_id:
                    if time.time() >= next_send:
                        self._udp_send(peer_id, {"t": "start", "from": self.node_id, "game": game_name})
                        next_send = time.time() + 0.3
                    time.sleep(0.02)
                print(f"[ble] start ack={'yes' if self._start_acked else 'NO (timeout)'}", flush=True)
        finally:
            self._start_pending = False
            if on_ready:
                on_ready()

    def _burst_start_match(self, peer_id: int, gcode: int, repeats: int = 4, gap: float = 0.6):
        for i in range(repeats):
            if self._start_acked:
                break
            last = i == repeats - 1
            self._send_adv(
                peer_id, OP_START_MATCH, [gcode] + self._ip_to_bytes(self.local_ip),
                revert_after=3.0 if last else 0,
            )
            if not last:
                time.sleep(gap)
        if self._start_acked:
            self.advertise_idle()

    def send_game_dict(self, msg: dict):
        if not self._active_peer_id:
            return
        game_name = msg.get("game", "pong")
        gcode = GAME_CODES.get(game_name, 2)
        mtype = msg.get("type", "")

        # Preferred path: Wi-Fi UDP (full-fidelity dict, low latency). Fall back to BLE only
        # while no UDP traffic has been seen from the peer.
        if self._udp_send(self._active_peer_id, {"t": "g", "from": self.node_id, "msg": msg}):
            if self._udp_alive(self._active_peer_id):
                return

        if mtype == "quit":
            self._send_adv(self._active_peer_id, OP_GAME_MSG, [gcode, MSG_QUIT], revert_after=2.0)
        elif mtype == "move":
            # Tic-Tac-Toe cell move
            cell = int(msg.get("cell", 0)) & 0xFF
            self._send_adv(self._active_peer_id, OP_GAME_MSG, [gcode, MSG_MOVE, cell], revert_after=2.5)
        elif mtype == "paddle":
            # Pong paddle + authoritative ball/score sync from host
            py = int(msg.get("y", 0)) & 0xFF
            bx = int(msg.get("bx", 255)) & 0xFF
            by = int(msg.get("by", 255)) & 0xFF
            s1 = int(msg.get("s1", 0)) & 0xFF
            s2 = int(msg.get("s2", 0)) & 0xFF
            over = 1 if msg.get("over") else 0
            self._send_adv(
                self._active_peer_id,
                OP_GAME_MSG,
                [gcode, MSG_STATE, py, bx, by, s1, s2, over],
                revert_after=1.8,
            )
        elif mtype == "snake_turn":
            # Guest sends steering direction to host
            d = int(msg.get("dir", 0)) & 0xFF
            self._send_adv(self._active_peer_id, OP_GAME_MSG, [gcode, MSG_MOVE, d], revert_after=1.5)
        elif mtype == "snake_sync":
            # Host broadcasts compact authoritative tick state
            # args: [h1, h2, fpos, len1, len2, res]  (positions = y*13 + x, or 255)
            self._send_adv(
                self._active_peer_id,
                OP_GAME_MSG,
                [
                    gcode,
                    MSG_SYNC,
                    msg.get("h1", 0),
                    msg.get("h2", 0),
                    msg.get("food", 0),
                    msg.get("l1", 3),
                    msg.get("l2", 3),
                    msg.get("res", 0),
                ],
                revert_after=1.8,
            )

    # ---- Daemon socket loop ----------------------------------------------
    def _write_daemon(self, obj: dict):
        s = self._sock
        if not s:
            return
        try:
            s.sendall((json.dumps(obj) + "\n").encode("utf-8"))
        except OSError:
            pass

    def _conn_loop(self):
        last_warn = 0.0
        while self._running:
            if not os.path.exists(SOCK_PATH):
                if time.time() - last_warn > 15.0:
                    last_warn = time.time()
                    print(
                        "[ble] WARNING: host BLE daemon is not running (no socket) - "
                        "badges cannot discover each other. Run python/setup_ble_autostart.sh on the board.",
                        flush=True,
                    )
                time.sleep(1.2)
                continue
            try:
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.connect(SOCK_PATH)
                self._sock = s
                buf = ""
                while self._running:
                    data = s.recv(4096)
                    if not data:
                        break
                    buf += data.decode("utf-8", errors="ignore")
                    while "\n" in buf:
                        line, buf = buf.split("\n", 1)
                        line = line.strip()
                        if line:
                            self._handle_daemon_event(line)
            except OSError:
                pass
            finally:
                self._sock = None
            time.sleep(1.2)

    def _handle_daemon_event(self, raw_json: str):
        try:
            ev = json.loads(raw_json)
        except Exception:
            return

        kind = ev.get("event")
        if kind == "hello":
            self.node_id = int(ev.get("node_id", 0))
            self.badge_name = ev.get("name", "HW-????")
            ip = ev.get("ip", "")
            if ip and ip != "0.0.0.0":
                self.local_ip = ip
            print(
                f"[ble] connected to host radio as {self.badge_name} (0x{self.node_id:04X}) ip={self.local_ip}",
                flush=True,
            )
            self.advertise_idle()
            return

        if kind == "udp":
            self._handle_udp(ev)
            return

        if kind != "rx":
            return

        try:
            payload = bytes.fromhex(ev.get("hex", ""))
        except Exception:
            return
        if len(payload) < 6:
            return

        src_id = (payload[0] << 8) | payload[1]
        dst_id = (payload[2] << 8) | payload[3]
        seq = payload[4]
        opcode = payload[5]
        args = payload[6:]
        rssi = int(ev.get("rssi", -70))
        peer_name = f"HW-{src_id:04X}"

        now = time.time()
        count_changed = False
        is_new_seq = False
        should_auto_challenge = False

        with self._lock:
            prev_alive = sum(1 for p in self._peers.values() if now - p["last_seen"] <= PEER_STALE_SEC)
            entry = self._peers.get(src_id)
            if entry is None:
                entry = {
                    "id": src_id,
                    "name": peer_name,
                    "rssi": rssi,
                    "last_seen": now,
                    "last_seq": -1,
                }
                self._peers[src_id] = entry
                print(f"[ble] discovered peer {peer_name} (RSSI {rssi})", flush=True)
            else:
                entry["rssi"] = rssi
                entry["last_seen"] = now

            if seq != 0 and seq != entry["last_seq"]:
                entry["last_seq"] = seq
                is_new_seq = True

            new_alive = sum(1 for p in self._peers.values() if now - p["last_seen"] <= PEER_STALE_SEC)
            count_changed = (new_alive != prev_alive)

            # Check Proximity Auto-Challenge eligibility!
            cd_until = self._cooldowns.get(src_id, 0.0)
            if (
                not self.busy
                and rssi >= AUTO_CHALLENGE_RSSI
                and now >= cd_until
                and opcode in (OP_BEACON, OP_READY_YES, OP_INVITE)
            ):
                # Put brief 15s guard so this same discovery burst only pops once
                self._cooldowns[src_id] = now + 15.0
                should_auto_challenge = True

        if count_changed:
            self.on_peers_changed(new_alive)

        # Trigger simultaneous auto-challenge popup if discovered nearby while idle/menu!
        if should_auto_challenge:
            self.on_auto_challenge(src_id, peer_name)

        if opcode == OP_BEACON:
            self._remember_ip(src_id, self._bytes_to_ip(args))
        elif opcode in (OP_READY_YES, OP_INVITE, OP_ACCEPT, OP_START_MATCH):
            self._remember_ip(src_id, self._bytes_to_ip(args[1:]))

        if opcode != OP_BEACON:
            print(
                f"[ble] rx op=0x{opcode:02X} from 0x{src_id:04X} to 0x{dst_id:04X} "
                f"seq={seq} args={list(args)} new_seq={is_new_seq} me=0x{self.node_id:04X}",
                flush=True,
            )
        if dst_id not in (0x0000, self.node_id):
            return
        if not is_new_seq or opcode == OP_BEACON:
            return

        if opcode in (OP_READY_YES, OP_INVITE, OP_ACCEPT):
            gcode = args[0] if len(args) >= 1 else 0
            self.on_peer_voted_yes(src_id, peer_name, gcode)
        elif opcode == OP_DECLINE:
            self.set_cooldown(src_id, AUTO_CHALLENGE_COOLDOWN)
            self.on_peer_declined(src_id, peer_name)
        elif opcode == OP_START_MATCH and len(args) >= 1:
            gname = CODE_TO_GAME.get(args[0], "pong")
            self._active_peer_id = src_id
            self.advertise_idle()
            self.on_start_match(src_id, peer_name, gname, "guest")
        elif opcode == OP_GAME_MSG and len(args) >= 2:
            gname = CODE_TO_GAME.get(args[0], "pong")
            mkind = args[1]
            if mkind == MSG_QUIT:
                self.on_game_msg({"game": gname, "type": "quit"})
            elif mkind == MSG_MOVE and len(args) >= 3:
                if gname == "tictactoe":
                    self.on_game_msg({"game": gname, "type": "move", "cell": args[2]})
                elif gname == "snake":
                    self.on_game_msg({"game": gname, "type": "snake_turn", "dir": args[2]})
            elif mkind == MSG_STATE and len(args) >= 8:
                self.on_game_msg({
                    "game": gname,
                    "type": "paddle",
                    "y": args[2],
                    "bx": args[3],
                    "by": args[4],
                    "s1": args[5],
                    "s2": args[6],
                    "over": bool(args[7]),
                })
            elif mkind == MSG_SYNC and len(args) >= 8:
                self.on_game_msg({
                    "game": gname,
                    "type": "snake_sync",
                    "h1": args[2],
                    "h2": args[3],
                    "food": args[4],
                    "l1": args[5],
                    "l2": args[6],
                    "res": args[7],
                })

    def _handle_udp(self, ev: dict):
        try:
            obj = json.loads(ev.get("data", ""))
        except Exception:
            return
        src = int(obj.get("from", 0))
        if not src or src == self.node_id:
            return
        self._remember_ip(src, ev.get("ip", ""))
        self._udp_last_rx[src] = time.time()
        t = obj.get("t")

        if t == "start":
            gname = obj.get("game", "pong")
            self._udp_send(src, {"t": "ack", "from": self.node_id})
            self._active_peer_id = src
            self.advertise_idle()
            self.on_start_match(src, f"HW-{src:04X}", gname, "guest")
        elif t == "ack":
            self._start_acked = True
        elif t == "vote":
            self.on_peer_voted_yes(src, f"HW-{src:04X}", int(obj.get("gcode", 0)))
        elif t == "decline":
            self.set_cooldown(src, AUTO_CHALLENGE_COOLDOWN)
            self.on_peer_declined(src, f"HW-{src:04X}")
        elif t == "g" and src == self._active_peer_id:
            msg = obj.get("msg")
            if isinstance(msg, dict):
                self.on_game_msg(msg)

    def _prune_loop(self):
        last_reported = 0
        while self._running:
            time.sleep(3.0)
            cnt = self.peer_count()
            if cnt != last_reported:
                last_reported = cnt
                self.on_peers_changed(cnt)
