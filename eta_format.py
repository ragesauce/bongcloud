"""
eta_format.py

A tiny "time remaining" estimator for the Stockfish analysis progress
dialogs. There's no fixed per-unit cost to assume (position complexity,
the fast-mode checkbox, and analysis_cache hits finishing instantly all
vary it), so the estimate is just elapsed time / units done so far, applied
to what's left - a running rate that self-corrects as analysis proceeds.
"""

from __future__ import annotations

import time


def format_eta(start_time: float, done: int, total: int) -> str:
    """A " (~1m 20s remaining)" suffix for a progress label, or "" when
    there isn't enough progress yet to estimate from."""
    if done <= 0 or total <= 0 or done >= total:
        return ""
    elapsed = time.monotonic() - start_time
    if elapsed <= 0:
        return ""
    remaining = (elapsed / done) * (total - done)
    return f" (~{_format_duration(remaining)} remaining)"


def _format_duration(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    minutes, seconds = divmod(seconds, 60)
    if minutes:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"
