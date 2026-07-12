"""
puzzle_history.py

A small Leitner-style spaced-repetition scheduler for the Puzzle Trainer.
Solving a puzzle cleanly promotes it to a higher "box" (a longer interval
before it's due again); getting it wrong or revealing the answer resets it
to box 0 (due immediately). Persisted as JSON so the schedule survives
across app sessions - the only thing this app writes to disk between runs.
"""

from __future__ import annotations

import json
import os
from datetime import date, timedelta
from pathlib import Path

# Days until a puzzle is due again after landing in box N. Index 0 = due
# immediately (just missed it); box grows with each clean solve.
_BOX_INTERVALS_DAYS = [0, 1, 3, 7, 16, 35]
_MAX_BOX = len(_BOX_INTERVALS_DAYS) - 1


def _history_path() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home())
    return Path(base) / "Bongcloud" / "puzzle_history.json"


def puzzle_key(fen_before: str, move_uci: str) -> str:
    return f"{fen_before}|{move_uci}"


def load_history() -> dict:
    path = _history_path()
    if not path.is_file():
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_history(history: dict) -> None:
    path = _history_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(history, f, indent=2)


def record_result(history: dict, key: str, correct: bool, today: date | None = None) -> dict:
    today = today or date.today()
    entry = history.get(key, {"box": 0, "attempts": 0, "fails": 0})
    entry["attempts"] = entry.get("attempts", 0) + 1
    if correct:
        entry["box"] = min(entry.get("box", 0) + 1, _MAX_BOX)
    else:
        entry["fails"] = entry.get("fails", 0) + 1
        entry["box"] = 0
    entry["last_seen"] = today.isoformat()
    entry["due"] = (today + timedelta(days=_BOX_INTERVALS_DAYS[entry["box"]])).isoformat()
    history[key] = entry
    return entry


def is_due(history: dict, key: str, today: date | None = None) -> bool:
    today = today or date.today()
    entry = history.get(key)
    if entry is None or not entry.get("due"):
        return True
    return date.fromisoformat(entry["due"]) <= today


def sort_key(history: dict, key: str, today: date | None = None) -> tuple[int, int]:
    """Lower = higher priority (shown first). Due/never-seen puzzles sort
    before not-yet-due ones; within each group, more overdue (or
    sooner-due) puzzles sort first. Nothing is ever hidden - this only
    reorders the queue."""
    today = today or date.today()
    entry = history.get(key)
    if entry is None:
        return (0, 0)
    due_str = entry.get("due") or today.isoformat()
    overdue_days = (today - date.fromisoformat(due_str)).days
    if overdue_days >= 0:
        return (0, -overdue_days)
    return (1, -overdue_days)
