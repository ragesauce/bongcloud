"""
opening_book.py

A small, self-contained "is this still known theory" check, used to tag
early moves as Book instead of grading them against the engine like every
other move. There's no bundled opening book file here (a real polyglot
book is a sizeable binary asset with its own licensing questions) - just a
hand-authored table of well-known main lines, a few plies deep each. This
means coverage is genuinely limited to common openings and their most
standard move orders, not sidelines or transpositions - a deliberate,
accepted tradeoff for staying dependency-free.

Lines are written as SAN (easy to read/verify) and converted to UCI once at
import time via python-chess's own move parser, so a typo here fails loudly
at import instead of silently matching the wrong moves.
"""

from __future__ import annotations

import chess

_SAN_LINES: list[list[str]] = [
    ["e4", "e5", "Nf3", "Nc6", "Bc4", "Bc5", "c3", "Nf6", "d3"],  # Italian Game / Giuoco Piano
    ["e4", "e5", "Nf3", "Nc6", "Bc4", "Nf6"],  # Italian Game / Two Knights Defense
    ["e4", "e5", "Nf3", "Nc6", "Bb5", "a6", "Ba4", "Nf6", "O-O", "Be7"],  # Ruy Lopez, Closed main line
    ["e4", "e5", "Nf3", "Nc6", "Bb5", "Nf6"],  # Ruy Lopez, Berlin Defense
    ["e4", "e5", "Nf3", "Nc6", "Bb5", "a6", "Bxc6", "dxc6"],  # Ruy Lopez, Exchange Variation
    ["e4", "e5", "Nf3", "Nc6", "d4", "exd4", "Nxd4"],  # Scotch Game
    ["e4", "e5", "Nf3", "Nc6", "Nc3", "Nf6"],  # Four Knights Game
    ["e4", "e5", "Nf3", "Nf6"],  # Petrov / Russian Defense
    ["e4", "e5", "Nc3"],  # Vienna Game
    ["e4", "e5", "f4"],  # King's Gambit
    ["e4", "c5", "Nf3", "d6", "d4", "cxd4", "Nxd4", "Nf6", "Nc3"],  # Sicilian, Open main tabiya
    ["e4", "c5", "Nf3", "d6", "d4", "cxd4", "Nxd4", "Nf6", "Nc3", "a6"],  # Sicilian Najdorf
    ["e4", "c5", "Nf3", "d6", "d4", "cxd4", "Nxd4", "Nf6", "Nc3", "g6"],  # Sicilian Dragon
    ["e4", "c5", "Nc3"],  # Sicilian, Closed Variation
    ["e4", "e6", "d4", "d5", "e5"],  # French Defense, Advance Variation
    ["e4", "e6", "d4", "d5", "exd5", "exd5"],  # French Defense, Exchange Variation
    ["e4", "e6", "d4", "d5", "Nc3", "Nf6"],  # French Defense, Classical Variation
    ["e4", "c6", "d4", "d5", "e5"],  # Caro-Kann, Advance Variation
    ["e4", "c6", "d4", "d5", "Nc3", "dxe4", "Nxe4", "Bf5"],  # Caro-Kann, Classical Variation
    ["e4", "c6", "d4", "d5", "exd5", "cxd5"],  # Caro-Kann, Exchange Variation
    ["e4", "d5", "exd5", "Qxd5"],  # Scandinavian Defense
    ["e4", "d6", "d4", "Nf6", "Nc3", "g6"],  # Pirc Defense
    ["e4", "Nf6"],  # Alekhine's Defense
    ["d4", "d5", "c4", "e6"],  # Queen's Gambit Declined
    ["d4", "d5", "c4", "dxc4"],  # Queen's Gambit Accepted
    ["d4", "d5", "c4", "c6"],  # Slav Defense
    ["d4", "Nf6", "c4", "g6", "Nc3", "Bg7"],  # King's Indian Defense
    ["d4", "Nf6", "c4", "e6", "Nc3", "Bb4"],  # Nimzo-Indian Defense
    ["c4", "e5"],  # English Opening, Reversed Sicilian
    ["Nf3", "d5", "c4"],  # Réti Opening
    ["d4", "d5", "Nf3", "Nf6", "Bf4"],  # London System
]


def _lines_to_uci(san_lines: list[list[str]]) -> list[tuple[str, ...]]:
    result = []
    for line in san_lines:
        board = chess.Board()
        ucis = []
        for san in line:
            move = board.parse_san(san)
            board.push(move)
            ucis.append(move.uci())
        result.append(tuple(ucis))
    return result


_LINES: list[tuple[str, ...]] = _lines_to_uci(_SAN_LINES)

_PREFIXES: set[tuple[str, ...]] = set()
for _line in _LINES:
    for _i in range(1, len(_line) + 1):
        _PREFIXES.add(_line[:_i])


def is_book_move(board_before: chess.Board, move: chess.Move) -> bool:
    """True if playing `move` from `board_before` stays within one of the
    known lines above - board_before.move_stack already holds the game's
    move history, so no extra bookkeeping is needed from callers."""
    sequence = tuple(m.uci() for m in board_before.move_stack) + (move.uci(),)
    return sequence in _PREFIXES
