"""1v1 Pong on the 13x8 LED Matrix.

Roles:
    "local": Solo vs built-in AI paddle (left = You, right = AI).
    "host":  BLE Host (left col 0 = Host, right col 12 = Guest; simulates ball).
    "guest": BLE Guest (mirror-rendered so YOU are always left col 0 on your badge!).

Controls:
    Knob turn ("next" / "prev") moves your 3-pixel paddle up/down along column 0.
    First player to 5 points wins!

Snapshot wire format to MCU (`draw_board`):
    "P,<my_y>,<opp_y>,<ball_x>,<ball_y>,<my_score>,<opp_score>,<state>"
    where state = 'p' (playing), 'w' (you win), 'l' (you lose), 'q' (quit).
"""

import random
import threading
import time

from .base import Game

W = 13
H = 8
PADDLE_H = 3
WIN_SCORE = 5
TICK_SEC = 0.14


class Pong(Game):
    name = "pong"
    title = "1v1 Pong"

    def __init__(self, io, role: str):
        super().__init__(io, role)
        self.my_y = 2          # top of 3-pixel paddle (0..5)
        self.opp_y = 2
        self.bx = 6
        self.by = 3
        self.vx = 1 if random.random() < 0.5 else -1
        self.vy = 1 if random.random() < 0.5 else -1
        self.my_score = 0
        self.opp_score = 0
        self._lock = threading.Lock()
        self._running = False
        self._tick_count = 0
        self._last_n = -1

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
        moved = False
        with self._lock:
            if action == "next":
                if self.my_y < H - PADDLE_H:
                    self.my_y += 1
                    moved = True
            elif action == "prev":
                if self.my_y > 0:
                    self.my_y -= 1
                    moved = True
        if moved:
            self.render()
            if self.role == "guest":
                self.io.send(self._guest_paddle_pkt())

    def _guest_paddle_pkt(self) -> dict:
        return {
            "game": self.name,
            "type": "paddle",
            "y": self.my_y,
            "bx": 255,
            "by": 255,
            "s1": 0,
            "s2": 0,
            "over": False,
        }

    def _host_pkt(self) -> dict:
        return {
            "game": self.name,
            "type": "paddle",
            "n": self._tick_count,
            "y": self.my_y,
            "bx": self.bx,
            "by": self.by,
            "s1": self.my_score,
            "s2": self.opp_score,
            "over": self.over,
        }

    def handle_remote(self, msg: dict):
        if self.over or self.role == "local":
            return
        kind = msg.get("type")
        if kind == "quit":
            self._running = False
            self._finish("quit")
            return
        if kind != "paddle":
            return

        with self._lock:
            if self.role == "host":
                # Guest sent its paddle position
                gy = int(msg.get("y", 2))
                self.opp_y = max(0, min(H - PADDLE_H, gy))
            elif self.role == "guest":
                # Drop stale/reordered state (UDP can reorder; "n" is the host tick counter)
                n = msg.get("n")
                if n is not None:
                    if n <= self._last_n:
                        return
                    self._last_n = n
                # Host sent full authoritative state; mirror X so guest sees itself on col 0!
                hy = int(msg.get("y", 2))
                self.opp_y = max(0, min(H - PADDLE_H, hy))
                bx = int(msg.get("bx", 6))
                by = int(msg.get("by", 3))
                if 0 <= bx < W:
                    self.bx = (W - 1) - bx
                if 0 <= by < H:
                    self.by = by
                # Host s1 = host score, s2 = guest score
                self.opp_score = int(msg.get("s1", 0))
                self.my_score = int(msg.get("s2", 0))
                if msg.get("over"):
                    self._running = False
                    res = "win" if self.my_score > self.opp_score else "lose"
                    self.over = True
                    self.result = res
        self.render()
        if self.role == "guest" and not self.over:
            # Re-announce our paddle with every host state so a lost paddle packet self-heals
            self.io.send(self._guest_paddle_pkt())

    def _reset_ball(self, serve_dir: int):
        self.bx = 6
        self.by = random.randint(1, H - 2)
        self.vx = serve_dir
        self.vy = random.choice([-1, 1])

    def _loop(self):
        while self._running and not self.over:
            time.sleep(TICK_SEC)
            if not self._running or self.over:
                break

            sync_needed = False
            with self._lock:
                self._tick_count += 1

                # Solo AI paddle tracks the ball with human-like reaction speed
                if self.role == "local" and (self._tick_count % 2 == 0):
                    center = self.opp_y + 1
                    if self.vx > 0:
                        if self.by > center and self.opp_y < H - PADDLE_H:
                            self.opp_y += 1
                        elif self.by < center and self.opp_y > 0:
                            self.opp_y -= 1

                nx = self.bx + self.vx
                ny = self.by + self.vy

                # Top / bottom wall bounce
                if ny < 0:
                    ny = 1
                    self.vy = 1
                elif ny >= H:
                    ny = H - 2
                    self.vy = -1

                # Left paddle collision (col 1 / col 0)
                if nx <= 1 and self.vx < 0:
                    if self.my_y <= ny < self.my_y + PADDLE_H:
                        nx = 1
                        self.vx = 1
                        # Edge spin
                        if ny == self.my_y:
                            self.vy = -1
                        elif ny == self.my_y + PADDLE_H - 1:
                            self.vy = 1
                        sync_needed = True
                    elif nx < 0:
                        self.opp_score += 1
                        sync_needed = True
                        if self.opp_score >= WIN_SCORE:
                            self._running = False
                            self.over = True
                            self.result = "lose"
                        else:
                            self._reset_ball(1)
                            nx, ny = self.bx, self.by

                # Right paddle collision (col W-2 / W-1)
                elif nx >= W - 2 and self.vx > 0:
                    if self.opp_y <= ny < self.opp_y + PADDLE_H:
                        nx = W - 2
                        self.vx = -1
                        if ny == self.opp_y:
                            self.vy = -1
                        elif ny == self.opp_y + PADDLE_H - 1:
                            self.vy = 1
                        sync_needed = True
                    elif nx >= W:
                        self.my_score += 1
                        sync_needed = True
                        if self.my_score >= WIN_SCORE:
                            self._running = False
                            self.over = True
                            self.result = "win"
                        else:
                            self._reset_ball(-1)
                            nx, ny = self.bx, self.by

                if not self.over:
                    self.bx = nx
                    self.by = ny

                if self.role == "host" and (sync_needed or self._tick_count % 2 == 0 or self.over):
                    pkt = self._host_pkt()
                else:
                    pkt = None

            if pkt is not None:
                self.io.send(pkt)
            self.render()

        # Game over: the final packet decides the guest's result, so repeat it (lossy link)
        if self.role == "host" and self.over and self.result in ("win", "lose"):
            for _ in range(4):
                time.sleep(0.25)
                self.io.send(self._host_pkt())

    def snapshot(self) -> str:
        if self.over:
            if self.result == "quit":
                st = "q"
            elif self.result == "win":
                st = "w"
            else:
                st = "l"
        else:
            st = "p"
        return f"P,{self.my_y},{self.opp_y},{self.bx},{self.by},{self.my_score},{self.opp_score},{st}"

    def status_text(self) -> str:
        return f"Pong {self.my_score}-{self.opp_score}"
