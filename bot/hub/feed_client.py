"""
FeedClient — subprocess-side TCP client for the shared FeedServer.

Replaces DualFeedManager in every client-bot subprocess.
Receives normalized JSON ticks from FeedServer (running in the web process)
and publishes them to the subprocess's local event_bus so all existing tick
handlers (PriceFeedHandler.handle_normalized_tick, etc.) work unchanged.

Implements the same DataFeed interface as DualFeedManager so it is a
drop-in replacement with zero changes to callers.

Runtime fallback:
  If FeedServer becomes unreachable after a configured number of consecutive
  reconnect failures (mid-session scenario, e.g. web process restart), the
  FeedClient activates an optional local DualFeedManager that was pre-built
  in ProviderFactory and held in reserve.  All registered handlers and
  current subscriptions are transferred to it automatically.
"""

import asyncio
import json
import time
import datetime
import pytz

from utils.logger import logger

_KOLKATA = pytz.timezone('Asia/Kolkata')
from hub.data_feed_base import DataFeed

_HOST = '127.0.0.1'
_PORT = 15765
_CONNECT_TIMEOUT = 3.0      # seconds per single connection attempt
_CONNECT_RETRIES = 3        # attempts per round inside try_connect()
_RECONNECT_DELAY = 5.0      # seconds between reconnect rounds
_IDLE_TIMEOUT = 90          # seconds of silence → send ping
# After this many consecutive round-failures the fallback DualFeedManager is activated.
# Each round takes ~(_CONNECT_RETRIES × 2s) + _RECONNECT_DELAY ≈ 11s, so 3 rounds ≈ 33s
# before degrading — long enough for a transient web-process restart to recover.
_FALLBACK_TRIGGER_ROUNDS = 3


class FeedClient(DataFeed):
    """
    Lightweight TCP client for the shared FeedServer.

    Usage (from ProviderFactory / EngineManager):
        fc = FeedClient(fallback_feed=dual_feed_manager)
        # optionally probe: if not await fc.try_connect(): use fallback directly
        ws_mgr = fc
        ws_mgr.register_message_handler(price_feed_handler.handle_message)
        ws_mgr.start()        # begins async connection + read loop
        ws_mgr.subscribe(symbols)
        ...
        await ws_mgr.close()
    """

    def __init__(self, fallback_feed=None):
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._connected: bool = False
        # Handlers registered by the orchestrator — kept so they can be forwarded
        # to the fallback DualFeedManager if FeedServer goes away permanently.
        self._message_handlers: list = []
        self._subscribed_symbols: list = []
        self._subscribed_mode: str = 'full'
        self._read_task: asyncio.Task | None = None

        # Optional pre-built DualFeedManager to fall back to after FeedServer outage
        self._fallback_feed = fallback_feed
        self._fallback_active: bool = False
        self._reconnect_fail_rounds: int = 0

        # DataFeed-compatible state used by watchdog / health checks
        self.is_connected: bool = False
        self._last_tick_epoch: float = 0.0

    # ── DataFeed interface ────────────────────────────────────────────────────

    def register_message_handler(self, handler) -> None:
        """
        Store for interface compatibility and fallback transfer.
        Ticks from FeedServer arrive via event_bus (BROKER_TICK_RECEIVED)
        so the protobuf handler is never called directly — but it is
        transferred to the fallback DualFeedManager if activated.
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
        # If the fallback DualFeedManager was activated, close it too
        # to prevent lingering WebSocket tasks during shutdown / restart cycles.
        if self._fallback_active and self._fallback_feed:
            try:
                await self._fallback_feed.close()
            except Exception:
                pass

    def subscribe(self, symbols, mode: str = 'full') -> None:
        new = [s for s in symbols if s not in self._subscribed_symbols]
        for s in new:
            self._subscribed_symbols.append(s)
        self._subscribed_mode = mode
        if self._fallback_active and self._fallback_feed:
            self._fallback_feed.subscribe(symbols, mode)
            return
        if self._connected and self._writer:
            self._write_cmd({'cmd': 'subscribe', 'instruments': symbols, 'mode': mode})
            logger.info(
                f"[FeedClient] Subscription sent to FeedServer: {len(symbols)} instruments "
                f"(mode={mode}). Keys: {symbols[:3]}{'...' if len(symbols) > 3 else ''}"
            )
        else:
            logger.info(
                f"[FeedClient] Subscription QUEUED (not yet connected): {len(symbols)} instruments. "
                f"Keys: {symbols[:3]}{'...' if len(symbols) > 3 else ''}. "
                "Will be sent automatically on connect."
            )

    def unsubscribe(self, symbols) -> None:
        for s in symbols:
            if s in self._subscribed_symbols:
                self._subscribed_symbols.remove(s)
        if self._fallback_active and self._fallback_feed:
            self._fallback_feed.unsubscribe(symbols)
            return
        if self._connected and self._writer and symbols:
            self._write_cmd({'cmd': 'unsubscribe', 'instruments': symbols})

    def start(self) -> asyncio.Task:
        """Begin the connection/read loop. Returns the background Task."""
        logger.info("[FeedClient] start() called — creating _connection_loop task.")
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

    def _write_cmd(self, payload: dict) -> None:
        """Fire-and-forget JSON command to FeedServer."""
        if not self._writer:
            return
        try:
            line = json.dumps(payload) + '\n'
            self._writer.write(line.encode())
            asyncio.create_task(self._writer.drain())
        except Exception as exc:
            logger.warning(f"[FeedClient] Write failed: {exc}")

    # ── Async loops ───────────────────────────────────────────────────────────

    async def _connection_loop(self) -> None:
        """Maintain connection to FeedServer, reconnecting on drop.
        After _FALLBACK_TRIGGER_ROUNDS consecutive round failures the optional
        local DualFeedManager is activated as a permanent fallback."""
        while True:
            try:
                if not self._connected:
                    connected = await self.try_connect()
                    if not connected:
                        self._reconnect_fail_rounds += 1
                        logger.warning(
                            f"[FeedClient] FeedServer unreachable "
                            f"(round {self._reconnect_fail_rounds}/{_FALLBACK_TRIGGER_ROUNDS})."
                        )
                        if (
                            self._fallback_feed is not None
                            and self._reconnect_fail_rounds >= _FALLBACK_TRIGGER_ROUNDS
                        ):
                            logger.warning(
                                "[FeedClient] Activating local DualFeedManager fallback "
                                "— FeedServer has been unreachable too long."
                            )
                            await self._activate_fallback()
                            return  # fallback is now running; no need to loop
                        # Keep retrying — FeedServer may come back (e.g. web restart)
                        await asyncio.sleep(_RECONNECT_DELAY)
                        continue

                # Successful connection: reset failure counter
                self._reconnect_fail_rounds = 0

                # Re-subscribe all tracked symbols after reconnect
                if self._subscribed_symbols and self._writer:
                    self._write_cmd({
                        'cmd': 'subscribe',
                        'instruments': self._subscribed_symbols,
                        'mode': self._subscribed_mode,
                    })

                await self._read_loop()

            except asyncio.CancelledError:
                logger.info("[FeedClient] Connection loop cancelled.")
                break
            except Exception as exc:
                logger.error(f"[FeedClient] Unexpected error: {exc}", exc_info=True)

            self._connected = False
            self.is_connected = False
            self._reader = None
            self._writer = None
            logger.warning(f"[FeedClient] Disconnected. Reconnecting in {_RECONNECT_DELAY}s...")
            try:
                await asyncio.sleep(_RECONNECT_DELAY)
            except asyncio.CancelledError:
                break

    async def _read_loop(self) -> None:
        """Read JSON lines from FeedServer and dispatch normalized ticks."""
        logger.info(
            f"[FeedClient] _read_loop ENTERED. connected={self._connected} "
            f"reader_present={self._reader is not None}"
        )
        while self._connected and self._reader:
            try:
                line = await asyncio.wait_for(self._reader.readline(), timeout=_IDLE_TIMEOUT)
            except asyncio.TimeoutError:
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
                self._rx_count = getattr(self, '_rx_count', 0) + 1
                if self._rx_count == 1:
                    logger.info(
                        f"[FeedClient] RX first tick: "
                        f"{msg.get('instrument_key')} @ {msg.get('ltp')} "
                        f"(subscribed={len(self._subscribed_symbols)})"
                    )
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
            'timestamp': datetime.datetime.now(_KOLKATA),
            'broker': msg.get('source', 'feed_server'),
        }
        atp = msg.get('atp')
        if atp:
            tick['atp'] = float(atp)

        from hub.event_bus import event_bus
        await event_bus.publish('BROKER_TICK_RECEIVED', tick)

    # ── Fallback activation ───────────────────────────────────────────────────

    async def _activate_fallback(self) -> None:
        """
        Start the pre-built fallback DualFeedManager.
        Transfers all registered handlers and current subscriptions.
        """
        dm = self._fallback_feed
        self._fallback_active = True

        # Transfer orchestrator message handlers (e.g. PriceFeedHandler.handle_message)
        # so that the DualFeedManager wires up Upstox/Dhan correctly.
        for h in self._message_handlers:
            dm.register_message_handler(h)

        # Forward all current subscriptions
        if self._subscribed_symbols:
            dm.subscribe(self._subscribed_symbols, self._subscribed_mode)

        # Start the WebSocket connections
        dm.start()
        logger.info(
            "[FeedClient] Fallback DualFeedManager active — "
            f"{len(self._subscribed_symbols)} symbols subscribed."
        )
