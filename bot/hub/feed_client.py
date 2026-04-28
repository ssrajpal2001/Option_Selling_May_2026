"""
FeedClient — subprocess-side TCP client for the shared FeedServer.

Replaces DualFeedManager in every client-bot subprocess.
Receives normalized JSON ticks from FeedServer (running in the web process)
and publishes them to the subprocess's local event_bus so all existing tick
handlers (PriceFeedHandler.handle_normalized_tick, etc.) work unchanged.

Implements the same DataFeed interface as DualFeedManager so it is a
drop-in replacement with zero changes to callers.
"""

import asyncio
import json
import time
import datetime

from utils.logger import logger
from hub.data_feed_base import DataFeed

_HOST = '127.0.0.1'
_PORT = 15765
_CONNECT_TIMEOUT = 3.0    # seconds per attempt
_CONNECT_RETRIES = 3
_RECONNECT_DELAY = 5.0    # seconds between reconnect attempts
_IDLE_TIMEOUT = 90        # seconds before sending a ping to keep the connection alive


class FeedClient(DataFeed):
    """
    Lightweight TCP client for the shared FeedServer.

    Usage (from ProviderFactory / orchestrator):
        fc = FeedClient()
        # optionally: if not await fc.try_connect(): fallback to DualFeedManager
        ws_mgr = fc
        ws_mgr.register_message_handler(price_feed_handler.handle_message)
        ws_mgr.start()        # begins async connection+read loop
        ws_mgr.subscribe(symbols)
        ...
        await ws_mgr.close()
    """

    def __init__(self):
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._connected: bool = False
        self._message_handlers: list = []   # stored but not called (ticks go via event_bus)
        self._subscribed_symbols: list = []
        self._subscribed_mode: str = 'full'
        self._read_task: asyncio.Task | None = None

        # DataFeed-compatible state used by watchdog / health checks
        self.is_connected: bool = False
        self._last_tick_epoch: float = 0.0

    # ── DataFeed interface ────────────────────────────────────────────────────

    def register_message_handler(self, handler) -> None:
        """
        Kept for interface compatibility.  Protobuf handlers registered here
        are stored but never invoked — all ticks arrive via event_bus
        (BROKER_TICK_RECEIVED) so existing handlers work without changes.
        """
        if handler not in self._message_handlers:
            self._message_handlers.append(handler)

    async def close(self) -> None:
        self._connected = False
        self.is_connected = False
        if self._read_task and not self._read_task.done():
            self._read_task.cancel()
        if self._writer:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                pass
        self._reader = None
        self._writer = None

    def subscribe(self, symbols, mode: str = 'full') -> None:
        new = [s for s in symbols if s not in self._subscribed_symbols]
        for s in new:
            self._subscribed_symbols.append(s)
        self._subscribed_mode = mode
        if self._connected and self._writer:
            self._send_subscribe(symbols, mode)

    def unsubscribe(self, symbols) -> None:
        for s in symbols:
            if s in self._subscribed_symbols:
                self._subscribed_symbols.remove(s)

    def start(self) -> asyncio.Task:
        """Begin the connection/read loop. Returns the background Task."""
        self._read_task = asyncio.create_task(self._connection_loop())
        return self._read_task

    # ── Connection helpers ────────────────────────────────────────────────────

    async def try_connect(self) -> bool:
        """
        Try to open a TCP connection to FeedServer.
        Returns True on success and keeps the connection alive for the read loop.
        Returns False if all retries are exhausted.
        """
        for attempt in range(1, _CONNECT_RETRIES + 1):
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(_HOST, _PORT),
                    timeout=_CONNECT_TIMEOUT,
                )
                self._reader = reader
                self._writer = writer
                self._connected = True
                self.is_connected = True
                logger.info(f"[FeedClient] Connected to FeedServer (attempt {attempt}).")
                return True
            except (ConnectionRefusedError, asyncio.TimeoutError, OSError) as exc:
                logger.warning(
                    f"[FeedClient] Connect attempt {attempt}/{_CONNECT_RETRIES} failed: {exc}"
                )
                if attempt < _CONNECT_RETRIES:
                    await asyncio.sleep(2)
        return False

    def _send_subscribe(self, symbols, mode: str = 'full') -> None:
        msg = json.dumps({'cmd': 'subscribe', 'instruments': symbols, 'mode': mode}) + '\n'
        try:
            self._writer.write(msg.encode())
            asyncio.create_task(self._writer.drain())
        except Exception as exc:
            logger.warning(f"[FeedClient] Subscribe write failed: {exc}")

    # ── Async loops ───────────────────────────────────────────────────────────

    async def _connection_loop(self) -> None:
        """Maintain connection to FeedServer, reconnecting on drop."""
        while True:
            try:
                # Re-use connection from try_connect() if already open
                if not self._connected:
                    connected = await self.try_connect()
                    if not connected:
                        logger.critical(
                            "[FeedClient] FeedServer unreachable after retries. "
                            "Tick distribution via FeedClient is unavailable."
                        )
                        return

                # Re-subscribe all previously tracked symbols after reconnect
                if self._subscribed_symbols and self._writer:
                    self._send_subscribe(self._subscribed_symbols, self._subscribed_mode)

                await self._read_loop()

            except asyncio.CancelledError:
                logger.info("[FeedClient] Connection loop cancelled.")
                break
            except Exception as exc:
                logger.error(f"[FeedClient] Unexpected error: {exc}", exc_info=True)

            self._connected = False
            self.is_connected = False
            logger.warning(f"[FeedClient] Reconnecting in {_RECONNECT_DELAY}s...")
            try:
                await asyncio.sleep(_RECONNECT_DELAY)
            except asyncio.CancelledError:
                break

    async def _read_loop(self) -> None:
        """Read JSON lines from FeedServer and dispatch normalized ticks."""
        while self._connected and self._reader:
            try:
                line = await asyncio.wait_for(self._reader.readline(), timeout=_IDLE_TIMEOUT)
            except asyncio.TimeoutError:
                # Connection idle — send ping to detect broken pipe
                if self._writer:
                    try:
                        self._writer.write(b'{"cmd":"ping"}\n')
                        await self._writer.drain()
                    except Exception:
                        break
                continue
            except Exception:
                break

            if not line:
                logger.warning("[FeedClient] Server closed the connection.")
                break

            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue

            msg_type = msg.get('type')
            if msg_type == 'tick':
                self._last_tick_epoch = time.time()
                await self._dispatch_tick(msg)
            elif msg_type in ('pong', 'keepalive', 'feed_status'):
                pass

        self._connected = False
        self.is_connected = False

    async def _dispatch_tick(self, msg: dict) -> None:
        """
        Convert a FeedServer tick to a normalized dict and publish to the
        subprocess-local event_bus so PriceFeedHandler.handle_normalized_tick
        processes it exactly as it would a Dhan or Upstox normalized tick.
        """
        key = msg.get('instrument_key')
        ltp = msg.get('ltp')
        if not key or ltp is None:
            return

        tick = {
            'user_id': 'GLOBAL',
            'instrument_key': key,
            'ltp': float(ltp),
            'timestamp': datetime.datetime.now(),
            'broker': msg.get('source', 'feed_server'),
        }

        from hub.event_bus import event_bus
        await event_bus.publish('BROKER_TICK_RECEIVED', tick)
