"""
weakness_report.py

Cross-game weakness report: aggregates classification, tactical-motif, and
game-phase stats across a batch of your own games to find recurring leaks -
the "what to work on next" coaching breakdown chess.com doesn't surface,
since its own stats are always scoped to a single game.

Each selected file can contain any number of games. Games that already
carry [%eval] annotations (chess.com exports, or files already analyzed in
this app) are used as-is; games with no annotations are offered up for a
batch Stockfish pass (one shared engine process for the whole batch) before
being folded into the same report.
"""

from __future__ import annotations

import threading
from collections import Counter
from dataclasses import dataclass
from typing import Callable, Optional

import chess.engine
import chess.pgn
from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

import player_identity
from chesscom_import_dialog import ChessComImportDialog
from engine_analysis import AUTO_ANALYZE_THRESHOLD, DEFAULT_LIMIT, FAST_LIMIT, analyze_games, find_engine
from pgn_loader import MoveRecord, _build_records, game_has_eval_annotations, my_records, read_games
from scoresheet import game_accuracy, win_percent_for_mover
from summary_dialog import _CLASS_COLORS, _CLASS_ORDER

_TEXT_PRIMARY = "#f5f5f6"
_TEXT_MUTED = "#b8b8bd"
_SOLID_PANEL_BG = "rgb(52, 52, 55)"
_BORDER_COLOR = "#3d3d42"

_MOTIF_LABELS = {
    "captured_free_piece": "Hanging a piece for free",
    "favorable_capture": "Losing material to a capture",
    "capture": "Losing material to a capture",
    "fork": "Falling for a fork",
    "pin": "Walking into a pin",
    "discovered_check": "Allowing a discovered check",
    "direct_check": "Allowing a strong check",
    "promotion": "Allowing a pawn promotion",
    "skewer": "Falling for a skewer",
    "discovered_attack": "Allowing a discovered attack",
    # back_rank_weakness intentionally absent: it never comes from
    # describe_refutation (see scoresheet.py's _REFUTATION_PHRASES comment),
    # so it never reaches this motif-ranking aggregation - it only ever
    # shows up in a move's own commentary text.
}

_WEAK_CLASSES = {"Mistake", "Blunder", "Miss"}

_BUTTON_STYLE = f"""
    QPushButton {{ padding: 6px 14px; border-radius: 5px; border: 1px solid {_BORDER_COLOR};
                   background: rgba(255,255,255,0.06); color: {_TEXT_PRIMARY}; }}
    QPushButton:hover {{ background: rgba(58,168,255,0.18); border-color: #3aa8ff; }}
    QPushButton:pressed {{ background: rgba(58,168,255,0.30); }}
"""

# (file_path, game_index_within_file, game, records)
_GameEntry = tuple[str, int, "chess.pgn.Game", list[MoveRecord]]


class _MyMove:
    __slots__ = ("record", "file_path", "game_index", "ply_index", "opponent")

    def __init__(self, record: MoveRecord, file_path: str, game_index: int, ply_index: int, opponent: str):
        self.record = record
        self.file_path = file_path
        self.game_index = game_index
        self.ply_index = ply_index
        self.opponent = opponent


def _collect_my_moves(games: list[_GameEntry], my_name: str) -> list[_MyMove]:
    moves: list[_MyMove] = []
    for path, game_index, game, records in games:
        white = game.headers.get("White", "?")
        black = game.headers.get("Black", "?")
        opponent = black if white == my_name else white
        for i, rec in my_records(records, white, black, my_name):
            moves.append(_MyMove(rec, path, game_index, i, opponent))
    return moves


# Which of why_piece_info's keys is worth naming for a given motif tag - the
# piece you lost for capture-flavored motifs, the piece that got pinned for
# a pin. Motifs not listed here (fork, checks, promotion, ...) don't have a
# single clean "your X" piece to name, so they're left without one.
_MOTIF_PIECE_KEY = {
    "captured_free_piece": "captured",
    "favorable_capture": "captured",
    "capture": "captured",
    "pin": "pinned",
    "skewer": "skewered",
    "discovered_attack": "discovered",
}


@dataclass
class _MotifStats:
    tag: Optional[str]  # None = the untagged "Other" bucket
    moves: list[_MyMove]
    total_loss: float
    avg_loss: float
    blunders: int
    mistakes: int
    worst: _MyMove
    phase_counts: Counter
    color_counts: Counter
    piece_counts: Counter
    piece_kind: Optional[str]


def _summarize_motif(tag: Optional[str], moves: list[_MyMove]) -> _MotifStats:
    losses = [m.record.commentary["win_prob_loss"] for m in moves]
    blunders = sum(1 for m in moves if m.record.commentary["classification"] == "Blunder")
    worst = max(moves, key=lambda m: m.record.commentary["win_prob_loss"])
    phase_counts = Counter(m.record.phase or "Unknown" for m in moves)
    color_counts = Counter("White" if m.record.color_white else "Black" for m in moves)

    piece_kind = _MOTIF_PIECE_KEY.get(tag)
    piece_counts: Counter = Counter()
    if piece_kind:
        for m in moves:
            piece = m.record.commentary.get("why_piece_info", {}).get(piece_kind)
            if piece:
                piece_counts[piece] += 1

    return _MotifStats(
        tag=tag,
        moves=moves,
        total_loss=sum(losses),
        avg_loss=sum(losses) / len(losses),
        blunders=blunders,
        mistakes=len(moves) - blunders,
        worst=worst,
        phase_counts=phase_counts,
        color_counts=color_counts,
        piece_counts=piece_counts,
        piece_kind=piece_kind,
    )


# Practical-outcome tiers for the endgame conversion tracker - not the same
# question as classify_move's flat centipawn-loss thresholds (which drive
# Best/.../Blunder labels): this asks "did the *result you'd expect to get*
# just get worse" (won -> drawish, drawish -> lost), which can trip on a
# move that's individually only an Inaccuracy or Good by eval-swing alone.
_ENDGAME_WIN_THRESHOLD = 85.0
_ENDGAME_LOSS_THRESHOLD = 15.0
_TIER_RANK = {"winning": 2, "unclear": 1, "losing": 0}


def _outcome_tier(win_pct: float) -> str:
    if win_pct >= _ENDGAME_WIN_THRESHOLD:
        return "winning"
    if win_pct <= _ENDGAME_LOSS_THRESHOLD:
        return "losing"
    return "unclear"


def _find_endgame_swings(my_moves: list[_MyMove]) -> list[_MyMove]:
    """One entry per game: the single worst Endgame-phase move where the
    mover's practical outcome tier got worse - i.e. a won or drawn endgame
    slipping away, not just any eval dip."""
    worst_by_game: dict[tuple[str, int], _MyMove] = {}
    worst_drop: dict[tuple[str, int], int] = {}
    for m in my_moves:
        rec = m.record
        if rec.phase != "Endgame":
            continue
        before_tier = _outcome_tier(win_percent_for_mover(rec.eval_before, rec.color_white))
        after_tier = _outcome_tier(win_percent_for_mover(rec.eval_after, rec.color_white))
        drop = _TIER_RANK[before_tier] - _TIER_RANK[after_tier]
        if drop <= 0:
            continue
        key = (m.file_path, m.game_index)
        if drop > worst_drop.get(key, 0):
            worst_drop[key] = drop
            worst_by_game[key] = m
    return sorted(worst_by_game.values(), key=lambda m: worst_drop[(m.file_path, m.game_index)], reverse=True)


class _BatchAnalysisWorker(QObject):
    """Runs engine_analysis.analyze_games on a plain thread (not QThread -
    python-chess's engine transport needs asyncio's Windows ProactorEventLoop
    for subprocess pipes, which breaks inside a QThread's OS thread) so a
    batch of raw games can be Stockfish-analyzed without freezing the UI."""

    progress = Signal(int, int, int, int)  # games_done, games_total, moves_done, moves_total
    finished_ok = Signal(list)  # list[list[MoveRecord]]
    failed = Signal(str)

    def __init__(self, games: list["chess.pgn.Game"], engine_path: str, limit: "chess.engine.Limit"):
        super().__init__()
        self.games = games
        self.engine_path = engine_path
        self.limit = limit
        self._stop_requested = False

    def request_stop(self):
        self._stop_requested = True

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        try:
            results = analyze_games(
                self.games,
                self.engine_path,
                self.limit,
                progress_callback=lambda gd, gt, md, mt: self.progress.emit(gd, gt, md, mt),
                should_stop=lambda: self._stop_requested,
            )
            self.finished_ok.emit(results)
        except Exception as exc:  # noqa: BLE001 - surface any engine/parsing error to the UI
            self.failed.emit(str(exc))


class WeaknessReportDialog(QDialog):
    """Pick a batch of your own games - annotated or raw, one game per file
    or many - and get a ranked "what to work on" breakdown, with a jump list
    of the costliest moves across all of them."""

    def __init__(self, on_jump: Callable[[str, int, int], None], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Weakness Report")
        self.resize(660, 700)
        self.setStyleSheet(f"QDialog {{ background-color: {_SOLID_PANEL_BG}; }}")
        self._on_jump = on_jump
        self._leak_jump_targets: list[tuple[str, int, int]] = []

        self.progress_dialog: Optional[QProgressDialog] = None
        self.analysis_worker: Optional[_BatchAnalysisWorker] = None
        self._pending_annotated: list[_GameEntry] = []
        self._pending_raw: list[tuple[str, int, "chess.pgn.Game"]] = []
        self._pending_known_name: Optional[str] = None
        self._total_selected_games = 0
        self.chesscom_dialog: Optional[ChessComImportDialog] = None

        pick_btn = QPushButton("Choose Games...")
        pick_btn.setStyleSheet(_BUTTON_STYLE)
        pick_btn.clicked.connect(self._pick_games)

        import_btn = QPushButton("Import from chess.com...")
        import_btn.setStyleSheet(_BUTTON_STYLE)
        import_btn.clicked.connect(self._open_chesscom_import)

        pick_row = QHBoxLayout()
        pick_row.addWidget(pick_btn)
        pick_row.addWidget(import_btn)

        self.intro_label = QLabel(
            "Pick a batch of your own games - chess.com exports with analysis, games you've "
            "already analyzed with Stockfish in this app, or plain PGNs (raw games can be "
            "analyzed on the spot). A file can hold one game or many. Bongcloud finds patterns "
            "across all of them at once."
        )
        self.intro_label.setWordWrap(True)
        self.intro_label.setStyleSheet(f"color: {_TEXT_MUTED};")

        self.content_container = QWidget()
        self.content_container.setStyleSheet("background: transparent;")
        self.content_layout = QVBoxLayout()
        self.content_layout.setSpacing(8)
        self.content_container.setLayout(self.content_layout)

        scroll = QScrollArea()
        scroll.setWidget(self.content_container)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        scroll.setStyleSheet("background: transparent;")
        scroll.viewport().setStyleSheet("background: transparent;")

        layout = QVBoxLayout()
        layout.addLayout(pick_row)
        layout.addWidget(self.intro_label)
        layout.addWidget(scroll, stretch=1)
        self.setLayout(layout)

    # -- picking / analyzing games ------------------------------------------

    def _pick_games(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "Choose games", "", "PGN files (*.pgn)")
        if not paths:
            return
        self._process_paths(paths)

    def _open_chesscom_import(self):
        self.chesscom_dialog = ChessComImportDialog(self._on_chesscom_imported, parent=self)
        self.chesscom_dialog.show()

    def _on_chesscom_imported(self, path: str, username: str):
        self._process_paths([path], known_name=username)

    def _process_paths(self, paths: list[str], known_name: Optional[str] = None):
        self._pending_known_name = known_name
        entries: list[tuple[str, int, "chess.pgn.Game"]] = []
        for p in paths:
            try:
                file_games = read_games(p)
            except (OSError, ValueError):
                continue
            for gi, game in enumerate(file_games):
                entries.append((p, gi, game))

        if not entries:
            self._clear_content()
            no_games = QLabel(f"No games found in the {len(paths)} selected file(s).")
            no_games.setStyleSheet(f"color: {_TEXT_MUTED};")
            self.content_layout.addWidget(no_games)
            return

        self._total_selected_games = len(entries)
        annotated: list[_GameEntry] = [
            (p, gi, g, _build_records(g)) for p, gi, g in entries if game_has_eval_annotations(g)
        ]
        raw = [(p, gi, g) for p, gi, g in entries if not game_has_eval_annotations(g)]
        self._pending_annotated = annotated

        if not raw:
            self._finish_report(annotated, skipped=0)
            return

        fast = False
        if len(raw) > AUTO_ANALYZE_THRESHOLD:
            plural = "game" if len(raw) == 1 else "games"
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Question)
            box.setWindowTitle("Unannotated games found")
            box.setText(
                f"{len(raw)} of the {len(entries)} selected {plural} have no Stockfish annotations. "
                "Analyze them now with a local Stockfish engine? Large batches can take a while."
            )
            box.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
            fast_checkbox = QCheckBox("Faster analysis (lower accuracy)")
            fast_checkbox.setChecked(True)
            box.setCheckBox(fast_checkbox)
            answer = box.exec()
            if answer != QMessageBox.Yes:
                self._finish_report(annotated, skipped=len(raw))
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
                self._finish_report(annotated, skipped=len(raw))
                return

        self._pending_raw = raw
        self._run_batch_analysis(engine_path, raw, fast=fast)

    def _run_batch_analysis(
        self, engine_path: str, raw: list[tuple[str, int, "chess.pgn.Game"]], fast: bool = False
    ):
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
        games_only = [g for _, _, g in raw]
        self.analysis_worker = _BatchAnalysisWorker(games_only, engine_path, limit)
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
        analyzed: list[_GameEntry] = [
            (p, gi, g, records) for (p, gi, g), records in zip(self._pending_raw, results)
        ]
        skipped = len(self._pending_raw) - len(analyzed)
        self._finish_report(self._pending_annotated + analyzed, skipped=skipped)

    def _on_batch_failed(self, message: str):
        if self.progress_dialog:
            self.progress_dialog.close()
            self.progress_dialog = None
        QMessageBox.warning(self, "Analysis failed", message)
        self._finish_report(self._pending_annotated, skipped=len(self._pending_raw))

    # -- report building -----------------------------------------------------

    def _finish_report(self, games: list[_GameEntry], skipped: int):
        if not games:
            self._clear_content()
            no_games = QLabel(
                f"None of the {self._total_selected_games} selected game(s) have Stockfish "
                "annotations to work from."
            )
            no_games.setStyleSheet(f"color: {_TEXT_MUTED};")
            self.content_layout.addWidget(no_games)
            return

        if self._pending_known_name:
            # Came from a chess.com import - we already know exactly who
            # "you" are (it's who we fetched games for), no guessing needed.
            my_name = self._pending_known_name
        else:
            name_counter: Counter[str] = Counter()
            for _, _, game, _ in games:
                name_counter[game.headers.get("White", "?")] += 1
                name_counter[game.headers.get("Black", "?")] += 1
            candidates = [n for n, c in name_counter.items() if c == len(games)]
            remembered = player_identity.load_last_name()

            if len(candidates) == 1:
                my_name = candidates[0]
            elif remembered and remembered in name_counter:
                # A single game (or a batch against the same opponent every
                # time) can't be disambiguated by "appears in every game"
                # alone - fall back to whoever we last confirmed was you.
                my_name = remembered
            else:
                names_sorted = [n for n, _ in name_counter.most_common()]
                default_index = names_sorted.index(remembered) if remembered in names_sorted else 0
                my_name, ok = QInputDialog.getItem(
                    self, "Which player is you?", "Player name across these games:",
                    names_sorted, default_index, False,
                )
                if not ok or not my_name:
                    return

        player_identity.save_last_name(my_name)
        my_moves = _collect_my_moves(games, my_name)
        self._render_report(my_name, my_moves, games, skipped, self._total_selected_games)

    def _clear_content(self):
        while self.content_layout.count():
            item = self.content_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def _render_report(
        self, my_name: str, my_moves: list[_MyMove], games: list[_GameEntry], skipped: int, total_selected: int
    ):
        self._clear_content()

        if not my_moves:
            empty = QLabel(f"No moves found for \"{my_name}\" in the selected games.")
            empty.setStyleSheet(f"color: {_TEXT_MUTED};")
            self.content_layout.addWidget(empty)
            return

        header = QLabel(
            f"<b>{my_name}</b> — {len(my_moves)} moves across {total_selected - skipped} games"
            + (f" ({skipped} skipped, no annotations)" if skipped else "")
        )
        header.setTextFormat(Qt.RichText)
        header.setWordWrap(True)
        header.setStyleSheet(f"color: {_TEXT_PRIMARY};")
        self.content_layout.addWidget(header)

        # Accuracy is windowed/weighted within a single game's flow (see
        # scoresheet.game_accuracy), which doesn't carry over to a batch
        # spanning several games - so "combined" here means the average of
        # each game's own accuracy, not one pooled calculation across moves
        # from different games.
        per_game_accuracies = []
        for _, _, game, records in games:
            white = game.headers.get("White", "?")
            black = game.headers.get("Black", "?")
            if my_name == white and my_name != black:
                per_game_accuracies.append(game_accuracy(records, True))
            elif my_name == black and my_name != white:
                per_game_accuracies.append(game_accuracy(records, False))
        accuracy = sum(per_game_accuracies) / len(per_game_accuracies) if per_game_accuracies else 0.0
        accuracy_label = QLabel(f"Combined accuracy: <b>{accuracy:.1f}%</b>")
        accuracy_label.setTextFormat(Qt.RichText)
        accuracy_label.setStyleSheet(f"color: {_TEXT_PRIMARY};")
        self.content_layout.addWidget(accuracy_label)

        self.content_layout.addWidget(self._section_label("Move quality breakdown:"))
        self.content_layout.addWidget(self._breakdown_widget(my_moves))

        weak_moves = [m for m in my_moves if m.record.commentary["classification"] in _WEAK_CLASSES]

        self.content_layout.addWidget(self._section_label("What to work on:"))
        self.content_layout.addWidget(self._leaks_widget(weak_moves))

        self.content_layout.addWidget(self._section_label("Mistakes/Blunders by game phase:"))
        self.content_layout.addWidget(self._phase_widget(weak_moves))

        self.content_layout.addWidget(self._section_label("Endgame conversions - games where a won/drawn endgame slipped (click to jump):"))
        self.content_layout.addWidget(self._endgame_widget(_find_endgame_swings(my_moves)))

        self.content_layout.addWidget(self._section_label("Costliest moves across all games (click to jump):"))
        self.content_layout.addWidget(self._costliest_widget(my_moves))

    @staticmethod
    def _section_label(text: str) -> QLabel:
        label = QLabel(text)
        label.setStyleSheet(f"color: {_TEXT_MUTED}; padding-top: 6px;")
        return label

    @staticmethod
    def _breakdown_widget(my_moves: list[_MyMove]) -> QLabel:
        counts = Counter(m.record.commentary["classification"] for m in my_moves)
        rows = []
        for cls in _CLASS_ORDER:
            n = counts.get(cls, 0)
            if n == 0:
                continue
            pct = 100.0 * n / len(my_moves)
            color = _CLASS_COLORS[cls]
            rows.append(f"<tr><td style='color:{color}'>{cls}</td><td>{n}</td><td>{pct:.0f}%</td></tr>")
        label = QLabel("<table cellpadding='4'>" + "".join(rows) + "</table>")
        label.setTextFormat(Qt.RichText)
        label.setStyleSheet(f"color: {_TEXT_PRIMARY};")
        return label

    def _leaks_widget(self, weak_moves: list[_MyMove]) -> QLabel:
        if not weak_moves:
            label = QLabel("No Mistakes or Blunders in this batch - nothing to flag.")
            label.setStyleSheet(f"color: {_TEXT_MUTED};")
            return label

        by_tag: dict[Optional[str], list[_MyMove]] = {}
        for m in weak_moves:
            by_tag.setdefault(m.record.commentary.get("why_tag"), []).append(m)

        ranked = sorted(
            (_summarize_motif(tag, moves) for tag, moves in by_tag.items()),
            key=lambda s: s.total_loss,
            reverse=True,
        )

        self._leak_jump_targets = []
        blocks = []
        for rank, s in enumerate(ranked, start=1):
            label_text = (
                _MOTIF_LABELS.get(s.tag, s.tag.replace("_", " ").capitalize())
                if s.tag else "Other slips (no single clean tactical cause)"
            )
            split_parts = []
            if s.blunders:
                split_parts.append(f"{s.blunders} Blunder{'s' if s.blunders != 1 else ''}")
            if s.mistakes:
                split_parts.append(f"{s.mistakes} Mistake{'s' if s.mistakes != 1 else ''}")
            split = f" ({', '.join(split_parts)})" if split_parts else ""

            detail_bits = [f"avg ~{s.avg_loss:.0f}% win probability lost"]
            if s.phase_counts:
                detail_bits.append(", ".join(f"{phase}: {n}" for phase, n in s.phase_counts.most_common()))
            if s.color_counts:
                detail_bits.append(", ".join(f"{color}: {n}" for color, n in s.color_counts.most_common()))
            if s.piece_counts:
                total = len(s.moves)
                top_piece, top_n = s.piece_counts.most_common(1)[0]
                verb = "pinned" if s.piece_kind == "pinned" else "lost"
                if top_n == total:
                    detail_bits.append(f"always your {top_piece}")
                elif top_n > total / 2:
                    detail_bits.append(f"mostly your {top_piece}")
                else:
                    detail_bits.append(
                        "pieces " + verb + ": " + ", ".join(f"{p} ({n})" for p, n in s.piece_counts.most_common(3))
                    )

            jump_idx = len(self._leak_jump_targets)
            self._leak_jump_targets.append((s.worst.file_path, s.worst.game_index, s.worst.ply_index))
            worst_rec = s.worst.record
            move_no = f"{worst_rec.move_number}." if worst_rec.color_white else f"{worst_rec.move_number}..."
            worst_loss = worst_rec.commentary["win_prob_loss"]
            worst_link = (
                f'<a href="jump:{jump_idx}" style="color:#3aa8ff; text-decoration:none;">'
                f"{move_no} {worst_rec.san} vs {s.worst.opponent} (-{worst_loss:.0f}%)</a>"
            )

            blocks.append(
                f"<b>{rank}. {label_text}</b> — {len(s.moves)}x{split}<br>"
                f"<span style='color:{_TEXT_MUTED};'>{' · '.join(detail_bits)}<br>"
                f"Worst: {worst_link}</span>"
            )

        label = QLabel("<br><br>".join(blocks))
        label.setTextFormat(Qt.RichText)
        label.setWordWrap(True)
        label.setStyleSheet(f"color: {_TEXT_PRIMARY};")
        label.setOpenExternalLinks(False)
        label.linkActivated.connect(self._on_leak_jump_link)
        return label

    def _on_leak_jump_link(self, href: str):
        if not href.startswith("jump:"):
            return
        idx = int(href[len("jump:"):])
        if 0 <= idx < len(self._leak_jump_targets):
            self._on_jump(*self._leak_jump_targets[idx])

    @staticmethod
    def _phase_widget(weak_moves: list[_MyMove]) -> QLabel:
        counts = Counter((m.record.phase or "Unknown") for m in weak_moves)
        if not counts or set(counts) == {"Unknown"}:
            label = QLabel("No phase data in these games.")
            label.setStyleSheet(f"color: {_TEXT_MUTED};")
            return label
        rows = []
        for phase in ("Opening", "Middlegame", "Endgame", "Unknown"):
            n = counts.get(phase, 0)
            if n:
                rows.append(f"{phase}: {n}")
        label = QLabel("  ·  ".join(rows))
        label.setStyleSheet(f"color: {_TEXT_PRIMARY};")
        return label

    def _endgame_widget(self, swings: list[_MyMove]) -> QListWidget:
        list_widget = QListWidget()
        list_widget.setStyleSheet(
            """
            QListWidget { border: 1px solid #3d3d42; border-radius: 6px; outline: none;
                          background: rgba(255,255,255,0.03); color: #f5f5f6; }
            QListWidget::item { padding: 4px 8px; }
            QListWidget::item:alternate { background: rgba(255,255,255,0.05); }
            QListWidget::item:selected { background: rgba(58,168,255,0.35); }
            """
        )
        list_widget.setAlternatingRowColors(True)
        list_widget.setMinimumHeight(120)

        for m in swings:
            rec = m.record
            before_tier = _outcome_tier(win_percent_for_mover(rec.eval_before, rec.color_white))
            after_tier = _outcome_tier(win_percent_for_mover(rec.eval_after, rec.color_white))
            move_no = f"{rec.move_number}." if rec.color_white else f"{rec.move_number}..."
            item = QListWidgetItem(
                f"{move_no} {rec.san}  [{before_tier} → {after_tier}]  vs {m.opponent}"
            )
            item.setForeground(QColor(_CLASS_COLORS.get(rec.commentary["classification"], _TEXT_PRIMARY)))
            item.setData(Qt.UserRole, (m.file_path, m.game_index, m.ply_index))
            list_widget.addItem(item)

        if not swings:
            placeholder = QListWidgetItem("No endgames slipped away in this batch - good conversions.")
            placeholder.setForeground(QColor(_TEXT_MUTED))
            list_widget.addItem(placeholder)

        list_widget.itemClicked.connect(self._on_item_clicked)
        return list_widget

    def _costliest_widget(self, my_moves: list[_MyMove]) -> QListWidget:
        ranked = sorted(my_moves, key=lambda m: m.record.commentary["win_prob_loss"], reverse=True)
        ranked = [m for m in ranked if m.record.commentary["win_prob_loss"] > 0][:15]

        list_widget = QListWidget()
        list_widget.setStyleSheet(
            """
            QListWidget { border: 1px solid #3d3d42; border-radius: 6px; outline: none;
                          background: rgba(255,255,255,0.03); color: #f5f5f6; }
            QListWidget::item { padding: 4px 8px; }
            QListWidget::item:alternate { background: rgba(255,255,255,0.05); }
            QListWidget::item:selected { background: rgba(58,168,255,0.35); }
            """
        )
        list_widget.setAlternatingRowColors(True)
        list_widget.setMinimumHeight(260)

        for m in ranked:
            rec = m.record
            move_no = f"{rec.move_number}." if rec.color_white else f"{rec.move_number}..."
            loss = rec.commentary["win_prob_loss"]
            cls = rec.commentary["classification"]
            item = QListWidgetItem(
                f"{move_no} {rec.san}  [{cls}]  -{loss:.0f}% win prob  vs {m.opponent}"
            )
            item.setForeground(QColor(_CLASS_COLORS.get(cls, _TEXT_PRIMARY)))
            item.setData(Qt.UserRole, (m.file_path, m.game_index, m.ply_index))
            list_widget.addItem(item)

        if not ranked:
            placeholder = QListWidgetItem("No costly moments in this batch - nicely played.")
            placeholder.setForeground(QColor(_TEXT_MUTED))
            list_widget.addItem(placeholder)

        list_widget.itemClicked.connect(self._on_item_clicked)
        return list_widget

    def _on_item_clicked(self, item: QListWidgetItem):
        data = item.data(Qt.UserRole)
        if data is not None:
            file_path, game_index, ply_index = data
            self._on_jump(file_path, game_index, ply_index)
