"""
summary_dialog.py

Whole-game summary panel: per-side accuracy, a move-quality breakdown, an
eval graph across the game, and a jump list of the costliest moments - the
"game report" view embedded as a tab alongside the move-by-move review in
review_app.py.
"""

from __future__ import annotations

from collections import Counter
from typing import Callable, Optional

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen, QPolygonF
from PySide6.QtWidgets import (
    QLabel,
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
    QWidget,
)

from pgn_loader import MoveRecord
from scoresheet import game_accuracy, win_percent_white

_CLASS_ORDER = [
    "Brilliant", "Great", "Best", "Excellent", "Good", "Book", "Inaccuracy", "Mistake", "Miss", "Blunder",
]
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

# Same small colored symbols the move list uses, so a "costly moment" reads
# the same way everywhere in the app instead of a garish full-line color.
_CLASS_SYMBOLS = {
    "Brilliant": "!!",
    "Great": "!",
    "Best": "★",
    "Excellent": "✓",
    "Good": "✓",
    "Book": "",
    "Inaccuracy": "?!",
    "Mistake": "?",
    "Blunder": "??",
    "Miss": "?",
}

_TEXT_PRIMARY = "#f5f5f6"
_TEXT_MUTED = "#b8b8bd"


class ClickableLabel(QLabel):
    """A QLabel that emits clicked() so it can act as a clickable list row."""

    clicked = Signal()

    def mousePressEvent(self, event):
        self.clicked.emit()
        super().mousePressEvent(event)


class EvalChartWidget(QWidget):
    """Win-probability eval graph in the chess.com style: a filled white/black
    "skyline" silhouette (not a thin line) that tracks White's win chances,
    with blunders/mistakes/misses marked. Click anywhere to jump to that move."""

    _WHITE_FILL = QColor(235, 235, 238)
    _BLACK_FILL = QColor(24, 24, 27)
    _BORDER = QColor("#3d3d42")

    _CURRENT_MARKER = QColor("#3aa8ff")

    def __init__(self, records: list[MoveRecord]):
        super().__init__()
        self.records = records
        self.setMinimumHeight(140)
        self._on_click: Optional[Callable[[int], None]] = None
        self.current_index: Optional[int] = None

    def set_on_click(self, callback: Callable[[int], None]):
        self._on_click = callback

    def set_current_index(self, index: Optional[int]):
        """index uses the same convention as ReviewWindow.index: -1 for the
        starting position (before any move), 0..n-1 for "just after move i"
        - matches what's passed in directly, no translation needed."""
        self.current_index = index
        self.update()

    @staticmethod
    def _white_win_pct(i: int, records: list[MoveRecord]) -> float:
        return win_percent_white(records[i].eval_after)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        radius = 8

        clip = QPainterPath()
        clip.addRoundedRect(QRectF(0, 0, w, h), radius, radius)
        painter.setClipPath(clip)

        painter.fillRect(self.rect(), self._BLACK_FILL)

        n = len(self.records)
        if n > 0:
            points = [
                QPointF((i / max(n - 1, 1)) * w, h - (self._white_win_pct(i, self.records) / 100.0) * h)
                for i in range(n)
            ]

            # White's win-percentage share, filled from the curve up to the top edge -
            # the "skyline" look: white silhouette over the black background above.
            silhouette = QPolygonF(points + [QPointF(w, 0), QPointF(0, 0)])
            painter.setPen(Qt.NoPen)
            painter.setBrush(self._WHITE_FILL)
            painter.drawPolygon(silhouette)

            painter.setPen(QPen(QColor(120, 120, 120), 1, Qt.DashLine))
            painter.drawLine(QPointF(0, h / 2), QPointF(w, h / 2))

            painter.setPen(Qt.NoPen)
            for i, rec in enumerate(self.records):
                cls = rec.commentary["classification"]
                if cls in ("Blunder", "Mistake", "Miss"):
                    painter.setBrush(QColor(_CLASS_COLORS[cls]))
                    painter.drawEllipse(points[i], 3.5, 3.5)

            if self.current_index is not None:
                idx = max(0, min(self.current_index, n - 1))
                x = (idx / max(n - 1, 1)) * w if self.current_index >= 0 else 0.0
                # A small downward-pointing arrow anchored to the top edge,
                # plus a thin guideline through the whole chart - kept off
                # the curve itself (where the blunder/mistake dots sit) so
                # the two markers never overlap.
                guide_color = QColor(self._CURRENT_MARKER)
                guide_color.setAlpha(130)
                painter.setPen(QPen(guide_color, 1))
                painter.drawLine(QPointF(x, 0), QPointF(x, h))

                painter.setPen(Qt.NoPen)
                painter.setBrush(self._CURRENT_MARKER)
                arrow_w, arrow_h = 8, 7
                arrow = QPolygonF([
                    QPointF(x - arrow_w / 2, 0),
                    QPointF(x + arrow_w / 2, 0),
                    QPointF(x, arrow_h),
                ])
                painter.drawPolygon(arrow)

        painter.setClipping(False)
        painter.setPen(QPen(self._BORDER, 1))
        painter.setBrush(Qt.NoBrush)
        painter.drawRoundedRect(QRectF(0.5, 0.5, w - 1, h - 1), radius, radius)

    def mousePressEvent(self, event):
        if not self.records or self._on_click is None:
            return
        n = len(self.records)
        frac = max(0.0, min(1.0, event.position().x() / max(self.width(), 1)))
        self._on_click(round(frac * (n - 1)))


class SummaryPanel(QWidget):
    """Embeddable whole-game report: accuracy, move-quality breakdown, eval
    chart, and a costliest-moments jump list. Lives inside a tab in the main
    window rather than a separate popup."""

    def __init__(
        self,
        records: list[MoveRecord],
        white: str,
        black: str,
        on_jump: Callable[[int], None],
        parent=None,
    ):
        super().__init__(parent)
        self._on_jump = on_jump

        if not records:
            layout = QVBoxLayout()
            placeholder = QLabel("Load a game to see its summary.")
            placeholder.setStyleSheet(f"color: {_TEXT_MUTED};")
            layout.addWidget(placeholder)
            layout.addStretch()
            self.setLayout(layout)
            return

        acc_white, acc_black = self._accuracy(records)
        counts_white, counts_black = self._counts(records)

        header = QLabel(
            f"<b>{white}</b> accuracy: <b>{acc_white:.1f}%</b>"
            f"&nbsp;&nbsp;&nbsp;&nbsp;<b>{black}</b> accuracy: <b>{acc_black:.1f}%</b>"
        )
        header.setTextFormat(Qt.RichText)
        header.setWordWrap(True)
        header.setStyleSheet(f"color: {_TEXT_PRIMARY};")

        breakdown = QLabel(self._breakdown_html(counts_white, counts_black, white, black))
        breakdown.setTextFormat(Qt.RichText)
        breakdown.setStyleSheet(f"color: {_TEXT_PRIMARY};")

        chart_caption = QLabel("Eval across the game (click to jump):")
        chart_caption.setStyleSheet(f"color: {_TEXT_MUTED};")
        chart = EvalChartWidget(records)
        chart.set_on_click(self._jump)
        self.eval_chart = chart

        list_caption = QLabel("Costliest moments (click to jump):")
        list_caption.setStyleSheet(f"color: {_TEXT_MUTED};")

        self.critical_list = QListWidget()
        self.critical_list.setStyleSheet(
            """
            QListWidget { border: 1px solid #3d3d42; border-radius: 6px; outline: none;
                          background: rgba(255,255,255,0.03); }
            QListWidget::item { padding: 0px; }
            QListWidget::item:alternate { background: rgba(255,255,255,0.05); }
            QListWidget::item:selected { background: rgba(58,168,255,0.35); }
            """
        )
        self.critical_list.setAlternatingRowColors(True)
        indices = self._critical_indices(records)
        for idx in indices:
            item = QListWidgetItem()
            label = self._make_critical_label(records[idx], idx)
            item.setSizeHint(label.sizeHint())
            self.critical_list.addItem(item)
            self.critical_list.setItemWidget(item, label)

        if not indices:
            no_moments = QListWidgetItem("No costly moments - nicely played.")
            no_moments.setForeground(QColor(_TEXT_MUTED))
            self.critical_list.addItem(no_moments)

        layout = QVBoxLayout()
        layout.setSpacing(8)
        layout.addWidget(header)
        layout.addWidget(breakdown)
        layout.addWidget(chart_caption)
        layout.addWidget(chart)
        layout.addWidget(list_caption)
        layout.addWidget(self.critical_list, stretch=1)
        self.setLayout(layout)

    def _jump(self, idx: int):
        self._on_jump(idx)

    def set_current_index(self, index: Optional[int]):
        chart = getattr(self, "eval_chart", None)
        if chart is not None:
            chart.set_current_index(index)

    def _make_critical_label(self, rec: MoveRecord, idx: int) -> ClickableLabel:
        cls = rec.commentary["classification"]
        symbol = _CLASS_SYMBOLS.get(cls, "")
        color = _CLASS_COLORS.get(cls, _TEXT_PRIMARY)
        move_no = f"{rec.move_number}." if rec.color_white else f"{rec.move_number}..."
        loss = rec.commentary["win_prob_loss"]

        label = ClickableLabel()
        label.setTextFormat(Qt.RichText)
        label.setContentsMargins(8, 5, 8, 5)
        label.setText(
            f'<span style="color:{color}; font-weight:700;">{symbol}</span> '
            f'<span style="color:{_TEXT_PRIMARY}; font-weight:600;">{move_no} {rec.san}</span> '
            f'<span style="color:{_TEXT_MUTED};">-{loss:.0f}% win prob</span>'
        )
        label.clicked.connect(lambda i=idx: self._jump(i))
        return label

    @staticmethod
    def _accuracy(records: list[MoveRecord]) -> tuple[float, float]:
        return game_accuracy(records, True), game_accuracy(records, False)

    @staticmethod
    def _counts(records: list[MoveRecord]) -> tuple[Counter, Counter]:
        white = Counter(r.commentary["classification"] for r in records if r.color_white)
        black = Counter(r.commentary["classification"] for r in records if not r.color_white)
        return white, black

    @staticmethod
    def _breakdown_html(counts_white: Counter, counts_black: Counter, white: str, black: str) -> str:
        rows = []
        for cls in _CLASS_ORDER:
            w, b = counts_white.get(cls, 0), counts_black.get(cls, 0)
            if w == 0 and b == 0:
                continue
            color = _CLASS_COLORS[cls]
            rows.append(f"<tr><td style='color:{color}'>{cls}</td><td>{w}</td><td>{b}</td></tr>")
        return (
            f"<table cellpadding='4'><tr><th></th><th>{white}</th><th>{black}</th></tr>"
            + "".join(rows) + "</table>"
        )

    @staticmethod
    def _critical_indices(records: list[MoveRecord], top_n: int = 8) -> list[int]:
        scored = [(i, r.commentary["win_prob_loss"]) for i, r in enumerate(records)]
        scored = [(i, loss) for i, loss in scored if loss > 0]
        scored.sort(key=lambda pair: pair[1], reverse=True)
        return [i for i, _ in scored[:top_n]]
