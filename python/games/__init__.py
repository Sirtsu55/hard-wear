"""Mini-game package. Plug in a new game by adding it to GAMES below."""

from .base import Game, GameIO
from .pong import Pong
from .snake import Snake
from .tictactoe import TicTacToe

GAMES = {g.name: g for g in (Pong, Snake, TicTacToe)}


def game_names():
    return list(GAMES)


def create_game(name: str, io: GameIO, role: str) -> Game:
    if name not in GAMES:
        raise ValueError("unknown game: " + name)
    return GAMES[name](io, role)


def route_message(session: Game, msg: dict) -> bool:
    """Forward a peer message to the active session if it is for that game."""
    if session is None or session.over or msg.get("game") != session.name:
        return False
    session.handle_remote(msg)
    return True
