"""Base class for turn-based mini games.

A game knows nothing about hardware, Bridge, BLE or the LLM. It only talks to
the outside world through a GameIO object injected by the app:

    io.send(msg: dict)  -> deliver a message to the peer (BLE later, mock now)
    io.show(text: str)  -> show the local state (OLED later, Monitor now)

Local input arrives as plain action strings ("next", "prev", "press") so the
same game can be driven by the knob, buttons or a test.
"""


class GameIO:
    def __init__(self, send, show, draw=None):
        self.send = send
        self.show = show
        self.draw = draw  # optional: draw(snapshot_str) for the LED matrix


class Game:
    name = "game"        # registry key, must be unique and stable on the wire
    title = "Game"       # human readable name

    def __init__(self, io: GameIO, role: str):
        # role: "host" started the invite, "guest" accepted it
        self.io = io
        self.role = role
        self.over = False
        self.result = None  # "win" | "lose" | "draw" | "quit" once over

    # ---- lifecycle -------------------------------------------------------
    def start(self):
        """Called once after the peer accepted. Override to set up state."""
        self.render()

    def handle_local(self, action: str):
        """Local player input: "next", "prev" or "press"."""
        raise NotImplementedError

    def handle_remote(self, msg: dict):
        """Message from the peer. msg["game"] == self.name."""
        raise NotImplementedError

    def quit(self, notify_peer: bool = True):
        if self.over:
            return
        if notify_peer and self.role != "local":
            self.io.send({"game": self.name, "type": "quit"})
        self._finish("quit")

    # ---- helpers for subclasses -----------------------------------------
    def _finish(self, result: str):
        self.over = True
        self.result = result
        self.render()

    def render(self):
        self.io.show(self.status_text())
        if self.io.draw:
            self.io.draw(self.snapshot())

    def status_text(self) -> str:
        return self.title

    def snapshot(self) -> str:
        """Compact state for the display. Empty if the game has no board."""
        return ""
