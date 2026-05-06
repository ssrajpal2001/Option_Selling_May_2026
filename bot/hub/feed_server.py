"""
FeedServer — single-process TCP broadcast hub for market data ticks.

Runs as a background asyncio task inside the web (uvicorn) process.
Maintains ONE Upstox + ONE Dhan WebSocket connection for the whole server,
then fans out normalized JSON ticks to every connected client-bot subprocess
via a lightweight TCP newline-delimited JSON protocol.

Protocol (newline-delimited JSON):
  Client → Server:
    {"cmd": "subscribe",    "instruments": ["NSE_FO|50973", ...], "mode": "full"}
    {"cmd": "unsubscribe",  "instruments": ["NSE_FO|50973", ...]}
    {"cmd": "ping"}
    {"cmd": "feed_status"}

  Server → Client:
    {"type": "tick",        "instrument_key": "...", "ltp": 120.5,
     "timestamp": 1714486539.0, "source": "upstox_global"}
    {"type": "pong"}
    {"type": "feed_status", "dhan": true, "upstox": true}
    {"type": "keepalive"}
"""

import asyncio
import json
import time
import datetime
import os

from utils.logger import logger

_HOST = '127.0.0.1'
_PORT = 15765

# Module-level singleton
_instance = None


def get_feed_server() -> 'FeedServer':
    global _instance
    if _instance is None:
        _instance = FeedServer()
    return _instance


class FeedServer:
    """
    Singleton TCP broadcast server.
    Start once from web/server.py startup_event(); every client subprocess
    connects and receives all ticks without opening its own broker WebSocket.
    """

    def __init__(self):
        self._writers: list = []
        self._dual_feed = None
        self._server = None
        self._started = False
        # Union of all instrument keys requested by any connected FeedClient.
        # Used to re-subscribe the DualFeedManager after a reconnect/restart.
        self._all_subscribed: set = set()
        self._last_broadcast_epoch: float = 0.0

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def start(self) -> None:
        if self._started:
            return
        logger.info("[FeedServer] Starting up...")

        # Subscribe to the process-local event bus.
        # Dhan ticks arrive here as BROKER_TICK_RECEIVED (via DualFeedManager._on_dhan_tick).
        # Upstox ticks arrive here after extraction in _on_upstox_raw (published via same path).
        from hub.event_bus import event_bus
        event_bus.subscribe('BROKER_TICK_RECEIVED', self._on_normalized_tick)

        # Bind the TCP socket first so FeedClients can connect immediately.
        # DualFeedManager initialization (which may involve slow token refresh/auth)
        # runs in a separate background task so it never blocks the listener.
        self._server = await asyncio.start_server(
            self._handle_client, _HOST, _PORT, reuse_address=True
        )
        self._started = True
        logger.info(f"[FeedServer] Listening on {_HOST}:{_PORT}")

        # Kick off feed initialization in the background
        asyncio.create_task(self._init_dual_feed_background())

        async with self._server:
            await self._server.serve_forever()

    async def _init_dual_feed_background(self) -> None:
        """Initialize DualFeedManager asynchronously after the TCP server is up."""
        os.environ['_FEED_SERVER_INIT'] = '1'
        try:
            await self._init_dual_feed()
        finally:
            os.environ.pop('_FEED_SERVER_INIT', None)

    async def _init_dual_feed(self) -> None:
        """Create and start a DualFeedManager using global provider credentials."""
        try:
            from utils.config_manager import ConfigManager
            cfg = ConfigManager('config/config_trader.ini')

            from hub.provider_factory import ProviderFactory
            _, ws_mgr = await ProviderFactory.create_data_provider(
                api_client_manager=None,
                config_manager=cfg,
                is_backtest=False,
            )
            if ws_mgr is None:
                logger.warning("[FeedServer] No data feeds configured — server will forward no ticks.")
                return

            self._dual_feed = ws_mgr

            # Register a handler so that DualFeedManager wires up BOTH feeds:
            #   • Upstox: ws_mgr.upstox.register_message_handler(_on_upstox_raw) → extracts LTP
            #   • Dhan:   ws_mgr.dhan.register_message_handler(_on_dhan_tick)    → publishes
            #             BROKER_TICK_RECEIVED, which _on_normalized_tick forwards to TCP clients.
            # Without this call the DualFeedManager would start but Dhan ticks would never reach
            # the event_bus because _on_dhan_tick is only registered via register_message_handler.
            ws_mgr.register_message_handler(self._on_upstox_raw)

            # Replay any subscriptions that connected clients already sent before the
            # DualFeedManager was ready (clients may connect before init finishes).
            if self._all_subscribed:
                logger.info(
                    f"[FeedServer] Replaying {len(self._all_subscribed)} queued subscriptions."
                )
                ws_mgr.subscribe(list(self._all_subscribed))

            # Connect the WebSockets
            ws_mgr.start()
            logger.info("[FeedServer] DualFeedManager started.")
        except Exception as exc:
            logger.error(f"[FeedServer] Feed initialization failed: {exc}", exc_info=True)

    # ── Tick capture ─────────────────────────────────────────────────────────

    async def _on_upstox_raw(self, feed_response) -> None:
        """
        Handler registered with DualFeedManager (and thus Upstox WebSocketManager).
        Extracts (instrument_key, ltp) from Upstox protobuf and publishes
        BROKER_TICK_RECEIVED so _on_normalized_tick forwards it to TCP clients.

        NOTE: DualFeedManager.register_message_handler() also registers _on_dhan_tick
        onto the Dhan WebSocketManager; those normalized dicts are published directly
        to BROKER_TICK_RECEIVED by DualFeedManager without going through this handler.
        """
        if not hasattr(feed_response, 'feeds'):
            return
        from hub.event_bus import event_bus
        now = datetime.datetime.now()
        for key, feed in feed_response.feeds.items():
            ltp = 0.0
            try:
                if feed.HasField('ltpc'):
                    ltp = float(feed.ltpc.ltp)
                elif feed.HasField('fullFeed'):
                    if feed.fullFeed.HasField('indexFF'):
                        ltp = float(feed.fullFeed.indexFF.ltpc.ltp)
                    elif feed.fullFeed.HasField('marketFF'):
                        ltp = float(feed.fullFeed.marketFF.ltpc.ltp)
                elif feed.HasField('firstLevelWithGreeks'):
                    ltp = float(feed.firstLevelWithGreeks.ltpc.ltp)
            except Exception:
                pass
            if ltp > 0:
                tick_data = {
                    'user_id': 'GLOBAL',
                    'instrument_key': key,
                    'ltp': ltp,
                    'timestamp': now,
                    'broker': 'upstox_global',
                }
                # Extract ATP (avg traded price) from marketFF for VWAP/CSV recording
                try:
                    if feed.HasField('fullFeed') and feed.fullFeed.HasField('marketFF'):
                        atp_val = float(feed.fullFeed.marketFF.atp or 0)
                        if atp_val > 0:
                            tick_data['atp'] = atp_val
                except Exception:
                    pass
                await event_bus.publish('BROKER_TICK_RECEIVED', tick_data)

    async def _on_normalized_tick(self, data: dict) -> None:
        """
        Called for every BROKER_TICK_RECEIVED event in this process.
        Broadcasts to all connected FeedClients via TCP.
        """
        key = data.get('instrument_key')
        ltp = data.get('ltp')
        if not key or ltp is None:
            return

        now = time.time()
        # Periodic heartbeat for FeedServer (every 60s per instrument, or overall)
        if now - getattr(self, '_last_log_time', 0) > 60:
            self._last_log_time = now
            logger.info(f"[FeedServer] Broadcasting ticks. Active clients: {len(self._writers)}")

        self._last_broadcast_epoch = now
        msg = {
            'type': 'tick',
            'instrument_key': key,
            'ltp': float(ltp),
            'timestamp': self._last_broadcast_epoch,
            'source': data.get('broker', 'unknown'),
        }
        atp = data.get('atp')
        if atp:
            msg['atp'] = float(atp)
        await self._broadcast(msg)

    # ── TCP broadcast ─────────────────────────────────────────────────────────

    async def _broadcast(self, msg: dict) -> None:
        if not self._writers:
            return
        try:
            line = (json.dumps(msg) + '\n').encode()
        except Exception:
            return
        dead = []
        for w in list(self._writers):
            try:
                w.write(line)
                # Task #152: Harden against slow consumers.
                # If a client is slow, drain() will block. We use a short timeout
                # to ensure one stuck client doesn't freeze the broadcast loop
                # or cause task accumulation.
                try:
                    await asyncio.wait_for(w.drain(), timeout=0.05)
                except asyncio.TimeoutError:
                    # Buffer full — skip waiting but keep client for next tick
                    pass
            except Exception:
                dead.append(w)
        for w in dead:
            if w in self._writers:
                self._writers.remove(w)

    # ── Client handler ────────────────────────────────────────────────────────

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        peer = writer.get_extra_info('peername', 'unknown')
        logger.info(f"[FeedServer] Client connected: {peer}")
        self._writers.append(writer)
        try:
            while True:
                try:
                    line = await asyncio.wait_for(reader.readline(), timeout=90)
                except asyncio.TimeoutError:
                    writer.write(b'{"type":"keepalive"}\n')
                    await writer.drain()
                    continue
                if not line:
                    break
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue

                cmd = msg.get('cmd')
                if cmd == 'subscribe':
                    instruments = msg.get('instruments') or []
                    mode = msg.get('mode', 'full')
                    if instruments:
                        # Track subscription state so we can replay on DualFeedManager reinit
                        self._all_subscribed.update(instruments)
                        if self._dual_feed:
                            self._dual_feed.subscribe(instruments, mode)
                        logger.info(
                            f"[FeedServer] Subscribed {len(instruments)} instruments from {peer}. "
                            f"Total: {len(self._all_subscribed)}."
                        )
                elif cmd == 'unsubscribe':
                    instruments = msg.get('instruments') or []
                    # We do NOT unsubscribe from DualFeedManager because other clients may still
                    # want those instruments. Over-subscription is harmless; missed ticks are not.
                    # We do remove from our tracking set if truly no one needs them.
                    # (Simple policy: only remove if FeedServer has a single client right now)
                    if len(self._writers) <= 1 and instruments:
                        self._all_subscribed.difference_update(instruments)
                elif cmd == 'ping':
                    writer.write(b'{"type":"pong"}\n')
                    await writer.drain()
                elif cmd == 'feed_status':
                    from hub import feed_registry
                    d = feed_registry.get_ws_state('dhan')
                    u = feed_registry.get_ws_state('upstox')
                    resp = {
                        'type': 'feed_status',
                        'dhan': d.get('ws_connected', False),
                        'upstox': u.get('ws_connected', False),
                    }
                    writer.write((json.dumps(resp) + '\n').encode())
                    await writer.drain()
        except Exception:
            pass
        finally:
            logger.info(f"[FeedServer] Client disconnected: {peer}")
            if writer in self._writers:
                self._writers.remove(writer)
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
