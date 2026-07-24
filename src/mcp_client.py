#!/usr/bin/env python3
"""
MCP Client Bridge — generic MCP client wrapper for the paper trading data bus.

Provides synchronous wrappers around the async MCP Python SDK for:
- stdio transport (Alpaca, AlphaAI, Praesentire)
- SSE transport (Alpha Vantage, LoneStarOracle)
- Tool discovery, tool calling
- Connection pooling with health checks
- Graceful degradation (timeouts, reconnection, error handling)

Usage:
    from mcp_client import MCPConnectionManager, MCPConnectionConfig, get_manager

    manager = get_manager()
    manager.register(MCPConnectionConfig(
        name="lonestar",
        transport="sse",
        url="https://mcp.lonestaroracle.xyz/sse",
    ))

    # List tools
    tools = manager.list_tools("lonestar")

    # Call a tool
    result = manager.call_tool("lonestar", "some_tool_name", {"param": "value"})

    # Health status
    status = manager.status()

Architecture:
    ┌──────────────────────────────────────────────────────────────────┐
    │  data_bus.py (sync, threaded)                                    │
    │  ┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐  │
    │  │ /quotes         │  │ /fundamentals   │  │ /mcp-status     │  │
    │  └────────┬────────┘  └────────┬────────┘  └────────┬────────┘  │
    │           │                    │                    │           │
    │           ▼                    ▼                    ▼           │
    │  ┌──────────────────────────────────────────────────────────────┤
    │  │              MCPConnectionManager (singleton)                 │
    │  │  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐        │
    │  │  │AlphaVan. │ │LoneStar  │ │AlphaAI   │ │Praesent. │  ...   │
    │  │  │std::sync │ │std::sync │ │std::sync │ │std::sync │        │
    │  │  └────┬─────┘ └────┬─────┘ └────┬─────┘ └────┬─────┘        │
    │  └───────┼────────────┼────────────┼────────────┼──────────────┤
    │          │            │            │            │               │
    │  ┌───────┼────────────┼────────────┼────────────┼──────────────┤
    │  │       ▼            ▼            ▼            ▼               │
    │  │                  MCPClient (sync wrapper)                    │
    │  │         asyncio bridge → MCP Python SDK (async)              │
    │  └──────────────────────────────────────────────────────────────┤
    └──────────────────────────────────────────────────────────────────┘
"""

import asyncio
import os
import queue
import threading
import time
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any, Tuple

try:
    from mcp import ClientSession
    from mcp.client.stdio import stdio_client, StdioServerParameters
    from mcp.client.sse import sse_client
    from mcp.client.streamable_http import streamablehttp_client
    HAS_MCP = True
except ImportError:
    HAS_MCP = False
    ClientSession = None  # type: ignore
    stdio_client = None  # type: ignore
    StdioServerParameters = None  # type: ignore
    sse_client = None  # type: ignore
    streamablehttp_client = None  # type: ignore

log = logging.getLogger("databus.mcp")

# ═══════════════════════════════════════════════════════════════════════════════
# Configuration
# ═══════════════════════════════════════════════════════════════════════════════

DEFAULT_CONNECT_TIMEOUT = 30.0   # seconds
DEFAULT_CALL_TIMEOUT = 60.0      # seconds
DEFAULT_RECONNECT_DELAY = 5.0    # seconds
MAX_RECONNECT_ATTEMPTS = 3
DEFAULT_TOOLS_CACHE_TTL = 3600.0  # 1 hour

# ═══════════════════════════════════════════════════════════════════════════════
# Data Classes
# ═══════════════════════════════════════════════════════════════════════════════


@dataclass
class MCPConnectionConfig:
    """Configuration for a single MCP server connection."""

    name: str
    transport: str  # "stdio", "sse", or "streamable_http"

    # ── stdio ──
    command: Optional[str] = None
    args: Optional[List[str]] = None
    env: Optional[Dict[str, str]] = None

    # ── sse ──
    url: Optional[str] = None
    headers: Optional[Dict[str, str]] = None

    # ── common ──
    enabled: bool = True
    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT
    call_timeout: float = DEFAULT_CALL_TIMEOUT
    protocol_version: Optional[str] = None  # None = use latest

    def validate(self) -> Optional[str]:
        """Validate configuration. Returns error string or None."""
        if self.transport == "stdio":
            if not self.command:
                return f"stdio transport requires 'command' for {self.name}"
        elif self.transport == "sse":
            if not self.url:
                return f"sse transport requires 'url' for {self.name}"
        elif self.transport == "streamable_http":
            if not self.url:
                return f"streamable_http transport requires 'url' for {self.name}"
        else:
            return f"unknown transport '{self.transport}' for {self.name} (must be stdio, sse, or streamable_http)"
        return None


@dataclass
class MCPConnectionState:
    """Runtime state for a managed MCP connection."""

    config: MCPConnectionConfig
    connected: bool = False
    last_health_check: float = 0.0
    last_error: Optional[str] = None
    error_count: int = 0
    tools_cache: Optional[List[dict]] = None
    tools_cache_time: float = 0.0
    tools_cache_ttl: float = DEFAULT_TOOLS_CACHE_TTL

    # Internal — set by connect/disconnect
    _session: Optional[ClientSession] = field(default=None, repr=False)
    _read: Any = field(default=None, repr=False)
    _write: Any = field(default=None, repr=False)
    _session_id: Any = field(default=None, repr=False)  # streamable HTTP session ID
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)


# ═══════════════════════════════════════════════════════════════════════════════
# Errors
# ═══════════════════════════════════════════════════════════════════════════════


class MCPClientError(Exception):
    """Base error for MCP client operations."""


class MCPConnectionError(MCPClientError):
    """Connection-level error."""


class MCPToolError(MCPClientError):
    """Tool-call-level error."""


# ═══════════════════════════════════════════════════════════════════════════════
# Sync-Aware Async to Sync Bridge
# ═══════════════════════════════════════════════════════════════════════════════

# A single daemon thread running its own event loop — used when the calling
# thread does not have one.  This avoids creating/closing event loops per call.
_event_loop_thread: Optional[threading.Thread] = None
_event_loop: Optional[asyncio.AbstractEventLoop] = None
_event_loop_lock = threading.Lock()


def _get_or_create_loop() -> asyncio.AbstractEventLoop:
    """Return an event loop that async MCP calls can run on.

    If the calling thread already has a running loop (rare, but possible inside
    an async Flask server), it is returned directly.  Otherwise a shared
    background loop is lazily created and used for all sync→async bridging.
    """
    global _event_loop, _event_loop_thread
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        pass

    with _event_loop_lock:
        if _event_loop is not None and not _event_loop.is_closed():
            return _event_loop

        _event_loop = asyncio.new_event_loop()
        _event_loop_thread = threading.Thread(
            target=_event_loop.run_forever,
            name="mcp-event-loop",
            daemon=True,
        )
        _event_loop_thread.start()
        return _event_loop


def _shutdown_loop():
    """Shut down the shared background event loop (called at process exit)."""
    global _event_loop, _event_loop_thread
    with _event_loop_lock:
        if _event_loop is not None and not _event_loop.is_closed():
            _event_loop.call_soon_threadsafe(_event_loop.stop)
            _event_loop_thread = None
            try:
                _event_loop.close()
            except Exception:
                pass
            _event_loop = None


def _run_async(coro, timeout: float = DEFAULT_CALL_TIMEOUT, loop: asyncio.AbstractEventLoop = None) -> Any:
    """Run a coroutine synchronously via the shared background loop.

    The future is scheduled on the loop and the calling thread blocks until
    completion or timeout. If loop is provided, runs on that loop instead
    of the shared background loop (needed for streamable HTTP sessions).
    """
    target_loop = loop or _get_or_create_loop()
    if loop is not None:
        # For dedicated loops (streamable HTTP), run directly on that loop
        # using run_coroutine_threadsafe
        future = asyncio.run_coroutine_threadsafe(
            asyncio.wait_for(coro, timeout=timeout), target_loop
        )
        try:
            return future.result(timeout=timeout + 5)
        except asyncio.TimeoutError:
            future.cancel()
            raise MCPClientError(f"Operation timed out after {timeout}s")
        except Exception as e:
            raise MCPClientError(f"Async execution failed: {e}") from e
    else:
        future = asyncio.run_coroutine_threadsafe(
            asyncio.wait_for(coro, timeout=timeout), target_loop
        )
        try:
            return future.result(timeout=timeout + 5)
        except asyncio.TimeoutError:
            future.cancel()
            raise MCPClientError(f"Operation timed out after {timeout}s")
        except Exception as e:
            raise MCPClientError(f"Async execution failed: {e}") from e


# ═══════════════════════════════════════════════════════════════════════════════
# MCPClient — Sync Bridge
# ═══════════════════════════════════════════════════════════════════════════════


class MCPClient:
    """Synchronous MCP client wrapper.

    Thread-safe.  All async MCP operations are bridged through a shared
    background asyncio event loop so that callers in threaded (Flask/werkzeug)
    contexts can use the client directly without `async`/`await`.
    """

    # ── Lifecycle ─────────────────────────────────────────────────────────

    def connect_stdio(
        self,
        command: str,
        args: Optional[List[str]] = None,
        env: Optional[Dict[str, str]] = None,
        timeout: float = DEFAULT_CONNECT_TIMEOUT,
    ) -> Tuple[ClientSession, Any, Any]:
        """Connect to an MCP server via stdio transport.

        Uses anyio with the trio backend on a dedicated thread to avoid the
        anyio-asyncio cross-task cancel scope issue that plagues the MCP
        SDK's stdio transport on asyncio backends (anyio CancelScopes are
        entered in one asyncio task but exited in another during cleanup).

        A thread-safe queue bridges the async world to synchronous callers.

        Returns (session, read_transport, write_transport).
        The session gains attributes:
          - _stdio_call_queue — queue.Queue for submitting tool calls
          - _stdio_runner      — threading.Thread (alive flag for disconnect)
        """
        params = StdioServerParameters(
            command=command,
            args=args or [],
            env=env or {},
        )

        _call_queue: queue.Queue = queue.Queue()
        _result = {}
        _error = {}
        _ready = threading.Event()

        def _runner():
            import anyio as _anyio

            async def _main():
                # ── Connect (proper async with to avoid trio nursery corruption) ─
                async with stdio_client(params) as transport:
                    read, write = transport
                    async with ClientSession(read, write) as session:
                        await session.initialize()

                        session._stdio_call_queue = _call_queue  # type: ignore[attr-defined]
                        _result['value'] = (session, read, write)
                        _ready.set()

                        # ── Process tool calls (same async context) ─
                        while True:
                            try:
                                item = _call_queue.get_nowait()
                            except queue.Empty:
                                await _anyio.sleep(0.05)
                                continue
                            fn_name, args_tuple, result_q = item
                            if fn_name is None:  # sentinel: shut down
                                break
                            try:
                                fn = getattr(session, fn_name)
                                result = await fn(*args_tuple)
                                result_q.put(result)
                            except Exception as e:
                                result_q.put(e)

            try:
                _anyio.run(_main, backend="trio")
            except Exception as e:
                _error['e'] = e
                _ready.set()

        t = threading.Thread(target=_runner, daemon=True, name="mcp-stdio")
        t.start()
        if not _ready.wait(timeout=timeout + 5):
            raise MCPClientError(
                f"Stdio connection to {command} timed out after {timeout}s"
            )
        if 'e' in _error:
            raise MCPClientError(
                f"Stdio connection failed: {_error['e']}"
            ) from _error['e']
        if 'value' not in _result:
            raise MCPClientError(
                f"Stdio connection to {command} failed (no result)"
            )
        session, read, write = _result['value']
        session._stdio_runner = t  # type: ignore[attr-defined]
        return session, read, write

    def connect_sse(
        self,
        url: str,
        headers: Optional[Dict[str, str]] = None,
        timeout: float = DEFAULT_CONNECT_TIMEOUT,
    ) -> Tuple[ClientSession, Any, Any]:
        """Connect to an MCP server via SSE transport.

        Returns (session, read_transport, write_transport).
        """
        async def _connect():
            read, write = await sse_client(
                url, headers=headers, timeout=timeout
            ).__aenter__()
            session = ClientSession(read, write)
            await session.__aenter__()
            await session.initialize()
            return session, read, write

        return _run_async(_connect(), timeout=timeout)

    def connect_streamable_http(
        self,
        url: str,
        headers: Optional[Dict[str, str]] = None,
        timeout: float = DEFAULT_CONNECT_TIMEOUT,
    ) -> Tuple[ClientSession, Any, Any, Any]:
        """Connect to an MCP server via Streamable HTTP transport.

        Returns (session, read_transport, write_transport, get_session_id).

        Streamable HTTP uses anyio task groups that are incompatible with
        run_coroutine_threadsafe (cross-task cancel scope). We run the full
        connection lifecycle on a dedicated event loop in a daemon thread.
        All subsequent tool calls on this session must also go through this
        dedicated loop, which is managed by MCPConnectionManager.call_tool.
        """
        import threading

        _result = {}
        _error = {}
        _ready = threading.Event()

        def _runner():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                async def _connect():
                    ctx = streamablehttp_client(url, headers=headers, timeout=timeout)
                    read, write, get_session_id = await ctx.__aenter__()
                    session = ClientSession(read, write)
                    await session.__aenter__()
                    await session.initialize()
                    return session, read, write, get_session_id, ctx, loop

                result = loop.run_until_complete(
                    asyncio.wait_for(_connect(), timeout=timeout)
                )
                session, read, write, sid, ctx, l = result
                session._streamable_loop = l   # type: ignore[attr-defined]
                session._streamable_ctx = ctx  # type: ignore[attr-defined]
                _result['value'] = (session, read, write, sid)
                _ready.set()
                # Keep loop alive for tool calls; stop_loop kills it
                loop.run_forever()
            except Exception as e:
                _error['e'] = e
                _ready.set()
            finally:
                # Clean up context if possible
                try:
                    if 'value' in _result:
                        session = _result['value'][0]
                        ctx = getattr(session, '_streamable_ctx', None)
                        if ctx:
                            loop.run_until_complete(ctx.__aexit__(None, None, None))
                except Exception:
                    pass
                loop.close()

        t = threading.Thread(target=_runner, daemon=True, name="mcp-streamable")
        t.start()
        if not _ready.wait(timeout=timeout + 5):
            raise MCPClientError(
                f"Streamable HTTP connection to {url} timed out after {timeout}s"
            )
        if 'e' in _error:
            raise MCPClientError(
                f"Streamable HTTP connection failed: {_error['e']}"
            ) from _error['e']
        if 'value' not in _result:
            raise MCPClientError(
                f"Streamable HTTP connection to {url} failed (no result)"
            )
        return _result['value']

    def disconnect(
        self,
        session: ClientSession,
        read_transport: Any,
        write_transport: Any,
        timeout: float = 10.0,
    ):
        """Gracefully disconnect from an MCP server."""

        # ── Queue-based path (stdio with dedicated thread) ──
        call_queue: Optional[queue.Queue] = getattr(session, '_stdio_call_queue', None)
        runner_thread: Optional[threading.Thread] = getattr(session, '_stdio_runner', None)
        if call_queue is not None and runner_thread is not None and runner_thread.is_alive():
            # Send sentinel to break the processing loop
            try:
                call_queue.put_nowait((None, (), queue.Queue()))
            except queue.Full:
                pass
            # The thread will exit when _main() returns after processing
            # the sentinel. Wait briefly for clean shutdown.
            runner_thread.join(timeout=5)
            return

        # ── Async path (sse, streamable_http) ──
        async def _disconnect():
            # Close session first
            try:
                await session.__aexit__(None, None, None)
            except Exception:
                pass
            # Close transport
            try:
                # Both read and write are the two halves of the same transport
                # context manager; we need to close the underlying transport.
                # For stdio: the context manager's __aexit__ kills the process.
                # The pair (read, write) is the yielded tuple; the context
                # manager object was already consumed by __aenter__. We rely
                # on session closure to clean up the transport.
                pass
            except Exception:
                pass

        try:
            _run_async(_disconnect(), timeout=timeout)
        except Exception as e:
            log.debug("Error during disconnect: %s", e)

    # ── Tool Operations ───────────────────────────────────────────────────

    def call_tool(
        self,
        session: ClientSession,
        tool_name: str,
        params: Optional[Dict[str, Any]] = None,
        timeout: float = DEFAULT_CALL_TIMEOUT,
        loop: asyncio.AbstractEventLoop = None,
    ) -> Dict[str, Any]:
        """Call an MCP tool and return parsed JSON response.

        Returns:
            Dict with keys: content, isError, structuredContent

        The *content* list contains objects like:
            {"type": "text", "text": "..."}
            {"type": "resource", "data": "..."}
        """

        # ── Queue-based path (stdio with dedicated thread) ──
        call_queue: Optional[queue.Queue] = getattr(session, '_stdio_call_queue', None)
        if call_queue is not None:
            result_q: queue.Queue = queue.Queue(maxsize=1)
            call_queue.put(("call_tool", (tool_name, params or {}), result_q))
            try:
                raw_result = result_q.get(timeout=timeout)
            except queue.Empty:
                raise MCPClientError(
                    f"Tool '{tool_name}' timed out after {timeout}s"
                )
            if isinstance(raw_result, Exception):
                raise raw_result
            # Normalize result (same as async path below)
            content_parts: List[Dict[str, Any]] = []
            for block in raw_result.content:
                item: Dict[str, Any] = {}
                if hasattr(block, "text"):
                    item["type"] = "text"
                    item["text"] = block.text
                elif hasattr(block, "data"):
                    item["type"] = getattr(block, "type", "resource")
                    raw = str(block.data)
                    item["data"] = raw[:500]
                    if len(raw) > 500:
                        item["data"] += "..."
                else:
                    item["type"] = "unknown"
                    item["data"] = str(block)[:500]
                content_parts.append(item)
            return {
                "content": content_parts,
                "isError": raw_result.isError,
                "structuredContent": raw_result.structuredContent,
            }

        # ── Async path (sse, streamable_http, shared-loop stdio) ──
        async def _call():
            result = await session.call_tool(tool_name, arguments=params or {})

            # Normalize content blocks into plain dicts
            content_parts: List[Dict[str, Any]] = []
            for block in result.content:
                item: Dict[str, Any] = {}
                # Text content
                if hasattr(block, "text"):
                    item["type"] = "text"
                    item["text"] = block.text
                # Image / embedded resource
                elif hasattr(block, "data"):
                    item["type"] = getattr(block, "type", "resource")
                    # Truncate binary data to avoid explosion
                    raw = str(block.data)
                    item["data"] = raw[:500]
                    if len(raw) > 500:
                        item["data"] += "..."
                else:
                    item["type"] = "unknown"
                    item["data"] = str(block)[:500]

                content_parts.append(item)

            return {
                "content": content_parts,
                "isError": result.isError,
                "structuredContent": result.structuredContent,
            }

        return _run_async(_call(), timeout=timeout, loop=loop)

    def list_tools(
        self,
        session: ClientSession,
        timeout: float = DEFAULT_CALL_TIMEOUT,
        loop: asyncio.AbstractEventLoop = None,
    ) -> List[Dict[str, Any]]:
        """Discover available tools on an MCP server.

        Returns list of tool descriptors with keys: name, description, inputSchema.
        """
        # ── Queue-based path (stdio with dedicated thread) ──
        call_queue: Optional[queue.Queue] = getattr(session, '_stdio_call_queue', None)
        if call_queue is not None:
            result_q: queue.Queue = queue.Queue(maxsize=1)
            call_queue.put(("list_tools", (), result_q))
            try:
                raw_result = result_q.get(timeout=timeout)
            except queue.Empty:
                raise MCPClientError(f"list_tools timed out after {timeout}s")
            if isinstance(raw_result, Exception):
                raise raw_result
            tools: List[Dict[str, Any]] = []
            for t in raw_result.tools:
                tools.append({
                    "name": t.name,
                    "description": t.description or "",
                    "inputSchema": (
                        t.inputSchema
                        if hasattr(t, "inputSchema") and t.inputSchema
                        else None
                    ),
                })
            return tools

        # ── Async path ──
        async def _list():
            result = await session.list_tools()
            tools: List[Dict[str, Any]] = []
            for t in result.tools:
                tools.append({
                    "name": t.name,
                    "description": t.description or "",
                    "inputSchema": (
                        t.inputSchema
                        if hasattr(t, "inputSchema") and t.inputSchema
                        else None
                    ),
                })
            return tools

        return _run_async(_list(), timeout=timeout, loop=loop)

    def ping(self, session: ClientSession, timeout: float = 10.0, loop: asyncio.AbstractEventLoop = None) -> bool:
        """Send a ping to verify connection health. Returns True on success."""

        # ── Queue-based path (stdio with dedicated thread) ──
        call_queue: Optional[queue.Queue] = getattr(session, '_stdio_call_queue', None)
        if call_queue is not None:
            result_q: queue.Queue = queue.Queue(maxsize=1)
            call_queue.put(("send_ping", (), result_q))
            try:
                raw = result_q.get(timeout=timeout)
                if isinstance(raw, Exception):
                    return False
                return True
            except queue.Empty:
                return False

        # ── Async path ──
        async def _ping():
            await session.send_ping()
            return True

        try:
            return _run_async(_ping(), timeout=timeout, loop=loop)
        except Exception:
            return False


# ═══════════════════════════════════════════════════════════════════════════════
# MCPConnectionManager — Singleton
# ═══════════════════════════════════════════════════════════════════════════════


class MCPConnectionManager:
    """Singleton manager for multiple MCP server connections.

    Responsibilities:
    - Connection lifecycle (connect, health-check, reconnect, disconnect)
    - Tool discovery with caching
    - Graceful degradation (returns None on failure, callers fall back)
    - Thread-safe access to all connection state
    """

    _instance: Optional["MCPConnectionManager"] = None
    _instance_lock = threading.Lock()

    def __new__(cls) -> "MCPConnectionManager":
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    obj = super().__new__(cls)
                    obj._initialized = False
                    cls._instance = obj
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        self._client = MCPClient()
        self._connections: Dict[str, MCPConnectionState] = {}
        self._lock = threading.Lock()
        self._shutdown = False
        log.info("MCPConnectionManager singleton initialized")

    # ── Registration ──────────────────────────────────────────────────────

    def register(self, config: MCPConnectionConfig) -> "MCPConnectionManager":
        """Register a connection configuration. Does not connect yet.

        Returns self for chaining.
        """
        err = config.validate()
        if err:
            log.warning("MCP config validation failed: %s", err)

        with self._lock:
            state = MCPConnectionState(config=config)
            self._connections[config.name] = state
            log.info(
                "MCP registered: %s (transport=%s, enabled=%s)",
                config.name,
                config.transport,
                config.enabled,
            )
        return self

    # ── Connection Lifecycle ──────────────────────────────────────────────

    def connect(self, name: str) -> bool:
        """Connect to a registered MCP server by name.

        Returns True if connected successfully, False otherwise.
        Safe to call when already connected (no-op).
        """
        with self._lock:
            state = self._connections.get(name)
        if state is None:
            log.error("MCP: unknown server '%s'", name)
            return False

        cfg = state.config
        if not cfg.enabled:
            log.debug("MCP %s: disabled, skipping connect", name)
            return False

        with state._lock:
            # Already connected — ping to verify
            if state.connected and state._session is not None:
                if self._client.ping(state._session):
                    return True
                # Ping failed — connection is stale
                log.warning("MCP %s: ping failed, reconnecting", name)
                state.connected = False

            # Clean up any stale objects
            if state._session is not None:
                try:
                    self._client.disconnect(
                        state._session, state._read, state._write
                    )
                except Exception as e:
                    log.debug("MCP %s: cleanup error: %s", name, e)
                state._session = None
                state._read = None
                state._write = None
                state._session_id = None

            # Attempt connection
            try:
                if cfg.transport == "stdio":
                    session, read_t, write_t = self._client.connect_stdio(
                        command=cfg.command,  # type: ignore[arg-type]
                        args=cfg.args or [],
                        env=cfg.env,
                        timeout=cfg.connect_timeout,
                    )
                    session_id = None
                elif cfg.transport == "sse":
                    session, read_t, write_t = self._client.connect_sse(
                        url=cfg.url,  # type: ignore[arg-type]
                        headers=cfg.headers,
                        timeout=cfg.connect_timeout,
                    )
                    session_id = None
                elif cfg.transport == "streamable_http":
                    session, read_t, write_t, session_id = (
                        self._client.connect_streamable_http(
                            url=cfg.url,  # type: ignore[arg-type]
                            headers=cfg.headers,
                            timeout=cfg.connect_timeout,
                        )
                    )
                else:
                    log.error("MCP %s: unknown transport '%s'", name, cfg.transport)
                    return False

                state._session = session
                state._read = read_t
                state._write = write_t
                state._session_id = session_id
                state.connected = True
                state.last_error = None
                state.error_count = 0
                state.last_health_check = time.time()
                log.info("MCP %s: connected", name)
                return True

            except Exception as e:
                state.connected = False
                state.last_error = str(e)
                state.error_count += 1
                log.warning("MCP %s: connection failed: %s", name, e)
                return False

    def disconnect(self, name: str):
        """Disconnect a specific MCP server."""
        with self._lock:
            state = self._connections.get(name)
        if state is None:
            return

        with state._lock:
            if state._session is not None:
                try:
                    self._client.disconnect(
                        state._session, state._read, state._write
                    )
                except Exception as e:
                    log.debug("MCP %s: disconnect error: %s", name, e)
                # Stop dedicated event loop (streamable HTTP or stdio)
                for attr in ('_streamable_loop', '_stdio_loop'):
                    dedicated_loop = getattr(state._session, attr, None)
                    if dedicated_loop is not None:
                        try:
                            dedicated_loop.call_soon_threadsafe(dedicated_loop.stop)
                        except Exception:
                            pass
                state._session = None
                state._read = None
                state._write = None
                state._session_id = None
            state.connected = False
            log.info("MCP %s: disconnected", name)

    def disconnect_all(self):
        """Disconnect all MCP servers."""
        self._shutdown = True
        with self._lock:
            names = list(self._connections.keys())
        for name in names:
            self.disconnect(name)

    # ── Session Access ────────────────────────────────────────────────────

    def get_session(self, name: str) -> Optional[ClientSession]:
        """Get an active MCP session, auto-connecting if needed.

        Returns None if the server is unavailable (callers use graceful fallback).
        """
        with self._lock:
            state = self._connections.get(name)
        if state is None:
            return None
        if not state.config.enabled:
            return None

        # Fast path — already connected
        if state.connected and state._session is not None:
            # Quick health check (cheap — just reads an internal flag)
            return state._session

        # Try to connect (acquires state._lock internally)
        if self.connect(name):
            return state._session

        return None

    # ── Tool Operations ───────────────────────────────────────────────────

    def call_tool(
        self,
        server_name: str,
        tool_name: str,
        params: Optional[Dict[str, Any]] = None,
        timeout: Optional[float] = None,
    ) -> Optional[Dict[str, Any]]:
        """Call a tool on a named MCP server.

        Returns the parsed result dict on success, None on failure.
        Callers MUST handle None → fall back to existing source (graceful degradation).

        Example:
            result = manager.call_tool("lonestar", "get_options_flow", {"ticker": "AAPL"})
            if result is None:
                # fall back to RSS scraper
                return _fetch_options_flow()
        """
        with self._lock:
            state = self._connections.get(server_name)
        if state is None:
            log.debug("MCP: unknown server '%s'", server_name)
            return None

        session = self.get_session(server_name)
        if session is None:
            with state._lock:
                state.error_count += 1
                state.last_error = (
                    f"Server not connected (tool: {tool_name})"
                )
            log.debug(
                "MCP %s: unavailable for tool %s", server_name, tool_name
            )
            return None

        try:
            call_timeout = timeout or state.config.call_timeout
            # Detect dedicated loop (streamable HTTP or stdio)
            dedicated_loop = getattr(session, '_streamable_loop', None) or getattr(session, '_stdio_loop', None)
            result = self._client.call_tool(
                session, tool_name, params, timeout=call_timeout,
                loop=dedicated_loop
            )
            # Success — reset error counters
            with state._lock:
                state.last_error = None
                state.last_health_check = time.time()
            return result

        except Exception as e:
            log.warning(
                "MCP %s: tool '%s' failed: %s", server_name, tool_name, e
            )
            with state._lock:
                state.error_count += 1
                state.last_error = str(e)
                if state.error_count >= MAX_RECONNECT_ATTEMPTS:
                    state.connected = False
                    log.warning(
                        "MCP %s: marked disconnected after %d errors",
                        server_name,
                        state.error_count,
                    )
            return None

    def list_tools(
        self, server_name: str, force_refresh: bool = False
    ) -> List[Dict[str, Any]]:
        """List available tools for a server, using cache when available."""
        with self._lock:
            state = self._connections.get(server_name)
        if state is None:
            return []

        # Return cached tools if fresh enough
        if not force_refresh and state.tools_cache is not None:
            if (time.time() - state.tools_cache_time) < state.tools_cache_ttl:
                return state.tools_cache

        session = self.get_session(server_name)
        if session is None:
            # Serve stale cache if available
            if state.tools_cache is not None:
                log.debug(
                    "MCP %s: returning stale tool cache (%d tools)",
                    server_name,
                    len(state.tools_cache),
                )
                return state.tools_cache
            return []

        try:
            dedicated_loop = getattr(session, '_streamable_loop', None) or getattr(session, '_stdio_loop', None)
            tools = self._client.list_tools(session, loop=dedicated_loop)
            with state._lock:
                state.tools_cache = tools
                state.tools_cache_time = time.time()
            log.info(
                "MCP %s: discovered %d tools", server_name, len(tools)
            )
            return tools
        except Exception as e:
            log.warning("MCP %s: tool discovery failed: %s", server_name, e)
            return state.tools_cache if state.tools_cache is not None else []

    # ── Health ────────────────────────────────────────────────────────────

    def health_check(self, name: str) -> bool:
        """Run a health check (ping) on a named server. Returns True if healthy."""
        with self._lock:
            state = self._connections.get(name)
        if state is None:
            return False

        session = self.get_session(name)
        if session is None:
            return False

        try:
            dedicated_loop = getattr(session, '_streamable_loop', None) or getattr(session, '_stdio_loop', None)
            ok = self._client.ping(session, loop=dedicated_loop)
            with state._lock:
                state.last_health_check = time.time()
                if ok:
                    state.last_error = None
            return ok
        except Exception:
            return False

    def health_check_all(self) -> Dict[str, bool]:
        """Run health checks on all registered servers.

        Returns dict of server_name → healthy_bool.
        """
        results: Dict[str, bool] = {}
        with self._lock:
            names = list(self._connections.keys())
        for name in names:
            results[name] = self.health_check(name)
        return results

    # ── Status ────────────────────────────────────────────────────────────

    def status(self) -> Dict[str, Any]:
        """Get status of all MCP connections.

        Suitable for the /mcp-status data bus endpoint.
        """
        servers: Dict[str, Dict[str, Any]] = {}
        with self._lock:
            for name, state in self._connections.items():
                cfg = state.config
                servers[name] = {
                    "transport": cfg.transport,
                    "enabled": cfg.enabled,
                    "connected": state.connected,
                    "error_count": state.error_count,
                    "last_error": state.last_error,
                    "last_health_check": (
                        state.last_health_check
                        if state.last_health_check
                        else None
                    ),
                    "tools_cached": (
                        len(state.tools_cache) if state.tools_cache else 0
                    ),
                    "tools_cached_at": (
                        state.tools_cache_time
                        if state.tools_cache_time
                        else None
                    ),
                }

        connected_count = sum(
            1 for s in servers.values() if s["connected"]
        )

        return {
            "servers": servers,
            "total": len(servers),
            "connected_count": connected_count,
            "timestamp": time.time(),
        }

    # ── Shutdown ──────────────────────────────────────────────────────────

    def shutdown(self):
        """Graceful shutdown of all connections and background event loop."""
        self.disconnect_all()
        _shutdown_loop()
        log.info("MCPConnectionManager shut down")


# ═══════════════════════════════════════════════════════════════════════════════
# Module-Level Convenience
# ═══════════════════════════════════════════════════════════════════════════════


def get_manager() -> MCPConnectionManager:
    """Get the singleton MCP connection manager."""
    return MCPConnectionManager()


# ── Pre-built configs for the 5 MCP servers (plan §2) ──────────────────────
# These are registered lazily by the data bus on startup.
# Configs that depend on env vars use lambdas so the var is evaluated at
# registration time, not import time.

ALPHA_VANTAGE_CONFIG_FACTORY = lambda: MCPConnectionConfig(
    name="alphavantage",
    transport="sse",
    url=f"https://mcp.alphavantage.co/mcp?apikey={os.getenv('ALPHA_VANTAGE_API_KEY', '')}",
    enabled=bool(os.getenv("ALPHA_VANTAGE_API_KEY")),
    connect_timeout=15.0,
    call_timeout=30.0,
)

LONESTAR_CONFIG = MCPConnectionConfig(
    name="lonestar",
    transport="streamable_http",
    url="https://mcp.lonestaroracle.xyz/mcp",
    enabled=True,
    connect_timeout=15.0,
    call_timeout=45.0,
)

ALPHAAI_CONFIG_FACTORY = lambda: MCPConnectionConfig(
    name="alphai",
    transport="stdio",
    command="npx",
    args=["-y", "alphai-mcp"],
    enabled=False,  # OAuth 2.1 setup required before enabling
    connect_timeout=30.0,
    call_timeout=30.0,
    protocol_version="2024-11-05",
)

PRAESENTIRE_CONFIG_FACTORY = lambda: MCPConnectionConfig(
    name="praesentire",
    transport="stdio",
    command="/home/openclaw/.npm-global/bin/praesentire-mcp",
    args=[],
    env={"PRAESENTIRE_API_KEY": os.getenv("PRAESENTIRE_API_KEY", "")},
    enabled=bool(os.getenv("PRAESENTIRE_API_KEY")),
    connect_timeout=30.0,
    call_timeout=30.0,
)

# Per-trader Alpaca MCP configs (Phase 5 — disabled until then)
ALPACA_MCP_CONFIGS = {
    trader: MCPConnectionConfig(
        name=f"alpaca-{trader}",
        transport="stdio",
        command="uvx",
        args=["alpaca-mcp-server"],
        env={
            "ALPACA_API_KEY": os.getenv(f"ALPACA_{trader.upper()}_KEY", ""),
            "ALPACA_SECRET_KEY": os.getenv(
                f"ALPACA_{trader.upper()}_SECRET", ""
            ),
        },
        enabled=True,
        protocol_version="2024-11-05",
    )
    for trader in ["kairos", "aldridge", "stonks"]
}

# ── Registration helper ──────────────────────────────────────────────────────


def register_phase0_servers(manager: Optional[MCPConnectionManager] = None):
    """Register the MCP servers needed for Phase 1–4 integration.

    Only LoneStarOracle is enabled by default (public SSE, no key required).
    Other servers are registered but stay disabled until their API keys are
    configured and the corresponding phase begins.

    Call once at data bus startup.
    """
    if manager is None:
        manager = get_manager()

    # Phase 2: LoneStarOracle — enabled now (public, no key)
    manager.register(LONESTAR_CONFIG)

    # Phase 1: Alpha Vantage — enabled iff API key is set
    manager.register(ALPHA_VANTAGE_CONFIG_FACTORY())

    # Phase 3: AlphaAI — disabled until OAuth setup
    manager.register(ALPHAAI_CONFIG_FACTORY())

    # Phase 4: Praesentire — enabled iff API key is set
    manager.register(PRAESENTIRE_CONFIG_FACTORY())

    # Phase 5: Alpaca MCP per-trader — disabled until Phase 5
    for cfg in ALPACA_MCP_CONFIGS.values():
        manager.register(cfg)

    log.info(
        "Registered %d MCP server configs (enabled=%d)",
        len(manager._connections),
        sum(1 for s in manager._connections.values() if s.config.enabled),
    )

    return manager
