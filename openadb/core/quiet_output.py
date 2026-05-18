from __future__ import annotations

import contextlib
import io
import threading
from collections.abc import Iterator


_OUTPUT_REDIRECT_LOCK = threading.RLock()


@contextlib.contextmanager
def quiet_third_party_output() -> Iterator[None]:
    """Silence noisy third-party parsers that print diagnostics directly.

    Some APK parsing libraries use plain print() for recoverable binary resource
    warnings. Those messages are not OpenADB command logs and should not leak
    into the launcher console while Apps metadata is loading.
    """
    with _OUTPUT_REDIRECT_LOCK:
        sink = io.StringIO()
        with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
            yield
