"""
player_identity.py

Remembers which player name is "you" across sessions, so Weakness Report
doesn't have to ask "which player is you?" every time it could otherwise
make a confident guess. A batch where one name is consistent across every
loaded game already resolves without asking; this fills the far more
common gap - a single game, or a small batch against the same opponent -
where that heuristic can't tell the two players apart on its own.
"""

from __future__ import annotations

import os
from pathlib import Path


def _path() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home())
    return Path(base) / "Bongcloud" / "player_name.txt"


def load_last_name() -> str | None:
    path = _path()
    if not path.is_file():
        return None
    try:
        name = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return name or None


def save_last_name(name: str) -> None:
    if not name:
        return
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(name, encoding="utf-8")


def is_black(white_name: str, black_name: str, my_name: str | None) -> bool:
    """True if my_name is unambiguously the Black player in this game -
    used to decide whether the board should be flipped so "you" sit at the
    bottom. Ambiguous matches (my_name equal to both headers, e.g. two
    placeholder "?" names) default to False rather than guess."""
    return bool(my_name) and my_name == black_name and my_name != white_name
