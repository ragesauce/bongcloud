"""
chess_commentary.py

Generates chess.com-style move commentary ("Best - Your opponent blocked the
check from your queen! Smooth.") from a position + move + Stockfish evals.

This deliberately mirrors how chess.com actually builds these messages:
Stockfish never produces the sentence. It only produces numbers. Everything
readable is derived separately:

    1. EVAL        -> raw Stockfish score (you supply this; this module
                       does not call the engine)
    2. SEMANTICS    -> what happened on the board (check, capture, block,
                       fork, hanging piece, promotion...) via python-chess
                       board logic, not the engine
    3. CLASSIFICATION -> Best/Excellent/Good/Inaccuracy/Mistake/Blunder,
                       derived from eval-swing vs. the engine's best move
    4. TEXT         -> templates keyed on (classification, semantic tag),
                       with light variation

Requires: python-chess  (pip install python-chess --break-system-packages)
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Optional

import chess

import opening_book

if TYPE_CHECKING:
    # pgn_loader imports *from* this module, so importing MoveRecord back
    # here at runtime would be circular - only needed for the type hint.
    from pgn_loader import MoveRecord


# ---------------------------------------------------------------------------
# 1. EVAL representation (you feed these in from your own Stockfish calls)
# ---------------------------------------------------------------------------

@dataclass
class Eval:
    """A Stockfish evaluation, from White's perspective.

    cp:   centipawns (100 = 1 pawn), or None if it's a mate score
    mate: moves to mate (positive = side to move mates, negative = gets
          mated), or None if it's a cp score
    """
    cp: Optional[int] = None
    mate: Optional[int] = None

    def as_pawns(self) -> float:
        """Rough pawn-equivalent for threshold math. Mates clamp to +/-10."""
        if self.mate is not None:
            return 10.0 if self.mate > 0 else -10.0
        return (self.cp or 0) / 100.0

    def display(self) -> str:
        if self.mate is not None:
            return f"M{self.mate}" if self.mate > 0 else f"-M{abs(self.mate)}"
        pawns = (self.cp or 0) / 100.0
        sign = "+" if pawns >= 0 else ""
        return f"{sign}{pawns:.2f}"


def win_percent_white(ev: Eval) -> float:
    """Rough win probability for White, derived purely from the eval - the
    same logistic curve chess sites use to turn centipawns into a percent.
    Public: also used by the eval bar/eval chart, not just internally here."""
    if ev.mate is not None:
        return 100.0 if ev.mate > 0 else 0.0
    cp = ev.cp or 0
    return 50 + 50 * (2 / (1 + math.exp(-0.00368208 * cp)) - 1)


def win_percent_for_mover(ev: Eval, side_white: bool) -> float:
    pct = win_percent_white(ev)
    return pct if side_white else 100.0 - pct


def move_accuracy(win_pct_loss: float) -> float:
    """Chess.com's per-move accuracy curve (reverse-engineered, widely
    reproduced): an exponential decay of win-probability lost on that move,
    not a linear "100 - loss". This matters - a linear formula treats a
    10-point win% loss as "90% accurate", but the real curve puts that
    around 65%, since even a moderate slip is a much bigger practical deal
    than a linear scale suggests. Using the linear version was why the
    game/session accuracy shown elsewhere in the app used to read far
    higher than chess.com's own numbers for the same games."""
    raw = 103.1668 * math.exp(-0.04354 * win_pct_loss) - 3.1669
    return max(0.0, min(100.0, raw))


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _population_stdev(values: list[float]) -> float:
    n = len(values)
    if n < 2:
        return 0.0
    mean = sum(values) / n
    return math.sqrt(sum((v - mean) ** 2 for v in values) / n)


def _harmonic_mean(values: list[float]) -> float:
    # Floored so one 0%-accuracy move can't divide by zero (or otherwise
    # dominate the mean into near-zero on its own) - a small positive floor
    # still makes that move brutally punishing, just not literally infinite.
    floored = [max(v, 1.0) for v in values]
    return len(floored) / sum(1.0 / v for v in floored)


def _move_weights(win_percents_white: list[float], n_moves: int) -> list[float]:
    """One volatility weight per move, following lichess's published
    windowing scheme (chess.com's own constants are proprietary/undisclosed,
    but the same general idea): the game's win% sequence (White's
    perspective throughout - variance is identical either way, since
    stdev(100-x) == stdev(x)) is split into non-overlapping ~10-ply windows,
    and every move in a window shares that window's win%-standard-deviation
    as its weight, clamped to [0.5, 12]. Swingy stretches of the game count
    more toward the weighted mean below than dead-flat ones."""
    window_size = int(_clamp(round(n_moves / 10), 2, 8))
    weights: list[float] = []
    for start in range(0, n_moves, window_size):
        end = min(start + window_size, n_moves)
        window = win_percents_white[start:end + 1]
        weight = _clamp(_population_stdev(window), 0.5, 12)
        weights.extend([weight] * (end - start))
    return weights


def game_accuracy(records: list["MoveRecord"], color_white: bool) -> float:
    """One side's accuracy across a full game: the average of a
    volatility-weighted mean and the harmonic mean of per-move accuracy,
    not a plain average - this is what makes a handful of real blunders
    drag an otherwise-clean game's score down the way chess.com's own
    number does; a plain mean dilutes them too much across every quiet move
    in between. Reproduces lichess's disclosed algorithm (chess.com's exact
    constants aren't public, but they share the same per-move formula and
    the same weighted/harmonic-mean idea).

    `records` must be the *whole* game's moves in order, not pre-filtered
    to one color - the volatility windowing needs the whole game's flow to
    judge which stretches were sharp vs. quiet."""
    if not records:
        return 0.0

    win_percents_white = [win_percent_white(records[0].eval_before)] + [
        win_percent_white(r.eval_after) for r in records
    ]
    weights = _move_weights(win_percents_white, len(records))
    accuracies = [move_accuracy(r.commentary["win_prob_loss"]) for r in records]

    mine = [(a, w) for a, w, r in zip(accuracies, weights, records) if r.color_white == color_white]
    if not mine:
        return 0.0
    my_accuracies = [a for a, _ in mine]
    my_weights = [w for _, w in mine]

    weighted_mean = sum(a * w for a, w in zip(my_accuracies, my_weights)) / sum(my_weights)
    harmonic = _harmonic_mean(my_accuracies)
    return (weighted_mean + harmonic) / 2


# ---------------------------------------------------------------------------
# 3. CLASSIFICATION
# ---------------------------------------------------------------------------

class Classification(Enum):
    BRILLIANT = "Brilliant"
    GREAT = "Great"
    BEST = "Best"
    EXCELLENT = "Excellent"
    GOOD = "Good"
    BOOK = "Book"
    INACCURACY = "Inaccuracy"
    MISTAKE = "Mistake"
    BLUNDER = "Blunder"
    MISSED_WIN = "Miss"


_PIECE_VALUES = {
    chess.PAWN: 1,
    chess.KNIGHT: 3,
    chess.BISHOP: 3,
    chess.ROOK: 5,
    chess.QUEEN: 9,
    chess.KING: 0,
}


def _is_sacrifice(board_before: chess.Board, move: chess.Move) -> bool:
    """True if this move offers real *net* material: the moved piece (not a
    pawn or the king) lands on a square an enemy piece can take, without
    enough of our own pieces defending it, AND what we'd lose to that
    recapture clearly outweighs whatever we just captured getting there -
    a fair or favorable trade (e.g. queen takes queen, then gets
    recaptured) is not a sacrifice, even though the landing square is
    "attacked" in exactly the same shape a real sacrifice would be. This
    only says material was *offered* - not that the sacrifice "works";
    that's already covered by the move independently grading out as
    Best/Excellent by eval."""
    piece = board_before.piece_at(move.from_square)
    if piece is None or piece.piece_type in (chess.PAWN, chess.KING):
        return False
    captured = board_before.piece_at(move.to_square)
    gained = _PIECE_VALUES[captured.piece_type] if captured is not None else 0
    board_after = board_before.copy()
    board_after.push(move)
    enemy_color = not piece.color
    attackers = board_after.attackers(enemy_color, move.to_square)
    if not attackers:
        return False
    defenders = board_after.attackers(piece.color, move.to_square)
    if len(defenders) >= len(attackers):
        return False
    net_if_recaptured = gained - _PIECE_VALUES[piece.piece_type]
    return net_if_recaptured < -1


def classify_move(
    move: chess.Move,
    best_move: chess.Move,
    eval_before: Eval,
    eval_after_played: Eval,
    eval_after_best: Eval,
    side_to_move_was_white: bool,
    board_before: Optional[chess.Board] = None,
    second_best_eval: Optional[Eval] = None,
) -> Classification:
    """
    Compares the eval after your move to the eval after the engine's best
    move, from the mover's own point of view (loss is always >= 0).

    Thresholds are in pawns of "centipawn loss" and roughly track publicly
    documented chess.com behavior. Real chess.com also scales thresholds by
    win-probability curvature (a 0.6 pawn slip matters less at +6 than at
    +0.2); this simplified version uses flat thresholds, which is the main
    place a fancier implementation would diverge.

    board_before and second_best_eval are optional, since neither is always
    available: board_before enables the Book and Brilliant checks (pure
    board logic, no engine dependency), while second_best_eval - the eval of
    the engine's *second*-best line - enables Great, and is only ever
    supplied when we've analyzed the game ourselves with multipv=2;
    chess.com-exported PGN annotations only ever carry a single best line,
    so imported games can never produce a Great tag.
    """
    if board_before is not None and opening_book.is_book_move(board_before, move):
        return Classification.BOOK

    best_pawns = eval_after_best.as_pawns()
    before_pawns = eval_before.as_pawns() if side_to_move_was_white else -eval_before.as_pawns()

    if move == best_move:
        if board_before is not None and _is_sacrifice(board_before, move):
            win_before = win_percent_for_mover(eval_before, side_to_move_was_white)
            if 10.0 <= win_before <= 90.0:
                return Classification.BRILLIANT
        if second_best_eval is not None:
            second_pawns = second_best_eval.as_pawns()
            gap = (best_pawns - second_pawns) if side_to_move_was_white else (second_pawns - best_pawns)
            if gap >= 1.5:
                return Classification.GREAT
        return Classification.BEST

    played = eval_after_played.as_pawns()

    # Normalize both to "how good for the mover" so loss is always positive.
    if side_to_move_was_white:
        loss = best_pawns - played
    else:
        loss = played - best_pawns

    loss = max(loss, 0.0)

    # Missed forced mate (engine had mate, you let it go), or - more
    # broadly - you were already clearly winning and this move gave back a
    # real chunk of it (Mistake-or-worse by the thresholds below), even
    # without a forced mate on the board.
    missed_forced_mate = (
        eval_after_best.mate is not None and eval_after_best.mate > 0 and eval_after_played.mate is None
    )
    missed_big_swing = before_pawns >= 3.0 and loss >= 1.0
    if missed_forced_mate or missed_big_swing:
        return Classification.MISSED_WIN

    if loss <= 0.10:
        return Classification.EXCELLENT
    if loss <= 0.30:
        return Classification.GOOD
    if loss <= 1.00:
        return Classification.INACCURACY
    if loss <= 2.00:
        return Classification.MISTAKE
    return Classification.BLUNDER


# ---------------------------------------------------------------------------
# 2. SEMANTICS - pure board logic, no engine involved
# ---------------------------------------------------------------------------

@dataclass
class MoveSemantics:
    tags: list[str] = field(default_factory=list)
    piece_name: str = ""
    captured_piece_name: Optional[str] = None
    is_check_given: bool = False
    is_checkmate: bool = False
    pinned_piece_name: Optional[str] = None
    skewered_piece_name: Optional[str] = None
    discovered_piece_name: Optional[str] = None


_PIECE_NAMES = {
    chess.PAWN: "pawn",
    chess.KNIGHT: "knight",
    chess.BISHOP: "bishop",
    chess.ROOK: "rook",
    chess.QUEEN: "queen",
    chess.KING: "king",
}

_SLIDING_PIECES = (chess.BISHOP, chess.ROOK, chess.QUEEN)


def _ray_direction(from_square: int, to_square: int) -> Optional[tuple[int, int]]:
    """Unit (file, rank) step from from_square towards to_square if the two
    are aligned on a rank, file, or diagonal - None otherwise."""
    df = chess.square_file(to_square) - chess.square_file(from_square)
    dr = chess.square_rank(to_square) - chess.square_rank(from_square)
    if df == 0 and dr == 0:
        return None
    if df == 0:
        return (0, 1 if dr > 0 else -1)
    if dr == 0:
        return (1 if df > 0 else -1, 0)
    if abs(df) == abs(dr):
        return (1 if df > 0 else -1, 1 if dr > 0 else -1)
    return None


def _first_piece_beyond(board: chess.Board, start_square: int, direction: tuple[int, int]) -> Optional[int]:
    """Walks from start_square in `direction` (not including start_square
    itself) and returns the square of the first piece found, or None."""
    df, dr = direction
    f, r = chess.square_file(start_square) + df, chess.square_rank(start_square) + dr
    while 0 <= f <= 7 and 0 <= r <= 7:
        sq = chess.square(f, r)
        if board.piece_at(sq) is not None:
            return sq
        f, r = f + df, r + dr
    return None


def _enemy_attacked_squares(board: chess.Board, from_square: int, mover_color: bool) -> set[int]:
    result = set()
    for sq in board.attacks(from_square):
        p = board.piece_at(sq)
        if p is not None and p.color != mover_color:
            result.add(sq)
    return result


def analyze_semantics(board_before: chess.Board, move: chess.Move) -> MoveSemantics:
    """
    Figures out what kind of move this was, using only board state -
    exactly the part of chess.com's pipeline that ISN'T Stockfish.
    """
    sem = MoveSemantics()

    piece = board_before.piece_at(move.from_square)
    if piece is not None:
        sem.piece_name = _PIECE_NAMES[piece.piece_type]

    was_in_check = board_before.is_check()
    is_capture = board_before.is_capture(move)
    captured_piece = board_before.piece_at(move.to_square)
    if is_capture and captured_piece is not None:
        sem.captured_piece_name = _PIECE_NAMES[captured_piece.piece_type]
    elif board_before.is_en_passant(move):
        sem.captured_piece_name = "pawn"
        sem.tags.append("en_passant")

    board_after = board_before.copy()
    board_after.push(move)
    moved_piece_after = board_after.piece_at(move.to_square)
    mover_color = moved_piece_after.color if moved_piece_after is not None else board_before.turn

    sem.is_check_given = board_after.is_check()
    sem.is_checkmate = board_after.is_checkmate()

    if board_before.is_castling(move):
        sem.tags.append("castle")

    if move.promotion is not None:
        sem.tags.append("promotion")

    # --- resolving a check the mover was already facing ---
    if was_in_check:
        checkers = board_before.checkers()
        checker_squares = list(checkers)
        king_moved = piece is not None and piece.piece_type == chess.KING

        if king_moved:
            sem.tags.append("king_escaped_check")
        elif is_capture and move.to_square in checker_squares:
            sem.tags.append("captured_checking_piece")
        elif len(checker_squares) == 1:
            # Not the king, not capturing the checker -> must be an
            # interposition (a block) on the check ray.
            sem.tags.append("blocked_check")
        # (double check with a non-king move is illegal, so no extra case)

    # --- discovered check: the piece that moved isn't the one giving check ---
    if sem.is_check_given:
        checkers_after = board_after.checkers()
        if move.to_square not in checkers_after:
            sem.tags.append("discovered_check")
        else:
            sem.tags.append("direct_check")

    # --- skewer: a direct sliding check where, past the enemy king on that
    # same ray, sits another enemy piece the king's forced move will expose.
    # Scoped to this common/textbook case (skewers between two non-king
    # pieces aren't detected).
    if (
        sem.is_check_given
        and moved_piece_after is not None
        and moved_piece_after.piece_type in _SLIDING_PIECES
        and move.to_square in board_after.checkers()
    ):
        enemy_king_sq = board_after.king(not mover_color)
        direction = _ray_direction(move.to_square, enemy_king_sq) if enemy_king_sq is not None else None
        if direction is not None:
            behind_sq = _first_piece_beyond(board_after, enemy_king_sq, direction)
            behind_piece = board_after.piece_at(behind_sq) if behind_sq is not None else None
            if behind_piece is not None and behind_piece.color != mover_color and behind_piece.piece_type != chess.KING:
                sem.tags.append("skewer")
                sem.skewered_piece_name = _PIECE_NAMES[behind_piece.piece_type]

    # --- discovered (non-check) attack: moving this piece unmasks a
    # *different* friendly slider onto an undefended enemy piece it wasn't
    # attacking before. The check-giving case is already covered above by
    # discovered_check, so skip pieces that would double-count that.
    if not sem.is_check_given:
        for sq in chess.SQUARES:
            if sq == move.to_square:
                continue
            slider = board_after.piece_at(sq)
            if slider is None or slider.color != mover_color or slider.piece_type not in _SLIDING_PIECES:
                continue
            newly_attacked = _enemy_attacked_squares(board_after, sq, mover_color) - _enemy_attacked_squares(
                board_before, sq, mover_color
            )
            for target_sq in newly_attacked:
                target = board_after.piece_at(target_sq)
                if target is None or target.piece_type in (chess.KING, chess.PAWN):
                    continue
                if len(board_after.attackers(not mover_color, target_sq)) == 0:
                    sem.tags.append("discovered_attack")
                    sem.discovered_piece_name = _PIECE_NAMES[target.piece_type]
                    break
            if "discovered_attack" in sem.tags:
                break

    # --- back-rank weakness: the mover's own king ends up boxed in on its
    # home rank by its own pieces, with no forward escape square. Like every
    # other semantic tag, this only ever surfaces alongside an eval-driven
    # Mistake/Blunder classification - it explains why a move already
    # flagged as bad was bad, it doesn't independently judge danger, so
    # there's no need to also verify an enemy rook/queen can exploit it.
    home_rank = 0 if mover_color == chess.WHITE else 7
    king_sq = board_after.king(mover_color)
    if king_sq is not None and chess.square_rank(king_sq) == home_rank:
        forward_rank = home_rank + 1 if mover_color == chess.WHITE else home_rank - 1
        king_file = chess.square_file(king_sq)
        escape_squares = [
            chess.square(f, forward_rank) for f in (king_file - 1, king_file, king_file + 1) if 0 <= f <= 7
        ]
        escape_pieces = [board_after.piece_at(sq) for sq in escape_squares]
        if all(p is not None and p.color == mover_color for p in escape_pieces):
            sem.tags.append("back_rank_weakness")

    if is_capture:
        sem.tags.append("capture")
        if captured_piece is not None and piece is not None:
            if captured_piece.piece_type > piece.piece_type or captured_piece.piece_type == chess.QUEEN:
                sem.tags.append("favorable_capture")

    # --- hanging piece: did we capture something that had no defenders? ---
    if is_capture:
        defenders = board_before.attackers(not board_before.turn, move.to_square)
        # attackers() of the *victim's own color* = defenders of that square
        if len(defenders) == 0:
            sem.tags.append("captured_free_piece")

    # --- simple fork heuristic: moved piece now attacks 2+ enemy pieces ---
    attacked = board_after.attacks(move.to_square)
    targets_hit = 0
    if moved_piece_after is not None:
        for sq in attacked:
            p = board_after.piece_at(sq)
            if p is not None and p.color != mover_color and p.piece_type != chess.PAWN:
                targets_hit += 1
    if targets_hit >= 2:
        sem.tags.append("fork")

    # --- pin: did this move pin an enemy piece against its king? ---
    # python-chess's board.pin(color, square) returns the ray of squares a
    # pinned piece is restricted to (or the full board if it isn't pinned).
    # We only tag "pin" if the square we just moved to is on that ray -
    # i.e. our own piece is the one doing the pinning, not some pin that
    # already existed for unrelated reasons.
    if moved_piece_after is not None and moved_piece_after.piece_type != chess.KING:
        pin_enemy_color = not moved_piece_after.color
        for sq in chess.SQUARES:
            p = board_after.piece_at(sq)
            if p is None or p.color != pin_enemy_color or p.piece_type == chess.KING:
                continue
            if board_after.is_pinned(pin_enemy_color, sq):
                pin_ray = board_after.pin(pin_enemy_color, sq)
                if move.to_square in pin_ray:
                    sem.tags.append("pin")
                    sem.pinned_piece_name = _PIECE_NAMES[p.piece_type]
                    break

    if not sem.tags:
        sem.tags.append("quiet_move")

    return sem


# ---------------------------------------------------------------------------
# 4. TEXT - templates keyed on (classification, semantic tag)
# ---------------------------------------------------------------------------

_TEMPLATES: dict[tuple[Classification, str], list[str]] = {
    # ---------------- BEST ----------------
    (Classification.BEST, "blocked_check"): [
        "Your opponent blocked the check from your {piece}! Smooth.",
        "Blocking the check was the only sensible reply here.",
        "Interposing on the check line - clean defense.",
    ],
    (Classification.BEST, "captured_checking_piece"): [
        "Taking the checking piece removes the threat cleanly.",
        "Grabbing the {captured} that gave check - simplest solution.",
    ],
    (Classification.BEST, "king_escaped_check"): [
        "The king steps out of check, no better option available.",
        "The safest square for the king - well judged.",
    ],
    (Classification.BEST, "discovered_check"): [
        "A discovered check! Your {piece} unmasks an attack on the king.",
        "Unleashing a discovered check - hard to meet.",
    ],
    (Classification.BEST, "fork"): [
        "Your {piece} forks multiple pieces at once. Nice find!",
        "A fork with the {piece} - your opponent can't save everything.",
    ],
    (Classification.BEST, "pin"): [
        "Your {piece} pins the {pinned} to the king. Total lockdown.",
        "That pin leaves the {pinned} unable to move. Precise.",
    ],
    (Classification.BEST, "skewer"): [
        "A skewer with your {piece}! Whatever the king does, the {skewered} falls next.",
        "Skewering the king and the {skewered} behind it - the point is unanswerable.",
    ],
    (Classification.BEST, "discovered_attack"): [
        "A discovered attack from your {piece} lands on the {discovered} - hard to meet.",
        "Unmasking an attack on the {discovered} with a quiet in-between move. Nice.",
    ],
    (Classification.BEST, "captured_free_piece"): [
        "Free {captured}! Nothing was defending it.",
        "The {captured} was simply hanging - well spotted.",
    ],
    (Classification.BEST, "quiet_move"): [
        "The engine's top choice - solid and precise.",
        "Best move on the board. Nothing flashy, just correct.",
    ],
    (Classification.BEST, "castle"): [
        "Castling tucks the king away safely - good timing.",
    ],
    (Classification.BEST, "promotion"): [
        "Promotion! A new queen changes everything.",
    ],

    # ---------------- EXCELLENT ----------------
    (Classification.EXCELLENT, "quiet_move"): [
        "A strong, nearly-best move.",
        "Very close to the engine's top choice.",
    ],
    (Classification.EXCELLENT, "capture"): [
        "A strong capture, very close to the top engine line.",
    ],
    (Classification.EXCELLENT, "fork"): [
        "Still forks two pieces - just a hair off the very best try.",
    ],
    (Classification.EXCELLENT, "pin"): [
        "Pinning the {pinned} keeps the pressure on, even if not the sharpest try.",
    ],
    (Classification.EXCELLENT, "skewer"): [
        "Still a skewer on the {skewered} - just a shade off the sharpest version.",
    ],
    (Classification.EXCELLENT, "discovered_attack"): [
        "The discovered attack on the {discovered} is still strong, if not the sharpest try.",
    ],
    (Classification.EXCELLENT, "blocked_check"): [
        "A good block, just not the engine's very top pick.",
    ],
    (Classification.EXCELLENT, "king_escaped_check"): [
        "The king finds safety - nearly the best square available.",
    ],
    (Classification.EXCELLENT, "captured_free_piece"): [
        "The {captured} was hanging and you took it - just a shade off the sharpest line.",
    ],
    (Classification.EXCELLENT, "castle"): [
        "Castling here is excellent, if not the engine's exact preference.",
    ],

    # ---------------- GOOD ----------------
    (Classification.GOOD, "quiet_move"): [
        "A reasonable move that keeps your position healthy.",
        "Solid, if unspectacular.",
    ],
    (Classification.GOOD, "capture"): [
        "A fine capture, though a different one was slightly stronger.",
    ],
    (Classification.GOOD, "pin"): [
        "Pinning the {pinned} is useful, though there was a sharper follow-up.",
    ],
    (Classification.GOOD, "fork"): [
        "Still forks two pieces, though a cleaner shot was on the board.",
    ],
    (Classification.GOOD, "blocked_check"): [
        "This blocks the check well enough, if not the most precise interposition.",
    ],
    (Classification.GOOD, "king_escaped_check"): [
        "The king gets to safety, though a better square existed.",
    ],
    (Classification.GOOD, "captured_free_piece"): [
        "You pick up the free {captured}, though a stronger try was available.",
    ],
    (Classification.GOOD, "castle"): [
        "Reasonable castling, though the timing wasn't optimal.",
    ],

    # ---------------- INACCURACY ----------------
    (Classification.INACCURACY, "quiet_move"): [
        "This loosens your position slightly - there was something more precise.",
        "Playable, but not the most accurate choice.",
    ],
    (Classification.INACCURACY, "capture"): [
        "The capture is fine, but a different move kept more of your advantage.",
    ],
    (Classification.INACCURACY, "blocked_check"): [
        "This blocks the check, but a different interposition held up better.",
    ],
    (Classification.INACCURACY, "king_escaped_check"): [
        "The king moves to safety, but a less exposed square was available.",
    ],
    (Classification.INACCURACY, "fork"): [
        "The fork works, but your opponent has a saving resource here.",
    ],
    (Classification.INACCURACY, "pin"): [
        "The pin is real, but doesn't quite land as hard as it looks.",
    ],
    (Classification.INACCURACY, "castle"): [
        "Castling is reasonable, though the timing could be sharper.",
    ],

    # ---------------- MISTAKE ----------------
    (Classification.MISTAKE, "quiet_move"): [
        "This gives your opponent a real chance to fight back.",
        "A slip that hands back some of your advantage.",
    ],
    (Classification.MISTAKE, "capture"): [
        "Taking here opens up a tactic for your opponent - worth a second look.",
    ],
    (Classification.MISTAKE, "captured_free_piece"): [
        "You win material, but a stronger continuation was available.",
    ],
    (Classification.MISTAKE, "blocked_check"): [
        "Blocking here is understandable, but it walks into further problems.",
    ],
    (Classification.MISTAKE, "king_escaped_check"): [
        "The king finds safety for now, but this square has hidden issues.",
    ],
    (Classification.MISTAKE, "fork"): [
        "The fork looks tempting, but your opponent has a tactical answer.",
    ],
    (Classification.MISTAKE, "pin"): [
        "Pinning the {pinned} looks strong, but it lets your opponent untangle.",
    ],
    (Classification.MISTAKE, "castle"): [
        "Castling here walks into an attack - the king was safer in the center for now.",
    ],
    (Classification.MISTAKE, "back_rank_weakness"): [
        "This boxes your own king in on the back rank - a real danger if a rook or queen gets there.",
    ],

    # ---------------- BLUNDER ----------------
    (Classification.BLUNDER, "quiet_move"): [
        "This move hands your opponent a significant advantage.",
        "A costly slip - the position swings sharply here.",
    ],
    (Classification.BLUNDER, "capture"): [
        "This capture walks into trouble - check what recaptures next.",
    ],
    (Classification.BLUNDER, "captured_free_piece"): [
        "You grab the {captured}, but it's a trap - a bigger loss follows.",
    ],
    (Classification.BLUNDER, "blocked_check"): [
        "Blocking here loses material by force - a different piece needed to interpose.",
    ],
    (Classification.BLUNDER, "king_escaped_check"): [
        "The king escapes the check but walks straight into a worse attack.",
    ],
    (Classification.BLUNDER, "fork"): [
        "The fork backfires - your opponent has a much stronger reply.",
    ],
    (Classification.BLUNDER, "pin"): [
        "The pin looks scary but leaves your own {piece} badly placed.",
    ],
    (Classification.BLUNDER, "castle"): [
        "Castling directly into the attack - this is a serious problem.",
    ],
    (Classification.BLUNDER, "discovered_check"): [
        "The discovered check backfires - your own king ends up exposed.",
    ],
    (Classification.BLUNDER, "back_rank_weakness"): [
        "Your king is now trapped on the back rank with no escape squares - a back-rank disaster waiting to happen.",
    ],

    # ---------------- MISSED WIN ----------------
    (Classification.MISSED_WIN, "quiet_move"): [
        "There was a much stronger continuation here that slipped away.",
    ],
    (Classification.MISSED_WIN, "capture"): [
        "This capture is fine, but a much bigger blow was on the board.",
    ],
    (Classification.MISSED_WIN, "fork"): [
        "The fork wins something, but an even stronger shot was available.",
    ],

    # ---------------- BRILLIANT ----------------
    (Classification.BRILLIANT, "captured_free_piece"): [
        "A brilliant sacrifice! Your {piece} looks lost, but it wins far more than it gives up.",
    ],
    (Classification.BRILLIANT, "fork"): [
        "A brilliant shot - offering the {piece} to set up a fork your opponent can't answer.",
    ],
    (Classification.BRILLIANT, "quiet_move"): [
        "A brilliant sacrifice! Giving up material here is the engine's top choice - and it's not obvious why.",
    ],

    # ---------------- GREAT ----------------
    (Classification.GREAT, "quiet_move"): [
        "A great find - by far the only move that keeps your position together here.",
    ],
    (Classification.GREAT, "blocked_check"): [
        "The only real way to meet this check - a great, precise defense.",
    ],
    (Classification.GREAT, "capture"): [
        "A great capture - every other option here was significantly worse.",
    ],

    # ---------------- BOOK ----------------
    (Classification.BOOK, "quiet_move"): [
        "Still known opening theory.",
    ],
    (Classification.BOOK, "castle"): [
        "Castling here is standard opening theory.",
    ],
}

_NO_DETAIL_CLASSES = {Classification.BEST, Classification.BRILLIANT, Classification.GREAT, Classification.BOOK}

_FALLBACKS: dict[Classification, list[str]] = {
    Classification.BRILLIANT: ["A brilliant, unexpected sacrifice."],
    Classification.GREAT: ["A great, hard-to-find move - clearly the best option available."],
    Classification.BEST: ["The engine's top choice."],
    Classification.EXCELLENT: ["Nearly the best move available."],
    Classification.GOOD: ["A solid, reasonable move."],
    Classification.BOOK: ["A well-known opening move."],
    Classification.INACCURACY: ["Not the most precise choice here."],
    Classification.MISTAKE: ["This gives back some of your advantage."],
    Classification.BLUNDER: ["This move loses significant ground."],
    Classification.MISSED_WIN: ["A much stronger continuation was missed."],
}


def _pick_tag(sem: MoveSemantics) -> str:
    """Priority order when a move matches multiple tags."""
    priority = [
        "blocked_check", "captured_checking_piece", "king_escaped_check",
        "discovered_check", "discovered_attack", "skewer", "fork", "pin",
        "captured_free_piece", "castle", "promotion", "capture",
        # back_rank_weakness is deliberately last (just above the no-tag
        # fallback): it's a static king-safety fact true of most castled
        # positions, not something the move *did* - it should only be
        # reported when nothing more specific explains the move.
        "back_rank_weakness", "quiet_move",
    ]
    for tag in priority:
        if tag in sem.tags:
            return tag
    return sem.tags[0]


def generate_commentary(
    board_before: chess.Board,
    move: chess.Move,
    best_move: chess.Move,
    eval_before: Eval,
    eval_after_played: Eval,
    eval_after_best: Eval,
    rng: Optional[random.Random] = None,
    second_best_eval: Optional[Eval] = None,
) -> dict:
    """
    Full pipeline for one move. Returns a dict shaped like what you'd
    render in a UI: classification label, eval, commentary text, and a
    "detail" line spelling out the concrete cost (eval swing and roughly
    how much win probability that cost, relative to the engine's best
    move) plus what should have been played instead.

    second_best_eval is optional - only ever available when the game was
    analyzed by us with multipv=2, not for chess.com-exported annotations -
    and enables the Great classification.
    """
    rng = rng or random
    side_white = board_before.turn == chess.WHITE

    sem = analyze_semantics(board_before, move)
    cls = classify_move(
        move, best_move, eval_before, eval_after_played, eval_after_best, side_white,
        board_before=board_before, second_best_eval=second_best_eval,
    )
    tag = _pick_tag(sem)

    templates = _TEMPLATES.get((cls, tag)) or _FALLBACKS[cls]
    text = rng.choice(templates).format(
        piece=sem.piece_name,
        captured=sem.captured_piece_name or "piece",
        pinned=sem.pinned_piece_name or "piece",
        skewered=sem.skewered_piece_name or "piece",
        discovered=sem.discovered_piece_name or "piece",
    )

    win_after_best = win_percent_for_mover(eval_after_best, side_white)
    win_after_played = win_percent_for_mover(eval_after_played, side_white)
    win_prob_loss = max(win_after_best - win_after_played, 0.0)

    best_san: Optional[str] = None
    if move != best_move and cls != Classification.BOOK:
        try:
            best_san = board_before.san(best_move)
        except (ValueError, AssertionError):
            best_san = None

    detail: Optional[str] = None
    if cls not in _NO_DETAIL_CLASSES:
        cost = (
            "less than 1% of your winning chances"
            if win_prob_loss < 1
            else f"~{win_prob_loss:.0f}% of your winning chances"
        )
        detail = f"Eval went from {eval_before.display()} to {eval_after_played.display()} ({cost} lost)."
        if best_san:
            detail += f" {best_san} was the engine's choice instead."

    return {
        "classification": cls.value,
        "eval": eval_after_played.display(),
        "eval_cp_or_mate": {"cp": eval_after_played.cp, "mate": eval_after_played.mate},
        "tags": sem.tags,
        "text": text,
        "detail": detail,
        "win_prob_loss": round(win_prob_loss, 1),
        "best_san": best_san,
    }


_REFUTATION_PHRASES = {
    "captured_free_piece": "it leaves the {captured} hanging - the reply simply wins it for free",
    "favorable_capture": "it drops the {captured} to a straightforward capture",
    "capture": "it drops the {captured}",
    "fork": "it allows a fork - the {piece} now attacks two of your pieces at once",
    "pin": "it walks into a pin, leaving the {pinned} stuck in place",
    "discovered_check": "it allows a discovered check that swings the position further",
    "direct_check": "it allows a check that gains further ground",
    "promotion": "it even lets a pawn promote",
    "skewer": "it allows a skewer - the {piece} must move and exposes the {skewered} behind it",
    "discovered_attack": "it opens a discovered attack on the {discovered}",
    # back_rank_weakness is intentionally not listed here: describe_refutation
    # analyzes the *opponent's reply* move, so this tag would describe the
    # replying side's own king safety, not the boxed-in king the bad move
    # actually left behind - the phrase would be pointing at the wrong side.
}


def describe_refutation(board_after: chess.Board, reply_move: chess.Move) -> Optional[tuple[str, str, dict]]:
    """
    Explains what the opponent's best reply actually exploits after a bad
    move - the concrete "why" behind vague flavor text like "the position
    swings sharply here." board_after is the position right after the bad
    move (so it's the opponent's turn); reply_move is their best response to
    it. Returns None when that reply has no specific tactical hook worth
    naming (e.g. just a quiet improving move); otherwise (tag, sentence,
    pieces), where tag is the raw motif (e.g. "fork") for cross-game
    aggregation, sentence is the human-readable phrase, and pieces is a dict
    with whichever of "piece"/"captured"/"pinned" apply to this motif (e.g.
    "captured" names which of your pieces was lost) - the same semantic data
    already used to build the sentence, kept structured for aggregation
    instead of only ending up baked into prose.
    """
    if reply_move not in board_after.legal_moves:
        return None

    sem = analyze_semantics(board_after, reply_move)
    tag = _pick_tag(sem)
    phrase = _REFUTATION_PHRASES.get(tag)
    if phrase is None:
        return None
    sentence = phrase.format(
        captured=sem.captured_piece_name or "piece",
        piece=sem.piece_name,
        pinned=sem.pinned_piece_name or "piece",
        skewered=sem.skewered_piece_name or "piece",
        discovered=sem.discovered_piece_name or "piece",
    )
    pieces = {}
    if sem.piece_name:
        pieces["piece"] = sem.piece_name
    if sem.captured_piece_name:
        pieces["captured"] = sem.captured_piece_name
    if sem.pinned_piece_name:
        pieces["pinned"] = sem.pinned_piece_name
    if sem.skewered_piece_name:
        pieces["skewered"] = sem.skewered_piece_name
    if sem.discovered_piece_name:
        pieces["discovered"] = sem.discovered_piece_name
    return tag, sentence, pieces


# ---------------------------------------------------------------------------
# Demo / self-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Recreates the situation in the screenshot: White queen gives check,
    # Black interposes a piece to block it. Eval swings are illustrative.
    board = chess.Board("4k3/8/8/8/8/8/4Q3/4K3 w - - 0 1")  # White queen checks Black king down the e-file... adjust as needed
    board.turn = chess.WHITE

    # Simple constructed example: white queen already checking on e-file,
    # black to move blocks with a rook on e6.
    fen = "4k3/8/4r3/8/8/8/4Q3/4K3 b - - 0 1"
    board = chess.Board(fen)
    move = chess.Move.from_uci("e6e5")  # not a real block, just demo wiring

    print("This module is meant to be imported. Example wiring:\n")
    print("""
import chess
from chess_commentary import Eval, generate_commentary

board = chess.Board(some_fen)
move = chess.Move.from_uci(some_uci)
best_move = chess.Move.from_uci(engine_best_uci)

result = generate_commentary(
    board_before=board,
    move=move,
    best_move=best_move,
    eval_before=Eval(cp=eval_before_cp),
    eval_after_played=Eval(cp=eval_after_played_cp),
    eval_after_best=Eval(cp=eval_after_best_cp),
)
print(result["classification"], result["eval"], "-", result["text"])
""")