"""
chesscom_import.py

Pulls games straight from a player's public chess.com game archive (their
REST API needs no authentication for this) instead of requiring a manual
PGN export. Pure fetch/filter logic - no Qt here, see
chesscom_import_dialog.py for the UI that drives this.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import requests

BASE_URL = "https://api.chess.com/pub/player"
HEADERS = {"User-Agent": "Bongcloud chess review app (github.com - contact via chess.com support)"}
REQUEST_TIMEOUT = 15

# Hard cap on how many games a single import can pull in - an active
# player's account can easily have tens of thousands of games, and nobody
# wants that dumped into one PGN by accident.
MAX_IMPORT_GAMES = 300

LOOKBACK_OPTIONS = {
    "Last month": 1,
    "Last 3 months": 3,
    "Last 6 months": 6,
    "Last year": 12,
    "All time": None,
}

# Finer-grained lookback options than LOOKBACK_OPTIONS' calendar-month
# buckets - chess.com's archive API only exposes monthly archives, so these
# are handled separately: fetch the last couple of monthly archives (see
# archives_for_delta) and then filter individual games by exact timestamp.
LOOKBACK_DELTA_OPTIONS = {
    "Last 24 hours": timedelta(hours=24),
    "Last week": timedelta(days=7),
}


class ChessComError(Exception):
    """Raised for any chess.com API failure the UI should show to the user."""


def fetch_archives(username: str) -> list[str]:
    """Monthly archive URLs, oldest first (chess.com's own order)."""
    url = f"{BASE_URL}/{username.strip().lower()}/games/archives"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as exc:
        raise ChessComError(f"Couldn't reach chess.com: {exc}") from exc
    if resp.status_code == 404:
        raise ChessComError(f"No chess.com account found for \"{username}\".")
    if not resp.ok:
        raise ChessComError(f"chess.com returned an error (HTTP {resp.status_code}).")
    return resp.json().get("archives", [])


def fetch_month_games(archive_url: str) -> list[dict]:
    try:
        resp = requests.get(archive_url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as exc:
        raise ChessComError(f"Couldn't reach chess.com: {exc}") from exc
    if not resp.ok:
        raise ChessComError(f"chess.com returned an error (HTTP {resp.status_code}).")
    return resp.json().get("games", [])


def archives_in_lookback(archives: list[str], lookback_months: int | None) -> list[str]:
    """Archive URLs newest-first, trimmed to the requested lookback window
    (None = all time). Newest-first so a capped fetch naturally favors the
    most recent games."""
    ordered = list(reversed(archives))
    if lookback_months is None:
        return ordered
    return ordered[:lookback_months]


def archives_for_delta(archives: list[str]) -> list[str]:
    """Newest 2 monthly archives, newest first - enough to safely cover any
    LOOKBACK_DELTA_OPTIONS window (a week or less) regardless of where in
    the current month "now" falls."""
    return list(reversed(archives))[:2]


def cutoff_epoch(delta: timedelta) -> float:
    return (datetime.now(timezone.utc) - delta).timestamp()


def matches_filters(
    game: dict,
    *,
    username: str,
    time_classes: set[str],
    rated_only: bool,
    color: str | None,
    min_end_time: float | None = None,
) -> bool:
    if game.get("rules") != "chess":
        return False
    if time_classes and game.get("time_class") not in time_classes:
        return False
    if rated_only and not game.get("rated", False):
        return False
    if min_end_time is not None and (game.get("end_time") or 0) < min_end_time:
        return False
    if color:
        player = game.get(color) or {}
        if player.get("username", "").lower() != username.strip().lower():
            return False
    return True


def combined_pgn(games: list[dict]) -> str:
    return "\n\n".join(g["pgn"].strip() for g in games if g.get("pgn")) + "\n"
