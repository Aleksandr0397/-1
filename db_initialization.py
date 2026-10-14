"""Initialize each PostgreSQL store component once per successful process run.

Only initialization completion is cached. Callers must commit all schema changes
before their operation returns; failures remain retryable on the next call.
"""

import hashlib
import threading
from collections.abc import Callable


class _Initialization:
    def __init__(self):
        self.lock = threading.Lock()
        self.complete = False


_entries: dict[bytes, _Initialization] = {}
_entries_lock = threading.Lock()


def run_once(identity: str, group: str, operation: Callable[[], object]) -> None:
    """Run a committed initialization once for this identity and component.

    Concurrent calls for the same key wait for its first successful operation.
    Other keys proceed independently. Hashes keep connection credentials out of
    registry keys; neither the identity nor the operation is retained.
    """
    digest = hashlib.sha256()
    for value in (identity, group):
        encoded = value.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    key = digest.digest()
    with _entries_lock:
        entry = _entries.setdefault(key, _Initialization())
    with entry.lock:
        if not entry.complete:
            operation()
            entry.complete = True
