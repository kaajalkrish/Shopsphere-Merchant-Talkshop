"""
Order tracking by Order ID + email (website and chat app): limits that stop guessing.

  per caller      30 lookups / 15 min (the IP; a chat app's calls share its IP, so this is generous)
  per Order ID    5 failed lookups / 15 min, then that Order ID is locked for the rest of the window,
                  even with the right email: guessing emails for a known Order ID is pointless.

A refused or failed lookup gives the same generic answer as a missing order, so the limits never
reveal whether an Order ID exists. In-memory (per server process): a restart clears it.
"""
import threading
import time
from collections import defaultdict, deque
from typing import Optional

WINDOW = 15 * 60
CALLER_LIMIT = 30
ORDER_FAILURE_LIMIT = 5

_lock = threading.Lock()
_calls: dict[str, deque] = defaultdict(deque)
_failures: dict[str, deque] = defaultdict(deque)


def _trim(q: deque, now: float) -> deque:
    while q and q[0] < now - WINDOW:
        q.popleft()
    return q


def allow_tracking(caller: Optional[str], order_id: str) -> bool:
    """Count this lookup; False when the caller or the Order ID is over its limit."""
    now = time.time()
    key = (order_id or "").strip().upper()
    with _lock:
        calls = _trim(_calls[caller or "-"], now)
        if len(calls) >= CALLER_LIMIT or len(_trim(_failures[key], now)) >= ORDER_FAILURE_LIMIT:
            return False
        calls.append(now)
        return True


def tracking_failed(order_id: str) -> None:
    with _lock:
        _failures[(order_id or "").strip().upper()].append(time.time())


def reset() -> None:
    """Tests only."""
    with _lock:
        _calls.clear()
        _failures.clear()
