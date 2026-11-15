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
            mark = self.turn if self.local else self.mine
            if not self._place(self.cursor, mark):
                self.io.show("Cell taken")
                return
            if not self.local:
                self.io.send({"game": self.name, "type": "move", "cell": self.cursor})
            self._after_move()

    def handle_remote(self, msg: dict):
        if self.over or self.local:
            return
        kind = msg.get("type")
        if kind == "quit":
            self._finish("quit")
            return
        if kind != "move":
            return
        cell = msg.get("cell")
        if not isinstance(cell, int) or not 0 <= cell < 9:
            return
        if self.turn != self.theirs or not self._place(cell, self.theirs):
            return  # out-of-turn or illegal move: ignore
        self._after_move()

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
