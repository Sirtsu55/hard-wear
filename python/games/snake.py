"""1v1 & Solo Snake on the 13x8 LED Matrix.

Controls:
    Knob clockwise ("next")       -> steer 90 deg clockwise
    Knob counter-clockwise ("prev")-> steer 90 deg counter-clockwise

Roles:
    "local": Solo vs built-in AI rival snake (or survive & eat!).
    "host":  BLE Host simulates both snakes & food, sends compact tick sync.
    "guest": BLE Guest sends steering turns & reconstructs smooth snake trails locally.

Snapshot wire format to MCU (`draw_board`):
    "S,<state>,<my_len>,<opp_len>,<104-char grid>"
    Grid chars (y*13 + x):
      '.' = empty
      'M' = my snake head (bright 7)
      'm' = my snake body (level 5)
      'E' = rival snake head (level 6)
      'e' = rival snake body (level 2)
      'F' = food pellet (blinking 7)
"""

import random
import threading
import time

from .base import Game

W = 13
H = 8
# Directions: 0=Right, 1=Down, 2=Left, 3=Up
DX = [1, 0, -1, 0]
DY = [0, 1, 0, -1]
TICK_SEC = 0.28


class Snake(Game):
    name = "snake"
    title = "1v1 Snake"

    def __init__(self, io, role: str):
        super().__init__(io, role)
        self._lock = threading.Lock()
        self._running = False

        # Host/Solo spawns on left heading Right; Rival spawns on right heading Left
        if role == "guest":
            self.my_body = [(10, 5), (11, 5), (12, 5)]
            self.my_dir = 2
            self.opp_body = [(2, 2), (1, 2), (0, 2)]
            self.opp_dir = 0
        else:
            self.my_body = [(2, 2), (1, 2), (0, 2)]
            self.my_dir = 0
            self.opp_body = [(10, 5), (11, 5), (12, 5)]
            self.opp_dir = 2

        self._queued_dir = self.my_dir
        self._opp_queued_dir = self.opp_dir
        self._last_n = -1
        self._tick_n = 0
        self._last_pkt = None
        self.food = (6, 3)

    def start(self):
        self._running = True
        self.render()
        if self.role in ("local", "host"):
            threading.Thread(target=self._loop, daemon=True).start()

    def quit(self, notify_peer: bool = True):
        self._running = False
        super().quit(notify_peer=notify_peer)

    def handle_local(self, action: str):
        if self.over:
            return
        with self._lock:
            # Turn relative to the latest queued heading so quick double turns work
            base = self._queued_dir
            if action == "next":
                nd = (base + 1) % 4
            elif action == "prev":
                nd = (base - 1) % 4
            else:
                return
            # Prevent 180-degree instant self-reversal (vs the heading actually moving)
            if (nd + 2) % 4 != self.my_dir:
                self._queued_dir = nd

        if self.role == "guest":
            d = self._queued_dir
            self.io.send({"game": self.name, "type": "snake_turn", "dir": d})
            # A lost turn means the snake ignores the player: repeat it while still current
            for delay in (0.08, 0.2):
                t = threading.Timer(delay, self._resend_turn, args=(d,))
                t.daemon = True
                t.start()

    def _resend_turn(self, d: int):
        if self.over or self._queued_dir != d:
            return
        self.io.send({"game": self.name, "type": "snake_turn", "dir": d})

    def handle_remote(self, msg: dict):
        if self.over or self.role == "local":
            return
        kind = msg.get("type")
        if kind == "quit":
            self._running = False
            self._finish("quit")
            return

        with self._lock:
            if self.role == "host" and kind == "snake_turn":
                nd = int(msg.get("dir", 0)) % 4
                if (nd + 2) % 4 != self.opp_dir:
                    self._opp_queued_dir = nd
            elif self.role == "guest" and kind == "snake_sync":
                # Drop stale/reordered ticks ("n" = host tick counter; absent on the BLE path)
                n = msg.get("n")
                if n is not None:
                    if n <= self._last_n:
                        return
                    self._last_n = n
                # Host h1 = host head, h2 = guest head
                h1 = int(msg.get("h1", 0))
                h2 = int(msg.get("h2", 0))
                fpos = int(msg.get("food", 0))
                l1 = max(1, int(msg.get("l1", 3)))
                l2 = max(1, int(msg.get("l2", 3)))
                res = int(msg.get("res", 0))  # 0=playing, 1=host_win, 2=guest_win, 3=draw

                if 0 <= fpos < W * H:
                    self.food = (fpos % W, fpos // W)
                if 0 <= h2 < W * H:
                    nh = (h2 % W, h2 // W)
                    if not self.my_body or self.my_body[0] != nh:
                        if self.my_body:
                            # Derive real heading from the host-authoritative head move (torus wrap)
                            ox, oy = self.my_body[0]
                            for d in range(4):
                                if (ox + DX[d]) % W == nh[0] and (oy + DY[d]) % H == nh[1]:
                                    self.my_dir = d
                                    self._queued_dir = d
                                    break
                        self.my_body.insert(0, nh)
                    while len(self.my_body) > l2:
                        self.my_body.pop()
                if 0 <= h1 < W * H:
                    oh = (h1 % W, h1 // W)
                    if not self.opp_body or self.opp_body[0] != oh:
                        self.opp_body.insert(0, oh)
                    while len(self.opp_body) > l1:
                        self.opp_body.pop()

                if res != 0:
                    self._running = False
                    self.over = True
                    if res == 2:
                        self.result = "win"
                    elif res == 1:
                        self.result = "lose"
                    else:
                        self.result = "draw"
        self.render()

    def _ai_pick_dir(self):
        """Smart rival snake AI for local solo mode."""
        hx, hy = self.opp_body[0]
        fx, fy = self.food
        occupied = set(self.my_body) | set(self.opp_body[:-1])
        best_d = self.opp_dir
        best_score = 9999
        for cand in range(4):
            if (cand + 2) % 4 == self.opp_dir:
                continue
            nx = (hx + DX[cand]) % W
            ny = (hy + DY[cand]) % H
            if (nx, ny) in occupied:
                continue
            dist = abs(nx - fx) + abs(ny - fy)
            if dist < best_score:
                best_score = dist
                best_d = cand
        return best_d

    def _spawn_food(self):
        occupied = set(self.my_body) | set(self.opp_body)
        free = [(x, y) for y in range(H) for x in range(W) if (x, y) not in occupied]
        if free:
            self.food = random.choice(free)

    def _loop(self):
        while self._running and not self.over:
            time.sleep(TICK_SEC)
            if not self._running or self.over:
                break

            with self._lock:
                self.my_dir = self._queued_dir
                if self.role == "local":
                    self.opp_dir = self._ai_pick_dir()
                else:
                    self.opp_dir = self._opp_queued_dir

                # Wrap around edges on the compact 13x8 torus so duels last longer and feel great!
                nh1 = ((self.my_body[0][0] + DX[self.my_dir]) % W,
                       (self.my_body[0][1] + DY[self.my_dir]) % H)
                nh2 = ((self.opp_body[0][0] + DX[self.opp_dir]) % W,
                       (self.opp_body[0][1] + DY[self.opp_dir]) % H)

                ate1 = (nh1 == self.food)
                ate2 = (nh2 == self.food)

                body1_check = self.my_body if ate1 else self.my_body[:-1]
                body2_check = self.opp_body if ate2 else self.opp_body[:-1]

                dead1 = (nh1 == nh2) or (nh1 in body1_check) or (nh1 in body2_check)
                dead2 = (nh2 == nh1) or (nh2 in body2_check) or (nh2 in body1_check)

                res_code = 0  # 0=playing, 1=host_win, 2=guest_win, 3=draw
                if dead1 and dead2:
                    self._running = False
                    self.over = True
                    self.result = "draw"
                    res_code = 3
                elif dead1:
                    self._running = False
                    self.over = True
                    self.result = "lose"
                    res_code = 2
                elif dead2:
                    self._running = False
                    self.over = True
                    self.result = "win"
                    res_code = 1
                else:
                    self.my_body.insert(0, nh1)
                    if not ate1:
                        self.my_body.pop()
                    self.opp_body.insert(0, nh2)
                    if not ate2:
                        self.opp_body.pop()
                    if ate1 or ate2:
                        self._spawn_food()

                if self.role == "host":
                    h1_idx = self.my_body[0][1] * W + self.my_body[0][0]
                    h2_idx = self.opp_body[0][1] * W + self.opp_body[0][0]
                    f_idx = self.food[1] * W + self.food[0]
                    self._tick_n += 1
                    pkt = {
                        "game": self.name,
                        "type": "snake_sync",
                        "n": self._tick_n,
                        "h1": h1_idx,
                        "h2": h2_idx,
                        "food": f_idx,
                        "l1": len(self.my_body),
                        "l2": len(self.opp_body),
                        "res": res_code,
                    }
                else:
                    pkt = None

            if pkt is not None:
                self._last_pkt = pkt
                self.io.send(pkt)
            self.render()

        # Game over: the final tick carries the result, so repeat it (lossy link)
        if self.role == "host" and self.over and self._last_pkt and self._last_pkt.get("res"):
            for _ in range(4):
                time.sleep(0.25)
                self.io.send(self._last_pkt)

    def snapshot(self) -> str:
        grid = ["."] * (W * H)
        fx, fy = self.food
        if 0 <= fx < W and 0 <= fy < H:
            grid[fy * W + fx] = "F"

        for i, (x, y) in enumerate(reversed(self.opp_body)):
            if 0 <= x < W and 0 <= y < H:
                grid[y * W + x] = "E" if i == len(self.opp_body) - 1 else "e"

        for i, (x, y) in enumerate(reversed(self.my_body)):
            if 0 <= x < W and 0 <= y < H:
                grid[y * W + x] = "M" if i == len(self.my_body) - 1 else "m"

        if self.over:
            if self.result == "quit":
                st = "q"
            elif self.result == "win":
                st = "w"
            elif self.result == "lose":
                st = "l"
            else:
                st = "d"
        else:
            st = "p"

        return f"S,{st},{len(self.my_body)},{len(self.opp_body)}," + "".join(grid)

    def status_text(self) -> str:
        return f"Snake {len(self.my_body)} vs {len(self.opp_body)}"
