"""
analysis_cache.py

Persists computed Stockfish MoveRecords across sessions, keyed by the same
game_key used by game_history.py (the chess.com [Link] header, or a header
hash for PGNs without one). Re-analyzing a game already run through Stockfish
- reopening it in the review app, or pulling it into another Weakness Report
batch - recalls the stored result instead of spinning up the engine again.

CACHE_VERSION guards against an analysis-pipeline bug fix (e.g. a
classification or eval-sign fix) silently leaving stale, now-wrong cached
results in place - bump it whenever a code change could alter the output for
a position already in someone's cache, and every existing entry is treated
as a miss and recomputed.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

import chess
import chess.pgn

from game_history import game_key
from pgn_loader import MoveRecord
from scoresheet import Eval

CACHE_VERSION = 1


def _cache_path() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home())
    return Path(base) / "Bongcloud" / "analysis_cache.json"


def load_cache() -> dict:
    path = _cache_path()
    if not path.is_file():
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    if data.get("version") != CACHE_VERSION:
        return {}
    return data.get("games", {})


def save_cache(games: dict) -> None:
    path = _cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"version": CACHE_VERSION, "games": games}, f)


def _eval_to_json(ev: Eval) -> dict:
    return {"cp": ev.cp, "mate": ev.mate}


def _eval_from_json(data: dict) -> Eval:
    return Eval(cp=data.get("cp"), mate=data.get("mate"))


def _record_to_json(rec: MoveRecord) -> dict:
    return {
        "eval_before": _eval_to_json(rec.eval_before),
        "eval_after": _eval_to_json(rec.eval_after),
        "best_move": rec.best_move.uci() if rec.best_move else None,
        "best_san": rec.best_san,
        "phase": rec.phase,
        "pgn_tag": rec.pgn_tag,
        "wpl": rec.wpl,
        "commentary": rec.commentary,
    }


def _records_from_json(game: "chess.pgn.Game", entries: list[dict]) -> Optional[list[MoveRecord]]:
    """Reconstructs MoveRecords by replaying the game's actual moves against
    cached per-move analysis - only the expensive engine.analyse() calls are
    being skipped; board/move data always comes from the real game, not the
    cache. Returns None (a cache miss) if the entry doesn't line up with this
    game's move count or shape, rather than risk showing stale results."""
    records: list[MoveRecord] = []
    node = game
    for entry in entries:
        if not node.variations:
            return None
        next_node = node.variations[0]
        board_before = node.board()
        move = next_node.move

        best_uci = entry.get("best_move")
        try:
            best_move = chess.Move.from_uci(best_uci) if best_uci else move
        except ValueError:
            best_move = move

        try:
            records.append(MoveRecord(
                move_number=board_before.fullmove_number,
                color_white=board_before.turn == chess.WHITE,
                san=next_node.san(),
                board_before=board_before,
                move=move,
                best_move=best_move,
                eval_before=_eval_from_json(entry["eval_before"]),
                eval_after=_eval_from_json(entry["eval_after"]),
                best_san=entry.get("best_san", ""),
                phase=entry.get("phase", ""),
                pgn_tag=entry.get("pgn_tag"),
                wpl=entry.get("wpl"),
                commentary=entry["commentary"],
            ))
        except (KeyError, TypeError):
            return None
        node = next_node

    if node.variations:
        return None
    return records


def get_cached_records(cache: dict, game: "chess.pgn.Game") -> Optional[list[MoveRecord]]:
    entries = cache.get(game_key(game))
    if not entries:
        return None
    return _records_from_json(game, entries)


def store_records(cache: dict, game: "chess.pgn.Game", records: list[MoveRecord]) -> None:
    cache[game_key(game)] = [_record_to_json(r) for r in records]
