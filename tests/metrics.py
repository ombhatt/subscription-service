from __future__ import annotations

from collections.abc import Callable

from app.observability import REGISTRY


def counted(name: str, **labels: str) -> Callable[[], float]:
    """Start watching a counter; the returned function says how far it has moved.

    A labelled series does not exist until its first increment, so a missing
    sample reads as zero.
    """

    def read() -> float:
        return REGISTRY.get_sample_value(name, labels) or 0.0

    before = read()
    return lambda: read() - before
