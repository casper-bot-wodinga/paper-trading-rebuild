#!/usr/bin/env python3
"""
Rate-Limit-Aware Fetch Queue — SPEC-v3 §2.2, issue #199.

Provides a centralized fetch queue that respects API rate limits
(Alpaca 200 req/min) with priority tiers and exponential backoff.

Integrates with the data bus Scheduler system — each scheduler's
fetch_fn can be wrapped in a RateLimitedFetchQueue to enforce limits.

Architecture:
    RateLimitedFetchQueue
      ├── Priority tiers: URGENT > NORMAL > BACKGROUND
      ├── Sliding window rate tracker (max N requests per 60s)
      ├── Exponential backoff per source (base 2s, max 120s)
      ├── Queue depth tracking for diagnostics
      └── All thread-safe via threading.Lock

Usage:
    from src.fetch_queue import RateLimitedFetchQueue, FetchPriority

    queue = RateLimitedFetchQueue(max_per_minute=200)

    @queue.wrap(source="alpaca_quotes", priority=FetchPriority.NORMAL)
    def fetch_quotes(symbols):
        return api.get_quotes(symbols)

    # Or use directly:
    async with queue.throttle("alpaca"):
        result = await fetch_from_api()
"""

from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

log = logging.getLogger("fetch_queue")


# ═══════════════════════════════════════════════════════════════════════════════
# Priority & Types
# ═══════════════════════════════════════════════════════════════════════════════


class FetchPriority(Enum):
    """Priority tiers for fetch requests.

    URGENT — blocking requests (trading ticks, critical data)
    NORMAL — standard scheduled fetches (default)
    BACKGROUND — non-critical (cache warming, historical data)
    """
    URGENT = auto()
    NORMAL = auto()
    BACKGROUND = auto()


@dataclass
class FetchTask:
    """A single fetch task in the queue."""
    source: str
    priority: FetchPriority
    fn: Callable[..., Any]
    args: tuple = field(default_factory=tuple)
    kwargs: dict = field(default_factory=dict)
    submitted_at: float = 0.0
    started_at: float = 0.0
    completed_at: float = 0.0
    error: Optional[str] = None

    @property
    def wait_time(self) -> float:
        if self.started_at > 0:
            return self.started_at - self.submitted_at
        return 0.0

    @property
    def duration(self) -> float:
        if self.completed_at > 0 and self.started_at > 0:
            return self.completed_at - self.started_at
        return 0.0


@dataclass
class SourceState:
    """Internal state per API source."""
    backoff_until: float = 0.0     # timestamp when next request allowed
    backoff_seconds: float = 2.0   # current backoff duration
    consecutive_errors: int = 0
    total_requests: int = 0
    total_errors: int = 0
    last_error: Optional[str] = None
    last_success: float = 0.0


# ═══════════════════════════════════════════════════════════════════════════════
# Rate-Limit Queue
# ═══════════════════════════════════════════════════════════════════════════════


class RateLimitedFetchQueue:
    """Centralized rate-limit-aware fetch queue.

    Maintains a sliding window of API requests and delays requests
    when the configured rate limit is approached.

    Args:
        max_per_minute: Maximum requests per 60-second sliding window
                       (default: 200 = Alpaca free tier)
        min_interval: Minimum time between requests (default: 0.1s)
        backoff_base: Base backoff in seconds (default: 2)
        backoff_max: Maximum backoff in seconds (default: 120)
        track_stats: Track detailed per-source stats (default: True)
    """

    def __init__(
        self,
        max_per_minute: int = 200,
        min_interval: float = 0.1,
        backoff_base: float = 2.0,
        backoff_max: float = 120.0,
        track_stats: bool = True,
    ) -> None:
        self.max_per_minute = max_per_minute
        self.min_interval = min_interval
        self.backoff_base = backoff_base
        self.backoff_max = backoff_max
        self.track_stats = track_stats

        # Sliding window: timestamps of recent requests
        self._request_timestamps: deque = deque(maxlen=max_per_minute * 2)
        self._lock = threading.Lock()

        # Per-source backoff state
        self._sources: Dict[str, SourceState] = defaultdict(SourceState)

        # Queue depth tracking
        self._pending_count: int = 0
        self._max_pending: int = 0

    # ── Public API ────────────────────────────────────────────────────────

    def wrap(
        self,
        source: str,
        priority: FetchPriority = FetchPriority.NORMAL,
    ) -> Callable:
        """Decorator: wrap a fetch function with rate-limit enforcement.

        Usage:
            @queue.wrap(source="alpaca_quotes")
            def fetch_quotes():
                ...
        """
        def decorator(fn: Callable) -> Callable:
            def wrapper(*args: Any, **kwargs: Any) -> Any:
                task = FetchTask(
                    source=source,
                    priority=priority,
                    fn=fn,
                    args=args,
                    kwargs=kwargs,
                    submitted_at=time.time(),
                )
                self._wait_if_needed(task)
                task.started_at = time.time()
                try:
                    result = fn(*args, **kwargs)
                    task.completed_at = time.time()
                    self._mark_success(source)
                    return result
                except Exception as e:
                    task.completed_at = time.time()
                    task.error = str(e)
                    self._mark_error(source, str(e))
                    raise
            return wrapper
        return decorator

    def throttle(self, source: str) -> "_ThrottleContext":
        """Context manager: throttle a single fetch call.

        Usage:
            with queue.throttle("alpaca"):
                result = api.get_quotes()
        """
        return self._ThrottleContext(self, source)

    def wait_until_ready(self, source: str) -> float:
        """Wait until the source is not rate-limited. Returns wait time."""
        with self._lock:
            wait = self._calculate_wait(source)
        if wait > 0:
            time.sleep(wait)
        return wait

    def is_rate_limited(self, source: Optional[str] = None) -> bool:
        """Check if a source is currently rate-limited.

        Args:
            source: Source name, or None to check global rate only

        Returns:
            True if requests should be delayed
        """
        with self._lock:
            if source and source in self._sources:
                state = self._sources[source]
                if state.backoff_until > time.time():
                    return True
            # Check sliding window
            now = time.time()
            window_start = now - 60
            recent = sum(1 for t in self._request_timestamps if t > window_start)
            return recent >= self.max_per_minute

    def stats(self, source: Optional[str] = None) -> Dict[str, Any]:
        """Get statistics for diagnostics.

        Args:
            source: Optional source name for per-source stats

        Returns:
            Dict with stats including rate utilization, backoff state, etc.
        """
        with self._lock:
            now = time.time()
            window_start = now - 60
            recent = list(self._request_timestamps)
            recent_count = sum(1 for t in recent if t > window_start)

            base = {
                "rate_utilization_pct": round(
                    (recent_count / self.max_per_minute) * 100, 1
                ),
                "recent_requests_60s": recent_count,
                "max_per_minute": self.max_per_minute,
                "pending_count": self._pending_count,
                "max_pending": self._max_pending,
            }

            if source and source in self._sources:
                s = self._sources[source]
                base["source"] = source
                base["backoff_remaining"] = max(0, s.backoff_until - now)
                base["consecutive_errors"] = s.consecutive_errors
                base["total_requests"] = s.total_requests
                base["total_errors"] = s.total_errors
                base["last_error"] = s.last_error

            return base

    def reset(self, source: Optional[str] = None) -> None:
        """Reset rate limit state for a source or all sources.

        Args:
            source: Source to reset, or None for all
        """
        with self._lock:
            if source and source in self._sources:
                self._sources[source].backoff_until = 0
                self._sources[source].backoff_seconds = self.backoff_base
                self._sources[source].consecutive_errors = 0
            elif source is None:
                self._sources.clear()
                self._request_timestamps.clear()

    # ── Internal ──────────────────────────────────────────────────────────

    def _calculate_wait(self, source: str) -> float:
        """Calculate wait time in seconds before next request is allowed.

        Considers:
          1. Per-source backoff
          2. Global sliding window rate limit
          3. Minimum interval between requests

        Returns:
            Seconds to wait (0 = no wait needed)
        """
        now = time.time()

        # 1. Per-source backoff
        state = self._sources.get(source)
        if state and state.backoff_until > now:
            return state.backoff_until - now

        # 2. Global sliding window
        window_start = now - 60
        recent = sum(1 for t in self._request_timestamps if t > window_start)
        if recent >= self.max_per_minute:
            # Find when the oldest request in the window will expire
            oldest = min(t for t in self._request_timestamps if t > window_start)
            return oldest + 60 - now + 0.1  # small buffer

        # 3. Minimum interval
        if self._request_timestamps:
            last_req = max(self._request_timestamps)
            since_last = now - last_req
            if since_last < self.min_interval:
                return self.min_interval - since_last

        return 0.0

    def _wait_if_needed(self, task: FetchTask) -> None:
        """Block until the task can proceed according to rate limits."""
        wait = self._calculate_wait(task.source)
        if wait > 0:
            log.debug("Rate limit: waiting %.1fs for %s", wait, task.source)
            time.sleep(wait)

        with self._lock:
            # Low-priority tasks may wait longer under high load
            if task.priority == FetchPriority.BACKGROUND:
                # Add a small extra delay for background tasks
                extra = 0.5
                if self._get_recent_count() > self.max_per_minute * 0.8:
                    extra = 2.0
                    log.debug("Background task %s delayed extra %.1fs (high load)",
                              task.source, extra)
                    time.sleep(extra)

            self._request_timestamps.append(time.time())

    def _get_recent_count(self) -> int:
        """Get count of requests in the current 60s window."""
        now = time.time()
        return sum(1 for t in self._request_timestamps if t > now - 60)

    def _mark_success(self, source: str) -> None:
        """Record a successful fetch for a source."""
        if not self.track_stats:
            return
        with self._lock:
            state = self._sources[source]
            state.total_requests += 1
            state.consecutive_errors = 0
            state.backoff_seconds = self.backoff_base
            state.backoff_until = 0.0  # reset backoff on success
            state.last_success = time.time()
            state.last_error = None

    def _mark_error(self, source: str, error: str) -> None:
        """Record a fetch error and apply exponential backoff."""
        if not self.track_stats:
            return
        with self._lock:
            state = self._sources[source]
            state.total_requests += 1
            state.total_errors += 1
            state.consecutive_errors += 1
            state.last_error = error

            # Exponential backoff
            backoff = min(
                self.backoff_base * (2 ** (state.consecutive_errors - 1)),
                self.backoff_max,
            )
            state.backoff_seconds = backoff
            state.backoff_until = time.time() + backoff

            log.warning(
                "Backoff %s: %.1fs (error #%d: %s)",
                source, backoff, state.consecutive_errors, error,
            )

    # ── Context Manager ───────────────────────────────────────────────────

    class _ThrottleContext:
        """Context manager for throttling a single fetch."""

        def __init__(self, queue: "RateLimitedFetchQueue", source: str) -> None:
            self.queue = queue
            self.source = source
            self.started_at: float = 0.0

        def __enter__(self) -> "_ThrottleContext":
            self.queue.wait_until_ready(self.source)
            self.started_at = time.time()
            return self

        def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
            # Record the request timestamp for rate tracking
            with self.queue._lock:
                self.queue._request_timestamps.append(time.time())
            if exc_type is None:
                self.queue._mark_success(self.source)
            else:
                self.queue._mark_error(self.source, str(exc_val or ""))


# ═══════════════════════════════════════════════════════════════════════════════
# Integration helper: create a rate-limited wrapper for scheduler fetch functions
# ═══════════════════════════════════════════════════════════════════════════════


def make_rate_limited_fetch(
    fetch_fn: Callable,
    source: str,
    queue: RateLimitedFetchQueue,
    priority: FetchPriority = FetchPriority.NORMAL,
) -> Callable:
    """Wrap a scheduler fetch function with rate-limit enforcement.

    Usage:
        queue = RateLimitedFetchQueue(max_per_minute=200)
        sched = Scheduler(
            name="quotes",
            intervals=config["quotes"],
            fetch_fn=make_rate_limited_fetch(fetch_quotes, "alpaca_quotes", queue),
        )
    """
    wrapper = queue.wrap(source=source, priority=priority)
    return wrapper(fetch_fn)