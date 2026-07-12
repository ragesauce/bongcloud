"""
chesscom_import_dialog.py

Qt front end for chesscom_import.py: pick a username and filters (time
control, rated-only, color played, how far back to look), fetch matching
games from chess.com's public API in the background, let the user pick
which ones to keep, then write them to one combined PGN file on disk and
hand that path back to whichever dialog opened this one - review_app.py,
weakness_report.py, and puzzle_trainer.py all wire it up the same way.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Optional, Union

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QVBoxLayout,
)

from chesscom_import import (
    LOOKBACK_DELTA_OPTIONS,
    LOOKBACK_OPTIONS,
    MAX_IMPORT_GAMES,
    ChessComError,
    archives_for_delta,
    archives_in_lookback,
    combined_pgn,
    cutoff_epoch,
    fetch_archives,
    fetch_month_games,
    matches_filters,
)

_TEXT_PRIMARY = "#f5f5f6"
_TEXT_MUTED = "#b8b8bd"
_SOLID_PANEL_BG = "rgb(52, 52, 55)"
_BORDER_COLOR = "#3d3d42"

_BUTTON_STYLE = f"""
    QPushButton {{ padding: 6px 14px; border-radius: 5px; border: 1px solid {_BORDER_COLOR};
                   background: rgba(255,255,255,0.06); color: {_TEXT_PRIMARY}; }}
    QPushButton:hover {{ background: rgba(58,168,255,0.18); border-color: #3aa8ff; }}
    QPushButton:pressed {{ background: rgba(58,168,255,0.30); }}
"""

_TIME_CLASSES = [("Bullet", "bullet"), ("Blitz", "blitz"), ("Rapid", "rapid"), ("Daily", "daily")]
_COLOR_OPTIONS = [("Either color", None), ("White only", "white"), ("Black only", "black")]


class _ChessComFetchWorker(QObject):
    """Runs the archive-walk on a plain thread (not QThread - see
    _BatchAnalysisWorker in weakness_report.py for why) so the UI stays
    responsive while chess.com's API is queried month by month."""

    progress = Signal(int, int)  # months_done, months_total
    finished_ok = Signal(list)  # list[dict] of matched games
    failed = Signal(str)

    def __init__(
        self,
        username: str,
        lookback: Union[int, timedelta, None],
        filter_kwargs: dict,
        max_games: int = MAX_IMPORT_GAMES,
    ):
        super().__init__()
        self.username = username
        self.lookback = lookback
        self.filter_kwargs = filter_kwargs
        self.max_games = max_games
        self._stop_requested = False

    def request_stop(self):
        self._stop_requested = True

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        try:
            archives = fetch_archives(self.username)
            min_end_time = None
            if isinstance(self.lookback, timedelta):
                window = archives_for_delta(archives)
                min_end_time = cutoff_epoch(self.lookback)
            else:
                window = archives_in_lookback(archives, self.lookback)
            total = len(window)
            matched: list[dict] = []
            for i, url in enumerate(window):
                if self._stop_requested or len(matched) >= self.max_games:
                    break
                for game in fetch_month_games(url):
                    if len(matched) >= self.max_games:
                        break
                    if matches_filters(game, min_end_time=min_end_time, **self.filter_kwargs):
                        matched.append(game)
                self.progress.emit(i + 1, total)
            # Games arrive oldest-first within each month and newest-month-first
            # across months, which isn't chronological overall - sort once,
            # most recent first, before handing them back to the UI.
            matched.sort(key=lambda g: g.get("end_time") or 0, reverse=True)
            self.finished_ok.emit(matched)
        except ChessComError as exc:
            self.failed.emit(str(exc))


class ChessComImportDialog(QDialog):
    """Fetch + filter + pick chess.com games, then hand a saved PGN path and
    the account's canonical username back to on_imported(path, username) -
    the same callback-based pattern WeaknessReportDialog(on_jump, ...)
    already uses. The username is known outright (it's who we fetched games
    for), so callers can skip any "which player is you" guesswork."""

    def __init__(self, on_imported: Callable[[str, str], None], parent=None, single_select: bool = False):
        super().__init__(parent)
        self.setWindowTitle("Import from chess.com")
        self.resize(600, 660)
        self.setStyleSheet(f"QDialog {{ background-color: {_SOLID_PANEL_BG}; }}")
        self._on_imported = on_imported
        self.single_select = single_select
        self._enforcing_single_select = False

        self.progress_dialog: Optional[QProgressDialog] = None
        self.fetch_worker: Optional[_ChessComFetchWorker] = None
        self.matched_games: list[dict] = []
        self._last_clicked_row: Optional[int] = None

        self.username_edit = QLineEdit()
        self.username_edit.setPlaceholderText("chess.com username")

        username_row = QHBoxLayout()
        username_row.addWidget(QLabel("Username:"))
        username_row.addWidget(self.username_edit, stretch=1)

        self.time_class_checks: dict[str, QCheckBox] = {}
        tc_row = QHBoxLayout()
        tc_row.addWidget(QLabel("Time control:"))
        for label, key in _TIME_CLASSES:
            cb = QCheckBox(label)
            cb.setChecked(True)
            self.time_class_checks[key] = cb
            tc_row.addWidget(cb)
        tc_row.addStretch(1)

        self.rated_check = QCheckBox("Rated games only")

        self.color_combo = QComboBox()
        for label, _ in _COLOR_OPTIONS:
            self.color_combo.addItem(label)

        self.lookback_combo = QComboBox()
        self.lookback_combo.addItems(list(LOOKBACK_DELTA_OPTIONS.keys()) + list(LOOKBACK_OPTIONS.keys()))
        self.lookback_combo.setCurrentText("Last 24 hours")

        filters_row = QHBoxLayout()
        filters_row.addWidget(self.rated_check)
        filters_row.addStretch(1)
        filters_row.addWidget(QLabel("Color:"))
        filters_row.addWidget(self.color_combo)
        filters_row.addWidget(QLabel("Lookback:"))
        filters_row.addWidget(self.lookback_combo)

        fetch_btn = QPushButton("Fetch Games")
        fetch_btn.setStyleSheet(_BUTTON_STYLE)
        fetch_btn.clicked.connect(self._fetch)

        self.count_label = QLabel("")
        self.count_label.setWordWrap(True)
        self.count_label.setStyleSheet(f"color: {_TEXT_MUTED};")

        self.results_list = QListWidget()
        self.results_list.setStyleSheet(
            """
            QListWidget { border: 1px solid #3d3d42; border-radius: 6px; outline: none;
                          background: rgba(255,255,255,0.03); color: #f5f5f6; }
            QListWidget::item { padding: 4px 8px; }
            QListWidget::item:alternate { background: rgba(255,255,255,0.05); }
            """
        )
        self.results_list.setAlternatingRowColors(True)
        self.results_list.itemClicked.connect(self._on_result_item_clicked)
        if self.single_select:
            self.results_list.itemChanged.connect(self._on_item_changed_single_select)

        select_none_btn = QPushButton("Select None")
        select_none_btn.setStyleSheet(_BUTTON_STYLE)
        select_none_btn.clicked.connect(lambda: self._set_all_checked(False))

        select_row = QHBoxLayout()
        if not self.single_select:
            select_all_btn = QPushButton("Select All")
            select_all_btn.setStyleSheet(_BUTTON_STYLE)
            select_all_btn.clicked.connect(lambda: self._set_all_checked(True))
            select_row.addWidget(select_all_btn)
        select_row.addWidget(select_none_btn)
        select_row.addStretch(1)

        self.import_btn = QPushButton("Import Selected")
        self.import_btn.setStyleSheet(_BUTTON_STYLE)
        self.import_btn.setEnabled(False)
        self.import_btn.clicked.connect(self._import_selected)

        layout = QVBoxLayout()
        layout.addLayout(username_row)
        layout.addLayout(tc_row)
        layout.addLayout(filters_row)
        layout.addWidget(fetch_btn)
        layout.addWidget(self.count_label)
        layout.addLayout(select_row)
        layout.addWidget(self.results_list, stretch=1)
        layout.addWidget(self.import_btn)
        self.setLayout(layout)

    # -- fetching -------------------------------------------------------

    def _fetch(self):
        username = self.username_edit.text().strip()
        if not username:
            QMessageBox.warning(self, "Import from chess.com", "Enter a chess.com username first.")
            return

        time_classes = {key for key, cb in self.time_class_checks.items() if cb.isChecked()}
        if not time_classes:
            QMessageBox.warning(self, "Import from chess.com", "Select at least one time control.")
            return

        color = _COLOR_OPTIONS[self.color_combo.currentIndex()][1]
        lookback_label = self.lookback_combo.currentText()
        lookback = LOOKBACK_DELTA_OPTIONS.get(lookback_label, LOOKBACK_OPTIONS.get(lookback_label))
        filter_kwargs = dict(username=username, time_classes=time_classes,
                              rated_only=self.rated_check.isChecked(), color=color)

        self.results_list.clear()
        self.matched_games = []
        self.count_label.setText("")
        self.import_btn.setEnabled(False)

        self.progress_dialog = QProgressDialog("Resolving chess.com account...", "Cancel", 0, 0, self)
        self.progress_dialog.setWindowTitle("Fetching")
        self.progress_dialog.setWindowModality(Qt.WindowModal)
        self.progress_dialog.setMinimumDuration(0)
        self.progress_dialog.setStyleSheet(
            f"QProgressDialog {{ background-color: {_SOLID_PANEL_BG}; }}"
            f"QLabel {{ color: {_TEXT_PRIMARY}; background: transparent; }}"
            + _BUTTON_STYLE
        )
        self.progress_dialog.show()

        self.fetch_worker = _ChessComFetchWorker(username, lookback, filter_kwargs)
        self.fetch_worker.progress.connect(self._on_progress)
        self.fetch_worker.finished_ok.connect(self._on_fetch_finished)
        self.fetch_worker.failed.connect(self._on_fetch_failed)
        self.progress_dialog.canceled.connect(self.fetch_worker.request_stop)
        self.fetch_worker.start()

    def _on_progress(self, months_done: int, months_total: int):
        dialog = self.progress_dialog
        if dialog is None:
            return
        dialog.setMaximum(months_total)
        # setValue() can pump the event loop and dispatch an already-queued
        # finished_ok signal, which closes and nulls self.progress_dialog
        # before this call returns - re-check before touching it again.
        dialog.setValue(months_done)
        if self.progress_dialog is not None:
            dialog.setLabelText(f"Scanning month {months_done}/{months_total}...")

    def _on_fetch_finished(self, games: list[dict]):
        if self.progress_dialog:
            self.progress_dialog.close()
            self.progress_dialog = None

        self.matched_games = games
        self._last_clicked_row = None
        for i, game in enumerate(games):
            item = QListWidgetItem(self._describe_game(game))
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            # Single-select mode defaults to just the most recent match
            # (the list is already sorted newest-first) rather than
            # everything, since "check every box" makes no sense when only
            # one game can end up selected.
            checked = (i == 0) if self.single_select else True
            item.setCheckState(Qt.Checked if checked else Qt.Unchecked)
            self.results_list.addItem(item)

        note = ""
        if len(games) >= MAX_IMPORT_GAMES:
            note = (f" — showing the most recent {MAX_IMPORT_GAMES} matches; "
                    "narrow your filters or lookback window to see a different set")
        self.count_label.setText(f"{len(games)} game(s) matched your filters{note}")
        self.import_btn.setEnabled(bool(games))

    def _on_fetch_failed(self, message: str):
        if self.progress_dialog:
            self.progress_dialog.close()
            self.progress_dialog = None
        QMessageBox.warning(self, "Import from chess.com", message)

    @staticmethod
    def _describe_game(game: dict) -> str:
        end_time = game.get("end_time")
        when = datetime.fromtimestamp(end_time).strftime("%Y-%m-%d") if end_time else "?"
        white = game.get("white") or {}
        black = game.get("black") or {}
        time_class = (game.get("time_class") or "?").capitalize()
        rated = "rated" if game.get("rated") else "unrated"
        return (f"{when}  {white.get('username', '?')} ({white.get('rating', '?')}) vs "
                f"{black.get('username', '?')} ({black.get('rating', '?')})  [{time_class}, {rated}]")

    # -- selecting / importing ----------------------------------------------

    def _set_all_checked(self, checked: bool):
        state = Qt.Checked if checked else Qt.Unchecked
        for i in range(self.results_list.count()):
            self.results_list.item(i).setCheckState(state)

    def _on_result_item_clicked(self, item: QListWidgetItem):
        row = self.results_list.row(item)
        if (
            not self.single_select
            and QApplication.keyboardModifiers() & Qt.ShiftModifier
            and self._last_clicked_row is not None
        ):
            lo, hi = sorted((self._last_clicked_row, row))
            for i in range(lo, hi + 1):
                self.results_list.item(i).setCheckState(Qt.Checked)
        self._last_clicked_row = row

    def _on_item_changed_single_select(self, item: QListWidgetItem):
        """Keeps at most one game checked: checking one unchecks the rest."""
        if self._enforcing_single_select or item.checkState() != Qt.Checked:
            return
        self._enforcing_single_select = True
        try:
            for i in range(self.results_list.count()):
                other = self.results_list.item(i)
                if other is not item and other.checkState() == Qt.Checked:
                    other.setCheckState(Qt.Unchecked)
        finally:
            self._enforcing_single_select = False

    def _import_selected(self):
        selected = [
            game for i, game in enumerate(self.matched_games)
            if self.results_list.item(i).checkState() == Qt.Checked
        ]
        if not selected:
            QMessageBox.warning(self, "Import from chess.com", "Select at least one game to import.")
            return

        username = self.username_edit.text().strip() or "chesscom"
        # chess.com usernames are case-insensitive, but the PGN headers (and
        # therefore the "which player is you" matching downstream) use the
        # account's canonical casing - pull that from the fetched game data
        # itself rather than trusting however the user typed it.
        canonical_username = username
        for game in selected:
            for side in ("white", "black"):
                side_username = (game.get(side) or {}).get("username")
                if side_username and side_username.lower() == username.lower():
                    canonical_username = side_username
                    break
            else:
                continue
            break

        folder = Path.home() / "Documents" / "Bongcloud Imports"
        folder.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = folder / f"{username}_{stamp}.pgn"
        path.write_text(combined_pgn(selected), encoding="utf-8")

        self.accept()
        self._on_imported(str(path), canonical_username)
