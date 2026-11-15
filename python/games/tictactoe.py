"""Tic-Tac-Toe.

Roles:
    "local": hot-seat on one device, X and O take turns.
    "host":  started the invite, plays X, moves first.
    "guest": accepted the invite, plays O.

Wire messages (all carry "game": "tictactoe"):
    {"type": "move", "cell": 0..8}   a move by the sender
    {"type": "quit"}                 the sender left the game

Snapshot for the display: 9 board chars ('X', 'O', '.'), the cursor cell
(0..8), a state char ('t' = playing, 'w' = won, 'd' = draw, 'q' = quit)
and a mark char (whose turn it is, or the winner).
"""

import threading

from .base import Game

LINES = [
    (0, 1, 2), (3, 4, 5), (6, 7, 8),
    (0, 3, 6), (1, 4, 7), (2, 5, 8),
    (0, 4, 8), (2, 4, 6),
]


class TicTacToe(Game):
    name = "tictactoe"
    title = "Tic-Tac-Toe"

    def __init__(self, io, role):
        super().__init__(io, role)
        self.board = [""] * 9
        self.mine = "X" if role == "host" else "O"
        self.theirs = "O" if role == "host" else "X"
        self.turn = "X"       # X always starts
        self.cursor = 4       # centre cell by default
        self.winner = None
        self._lock = threading.RLock()
        self._running = False
        self._sent_n = 0   # move count of my latest move
        self._peer_n = 0   # highest move count the peer has reported seeing

    @property
    def local(self) -> bool:
        return self.role == "local"

    # ---- input -----------------------------------------------------------
    def handle_local(self, action: str):
        if self.over:
            return
        if action == "next":
            self.cursor = (self.cursor + 1) % 9
            self.render()
        elif action == "prev":
            self.cursor = (self.cursor - 1) % 9
            self.render()
        elif action == "press":
            if not self._my_turn():
                self.io.show("Wait for the other player")
                return
            with self._lock:
                mark = self.turn if self.local else self.mine
                if not self._place(self.cursor, mark):
                    self.io.show("Cell taken")
                    return
                self._after_move_locked_send()

    def _after_move_locked_send(self):
        self._after_move()
        if not self.local:
            self._send_move(self.cursor, self._count())

    def handle_remote(self, msg: dict):
        if self.over or self.local:
            return
        kind = msg.get("type")
        if kind == "quit":
            self._running = False
            self._finish("quit")
            return
        if kind != "state":
            return
        b = msg.get("b")
        n = msg.get("n")
        if not isinstance(b, str) or len(b) != 9 or not isinstance(n, int):
            return
        with self._lock:
            self._peer_n = max(self._peer_n, n)
            if n <= self._count():
                return  # nothing new (duplicate / heartbeat / stale)
            new = ["" if ch == "." else ch for ch in b]
            if any(o and o != nb for o, nb in zip(self.board, new)):
                return  # inconsistent with our marks: ignore
            if sum(1 for c in new if c) != n:
                return
            self.board = new
            # X moves on even counts, O on odd
            self.turn = "X" if n % 2 == 0 else "O"
            w = self._winner()
            if w:
                self.winner = w
                self.over = True
                self.result = "win" if w == self.mine else "lose"
            elif all(self.board):
                self.over = True
                self.result = "draw"
        self.render()

    # ---- state sync ------------------------------------------------------
    # Every message carries the FULL board and its move count, so any single packet
    # that arrives brings the peer fully up to date (idempotent, order-independent).
    # Unacknowledged moves are re-sent every 0.2s; a 1s heartbeat heals anything else.
    def _count(self) -> int:
        return sum(1 for c in self.board if c)

    def _state_pkt(self) -> dict:
        return {
            "game": self.name, "type": "state",
            "b": "".join(c or "." for c in self.board), "n": self._count(),
        }

    def _send_move(self, cell: int, n: int):
        self._sent_n = n
        self.io.send(self._state_pkt())

    def start(self):
        super().start()
        if not self.local:
            self._running = True
            threading.Thread(target=self._sync_loop, daemon=True).start()

    def _sync_loop(self):
        import time
        last = 0.0
        end_deadline = None
        while self._running:
            time.sleep(0.1)
            now = time.monotonic()
            with self._lock:
                unacked = self._sent_n > self._peer_n
                over = self.over
                pkt = self._state_pkt()
            if over:
                if end_deadline is None:
                    end_deadline = now + 5.0
                if not unacked or now > end_deadline:
                    if self.result == "quit" or now > end_deadline or not unacked:
                        break
            if now - last >= (0.2 if unacked else 1.0):
                last = now
                self.io.send(pkt)
        self._running = False

    def quit(self, notify_peer: bool = True):
        super().quit(notify_peer=notify_peer)
        # keep the sync thread alive after a loss/win so the final move still gets through

    # ---- rules -----------------------------------------------------------
    def _my_turn(self) -> bool:
        return self.local or self.turn == self.mine

    def _place(self, cell: int, mark: str) -> bool:
        if self.board[cell]:
            return False
        self.board[cell] = mark
        return True

    def _winner(self):
        for a, b, c in LINES:
            if self.board[a] and self.board[a] == self.board[b] == self.board[c]:
                return self.board[a]
        return None

    def _after_move(self):
        w = self._winner()
        if w:
            self.winner = w
            if self.local:
                self._finish("win")
            else:
                self._finish("win" if w == self.mine else "lose")
            return
        if all(self.board):
            self._finish("draw")
            return
        self.turn = "O" if self.turn == "X" else "X"
        self.render()

    # ---- display ---------------------------------------------------------
    def snapshot(self) -> str:
        cells = "".join(self.board[i] or "." for i in range(9))
        if self.over:
            if self.result == "quit":
                state, mark = "q", "-"
            elif self.winner:
                state, mark = "w", self.winner
            else:
                state, mark = "d", "-"
        else:
            state, mark = "t", self.turn
        return cells + str(self.cursor) + state + mark

    def status_text(self) -> str:
        rows = []
        for r in range(3):
            row = []
            for c in range(3):
                i = r * 3 + c
                mark = self.board[i] or str(i + 1)
                if not self.over and i == self.cursor:
                    row.append("[" + mark + "]")
                else:
                    row.append(" " + mark + " ")
            rows.append("".join(row))
        if self.over:
            if self.result == "quit":
                status = "Game ended"
            elif self.result == "draw":
                status = "Draw"
            elif self.local:
                status = self.winner + " wins"
            else:
                status = "You win!" if self.result == "win" else "You lose"
        elif self.local:
            status = self.turn + " to play"
        elif self.turn == self.mine:
            status = "Your turn (" + self.mine + ")"
        else:
            status = "Their turn"
        return self.title + ": " + status + " | " + " / ".join(rows)
