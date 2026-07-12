"""
engine_analysis.py

Runs a local UCI engine (Stockfish) over a plain PGN - one with no
chess.com-style [%eval]/[%best] annotations - and produces the same
MoveRecord list that pgn_loader.load_game() gives you from an annotated
file. This is what lets the review app work on any game, not just
pre-annotated exports.

One analysis per position (not two per move): the static eval of a
position already stands in for "value under best continuation", so it
doubles as both eval_before/eval_after_best for the move leaving that
position and eval_after for the move arriving at it - mirroring the
approximation pgn_loader already makes from PGN eval comments.
"""

from __future__ import annotations

import os
import shutil
import sys
from typing import Callable, Optional

import chess
import chess.engine
import chess.pgn

from pgn_loader import MoveRecord, _estimate_phase, attach_refutations
from scoresheet import Eval, generate_commentary

# Per-position think time. DEFAULT_LIMIT is the normal, more accurate pass;
# FAST_LIMIT trades accuracy for speed on large batches (offered as an
# opt-in checkbox rather than the default, since it can misjudge close
# positions).
DEFAULT_LIMIT = chess.engine.Limit(time=0.2)
FAST_LIMIT = chess.engine.Limit(time=0.05)

# Batches of raw (unannotated) games at or below this size are analyzed
# without asking first - since chess.com imports are essentially always raw,
# stopping to confirm every time is just friction for what's normally a
# quick, single-game operation. Above this, a batch can take long enough
# that it's worth asking (and offering the faster/less-accurate option).
AUTO_ANALYZE_THRESHOLD = 3


def find_engine() -> Optional[str]:
    for env_var in ("STOCKFISH_PATH", "STOCKFISH_EXE"):
        path = os.environ.get(env_var)
        if path and os.path.isfile(path):
            return path

    # Packaged distribution: check next to the running .exe, so a
    # stockfish.exe placed alongside it is picked up without needing PATH.
    if getattr(sys, "frozen", False):
        candidate = os.path.join(os.path.dirname(sys.executable), "stockfish.exe")
        if os.path.isfile(candidate):
            return candidate

    for name in ("stockfish", "stockfish.exe"):
        found = shutil.which(name)
        if found:
            return found
    return None


def _score_to_eval(score: chess.engine.PovScore) -> Eval:
    white_score = score.white()
    mate = white_score.mate()
    if mate is not None:
        return Eval(mate=mate)
    return Eval(cp=white_score.score())


def _analyze_game(
    game: chess.pgn.Game,
    engine: "chess.engine.SimpleEngine",
    limit: chess.engine.Limit,
    progress_callback: Optional[Callable[[int, int], None]] = None,
    should_stop: Optional[Callable[[], bool]] = None,
) -> list[MoveRecord]:
    nodes = list(game.mainline())
    total = len(nodes)
    records: list[MoveRecord] = []

    board_before = game.board()
    infos = engine.analyse(board_before, limit, multipv=2)
    eval_here = _score_to_eval(infos[0]["score"])
    best_move_here = infos[0]["pv"][0] if infos[0].get("pv") else None
    second_best_eval_here = _score_to_eval(infos[1]["score"]) if len(infos) > 1 else None
    prev_eval = eval_here

    for i, node in enumerate(nodes):
        if should_stop is not None and should_stop():
            break

        move = node.move
        best_move = best_move_here or move
        second_best_eval = second_best_eval_here

        board_after = node.board()
        infos_after = engine.analyse(board_after, limit, multipv=2)
        eval_after_played = _score_to_eval(infos_after[0]["score"])

        commentary = generate_commentary(
            board_before=board_before,
            move=move,
            best_move=best_move,
            eval_before=prev_eval,
            eval_after_played=eval_after_played,
            eval_after_best=eval_here,
            second_best_eval=second_best_eval,
        )

        best_san = None
        if best_move != move:
            try:
                best_san = board_before.san(best_move)
            except (ValueError, AssertionError):
                best_san = None

        records.append(MoveRecord(
            move_number=board_before.fullmove_number,
            color_white=board_before.turn == chess.WHITE,
            san=node.san(),
            board_before=board_before,
            move=move,
            best_move=best_move,
            eval_before=prev_eval,
            eval_after=eval_after_played,
            best_san=best_san or "",
            phase=_estimate_phase(board_before),
            pgn_tag=None,
            wpl=commentary["win_prob_loss"],
            commentary=commentary,
        ))

        if progress_callback:
            progress_callback(i + 1, total)

        prev_eval = eval_after_played
        board_before = board_after
        eval_here = eval_after_played
        best_move_here = infos_after[0]["pv"][0] if infos_after[0].get("pv") else None
        second_best_eval_here = _score_to_eval(infos_after[1]["score"]) if len(infos_after) > 1 else None

    attach_refutations(records)
    return records


def analyze_game(
    path: str,
    engine_path: str,
    limit: chess.engine.Limit,
    progress_callback: Optional[Callable[[int, int], None]] = None,
    should_stop: Optional[Callable[[], bool]] = None,
) -> tuple[chess.pgn.Game, list[MoveRecord]]:
    with open(path, encoding="utf-8") as f:
        game = chess.pgn.read_game(f)
    if game is None:
        raise ValueError(f"No game found in {path}")

    with chess.engine.SimpleEngine.popen_uci(engine_path) as engine:
        records = _analyze_game(game, engine, limit, progress_callback, should_stop)
    return game, records


def analyze_games(
    games: list[chess.pgn.Game],
    engine_path: str,
    limit: chess.engine.Limit,
    progress_callback: Optional[Callable[[int, int, int, int], None]] = None,
    should_stop: Optional[Callable[[], bool]] = None,
) -> list[list[MoveRecord]]:
    """Analyzes several already-parsed games with one shared engine process,
    instead of spinning up a fresh Stockfish per game - used to batch-analyze
    raw games for the Weakness Report.

    progress_callback receives (games_done, games_total, moves_done_in_game,
    moves_total_in_game).
    """
    results: list[list[MoveRecord]] = []
    with chess.engine.SimpleEngine.popen_uci(engine_path) as engine:
        for gi, game in enumerate(games):
            if should_stop is not None and should_stop():
                break
            per_move_cb = None
            if progress_callback is not None:
                per_move_cb = lambda done, total, gi=gi: progress_callback(gi, len(games), done, total)
            records = _analyze_game(game, engine, limit, progress_callback=per_move_cb, should_stop=should_stop)
            results.append(records)
    return results
