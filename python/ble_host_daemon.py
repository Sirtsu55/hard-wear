#!/usr/bin/env python3
"""Host-side BlueZ BLE beacon & scanner bridge for Hard Wear.

Runs on the UNO Q Linux host (outside Docker) where BlueZ `hci0` lives, and exposes
a newline-delimited JSON Unix domain socket at `/home/arduino/ArduinoApps/hard-wear/.ble.sock`
(mounted inside the app container at `/app/.ble.sock`).

Wire format (BLE Manufacturer ID 0xFFFF, up to 20 payload bytes):
  Byte 0..1 : 0x48 0x57 ("HW" magic)
  Byte 2..3 : sender 16-bit badge ID (last 2 bytes of HCI BD_ADDR)
  Byte 4..5 : target 16-bit badge ID (0x0000 = broadcast beacon)
  Byte 6    : seq (uint8 monotonic packet sequence number)
  Byte 7    : opcode (uint8)
  Byte 8..N : compact payload bytes (game id, move, paddle y, etc.)
"""

import json
import os
import pty
import re
import select
import signal
import socket
import subprocess
import sys
import threading
import time

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOCK_PATH = os.path.join(APP_DIR, ".ble.sock")

ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]|\r")
CTRL_MAC_RE = re.compile(r"Controller\s+([0-9A-F:]{17})", re.I)
DEV_HDR_RE = re.compile(r"\[(NEW|CHG)\]\s+Device\s+([0-9A-F:]{17})\s+(.*)", re.I)
HEX_LINE_RE = re.compile(r"^\s*([0-9a-f]{2}(?:\s+[0-9a-f]{2})+)", re.I)


def get_adapter_info() -> tuple[int, str]:
    """Return (16-bit node_id, short_name) from hci0 BD_ADDR and system hostname."""
    mac = ""
    try:
        out = subprocess.check_output(
            ["busctl", "call", "org.bluez", "/org/bluez/hci0",
             "org.freedesktop.DBus.Properties", "Get", "ss", "org.bluez.Adapter1", "Address"],
            text=True,
            timeout=3,
        )
        m = re.search(r'"([0-9A-F:]{17})"', out, re.I)
        if m:
            mac = m.group(1)
    except Exception:
        pass

    if mac:
        parts = mac.split(":")
        node_id = (int(parts[-2], 16) << 8) | int(parts[-1], 16)
        suffix = f"{parts[-2]}{parts[-1]}".upper()
    else:
        node_id = os.getpid() & 0xFFFF
        suffix = f"{node_id:04X}"

    return node_id, f"HW-{suffix}"


class BluezController:
    def __init__(self, on_rx_packet):
        self.node_id, self.badge_name = get_adapter_info()
        self.on_rx_packet = on_rx_packet
        self._pty_fd = None
        self._proc = None
        self._write_lock = threading.Lock()
        self._running = True
        self._cur_dev_mac = None
        self._awaiting_mfg_hex = False
        self._dev_rssi = {}
        self._dev_names = {}
        self._last_adv_hex = ""

    def start(self):
        master_fd, slave_fd = pty.openpty()
        self._pty_fd = master_fd
        self._proc = subprocess.Popen(
            ["bluetoothctl"],
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            close_fds=True,
        )
        os.close(slave_fd)

        threading.Thread(target=self._reader_loop, daemon=True).start()
        time.sleep(0.5)
        self._init_radio()

    def _send_cmds(self, cmds: list[str], delay: float = 0.06):
        with self._write_lock:
            if self._pty_fd is None:
                return
            for c in cmds:
                try:
                    os.write(self._pty_fd, (c + "\n").encode("utf-8"))
                except OSError:
                    return
                if delay > 0:
                    time.sleep(delay)

    def _init_radio(self):
        # Set friendly alias HW-XXXX, enable duplicate LE scan, and start default beacon
        idle_payload = [0x48, 0x57, (self.node_id >> 8) & 0xFF, self.node_id & 0xFF, 0x00, 0x00, 0x00, 0x01]
        hex_args = " ".join(f"0x{b:02x}" for b in idle_payload)
        self._last_adv_hex = hex_args
        self._send_cmds([
            "power on",
            f"system-alias {self.badge_name}",
            "menu scan",
            "clear",
            "transport le",
            "duplicate-data on",
            "back",
            "menu advertise",
            "clear",
            "interval 60 90",
            f"manufacturer 0xffff {hex_args}",
            "name on",
            "back",
            "advertise on",
            "scan on",
        ], delay=0.10)
        print(f"[ble-host] started as {self.badge_name} (0x{self.node_id:04X})", flush=True)

    def set_advertisement(self, payload_bytes: bytes):
        raw = [0x48, 0x57] + list(payload_bytes[:20])
        hex_args = " ".join(f"0x{b:02x}" for b in raw)
        if hex_args == self._last_adv_hex:
            return
        self._last_adv_hex = hex_args
        self._send_cmds([
            "advertise off",
            "menu advertise",
            f"manufacturer 0xffff {hex_args}",
            "back",
            "advertise on",
        ], delay=0.025)

    def _reader_loop(self):
        buf = ""
        while self._running and self._pty_fd is not None:
            try:
                r, _, _ = select.select([self._pty_fd], [], [], 0.5)
                if not r:
                    continue
                chunk = os.read(self._pty_fd, 4096).decode("utf-8", errors="ignore")
                if not chunk:
                    break
                buf += chunk
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    self._parse_line(line)
            except OSError:
                break

    def _parse_line(self, raw_line: str):
        clean = ANSI_RE.sub("", raw_line).strip()
        if not clean:
            return
        # Strip bluetoothctl prompt prefix if present on same line
        if "]>" in clean:
            clean = clean.split("]>", 1)[-1].strip()
        if not clean:
            return

        m = DEV_HDR_RE.search(clean)
        if m:
            mac = m.group(2).upper()
            rest = m.group(3).strip()
            self._cur_dev_mac = mac
            self._awaiting_mfg_hex = False

            if rest.startswith("RSSI:"):
                try:
                    val = int(rest.split()[1])
                    self._dev_rssi[mac] = val
                except Exception:
                    pass
            elif rest.startswith("Name:") or rest.startswith("Alias:"):
                self._dev_names[mac] = rest.split(":", 1)[1].strip()
            elif "ManufacturerData.Value:" in rest or "ManufacturerData Value:" in rest:
                self._awaiting_mfg_hex = True
            elif m.group(1).upper() == "NEW" and rest and not rest.startswith("ManufacturerData"):
                self._dev_names.setdefault(mac, rest)
            return

        if self._awaiting_mfg_hex and self._cur_dev_mac:
            hm = HEX_LINE_RE.match(clean)
            if hm:
                self._awaiting_mfg_hex = False
                hex_bytes = [int(x, 16) for x in hm.group(1).split()]
                if len(hex_bytes) >= 8 and hex_bytes[0] == 0x48 and hex_bytes[1] == 0x57:
                    mac = self._cur_dev_mac
                    self.on_rx_packet(
                        mac=mac,
                        name=self._dev_names.get(mac, ""),
                        rssi=self._dev_rssi.get(mac, -70),
                        payload=bytes(hex_bytes[2:]),
                    )
            elif not clean.startswith("ManufacturerData"):
                self._awaiting_mfg_hex = False

    def stop(self):
        self._running = False
        self._send_cmds(["scan off", "advertise off", "quit"], delay=0.05)
        if self._proc:
            try:
                self._proc.terminate()
            except Exception:
                pass


class BleSocketServer:
    def __init__(self):
        self.clients = set()
        self.clients_lock = threading.Lock()
        self.ble = BluezController(self._on_ble_rx)

    def _broadcast_json(self, obj: dict):
        line = (json.dumps(obj) + "\n").encode("utf-8")
        dead = []
        with self.clients_lock:
            for c in self.clients:
                try:
                    c.sendall(line)
                except OSError:
                    dead.append(c)
            for c in dead:
                self.clients.discard(c)
                try:
                    c.close()
                except Exception:
                    pass

    def _on_ble_rx(self, mac: str, name: str, rssi: int, payload: bytes):
        src_id = (payload[0] << 8) | payload[1]
        if src_id == self.ble.node_id:
            return  # ignore our own reflections
        self._broadcast_json({
            "event": "rx",
            "mac": mac,
            "name": name,
            "rssi": rssi,
            "hex": payload.hex(),
        })

    def _handle_client(self, conn: socket.socket):
        with self.clients_lock:
            self.clients.add(conn)
        # Immediately greet client with local node_id & badge_name
        hello = json.dumps({
            "event": "hello",
            "node_id": self.ble.node_id,
            "name": self.ble.badge_name,
        }) + "\n"
        try:
            conn.sendall(hello.encode("utf-8"))
        except OSError:
            with self.clients_lock:
                self.clients.discard(conn)
            return

        buf = ""
        try:
            while True:
                data = conn.recv(4096)
                if not data:
                    break
                buf += data.decode("utf-8", errors="ignore")
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        cmd = json.loads(line)
                    except Exception:
                        continue
                    if cmd.get("cmd") == "adv":
                        raw = bytes.fromhex(cmd.get("hex", ""))
                        self.ble.set_advertisement(raw)
        except OSError:
            pass
        finally:
            with self.clients_lock:
                self.clients.discard(conn)
            try:
                conn.close()
            except Exception:
                pass

    def serve_forever(self):
        self.ble.start()
        os.makedirs(APP_DIR, exist_ok=True)
        try:
            os.unlink(SOCK_PATH)
        except FileNotFoundError:
            pass

        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(SOCK_PATH)
        os.chmod(SOCK_PATH, 0o666)
        srv.listen(4)

        def _shutdown(*_):
            self.ble.stop()
            try:
                os.unlink(SOCK_PATH)
            except Exception:
                pass
            sys.exit(0)

        signal.signal(signal.SIGTERM, _shutdown)
        signal.signal(signal.SIGINT, _shutdown)

        while True:
            conn, _ = srv.accept()
            threading.Thread(target=self._handle_client, args=(conn,), daemon=True).start()


if __name__ == "__main__":
    BleSocketServer().serve_forever()
