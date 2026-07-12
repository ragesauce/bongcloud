"""
game_history.py

A lightweight, cross-session log of every game the user has ever viewed or
imported - date, rating, result, accuracy, blunder/mistake counts - so the
Progress dashboard can show whether practice is actually moving the needle.
Persisted as JSON in APPDATA, mirroring puzzle_history.py's storage pattern.
Recording is idempotent (keyed by game_key), so re-viewing the same game
never creates a duplicate entry.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Optional

import chess.pgn

from pgn_loader import MoveRecord, my_records
from scoresheet import game_accuracy


def _history_path() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home())
    return Path(base) / "Bongcloud" / "game_history.json"


def game_key(game: chess.pgn.Game) -> str:
    """A chess.com game's [Link ...] header uniquely identifies it; PGNs
    without one (hand-exported, non-chess.com) fall back to a hash of the
    headers that would otherwise make two different games collide."""
    headers = game.headers
    link = (headers.get("Link") or "").strip()
    if link:
        return link
    raw = "|".join(headers.get(k, "") for k in ("White", "Black", "Date", "Result", "UTCTime"))
    return "hash:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


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


def _clean_date(raw: str) -> Optional[str]:
    raw = (raw or "").strip()
    return raw if raw and "?" not in raw else None


def record_game(history: dict, game: chess.pgn.Game, records: list[MoveRecord], my_name: Optional[str]) -> bool:
    """Adds one entry for this game if it isn't already logged. Returns True
    if a new entry was added, False if it was already present (or there are
    no moves to summarize)."""
    key = game_key(game)
    if key in history or not records:
        return False

    headers = game.headers
    white = headers.get("White", "?")
    black = headers.get("Black", "?")

    my_color: Optional[str] = None
    if my_name == white and my_name != black:
        my_color = "white"
    elif my_name == black and my_name != white:
        my_color = "black"

    my_rating: Optional[int] = None
    opponent_rating: Optional[int] = None
    if my_color is not None:
        elo_field = "WhiteElo" if my_color == "white" else "BlackElo"
        opp_field = "BlackElo" if my_color == "white" else "WhiteElo"
        try:
            my_rating = int(headers.get(elo_field, ""))
        except ValueError:
            my_rating = None
        try:
            opponent_rating = int(headers.get(opp_field, ""))
        except ValueError:
            opponent_rating = None

    result_header = headers.get("Result", "*")
    result: Optional[str] = None
    if my_color is not None and result_header in ("1-0", "0-1", "1/2-1/2"):
        if result_header == "1/2-1/2":
            result = "draw"
        else:
            winner = "white" if result_header == "1-0" else "black"
            result = "win" if winner == my_color else "loss"

    mine = [rec for _, rec in my_records(records, white, black, my_name)] if my_color is not None else []
    accuracy: Optional[float] = None
    blunders = mistakes = 0
    if mine:
        accuracy = round(game_accuracy(records, my_color == "white"), 1)
        blunders = sum(1 for rec in mine if rec.commentary["classification"] == "Blunder")
        mistakes = sum(1 for rec in mine if rec.commentary["classification"] == "Mistake")

    history[key] = {
        "date": _clean_date(headers.get("EndDate") or headers.get("Date") or ""),
        "white": white,
        "black": black,
        "my_color": my_color,
        "my_rating": my_rating,
        "opponent_rating": opponent_rating,
        "result": result,
        "accuracy": accuracy,
        "blunders": blunders,
        "mistakes": mistakes,
        "moves": len(records),
    }
    return True
