"""
src/core/cache.py
-----------------
Thread-safe in-memory cache with TTL, LRU eviction and rolling expiration.
Provides dedicated instances for:
  - query_plan_cache (24h TTL)
  - color_decision_cache (24h TTL)
  - search_snapshot_cache (10m rolling TTL, max 30m)
"""
from __future__ import annotations
import threading
import time
from collections import OrderedDict
from typing import Any, Optional, Dict, Tuple


class TTLCache:
    """
    Thread-safe LRU cache with TTL expiration.
    Supports rolling TTL extension on access (up to max_lifetime).
    """

    def __init__(self, maxsize: int = 1000, default_ttl_seconds: float = 3600.0, max_lifetime_seconds: Optional[float] = None):
        self._maxsize = maxsize
        self._default_ttl = default_ttl_seconds
        self._max_lifetime = max_lifetime_seconds or (default_ttl_seconds * 3.0)
        self._lock = threading.Lock()
        # Storage: key -> (value, expires_at, created_at)
        self._store: OrderedDict[str, Tuple[Any, float, float]] = OrderedDict()

    def get(self, key: str, touch: bool = False) -> Optional[Any]:
        """
        Retrieve value if present and not expired.
        If touch=True and item is valid, extends expiration by default_ttl up to max_lifetime.
        """
        now = time.time()
        with self._lock:
            if key not in self._store:
                return None

            value, expires_at, created_at = self._store[key]
            if now > expires_at:
                del self._store[key]
                return None

            if touch:
                new_expires = min(now + self._default_ttl, created_at + self._max_lifetime)
                self._store[key] = (value, new_expires, created_at)

            # Move to end for LRU tracking
            self._store.move_to_end(key)
            return value

    def set(self, key: str, value: Any, ttl_seconds: Optional[float] = None) -> None:
        """
        Insert or update a key with specified or default TTL.
        """
        now = time.time()
        ttl = ttl_seconds if ttl_seconds is not None else self._default_ttl
        with self._lock:
            if key in self._store:
                _, _, created_at = self._store[key]
                self._store[key] = (value, now + ttl, created_at)
                self._store.move_to_end(key)
            else:
                if len(self._store) >= self._maxsize:
                    # Evict oldest
                    self._store.popitem(last=False)
                self._store[key] = (value, now + ttl, now)

    def delete(self, key: str) -> None:
        with self._lock:
            self._store.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._store.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._store)


# ---------------------------------------------------------------------------
# Global application cache instances
# ---------------------------------------------------------------------------

# Search Query Plan Cache: 24 hours TTL, max 1000 plans
query_plan_cache = TTLCache(
    maxsize=1000,
    default_ttl_seconds=86400.0,
    max_lifetime_seconds=86400.0,
)

# Color compatibility decision cache: 24 hours TTL, max 5000 pairs
color_decision_cache = TTLCache(
    maxsize=5000,
    default_ttl_seconds=86400.0,
    max_lifetime_seconds=86400.0,
)

# Search Results Snapshot Cache: 10 minutes rolling TTL, max 30 minutes
search_snapshot_cache = TTLCache(
    maxsize=100,
    default_ttl_seconds=600.0,
    max_lifetime_seconds=1800.0,
)
