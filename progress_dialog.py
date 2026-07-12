"""
progress_dialog.py

"Is any of this actually working?" view: aggregate stats and simple trend
charts over every game game_history.py has logged - rating, accuracy, and
win rate over time. Read-only; all the data comes from review_app.py
recording each game it displays.
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QDialog, QLabel, QVBoxLayout, QWidget

import game_history

_TEXT_PRIMARY = "#f5f5f6"
_TEXT_MUTED = "#b8b8bd"
_SOLID_PANEL_BG = "rgb(52, 52, 55)"
_BORDER_COLOR = "#3d3d42"


class _LineChartWidget(QWidget):
    """Minimal line chart: evenly-spaced points connected by a polyline,
    axis-scaled to the data's own min/max. Deliberately simpler than
    summary_dialog.EvalChartWidget (a chess-specific filled win-probability
    "skyline") - just a plain trend line, reused here for both the rating
    and accuracy charts."""

    _BG = QColor(24, 24, 27)
    _BORDER = QColor(_BORDER_COLOR)

    def __init__(self, title: str, line_color: str, y_suffix: str = ""):
        super().__init__()
        self.title = title
        self.line_color = QColor(line_color)
        self.y_suffix = y_suffix
        self.values: list[float] = []
        self.setMinimumHeight(150)

    def set_values(self, values: list[float]):
        self.values = values
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        radius = 8

        clip = QPainterPath()
        clip.addRoundedRect(QRectF(0, 0, w, h), radius, radius)
        painter.setClipPath(clip)
        painter.fillRect(self.rect(), self._BG)

        title_font = QFont("Segoe UI", 9)
        title_font.setWeight(QFont.DemiBold)
        painter.setFont(title_font)
        painter.setPen(QColor(_TEXT_MUTED))
        painter.drawText(QRectF(10, 4, w - 20, 16), Qt.AlignLeft, self.title)

        if len(self.values) < 2:
            painter.setPen(QColor(_TEXT_MUTED))
            painter.drawText(self.rect(), Qt.AlignCenter, "Not enough data yet")
        else:
            pad_top, pad_bottom, pad_left, pad_right = 22, 18, 10, 10
            lo, hi = min(self.values), max(self.values)
            if lo == hi:
                lo, hi = lo - 1, hi + 1
            plot_w = max(w - pad_left - pad_right, 1)
            plot_h = max(h - pad_top - pad_bottom, 1)
            n = len(self.values)

            def point(i, v):
                x = pad_left + (i / (n - 1)) * plot_w
                y = pad_top + plot_h - ((v - lo) / (hi - lo)) * plot_h
                return QPointF(x, y)

            points = [point(i, v) for i, v in enumerate(self.values)]

            path = QPainterPath()
            path.moveTo(points[0])
            for p in points[1:]:
                path.lineTo(p)
            painter.setPen(QPen(self.line_color, 2))
            painter.setBrush(Qt.NoBrush)
            painter.drawPath(path)

            painter.setPen(Qt.NoPen)
            painter.setBrush(self.line_color)
            for p in points:
                painter.drawEllipse(p, 2.5, 2.5)

            last_text = f"{self.values[-1]:.0f}{self.y_suffix}"
            painter.setPen(QColor(_TEXT_PRIMARY))
            painter.drawText(QRectF(w - 80, 4, 70, 16), Qt.AlignRight, last_text)

        painter.setClipping(False)
        painter.setPen(QPen(self._BORDER, 1))
        painter.setBrush(Qt.NoBrush)
        painter.drawRoundedRect(QRectF(0.5, 0.5, w - 1, h - 1), radius, radius)


class ProgressDialog(QDialog):
    """Read-only summary of every game logged in game_history.json, oldest
    first: overall win rate/accuracy plus rating and accuracy trend charts."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Progress")
        self.resize(560, 480)
        self.setStyleSheet(f"QDialog {{ background-color: {_SOLID_PANEL_BG}; }}")

        history = game_history.load_history()
        entries = sorted(history.values(), key=lambda e: e.get("date") or "")

        layout = QVBoxLayout()

        if not entries:
            empty = QLabel("No games tracked yet - open or import a game to start building your history.")
            empty.setWordWrap(True)
            empty.setStyleSheet(f"color: {_TEXT_MUTED};")
            layout.addWidget(empty)
            self.setLayout(layout)
            return

        results = [e["result"] for e in entries if e.get("result")]
        win_rate = (100.0 * sum(1 for r in results if r == "win") / len(results)) if results else None
        accuracies = [e["accuracy"] for e in entries if e.get("accuracy") is not None]
        avg_accuracy = (sum(accuracies) / len(accuracies)) if accuracies else None

        stats_bits = [f"{len(entries)} game{'s' if len(entries) != 1 else ''} tracked"]
        if win_rate is not None:
            stats_bits.append(f"{win_rate:.0f}% win rate")
        if avg_accuracy is not None:
            stats_bits.append(f"{avg_accuracy:.0f}% avg accuracy")
        header = QLabel("  ·  ".join(stats_bits))
        header.setWordWrap(True)
        header.setStyleSheet(f"color: {_TEXT_PRIMARY}; font-weight: 600;")
        layout.addWidget(header)

        ratings = [e["my_rating"] for e in entries if e.get("my_rating") is not None]
        rating_chart = _LineChartWidget("Rating over time", "#3aa8ff")
        rating_chart.set_values(ratings)
        layout.addWidget(rating_chart)

        accuracy_chart = _LineChartWidget("Accuracy over time (%)", "#6bb95b", y_suffix="%")
        accuracy_chart.set_values(accuracies)
        layout.addWidget(accuracy_chart)

        layout.addStretch(1)
        self.setLayout(layout)
