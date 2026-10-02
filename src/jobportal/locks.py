"""One sender at a time.

The web app's background worker and a command you run by hand (``jobportal
send``, ``jobportal run``) can be alive at once. Each application is claimed
atomically, so none can go out twice; this lock additionally keeps the daily
caps and the gap between emails exact, because those are counted and then
acted on, which only works for one sender at a time.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

try:  # not available on Windows, where the claim alone has to do
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

log = logging.getLogger(__name__)


@contextmanager
def sender_lock(data_dir: Path) -> Iterator[bool]:
    """Yield ``True`` when this caller is the one sender, ``False`` when another is at work."""
    if fcntl is None:  # pragma: no cover
        yield True
        return
    data_dir.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(data_dir / ".send.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)
