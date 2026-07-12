"""
sound.py

Move/capture/check sound effects for the interactive board, mirroring the
audio cues chess.com plays when a piece is moved. Clips are short
synthesized WAVs bundled under assets/sounds/ (no external sound library
or network fetch needed) and played via QSoundEffect, which decodes and
mixes off the UI thread so it never blocks input handling.
"""

from __future__ import annotations

import sys
from pathlib import Path

import chess
from PySide6.QtCore import QUrl
from PySide6.QtMultimedia import QSoundEffect

_CLIPS = ("move", "capture", "check")


def _assets_dir() -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / "assets" / "sounds"


class MoveSoundPlayer:
    """Owns one QSoundEffect per clip. Call play_for_move() with the board
    just before a move is made and the move itself; it figures out whether
    that was a plain move, a capture, or a check and plays the right cue."""

    def __init__(self):
        self._effects: dict[str, QSoundEffect] = {}
        assets_dir = _assets_dir()
        for name in _CLIPS:
            effect = QSoundEffect()
            effect.setSource(QUrl.fromLocalFile(str(assets_dir / f"{name}.wav")))
            effect.setVolume(0.6)
            self._effects[name] = effect

    def play_for_move(self, board_before: chess.Board, move: chess.Move, board_after: chess.Board):
        if board_after.is_check():
            clip = "check"
        elif board_before.is_capture(move):
            clip = "capture"
        else:
            clip = "move"
        self._effects[clip].play()
