"""
review_app.py

Native desktop viewer (PySide6) for annotated chess games. Steps through an
enhanced.pgn move by move, drawing the board and showing the scoresheet-
generated classification + commentary for the currently selected move.

Run: python review_app.py [path/to/game.pgn]
"""

from __future__ import annotations

import os
import sys
import threading

import chess
import chess.engine
import chess.pgn
from PySide6.QtCore import Qt, QObject, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QFont, QIcon, QKeySequence, QPalette, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMainWindow,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QScrollArea,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from board_widget import BoardWidget, PieceRenderer
from chesscom_import_dialog import ChessComImportDialog
from engine_analysis import analyze_game, find_engine
import game_history
import player_identity
from pgn_loader import MoveRecord, chesscom_link, has_eval_annotations, load_game, load_games
from progress_dialog import ProgressDialog
from puzzle_trainer import PuzzleTrainerDialog
from scoresheet import win_percent_white
from sound import MoveSoundPlayer
from summary_dialog import SummaryPanel
from weakness_report import WeaknessReportDialog

_CLASS_COLORS = {
    "Brilliant": "#1baaa6",
    "Great": "#5c8bb0",
    "Best": "#3aa8ff",
    "Excellent": "#3aa8ff",
    "Good": "#6bb95b",
    "Book": "#b8a068",
    "Inaccuracy": "#e0c22c",
    "Mistake": "#e08b2c",
    "Blunder": "#e0452c",
    "Miss": "#c73b3b",
}

# Small colored symbol shown next to each move in the move list, standing in
# for the icons chess.com uses (checkmark/"?!"/"?"/"??") since we don't have
# icon assets to draw on.
_CLASS_SYMBOLS = {
    "Brilliant": "!!",
    "Great": "!",
    "Best": "★",       # star
    "Excellent": "✓",  # check
    "Good": "✓",
    "Book": "",
    "Inaccuracy": "?!",
    "Mistake": "?",
    "Blunder": "??",
    "Miss": "?",
}

_MOVE_ROW_HIGHLIGHT = "background-color: rgba(58, 168, 255, 0.35); border-radius: 4px;"

# -- dark theme ------------------------------------------------------------
# True desktop-see-through transparency for just the negative space turned out
# not to render correctly in testing (WA_TranslucentBackground came out solid
# black rather than blending, likely a driver/compositor limitation) - rather
# than ship something that might look broken, the whole app is a flat, solid
# dark theme: negative space a touch darker than the opaque content panels
# (board, move list, message boxes, progress dialog, ...) for subtle depth.
_NEGATIVE_SPACE_BG = "rgb(40, 40, 43)"
_SOLID_PANEL_BG = "rgb(52, 52, 55)"
_PANEL_BG = "rgba(255, 255, 255, 0.03)"
_ROW_ALT_BG = "rgba(255, 255, 255, 0.05)"
_BORDER_COLOR = "#3d3d42"
_TEXT_PRIMARY = "#f5f5f6"
_TEXT_MUTED = "#b8b8bd"

_BUTTON_STYLE = f"""
    QPushButton {{ padding: 5px 12px; border-radius: 5px; border: 1px solid {_BORDER_COLOR};
                   background: rgba(255,255,255,0.06); color: {_TEXT_PRIMARY}; }}
    QPushButton:hover {{ background: rgba(58,168,255,0.18); border-color: #3aa8ff; }}
    QPushButton:pressed {{ background: rgba(58,168,255,0.30); }}
"""


def apply_dark_theme(app: QApplication):
    """Dark palette + Fusion style, applied once at startup. Fusion (unlike
    the native Windows style) actually honors a custom QPalette for standard
    widgets, so QMessageBox/QProgressDialog/QScrollBar etc. pick this up
    automatically without needing per-widget stylesheets."""
    app.setStyle("Fusion")
    palette = QPalette()
    palette.setColor(QPalette.Window, QColor(56, 56, 59))
    palette.setColor(QPalette.WindowText, QColor(_TEXT_PRIMARY))
    palette.setColor(QPalette.Base, QColor(50, 50, 53))
    palette.setColor(QPalette.AlternateBase, QColor(64, 64, 68))
    palette.setColor(QPalette.Text, QColor(_TEXT_PRIMARY))
    palette.setColor(QPalette.Button, QColor(70, 70, 74))
    palette.setColor(QPalette.ButtonText, QColor(_TEXT_PRIMARY))
    palette.setColor(QPalette.ToolTipBase, QColor(70, 70, 74))
    palette.setColor(QPalette.ToolTipText, QColor(_TEXT_PRIMARY))
    palette.setColor(QPalette.Highlight, QColor("#3aa8ff"))
    palette.setColor(QPalette.HighlightedText, QColor("#ffffff"))
    palette.setColor(QPalette.PlaceholderText, QColor(_TEXT_MUTED))
    palette.setColor(QPalette.Disabled, QPalette.Text, QColor("#6a6a6e"))
    palette.setColor(QPalette.Disabled, QPalette.WindowText, QColor("#6a6a6e"))
    app.setPalette(palette)


def _make_themed_message_box(parent, icon, title: str, text: str, buttons=QMessageBox.Ok) -> QMessageBox:
    """QMessageBox, dark-themed like the rest of the app. It's all content (no
    negative space to keep see-through), so it's fully opaque, not translucent.
    We build it manually (instead of the QMessageBox.information/... one-liners)
    so we can set the shared stylesheet on it."""
    box = QMessageBox(parent)
    box.setIcon(icon)
    box.setWindowTitle(title)
    box.setText(text)
    box.setStandardButtons(buttons)
    box.setStyleSheet(
        f"QMessageBox {{ background-color: {_SOLID_PANEL_BG}; }}"
        f"QLabel {{ color: {_TEXT_PRIMARY}; background: transparent; }}"
        + _BUTTON_STYLE
    )
    return box


class ClickableLabel(QLabel):
    """A QLabel that emits clicked() so it can act as a move-list cell."""

    clicked = Signal()

    def mousePressEvent(self, event):
        self.clicked.emit()
        super().mousePressEvent(event)


class AnalysisWorker(QObject):
    """Runs engine_analysis.analyze_game on a plain Python thread so the
    window stays responsive while Stockfish chews through the game.

    This deliberately is NOT a QThread: python-chess's engine transport
    relies on asyncio's Windows ProactorEventLoop for subprocess pipes, and
    running that inside a QThread's OS thread breaks handle inheritance
    there (fails with "WinError 6: the handle is invalid"). A plain
    threading.Thread avoids that, and Qt signals still marshal safely back
    to the main thread from it.
    """

    progress = Signal(int, int)
    finished_ok = Signal(object, object)  # game, records
    failed = Signal(str)

    def __init__(self, path: str, engine_path: str, limit: "chess.engine.Limit"):
        super().__init__()
        self.path = path
        self.engine_path = engine_path
        self.limit = limit
        self._stop_requested = False

    def request_stop(self):
        self._stop_requested = True

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        try:
            game, records = analyze_game(
                self.path,
                self.engine_path,
                self.limit,
                progress_callback=lambda done, total: self.progress.emit(done, total),
                should_stop=lambda: self._stop_requested,
            )
            self.finished_ok.emit(game, records)
        except Exception as exc:  # noqa: BLE001 - surface any engine/parsing error to the UI
            self.failed.emit(str(exc))


class ReviewWindow(QMainWindow):
    def __init__(self, pgn_path: str | None):
        super().__init__()
        self.setWindowTitle("Bongcloud")
        self.setWindowIcon(QIcon(PieceRenderer.get(chess.KNIGHT, chess.BLACK, 64)))
        self.resize(1000, 760)
        self.setStyleSheet(f"QMainWindow {{ background-color: {_NEGATIVE_SPACE_BG}; }}")

        self.records: list[MoveRecord] = []
        self.game: "chess.pgn.Game | None" = None
        self.index = -1  # -1 = starting position, before move 0
        self.white_name = "?"
        self.black_name = "?"
        self.engine_path: str | None = None
        self.analysis_thread: AnalysisWorker | None = None
        self.progress_dialog: QProgressDialog | None = None
        self.summary_panel: SummaryPanel | None = None
        self.puzzle_dialog: PuzzleTrainerDialog | None = None
        self.weakness_dialog: WeaknessReportDialog | None = None
        self.progress_report_dialog: ProgressDialog | None = None
        self.chesscom_dialog: ChessComImportDialog | None = None

        self.board_widget = BoardWidget()
        self.board_widget.show_eval_bar = True
        self.sound_player = MoveSoundPlayer()

        self.header_label = QLabel()
        self.header_label.setWordWrap(True)
        self.header_label.setFont(QFont("Segoe UI", 12))
        self.header_label.setStyleSheet(f"color: {_TEXT_PRIMARY}; padding-bottom: 2px; background: transparent;")

        self.chesscom_link_btn = QPushButton("View on Chess.com ↗")
        self.chesscom_link_btn.setStyleSheet(_BUTTON_STYLE)
        self.chesscom_link_btn.clicked.connect(self._on_open_chesscom_link)
        self.chesscom_link_btn.setVisible(False)
        self._chesscom_link: str | None = None

        self.move_table = QTableWidget()
        self.move_table.setColumnCount(3)
        self.move_table.horizontalHeader().hide()
        self.move_table.verticalHeader().hide()
        self.move_table.setShowGrid(False)
        self.move_table.setFocusPolicy(Qt.NoFocus)
        self.move_table.setSelectionMode(QAbstractItemView.NoSelection)
        self.move_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.move_table.setAlternatingRowColors(True)
        self.move_table.setColumnWidth(0, 34)
        self.move_table.setMinimumHeight(220)
        self.move_table.setMaximumHeight(280)
        self.move_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Fixed)
        self.move_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.move_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.move_table.setStyleSheet(
            f"""
            QTableWidget {{ border: 1px solid {_BORDER_COLOR}; border-radius: 6px; outline: none;
                           gridline-color: transparent; background: {_PANEL_BG}; }}
            QTableWidget::item {{ padding: 0px; }}
            QTableWidget::item:alternate {{ background: {_ROW_ALT_BG}; }}
            """
        )
        # record index -> the ClickableLabel showing that move (for highlighting the current move)
        self._move_labels: dict[int, ClickableLabel] = {}
        # record index -> table row (so we can scroll the current move into view)
        self._index_to_row: dict[int, int] = {}

        self.badge_label = QLabel()
        self.badge_label.setAlignment(Qt.AlignCenter)
        badge_font = QFont("Segoe UI", 14)
        badge_font.setBold(True)
        self.badge_label.setFont(badge_font)
        self.badge_label.setFixedHeight(40)

        self.eval_label = QLabel()
        eval_font = QFont("Segoe UI", 10)
        eval_font.setWeight(QFont.DemiBold)
        self.eval_label.setFont(eval_font)
        self.eval_label.setStyleSheet(f"color: {_TEXT_MUTED}; background: transparent;")

        self.commentary_label = QLabel()
        self.commentary_label.setWordWrap(True)
        commentary_font = QFont("Segoe UI", 11)
        commentary_font.setWeight(QFont.DemiBold)
        self.commentary_label.setFont(commentary_font)
        self.commentary_label.setStyleSheet(f"color: {_TEXT_PRIMARY}; padding-top: 4px; background: transparent;")

        self.detail_label = QLabel()
        self.detail_label.setWordWrap(True)
        self.detail_label.setStyleSheet(f"color: {_TEXT_MUTED}; padding-top: 2px; background: transparent;")
        self.detail_label.setFont(QFont("Segoe UI", 9.5))

        prev_btn = QPushButton("◀ Prev")
        next_btn = QPushButton("Next ▶")
        open_btn = QPushButton("Open PGN...")
        import_btn = QPushButton("Import from chess.com...")
        practice_btn = QPushButton("Practice My Blunders")
        weakness_btn = QPushButton("Weakness Report...")
        progress_btn = QPushButton("Progress...")
        for btn in (prev_btn, next_btn, open_btn, import_btn, practice_btn, weakness_btn, progress_btn):
            btn.setStyleSheet(_BUTTON_STYLE)
        prev_btn.clicked.connect(self.go_prev)
        next_btn.clicked.connect(self.go_next)
        open_btn.clicked.connect(self.open_pgn_dialog)
        import_btn.clicked.connect(self.open_chesscom_import)
        practice_btn.clicked.connect(self.open_puzzle_trainer)
        weakness_btn.clicked.connect(self.open_weakness_report)
        progress_btn.clicked.connect(self.open_progress_dialog)

        # Window-wide shortcuts, not a keyPressEvent override: the right-hand
        # QScrollArea takes keyboard focus by default and consumes arrow keys
        # for its own scrolling, so a keyPressEvent on the window never sees
        # them. QShortcut fires regardless of which child widget has focus.
        for key in (Qt.Key_Right, Qt.Key_Down):
            QShortcut(QKeySequence(key), self, activated=self.go_next, context=Qt.WindowShortcut)
        for key in (Qt.Key_Left, Qt.Key_Up):
            QShortcut(QKeySequence(key), self, activated=self.go_prev, context=Qt.WindowShortcut)

        nav_row = QHBoxLayout()
        nav_row.setSpacing(8)
        nav_row.addWidget(prev_btn)
        nav_row.addWidget(next_btn)
        nav_row.addStretch()

        open_row = QHBoxLayout()
        open_row.setSpacing(8)
        open_row.addWidget(open_btn)
        open_row.addWidget(import_btn)

        tools_row = QHBoxLayout()
        tools_row.setSpacing(8)
        tools_row.addWidget(practice_btn)
        tools_row.addWidget(weakness_btn)
        tools_row.addWidget(progress_btn)

        self.summary_container = QVBoxLayout()
        self.summary_container.setSpacing(8)

        right_panel = QVBoxLayout()
        right_panel.setSpacing(8)
        right_panel.addWidget(self.header_label)
        right_panel.addWidget(self.chesscom_link_btn)
        right_panel.addWidget(self.move_table)
        right_panel.addWidget(self.badge_label)
        right_panel.addWidget(self.eval_label)
        right_panel.addWidget(self.commentary_label)
        right_panel.addWidget(self.detail_label)
        right_panel.addLayout(nav_row)
        right_panel.addLayout(open_row)
        right_panel.addLayout(tools_row)
        right_panel.addLayout(self.summary_container)
        right_panel.addStretch()

        # This panel (and the board) are the only two "solid" surfaces in the window - fully
        # opaque regardless of the translucent negative space around them - so its background
        # is a plain solid color, not an rgba/transparent one.
        right_content = QWidget()
        right_content.setStyleSheet(f"background-color: {_SOLID_PANEL_BG}; border-radius: 8px;")
        right_content.setLayout(right_panel)

        right_scroll = QScrollArea()
        right_scroll.setWidget(right_content)
        right_scroll.setWidgetResizable(True)
        right_scroll.setFixedWidth(400)
        right_scroll.setFrameShape(QScrollArea.NoFrame)
        right_scroll.setStyleSheet("background: transparent; border: none;")
        right_scroll.viewport().setStyleSheet(f"background-color: {_SOLID_PANEL_BG};")

        main_layout = QHBoxLayout()
        main_layout.setContentsMargins(12, 12, 12, 12)
        main_layout.setSpacing(16)
        main_layout.addWidget(self.board_widget, stretch=1)
        main_layout.addWidget(right_scroll)

        central = QWidget()
        central.setStyleSheet("background: transparent;")
        central.setLayout(main_layout)
        self.setCentralWidget(central)

        if pgn_path:
            self.load_pgn(pgn_path)
        else:
            self._show_empty_state()

    # -- loading ---------------------------------------------------------

    def _show_empty_state(self):
        """Shown on startup when there's no default PGN to load (e.g. a
        packaged distribution that doesn't ship a sample game)."""
        self.header_label.setText("No game loaded.")
        self._build_move_table([])
        self._rebuild_summary_panel([])
        self.board_widget.set_position(chess.Board(), None)
        self.board_widget.set_eval(50.0, "")
        self.badge_label.setText("")
        self.badge_label.setStyleSheet("")
        self.eval_label.setText("")
        self.commentary_label.setText('Use "Open PGN..." to load a game.')
        self.detail_label.setText("")

    def load_pgn(self, path: str):
        game, records = load_game(path)
        self._populate(game, records)

    def _populate(self, game: "chess.pgn.Game", records: list[MoveRecord]):
        self.records = records
        self.game = game
        self.index = -1

        self.white_name = game.headers.get("White", "?")
        self.black_name = game.headers.get("Black", "?")
        my_name = player_identity.load_last_name()
        self.board_widget.set_flipped(player_identity.is_black(self.white_name, self.black_name, my_name))
        self._chesscom_link = chesscom_link(game)
        self.chesscom_link_btn.setVisible(self._chesscom_link is not None)
        history = game_history.load_history()
        if game_history.record_game(history, game, records, my_name):
            game_history.save_history(history)
        result = game.headers.get("Result", "*")
        eco = game.headers.get("ECO", "")
        self.header_label.setText(f"<b>{self.white_name}</b> vs <b>{self.black_name}</b>  ({result})<br/>ECO {eco}")

        self._build_move_table(records)
        self._rebuild_summary_panel(records)
        self._render_current()

    def _rebuild_summary_panel(self, records: list[MoveRecord]):
        if self.summary_panel is not None:
            self.summary_container.removeWidget(self.summary_panel)
            self.summary_panel.deleteLater()
        self.summary_panel = SummaryPanel(records, self.white_name, self.black_name, self._jump_to_index, parent=self)
        self.summary_container.addWidget(self.summary_panel)

    def _move_cell_html(self, rec: MoveRecord) -> str:
        cls = rec.commentary["classification"]
        symbol = _CLASS_SYMBOLS.get(cls, "")
        color = _CLASS_COLORS.get(cls, "#333333")
        return (
            f'<span style="color:{color}; font-weight:700;">{symbol}</span> '
            f'<span style="color:{_TEXT_PRIMARY}; font-weight:600;">{rec.san}</span>'
        )

    def _make_move_label(self, index: int) -> ClickableLabel:
        label = ClickableLabel()
        label.setTextFormat(Qt.RichText)
        label.setFont(QFont("Segoe UI", 10))
        label.setText(self._move_cell_html(self.records[index]))
        label.setContentsMargins(8, 5, 8, 5)
        label.clicked.connect(lambda idx=index: self._jump_to_index(idx))
        return label

    def _build_move_table(self, records: list[MoveRecord]):
        self.move_table.setRowCount(0)
        self._move_labels = {}
        self._index_to_row = {}

        row = 0
        i = 0
        n = len(records)
        while i < n:
            self.move_table.insertRow(row)

            number_item = QTableWidgetItem(f"{records[i].move_number}.")
            number_item.setForeground(QColor(_TEXT_MUTED))
            number_item.setTextAlignment(Qt.AlignCenter)
            self.move_table.setItem(row, 0, number_item)

            white_label = self._make_move_label(i)
            self.move_table.setCellWidget(row, 1, white_label)
            self._move_labels[i] = white_label
            self._index_to_row[i] = row
            i += 1

            if i < n and not records[i].color_white:
                black_label = self._make_move_label(i)
                self.move_table.setCellWidget(row, 2, black_label)
                self._move_labels[i] = black_label
                self._index_to_row[i] = row
                i += 1

            row += 1

    def open_pgn_dialog(self):
        path, _ = QFileDialog.getOpenFileName(self, "Open PGN", "", "PGN files (*.pgn);;All files (*)")
        if not path:
            return
        self._load_or_analyze(path)

    def _load_or_analyze(self, path: str):
        if has_eval_annotations(path):
            self.load_pgn(path)
        else:
            self.open_stockfish_dialog(path)

    def open_chesscom_import(self):
        self.chesscom_dialog = ChessComImportDialog(self._on_chesscom_imported, parent=self, single_select=True)
        self.chesscom_dialog.show()

    def _on_chesscom_imported(self, path: str, username: str):
        player_identity.save_last_name(username)
        self._load_or_analyze(path)

    # -- engine analysis ---------------------------------------------------

    def open_stockfish_dialog(self, path: str):
        engine_path = self.engine_path or find_engine()
        if not engine_path:
            _make_themed_message_box(
                self,
                QMessageBox.Information,
                "Locate Stockfish",
                "Couldn't auto-detect a Stockfish engine on this machine. "
                "Please browse to the stockfish executable.",
            ).exec()
            engine_path, _ = QFileDialog.getOpenFileName(self, "Locate Stockfish executable")
            if not engine_path:
                return
        self.engine_path = engine_path

        self.progress_dialog = QProgressDialog("Analyzing game with Stockfish...", "Cancel", 0, 0, self)
        self.progress_dialog.setWindowTitle("Analyzing")
        self.progress_dialog.setWindowModality(Qt.WindowModal)
        self.progress_dialog.setMinimumDuration(0)
        self.progress_dialog.setStyleSheet(
            f"QProgressDialog {{ background-color: {_SOLID_PANEL_BG}; }}"
            f"QLabel {{ color: {_TEXT_PRIMARY}; background: transparent; }}"
            + _BUTTON_STYLE
        )
        self.progress_dialog.show()

        limit = chess.engine.Limit(time=0.2)
        self.analysis_thread = AnalysisWorker(path, engine_path, limit)
        self.analysis_thread.progress.connect(self._on_analysis_progress)
        self.analysis_thread.finished_ok.connect(self._on_analysis_finished)
        self.analysis_thread.failed.connect(self._on_analysis_failed)
        self.progress_dialog.canceled.connect(self.analysis_thread.request_stop)
        self.analysis_thread.start()

    def _on_analysis_progress(self, done: int, total: int):
        if not self.progress_dialog:
            return
        self.progress_dialog.setMaximum(total)
        self.progress_dialog.setValue(done)
        self.progress_dialog.setLabelText(f"Analyzing move {done}/{total}...")

    def _on_analysis_finished(self, game, records):
        if self.progress_dialog:
            self.progress_dialog.close()
            self.progress_dialog = None
        self._populate(game, records)

    def _on_analysis_failed(self, message: str):
        if self.progress_dialog:
            self.progress_dialog.close()
            self.progress_dialog = None
        _make_themed_message_box(self, QMessageBox.Warning, "Analysis failed", message).exec()

    def _jump_to_index(self, index: int):
        self.index = index
        self._render_current()

    # -- puzzle trainer / weakness report -----------------------------------

    def open_puzzle_trainer(self):
        self.puzzle_dialog = PuzzleTrainerDialog(
            self.records, source_game=self.game, on_view_game=self._show_game_at, parent=self
        )
        self.puzzle_dialog.show()

    def open_weakness_report(self):
        self.weakness_dialog = WeaknessReportDialog(self._open_game_and_jump, parent=self)
        self.weakness_dialog.show()

    def open_progress_dialog(self):
        self.progress_report_dialog = ProgressDialog(parent=self)
        self.progress_report_dialog.show()

    def _on_open_chesscom_link(self):
        if self._chesscom_link:
            QDesktopServices.openUrl(QUrl(self._chesscom_link))

    def _open_game_and_jump(self, path: str, game_index: int, ply_index: int):
        games = load_games(path)
        if game_index >= len(games):
            return
        game, records = games[game_index]
        self._show_game_at(game, records, ply_index)

    def _show_game_at(self, game: "chess.pgn.Game", records: list[MoveRecord], ply_index: int):
        self._populate(game, records)
        self.index = ply_index
        self._render_current()
        self.activateWindow()
        self.raise_()

    # -- navigation --------------------------------------------------------

    def go_next(self):
        if self.index < len(self.records) - 1:
            self.index += 1
            self._render_current()

    def go_prev(self):
        if self.index > -1:
            self.index -= 1
            self._render_current()

    def _highlight_current(self):
        for idx, label in self._move_labels.items():
            label.setStyleSheet(_MOVE_ROW_HIGHLIGHT if idx == self.index else "")
        row = self._index_to_row.get(self.index)
        if row is not None:
            self.move_table.scrollToItem(self.move_table.item(row, 0))

    # -- rendering -----------------------------------------------------

    def _render_current(self):
        self._highlight_current()
        if self.summary_panel is not None:
            self.summary_panel.set_current_index(self.index)

        if self.index == -1:
            self.board_widget.set_position(chess.Board(), None)
            if self.records:
                start_eval = self.records[0].eval_before
                self.board_widget.set_eval(win_percent_white(start_eval), start_eval.display())
            else:
                self.board_widget.set_eval(50.0, "")
            self.badge_label.setText("")
            self.badge_label.setStyleSheet("")
            self.eval_label.setText("")
            self.commentary_label.setText("Starting position. Use Next to step through the game.")
            self.detail_label.setText("")
            return

        rec = self.records[self.index]
        board_after = rec.board_before.copy()
        board_after.push(rec.move)
        self.board_widget.set_position(board_after, rec.move)
        self.sound_player.play_for_move(rec.board_before, rec.move, board_after)
        self.board_widget.set_eval(win_percent_white(rec.eval_after), rec.commentary["eval"])

        cls = rec.commentary["classification"]
        color = _CLASS_COLORS.get(cls, "#333333")
        self.badge_label.setText(cls)
        self.badge_label.setStyleSheet(
            f"color: white; background-color: {color}; border-radius: 8px; padding: 4px;"
        )

        phase = f"  ·  {rec.phase}" if rec.phase else ""
        self.eval_label.setText(f"Eval: {rec.commentary['eval']}{phase}")
        self.commentary_label.setText(rec.commentary["text"])
        self.detail_label.setText(rec.commentary["detail"] or "")


def main():
    pgn_path = sys.argv[1] if len(sys.argv) > 1 else "enhanced.pgn"
    if not os.path.isfile(pgn_path):
        pgn_path = None
    app = QApplication(sys.argv)
    apply_dark_theme(app)
    window = ReviewWindow(pgn_path)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
