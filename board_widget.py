"""
board_widget.py

Shared chess board rendering - SVG piece art plus the board/frame/coordinate
painting - used by both the main review window (read-only) and the puzzle
trainer (click-to-move). Kept as pure rendering + click-to-square mapping;
callers own the chess logic via the on_square_clicked callback.
"""

from __future__ import annotations

from typing import Callable, Optional

import chess
import chess.svg
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QWidget

_LIGHT_SQUARE = QColor("#eeeed2")
_DARK_SQUARE = QColor("#769656")
_HIGHLIGHT = QColor(255, 235, 59, 130)
_BOARD_BORDER = QColor("#4b4437")
_SELECTED = QColor(58, 168, 255, 130)
_LEGAL_TARGET = QColor(58, 168, 255, 190)

_EVAL_BAR_WHITE = QColor("#eeeeee")
_EVAL_BAR_BLACK = QColor("#26262a")


class PieceRenderer:
    """Rasterizes python-chess's bundled "cburnett" SVG piece set (the
    classic lichess-style pieces) on demand, cached per (piece, pixel size)
    so repeated paints don't re-render the SVG every frame."""

    _cache: dict[tuple[int, bool, int], QPixmap] = {}

    @classmethod
    def get(cls, piece_type: int, color: bool, size: int) -> QPixmap:
        key = (piece_type, color, size)
        pixmap = cls._cache.get(key)
        if pixmap is None:
            svg_bytes = chess.svg.piece(chess.Piece(piece_type, color)).encode("utf-8")
            renderer = QSvgRenderer(svg_bytes)
            pixmap = QPixmap(size, size)
            pixmap.fill(Qt.transparent)
            painter = QPainter(pixmap)
            painter.setRenderHint(QPainter.Antialiasing)
            renderer.render(painter)
            painter.end()
            cls._cache[key] = pixmap
        return pixmap


class BoardWidget(QWidget):
    """Renders a position. Set `interactive = True` and `on_square_clicked`
    to let it report clicks (as square indices) back to an owner that knows
    the chess rules - this widget itself never validates or makes moves."""

    _BORDER = 6
    _EVAL_BAR_WIDTH = 22
    _EVAL_BAR_GAP = 10

    def __init__(self):
        super().__init__()
        self.board = chess.Board()
        self.last_move: Optional[chess.Move] = None
        self.setMinimumSize(480, 480)

        self.interactive = False
        self.on_square_clicked: Optional[Callable[[int], None]] = None
        self.selected_square: Optional[int] = None
        self.legal_targets: set[int] = set()
        self.flipped = False

        # Opt-in: puzzle trainer boards don't show one, the main review
        # window's does. Reserved as a left gutter inside this widget's own
        # geometry (rather than a separate sibling widget) so it's always
        # pixel-aligned to the board's actual rendered top/bottom edges,
        # including when the board is letterboxed narrower than the widget.
        self.show_eval_bar = False
        self.eval_white_percent = 50.0
        self.eval_display = ""

    def set_position(self, board: chess.Board, last_move: Optional[chess.Move]):
        self.board = board
        self.last_move = last_move
        self.update()

    def set_flipped(self, flipped: bool):
        if flipped == self.flipped:
            return
        self.flipped = flipped
        self.update()

    def set_eval(self, white_percent: float, display: str):
        self.eval_white_percent = max(0.0, min(100.0, white_percent))
        self.eval_display = display
        self.update()

    def _geometry(self):
        border = self._BORDER
        gutter = (self._EVAL_BAR_WIDTH + self._EVAL_BAR_GAP) if self.show_eval_bar else 0
        usable_w = self.width() - gutter
        side = min(usable_w, self.height()) - border * 2
        square = side / 8
        ox = gutter + (usable_w - side) / 2
        oy = (self.height() - side) / 2
        return border, side, square, ox, oy

    def _paint_eval_bar(self, painter: QPainter, side: float, oy: float):
        bar_w = self._EVAL_BAR_WIDTH

        painter.setPen(Qt.NoPen)
        painter.setBrush(_BOARD_BORDER)
        painter.drawRoundedRect(QRectF(-1, oy - 1, bar_w + 2, side + 2), 3, 3)

        white_h = side * (self.eval_white_percent / 100.0)
        painter.setBrush(_EVAL_BAR_BLACK)
        painter.drawRect(QRectF(0, oy, bar_w, side - white_h))
        painter.setBrush(_EVAL_BAR_WHITE)
        painter.drawRect(QRectF(0, oy + side - white_h, bar_w, white_h))

        if self.eval_display:
            font = QFont("Segoe UI", 8)
            font.setBold(True)
            painter.setFont(font)
            if self.eval_white_percent >= 50:
                text_rect = QRectF(0, oy + side - 16, bar_w, 14)
                painter.setPen(QColor("#1a1a1a"))
            else:
                text_rect = QRectF(0, oy + 2, bar_w, 14)
                painter.setPen(QColor("#f0f0f0"))
            painter.drawText(text_rect, Qt.AlignCenter, self.eval_display)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        border, side, square, ox, oy = self._geometry()

        if self.show_eval_bar:
            self._paint_eval_bar(painter, side, oy)

        painter.setPen(Qt.NoPen)
        painter.setBrush(_BOARD_BORDER)
        painter.drawRoundedRect(QRectF(ox - border, oy - border, side + border * 2, side + border * 2), 4, 4)

        label_font = QFont("Segoe UI", max(int(square * 0.15), 8))
        label_font.setBold(True)

        for rank in range(8):
            for file in range(8):
                board_rank = rank if self.flipped else 7 - rank
                board_file = 7 - file if self.flipped else file
                square_index = chess.square(board_file, board_rank)
                x, y = ox + file * square, oy + rank * square
                is_light = (file + rank) % 2 == 0
                square_color = _LIGHT_SQUARE if is_light else _DARK_SQUARE
                painter.setBrush(square_color)
                painter.drawRect(QRectF(x, y, square, square))

                if self.last_move and square_index in (self.last_move.from_square, self.last_move.to_square):
                    painter.fillRect(QRectF(x, y, square, square), _HIGHLIGHT)
                if square_index == self.selected_square:
                    painter.fillRect(QRectF(x, y, square, square), _SELECTED)

                painter.setFont(label_font)
                label_color = _DARK_SQUARE if is_light else _LIGHT_SQUARE
                painter.setPen(label_color)
                if file == 0:
                    painter.drawText(
                        QRectF(x + 2, y + 1, square - 4, square - 2), Qt.AlignTop | Qt.AlignLeft,
                        str(board_rank + 1),
                    )
                if rank == 7:
                    painter.drawText(
                        QRectF(x + 2, y + 1, square - 4, square - 2),
                        Qt.AlignBottom | Qt.AlignRight,
                        chess.FILE_NAMES[board_file],
                    )

                piece = self.board.piece_at(square_index)
                if piece is not None:
                    pixmap = PieceRenderer.get(piece.piece_type, piece.color, max(int(square), 1))
                    painter.drawPixmap(QRectF(x, y, square, square).toRect(), pixmap)

                if square_index in self.legal_targets:
                    painter.setPen(Qt.NoPen)
                    painter.setBrush(_LEGAL_TARGET)
                    radius = square * (0.16 if piece is None else 0.42)
                    painter.drawEllipse(QPointF(x + square / 2, y + square / 2), radius, radius)

    def mousePressEvent(self, event):
        if not self.interactive or self.on_square_clicked is None:
            return
        border, side, square, ox, oy = self._geometry()
        pos = event.position()
        x, y = pos.x() - ox, pos.y() - oy
        if x < 0 or y < 0 or x >= side or y >= side:
            return
        disp_file = int(x // square)
        disp_rank = int(y // square)
        board_rank = disp_rank if self.flipped else 7 - disp_rank
        board_file = 7 - disp_file if self.flipped else disp_file
        self.on_square_clicked(chess.square(board_file, board_rank))
