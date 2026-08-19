"""Keep the root logger from leaking between scheduler tests.

``main()`` legitimately calls ``configure_logging``, which attaches a handler
bound to ``sys.stdout`` **as it exists at that moment**. Under pytest that is
the capture object for the test running at the time. The handler is on the root
logger, so it outlives the test — but its stream does not, and every later log
record raises ``ValueError: I/O operation on closed file``.

That was not cosmetic. Python's logging prints the whole "--- Logging error ---"
traceback for each failed record, which is far more expensive than the log line
itself; on ARM64/QEMU that turned an idle scheduler loop into something that
measurably burned CPU, and the no-spin test failed for a reason that had nothing
to do with the loop.

The error is not suppressed here: the handler is removed and the previous
handlers restored, so nothing is ever writing to a dead stream in the first
place.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator

import pytest


@pytest.fixture(autouse=True)
def restore_root_logging() -> Iterator[None]:
    """Snapshot the root logger, and put it back exactly as it was."""
    root = logging.getLogger()
    handlers = list(root.handlers)
    level = root.level
    try:
        yield
    finally:
        for handler in list(root.handlers):
            if handler not in handlers:
                root.removeHandler(handler)
                # Detach the dead stream too, so a stray reference cannot write
                # to it later.
                handler.close()
        root.handlers = handlers
        root.setLevel(level)
