"""
puzzle_trainer.py

Turns your own Mistakes/Blunders into puzzles: shows the position right
before the bad move and challenges you to find the engine's move yourself,
instead of just being told what you should have played. Unlike a generic
puzzle database, every puzzle here is a mistake you actually made.

Puzzles can be filtered down to specific move classifications (Inaccuracy/
Mistake/Blunder), and a whole PGN file - including one containing several
games - can be loaded directly into the trainer, pooling bad moves from
every annotated game in it.
"""

from __future__ import annotations

import os
import random
import threading
from dataclasses import dataclass
from datetime import date
from typing import Callable, Optional

import chess
import chess.engine
import chess.pgn
from PySide6.QtCore import QObject, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QFont
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QVBoxLayout,
)

import player_identity
import puzzle_history
from board_widget import BoardWidget
from chesscom_import_dialog import ChessComImportDialog
from engine_analysis import AUTO_ANALYZE_THRESHOLD, DEFAULT_LIMIT, FAST_LIMIT, find_engine
from pgn_loader import MoveRecord, _build_records, chesscom_link, describe_game, game_has_eval_annotations, read_games
from sound import MoveSoundPlayer
from summary_dialog import _CLASS_COLORS
from weakness_report import _BatchAnalysisWorker

_TEXT_PRIMARY = "#f5f5f6"
_TEXT_MUTED = "#b8b8bd"
_SOLID_PANEL_BG = "rgb(52, 52, 55)"
_BORDER_COLOR = "#3d3d42"
_GOOD_COLOR = "#6bb95b"
_BAD_COLOR = "#e0452c"

_FILTERABLE_CLASSES = ["Inaccuracy", "Mistake", "Blunder", "Miss"]
_DEFAULT_CHECKED = {"Mistake", "Blunder"}

# How deep to rank a wrong guess against the engine's own choices, and how
# long to think per query - short, since this runs on every wrong attempt
# and only needs to distinguish "close" from "not even considered".
_RANK_MULTIPV = 5
_RANK_LIMIT = chess.engine.Limit(time=0.3)


def _ordinal(n: int) -> str:
    if 10 <= n % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"

_BUTTON_STYLE = f"""
    QPushButton {{ padding: 6px 14px; border-radius: 5px; border: 1px solid {_BORDER_COLOR};
                   background: rgba(255,255,255,0.06); color: {_TEXT_PRIMARY}; }}
    QPushButton:hover {{ background: rgba(58,168,255,0.18); border-color: #3aa8ff; }}
    QPushButton:pressed {{ background: rgba(58,168,255,0.30); }}
"""


@dataclass
class _Puzzle:
    record: MoveRecord
    game_label: Optional[str] = None
    game: Optional["chess.pgn.Game"] = None
    game_records: Optional[list[MoveRecord]] = None
    ply_index: int = -1


class _MoveRankWorker(QObject):
    """One-shot: spawns its own engine process to rank `move` among the top
    _RANK_MULTIPV choices at `board` - used to enrich a wrong puzzle guess
    with "that was the engine's Nth pick" instead of a flat "no". A fresh
    process per query (rather than one kept alive for the dialog) is fine
    since this already runs off the UI thread - spawn overhead just adds to
    the background wait, it never blocks input. Plain thread, not QThread:
    python-chess's engine transport needs asyncio's Windows ProactorEventLoop
    for subprocess pipes, which breaks inside a QThread's OS thread (see
    _BatchAnalysisWorker in weakness_report.py)."""

    finished_ok = Signal(object)  # Optional[int], 1-based rank
    failed = Signal(str)

    def __init__(self, board: chess.Board, move: chess.Move, engine_path: str):
        super().__init__()
        self.board = board
        self.move = move
        self.engine_path = engine_path

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        try:
            with chess.engine.SimpleEngine.popen_uci(self.engine_path) as engine:
                infos = engine.analyse(self.board, _RANK_LIMIT, multipv=_RANK_MULTIPV)
            ranked = [info["pv"][0] for info in infos if info.get("pv")]
            rank = ranked.index(self.move) + 1 if self.move in ranked else None
            self.finished_ok.emit(rank)
        except Exception as exc:  # noqa: BLE001 - surface any engine failure, the caller just skips the enrichment
            self.failed.emit(str(exc))


class PuzzleTrainerDialog(QDialog):
    """One puzzle per Mistake/Blunder (or Inaccuracy, if enabled) in the
    given records, served in order. Click a piece then a destination square
    to attempt a move; matches against the engine's recorded best move for
    that position."""

    def __init__(
        self,
        records: list[MoveRecord],
        source_game: Optional["chess.pgn.Game"] = None,
        on_view_game: Optional[Callable[["chess.pgn.Game", list[MoveRecord], int], None]] = None,
        parent=None,
    ):
        super().__init__(parent)
        self.setWindowTitle("Practice Your Blunders")
        self.resize(600, 760)
        self.setStyleSheet(f"QDialog {{ background-color: {_SOLID_PANEL_BG}; }}")

        self.on_view_game = on_view_game
        self.all_puzzles: list[_Puzzle] = [
            _Puzzle(record=r, game=source_game, game_records=records, ply_index=i) for i, r in enumerate(records)
        ]
        self.puzzles: list[_Puzzle] = []
        self.selected_classes: set[str] = set(_DEFAULT_CHECKED)
        self.index = 0
        self.solved = False
        self.selected_square: Optional[int] = None
        self._wrong_attempts = 0
        self.history: dict = puzzle_history.load_history()
        self.sound_player = MoveSoundPlayer()

        self._engine_path: Optional[str] = None
        self._engine_checked = False
        self._rank_worker: Optional[_MoveRankWorker] = None
        self._rank_token = 0

        self.progress_dialog: Optional[QProgressDialog] = None
        self.analysis_worker: Optional[_BatchAnalysisWorker] = None
        self._pending_path: Optional[str] = None
        self._pending_file_games: list["chess.pgn.Game"] = []
        self._pending_records_by_index: dict[int, list[MoveRecord]] = {}
        self._pending_raw_indices: list[int] = []
        self.chesscom_dialog: Optional[ChessComImportDialog] = None

        open_btn = QPushButton("Open PGN...")
        open_btn.setStyleSheet(_BUTTON_STYLE)
        open_btn.clicked.connect(self._open_pgn)

        import_btn = QPushButton("Import from chess.com...")
        import_btn.setStyleSheet(_BUTTON_STYLE)
        import_btn.clicked.connect(self._open_chesscom_import)

        self.checkboxes: dict[str, QCheckBox] = {}
        filter_row = QHBoxLayout()
        filter_label = QLabel("Practice:")
        filter_label.setStyleSheet(f"color: {_TEXT_MUTED};")
        filter_row.addWidget(filter_label)
        for cls in _FILTERABLE_CLASSES:
            cb = QCheckBox(cls)
            cb.setChecked(cls in _DEFAULT_CHECKED)
            cb.setStyleSheet(f"QCheckBox {{ color: {_CLASS_COLORS[cls]}; }}")
            cb.stateChanged.connect(self._on_filter_changed)
            self.checkboxes[cls] = cb
            filter_row.addWidget(cb)
        filter_row.addStretch(1)
        filter_row.addWidget(import_btn)
        filter_row.addWidget(open_btn)

        self.source_label = QLabel()
        self.source_label.setWordWrap(True)
        self.source_label.setStyleSheet(f"color: {_TEXT_MUTED};")

        self.board_widget = BoardWidget()
        self.board_widget.interactive = True
        self.board_widget.on_square_clicked = self._on_square_clicked

        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        self.status_label.setStyleSheet(f"color: {_TEXT_PRIMARY};")
        status_font = QFont("Segoe UI", 11)
        status_font.setWeight(QFont.DemiBold)
        self.status_label.setFont(status_font)

        self.feedback_label = QLabel()
        self.feedback_label.setWordWrap(True)
        self.feedback_label.setFont(QFont("Segoe UI", 10))
        self.feedback_label.setMinimumHeight(60)

        self.game_detail_label = QLabel()
        self.game_detail_label.setWordWrap(True)
        self.game_detail_label.setStyleSheet(f"color: {_TEXT_MUTED};")
        self.game_detail_label.setFont(QFont("Segoe UI", 9))
        self.game_detail_label.setVisible(False)

        self.reveal_btn = QPushButton("Reveal Answer")
        self.next_btn = QPushButton("Next Puzzle ▶")
        self.view_game_btn = QPushButton("View Full Game ▸")
        self.chesscom_link_btn = QPushButton("View on Chess.com ↗")
        for btn in (self.reveal_btn, self.next_btn, self.view_game_btn, self.chesscom_link_btn):
            btn.setStyleSheet(_BUTTON_STYLE)
        self.reveal_btn.clicked.connect(self._reveal)
        self.next_btn.clicked.connect(self._next_puzzle)
        self.view_game_btn.clicked.connect(self._on_view_game)
        self.chesscom_link_btn.clicked.connect(self._on_open_chesscom_link)
        self.view_game_btn.setVisible(False)
        self.chesscom_link_btn.setVisible(False)
        self._chesscom_link: Optional[str] = None

        btn_row = QHBoxLayout()
        btn_row.addWidget(self.reveal_btn)
        btn_row.addWidget(self.next_btn)
        btn_row.addWidget(self.view_game_btn)
        btn_row.addWidget(self.chesscom_link_btn)

        layout = QVBoxLayout()
        layout.addLayout(filter_row)
        layout.addWidget(self.source_label)
        layout.addWidget(self.status_label)
        layout.addWidget(self.board_widget, stretch=1)
        layout.addWidget(self.feedback_label)
        layout.addWidget(self.game_detail_label)
        layout.addLayout(btn_row)
        self.setLayout(layout)

        self._apply_filter()

    # -- puzzle set management ------------------------------------------

    def _on_filter_changed(self):
        self.selected_classes = {cls for cls, cb in self.checkboxes.items() if cb.isChecked()}
        self._apply_filter()

    def _puzzle_key(self, puzzle: _Puzzle) -> str:
        rec = puzzle.record
        return puzzle_history.puzzle_key(rec.fen_before, rec.move.uci())

    def _apply_filter(self):
        filtered = [p for p in self.all_puzzles if p.record.commentary["classification"] in self.selected_classes]
        random.shuffle(filtered)
        today = date.today()
        self.puzzles = sorted(filtered, key=lambda p: puzzle_history.sort_key(self.history, self._puzzle_key(p), today))
        self.index = 0
        if self.puzzles:
            self.reveal_btn.setEnabled(True)
            self.next_btn.setEnabled(True)
            self._load_puzzle()
        else:
            self.reveal_btn.setEnabled(False)
            self.next_btn.setEnabled(False)
            self.board_widget.set_position(chess.Board(), None)
            self.feedback_label.setText("")
            if not self.all_puzzles:
                self.status_label.setText("No Mistakes or Blunders to practice - nicely played! "
                                           "Use \"Open PGN...\" to load a different game.")
            elif not self.selected_classes:
                self.status_label.setText("Select at least one move type above to practice.")
            else:
                self.status_label.setText("No moves of the selected type(s) in the loaded game(s).")

    def _open_pgn(self):
        path, _ = QFileDialog.getOpenFileName(self, "Open PGN", "", "PGN files (*.pgn);;All files (*)")
        if not path:
            return
        self._process_path(path)

    def _open_chesscom_import(self):
        self.chesscom_dialog = ChessComImportDialog(self._on_chesscom_imported, parent=self)
        self.chesscom_dialog.show()

    def _on_chesscom_imported(self, path: str, username: str):
        self._process_path(path)

    def _process_path(self, path: str):
        try:
            file_games = read_games(path)
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "Open PGN", str(exc))
            return

        if not file_games:
            QMessageBox.warning(self, "Open PGN", "No games found in that file.")
            return

        self._pending_path = path
        self._pending_file_games = file_games
        records_by_index: dict[int, list[MoveRecord]] = {}
        raw_indices: list[int] = []
        for i, game in enumerate(file_games):
            if game_has_eval_annotations(game):
                records_by_index[i] = _build_records(game)
            else:
                raw_indices.append(i)
        self._pending_records_by_index = records_by_index

        if not raw_indices:
            self._finish_open_pgn(skipped=0)
            return

        fast = False
        if len(raw_indices) > AUTO_ANALYZE_THRESHOLD:
            plural = "game" if len(raw_indices) == 1 else "games"
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Question)
            box.setWindowTitle("Unannotated games found")
            box.setText(
                f"{len(raw_indices)} of the {len(file_games)} {plural} in that file have no Stockfish "
                "annotations. Analyze them now with a local Stockfish engine? Large batches can take a while."
            )
            box.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
            fast_checkbox = QCheckBox("Faster analysis (lower accuracy)")
            fast_checkbox.setChecked(True)
            box.setCheckBox(fast_checkbox)
            answer = box.exec()
            if answer != QMessageBox.Yes:
                self._finish_open_pgn(skipped=len(raw_indices))
                return
            fast = fast_checkbox.isChecked()

        engine_path = find_engine()
        if not engine_path:
            QMessageBox.information(
                self, "Locate Stockfish",
                "Couldn't auto-detect a Stockfish engine on this machine. "
                "Please browse to the stockfish executable.",
            )
            engine_path, _ = QFileDialog.getOpenFileName(self, "Locate Stockfish executable")
            if not engine_path:
                self._finish_open_pgn(skipped=len(raw_indices))
                return

        self._pending_raw_indices = raw_indices
        self._run_batch_analysis(engine_path, [file_games[i] for i in raw_indices], fast=fast)

    def _run_batch_analysis(self, engine_path: str, raw_games: list["chess.pgn.Game"], fast: bool = False):
        label = "Analyzing games with Stockfish (fast mode)..." if fast else "Analyzing games with Stockfish..."
        self.progress_dialog = QProgressDialog(label, "Cancel", 0, 0, self)
        self.progress_dialog.setWindowTitle("Analyzing")
        self.progress_dialog.setWindowModality(Qt.WindowModal)
        self.progress_dialog.setMinimumDuration(0)
        self.progress_dialog.setStyleSheet(
            f"QProgressDialog {{ background-color: {_SOLID_PANEL_BG}; }}"
            f"QLabel {{ color: {_TEXT_PRIMARY}; background: transparent; }}"
            + _BUTTON_STYLE
        )
        self.progress_dialog.show()

        limit = FAST_LIMIT if fast else DEFAULT_LIMIT
        self.analysis_worker = _BatchAnalysisWorker(raw_games, engine_path, limit)
        self.analysis_worker.progress.connect(self._on_batch_progress)
        self.analysis_worker.finished_ok.connect(self._on_batch_finished)
        self.analysis_worker.failed.connect(self._on_batch_failed)
        self.progress_dialog.canceled.connect(self.analysis_worker.request_stop)
        self.analysis_worker.start()

    def _on_batch_progress(self, games_done: int, games_total: int, moves_done: int, moves_total: int):
        dialog = self.progress_dialog
        if dialog is None:
            return
        dialog.setMaximum(games_total)
        # setValue() can pump the event loop and dispatch an already-queued
        # finished_ok signal, which closes and nulls self.progress_dialog
        # before this call returns - re-check before touching it again.
        dialog.setValue(games_done)
        if self.progress_dialog is not None:
            dialog.setLabelText(
                f"Analyzing game {games_done + 1}/{games_total} (move {moves_done}/{moves_total})..."
            )

    def _on_batch_finished(self, results: list):
        if self.progress_dialog:
            self.progress_dialog.close()
            self.progress_dialog = None
        for idx, records in zip(self._pending_raw_indices, results):
            self._pending_records_by_index[idx] = records
        skipped = len(self._pending_raw_indices) - len(results)
        self._finish_open_pgn(skipped=skipped)

    def _on_batch_failed(self, message: str):
        if self.progress_dialog:
            self.progress_dialog.close()
            self.progress_dialog = None
        QMessageBox.warning(self, "Analysis failed", message)
        self._finish_open_pgn(skipped=len(self._pending_raw_indices))

    def _finish_open_pgn(self, skipped: int):
        file_games = self._pending_file_games
        records_by_index = self._pending_records_by_index
        included = [(i, file_games[i], records_by_index[i]) for i in sorted(records_by_index)]

        if not included:
            QMessageBox.warning(
                self, "Open PGN",
                "None of the games in that file have Stockfish annotations to build puzzles from.",
            )
            return

        multi = len(included) > 1
        puzzles: list[_Puzzle] = []
        for pos, (_, game, recs) in enumerate(included, start=1):
            label = None
            if multi:
                white = game.headers.get("White", "?")
                black = game.headers.get("Black", "?")
                label = f"Game {pos}/{len(included)} ({white} vs {black})"
            puzzles.extend(
                _Puzzle(record=r, game_label=label, game=game, game_records=recs, ply_index=i)
                for i, r in enumerate(recs)
            )

        self.all_puzzles = puzzles
        note = f" ({skipped} game(s) skipped - no annotations)" if skipped else ""
        game_word = "game" if len(included) == 1 else "games"
        self.source_label.setText(
            f"Loaded {len(included)} {game_word} from {os.path.basename(self._pending_path)}{note}"
        )
        self._apply_filter()

    # -- puzzle flow -------------------------------------------------------

    def _current(self) -> _Puzzle:
        return self.puzzles[self.index]

    def _load_puzzle(self):
        puzzle = self._current()
        rec = puzzle.record
        self.solved = False
        self._wrong_attempts = 0
        self.selected_square = None
        self.board_widget.selected_square = None
        self.board_widget.legal_targets = set()
        self.board_widget.set_flipped(not rec.color_white)
        self.board_widget.set_position(rec.board_before.copy(), None)

        mover = "White" if rec.color_white else "Black"
        game_note = f"{puzzle.game_label}\n" if puzzle.game_label else ""
        review_note = self._review_note(puzzle)
        self.status_label.setText(
            f"{game_note}Puzzle {self.index + 1}/{len(self.puzzles)} — move {rec.move_number} ({mover} to move) "
            f"· {review_note}\n"
            f"You played {rec.san} here ({rec.commentary['classification']}). Find a better move."
        )
        self.feedback_label.setStyleSheet(f"color: {_TEXT_MUTED};")
        self.feedback_label.setText("")
        self.game_detail_label.setVisible(False)
        self.view_game_btn.setVisible(False)
        self.chesscom_link_btn.setVisible(False)
        self._chesscom_link = None
        self._rank_token += 1  # invalidate any in-flight rank query from the previous puzzle

    def _show_game_details(self, puzzle: _Puzzle):
        if puzzle.game is None:
            return
        my_name = player_identity.load_last_name()
        self.game_detail_label.setText(describe_game(puzzle.game, my_name))
        self.game_detail_label.setVisible(True)
        self.view_game_btn.setVisible(self.on_view_game is not None)
        self._chesscom_link = chesscom_link(puzzle.game)
        self.chesscom_link_btn.setVisible(self._chesscom_link is not None)

    def _on_view_game(self):
        puzzle = self._current()
        if self.on_view_game is None or puzzle.game is None or puzzle.game_records is None:
            return
        self.on_view_game(puzzle.game, puzzle.game_records, puzzle.ply_index)

    def _on_open_chesscom_link(self):
        if self._chesscom_link:
            QDesktopServices.openUrl(QUrl(self._chesscom_link))

    def _review_note(self, puzzle: _Puzzle) -> str:
        key = self._puzzle_key(puzzle)
        entry = self.history.get(key)
        if entry is None:
            return "new puzzle"
        box = entry.get("box", 0)
        if puzzle_history.is_due(self.history, key):
            return f"due for review (box {box})"
        return f"reviewing early (box {box}, not due yet)"

    def _on_square_clicked(self, square: int):
        if self.solved or not self.puzzles:
            return
        rec = self._current().record
        board = self.board_widget.board

        if self.selected_square is None:
            piece = board.piece_at(square)
            if piece is None or piece.color != rec.board_before.turn:
                return
            self._select(square, board)
            return

        move = chess.Move(self.selected_square, square)
        if move not in board.legal_moves:
            promo_move = chess.Move(self.selected_square, square, promotion=chess.QUEEN)
            move = promo_move if promo_move in board.legal_moves else None

        if move is None:
            piece = board.piece_at(square)
            if piece is not None and piece.color == rec.board_before.turn:
                self._select(square, board)
            else:
                self.selected_square = None
                self.board_widget.selected_square = None
                self.board_widget.legal_targets = set()
                self.board_widget.update()
            return

        self._check_attempt(move)

    def _select(self, square: int, board: chess.Board):
        self.selected_square = square
        self.board_widget.selected_square = square
        self.board_widget.legal_targets = {m.to_square for m in board.legal_moves if m.from_square == square}
        self.board_widget.update()

    def _check_attempt(self, move: chess.Move):
        rec = self._current().record
        self.selected_square = None
        self.board_widget.selected_square = None
        self.board_widget.legal_targets = set()

        preview = rec.board_before.copy()
        san = preview.san(move)
        preview.push(move)
        self.board_widget.set_position(preview, move)
        self.sound_player.play_for_move(rec.board_before, move, preview)

        if move == rec.best_move:
            self.solved = True
            self._record_outcome(correct=self._wrong_attempts == 0)
            self.feedback_label.setStyleSheet(f"color: {_GOOD_COLOR}; font-weight: 700;")
            self.feedback_label.setText(f"✓ Correct! {san} was the engine's top choice here.")
            self._show_game_details(self._current())
        else:
            self._wrong_attempts += 1
            self.feedback_label.setStyleSheet(f"color: {_BAD_COLOR}; font-weight: 700;")
            self.feedback_label.setText(f"✗ Not quite - {san} isn't the engine's pick. Try again.")
            self._query_move_rank(rec, move)
            QTimer.singleShot(700, self._reset_board_for_retry)

    def _record_outcome(self, correct: bool):
        key = self._puzzle_key(self._current())
        puzzle_history.record_result(self.history, key, correct)
        puzzle_history.save_history(self.history)

    def _resolve_engine_path(self) -> Optional[str]:
        if not self._engine_checked:
            self._engine_path = find_engine()
            self._engine_checked = True
        return self._engine_path

    def _query_move_rank(self, rec: MoveRecord, move: chess.Move):
        engine_path = self._resolve_engine_path()
        if not engine_path:
            return
        self._rank_token += 1
        token = self._rank_token
        worker = _MoveRankWorker(rec.board_before, move, engine_path)
        self._rank_worker = worker
        worker.finished_ok.connect(lambda rank, token=token: self._on_move_rank(token, rank))
        worker.failed.connect(lambda _msg: None)
        worker.start()

    def _on_move_rank(self, token: int, rank: Optional[int]):
        if token != self._rank_token:
            return  # stale - user has since moved to a different puzzle/attempt
        if rank is None or rank <= 1:
            # rank 1 would mean this move actually matched the engine's top
            # pick, which the == check above already would've caught as
            # correct - a mismatch here just means the two queries disagreed
            # at the margin, not worth surfacing.
            suffix = f"It wasn't among the engine's top {_RANK_MULTIPV} choices here." if rank is None else None
        else:
            suffix = f"It was the engine's {_ordinal(rank)} choice here."
        if suffix:
            self.feedback_label.setText(f"{self.feedback_label.text()} {suffix}")

    def _reset_board_for_retry(self):
        if not self.solved:
            self.board_widget.set_position(self._current().record.board_before.copy(), None)

    def _reveal(self):
        if not self.puzzles:
            return
        already_solved = self.solved
        rec = self._current().record
        self.solved = True
        if not already_solved:
            self._record_outcome(correct=False)
        self.selected_square = None
        self.board_widget.selected_square = None
        self.board_widget.legal_targets = set()

        preview = rec.board_before.copy()
        try:
            best_san = preview.san(rec.best_move)
        except (ValueError, AssertionError):
            best_san = "?"
        preview.push(rec.best_move)
        self.board_widget.set_position(preview, rec.best_move)
        self.sound_player.play_for_move(rec.board_before, rec.best_move, preview)

        self.feedback_label.setStyleSheet(f"color: {_TEXT_MUTED};")
        why = rec.commentary.get("detail") or ""
        self.feedback_label.setText(f"Answer: {best_san}. {why}")
        self._show_game_details(self._current())

    def _next_puzzle(self):
        if not self.puzzles:
            return
        if self.index < len(self.puzzles) - 1:
            self.index += 1
        else:
            self.index = 0
        self._load_puzzle()
