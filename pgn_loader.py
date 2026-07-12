"""
pgn_loader.py

Reads a chess.com-style annotated PGN (the [%eval ...] [%best ...] [%tag ...]
comment format seen in enhanced.pgn) and turns it into a list of MoveRecord
objects, each carrying the board position, the raw annotation data, and the
scoresheet-generated commentary for that move.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

import chess
import chess.pgn

from scoresheet import Eval, describe_refutation, generate_commentary

_TAG_RE = re.compile(r"\[%(\w+)\s+([^\]]+)\]")


def _parse_comment_tags(comment: str) -> dict[str, str]:
    return dict(_TAG_RE.findall(comment or ""))


def _parse_eval_tag(raw: str) -> Eval:
    raw = raw.strip()
    if raw.startswith("#"):
        return Eval(mate=int(raw[1:]))
    return Eval(cp=round(float(raw) * 100))


@dataclass
class MoveRecord:
    move_number: int
    color_white: bool
    san: str
    board_before: chess.Board
    move: chess.Move
    best_move: chess.Move
    eval_before: Eval
    eval_after: Eval
    best_san: str
    phase: str
    pgn_tag: Optional[str]
    wpl: Optional[float]
    commentary: dict

    @property
    def fen_before(self) -> str:
        return self.board_before.fen()


# Classifications vague enough ("the position swings sharply here") that
# it's worth spelling out what the opponent's best reply actually exploits.
_WANTS_REFUTATION = {"Inaccuracy", "Mistake", "Blunder", "Miss"}


def attach_refutations(records: list[MoveRecord]) -> None:
    """
    Second pass over an already-built record list: for moves that gave away
    real advantage, look at the position immediately after (records[i+1] is
    the opponent's turn there) and describe what their best reply exploits,
    appending it to that move's commentary "detail" line.
    """
    for i in range(len(records) - 1):
        rec = records[i]
        if rec.commentary["classification"] not in _WANTS_REFUTATION:
            continue
        next_rec = records[i + 1]
        result = describe_refutation(next_rec.board_before, next_rec.best_move)
        if not result:
            continue
        tag, why, pieces = result
        rec.commentary["why_tag"] = tag
        rec.commentary["why_piece_info"] = pieces
        sentence = why[0].upper() + why[1:]
        existing = rec.commentary.get("detail") or ""
        rec.commentary["detail"] = f"{existing} {sentence}.".strip()


def has_eval_annotations(path: str) -> bool:
    """Quick check for whether a PGN already carries chess.com-style
    [%eval ...] comments, or is a plain game that needs engine analysis."""
    with open(path, encoding="utf-8") as f:
        return "%eval" in f.read()


def game_has_eval_annotations(game: chess.pgn.Game) -> bool:
    """Per-game version of has_eval_annotations, for multi-game files where
    some games might carry annotations and others don't."""
    return any("%eval" in (node.comment or "") for node in game.mainline())


def chesscom_link(game: chess.pgn.Game) -> Optional[str]:
    """The game's chess.com URL from its PGN [Link ...] header, if it has
    one - only chess.com-sourced PGNs (manual export or the in-app import)
    carry this tag."""
    link = (game.headers.get("Link") or "").strip()
    if link.startswith("https://www.chess.com/") or link.startswith("http://www.chess.com/"):
        return link
    return None


def my_records(
    records: list[MoveRecord], white_name: str, black_name: str, my_name: str
) -> list[tuple[int, MoveRecord]]:
    """Filters a game's move records down to the ones made by my_name, paired
    with their original index (needed by callers that jump to a specific
    ply) - color is resolved per-game since the same player can be White in
    one game and Black in the next."""
    return [
        (i, rec) for i, rec in enumerate(records)
        if (rec.color_white and white_name == my_name) or (not rec.color_white and black_name == my_name)
    ]


def _with_rating(name: str, elo: str) -> str:
    elo = (elo or "").strip()
    return f"{name} ({elo})" if elo and elo != "?" else name


_RESULT_TEXT = {"1-0": "{white} won", "0-1": "{black} won", "1/2-1/2": "Draw"}


def describe_game(game: chess.pgn.Game, my_name: Optional[str] = None) -> str:
    """Human-readable "who / who won / when" summary from a game's PGN
    headers - e.g. "You (613) vs anka46-09 (525) · maplewoodstrat won by
    resignation · 2026.07.03 19:28". Falls back gracefully when a header
    (most often the timestamp, on a hand-edited or non-chess.com PGN) isn't
    present."""
    headers = game.headers
    white = headers.get("White", "?")
    black = headers.get("Black", "?")
    white_display = _with_rating(white, headers.get("WhiteElo", ""))
    black_display = _with_rating(black, headers.get("BlackElo", ""))

    if my_name and my_name == white and my_name != black:
        opponent = f"{_with_rating('You', headers.get('WhiteElo', ''))} vs {black_display}"
    elif my_name and my_name == black and my_name != white:
        opponent = f"{_with_rating('You', headers.get('BlackElo', ''))} vs {white_display}"
    else:
        opponent = f"{white_display} vs {black_display}"

    termination = (headers.get("Termination") or "").strip()
    if termination:
        result = termination
    else:
        result = _RESULT_TEXT.get(headers.get("Result", "*"), "Result unknown").format(white=white, black=black)

    date = headers.get("EndDate") or headers.get("Date") or ""
    time = headers.get("EndTime") or headers.get("UTCTime") or ""
    timestamp = f"{date} {time}".strip() if date and "?" not in date else ""

    parts = [opponent, result]
    if timestamp:
        parts.append(timestamp)
    return " · ".join(parts)


def read_games(path: str) -> list[chess.pgn.Game]:
    """Parses every game out of a (possibly multi-game) PGN file without
    building MoveRecords - the cheap first pass used to see how many games
    are in a file before deciding what (if anything) needs engine analysis."""
    games: list[chess.pgn.Game] = []
    with open(path, encoding="utf-8") as f:
        while True:
            game = chess.pgn.read_game(f)
            if game is None:
                break
            games.append(game)
    return games


def _estimate_phase(board: chess.Board) -> str:
    """Fallback for when a PGN carries no chess.com [%phase] tag (every
    Stockfish-analyzed game, including chess.com imports - their archive API
    never includes %phase, only %clk). A simple, standard heuristic rather
    than anything engine-driven: move count for the opening, remaining
    major/minor material for the endgame cutoff."""
    if board.fullmove_number <= 10:
        return "Opening"
    queens = len(board.pieces(chess.QUEEN, chess.WHITE)) + len(board.pieces(chess.QUEEN, chess.BLACK))
    minors_majors = sum(
        len(board.pieces(piece_type, color))
        for piece_type in (chess.KNIGHT, chess.BISHOP, chess.ROOK, chess.QUEEN)
        for color in (chess.WHITE, chess.BLACK)
    )
    if queens == 0 or minors_majors <= 6:
        return "Endgame"
    return "Middlegame"


def _build_records(game: chess.pgn.Game) -> list[MoveRecord]:
    records: list[MoveRecord] = []
    prev_eval = Eval(cp=0)
    node = game

    while node.variations:
        next_node = node.variations[0]
        board_before = node.board()
        move = next_node.move
        tags = _parse_comment_tags(next_node.comment)

        eval_after = _parse_eval_tag(tags["eval"]) if "eval" in tags else prev_eval

        best_san = tags.get("best", "")
        best_move = move
        if best_san:
            try:
                best_move = board_before.parse_san(best_san)
            except ValueError:
                best_move = move

        commentary = generate_commentary(
            board_before=board_before,
            move=move,
            best_move=best_move,
            eval_before=prev_eval,
            eval_after_played=eval_after,
            eval_after_best=prev_eval,
        )

        records.append(MoveRecord(
            move_number=board_before.fullmove_number,
            color_white=board_before.turn == chess.WHITE,
            san=next_node.san(),
            board_before=board_before,
            move=move,
            best_move=best_move,
            eval_before=prev_eval,
            eval_after=eval_after,
            best_san=best_san,
            phase=tags.get("phase") or _estimate_phase(board_before),
            pgn_tag=tags.get("tag"),
            wpl=float(tags["wpl"]) if "wpl" in tags else None,
            commentary=commentary,
        ))

        prev_eval = eval_after
        node = next_node

    attach_refutations(records)
    return records


def load_game(path: str) -> tuple[chess.pgn.Game, list[MoveRecord]]:
    with open(path, encoding="utf-8") as f:
        game = chess.pgn.read_game(f)
    if game is None:
        raise ValueError(f"No game found in {path}")
    return game, _build_records(game)


def load_games(path: str) -> list[tuple[chess.pgn.Game, list[MoveRecord]]]:
    """Reads every game out of a (possibly multi-game) PGN file, in order."""
    return [(game, _build_records(game)) for game in read_games(path)]
