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
        self._status_loop_task = None
        # Union of all instrument keys requested by any connected FeedClient.
        # Used to re-subscribe the DualFeedManager after a reconnect/restart.
        self._all_subscribed: set = set()
        self._last_broadcast_epoch: float = 0.0
        self._tick_count: int = 0  # For diagnostic logging
        # Queue of pending subscriptions while _dual_feed is transitioning/None
        self._pending_subscriptions: list = []  # List of (instruments, mode) tuples
        # Lock to prevent concurrent _init_dual_feed() calls from creating duplicate WebSocket instances
        self._init_lock = None  # Created on-demand in async context

    # ── Lifecycle ────────────────────────────────────────────────────────────

    def _get_init_lock(self) -> asyncio.Lock:
        """Lazily create the lock in the current event loop."""
        if self._init_lock is None:
            self._init_lock = asyncio.Lock()
        return self._init_lock

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
        """Create and start a DualFeedManager using global provider credentials.

        Serialized with a lock to prevent concurrent initialization calls from
        creating multiple WebSocket instances (Upstox allows only 1 concurrent WS).
        """
        lock = self._get_init_lock()
        async with lock:
            # Already initialized while we were waiting? Return early.
            if self._dual_feed is not None:
                logger.info("[FeedServer] _init_dual_feed: dual_feed already initialized, skipping.")
                return

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

                # If ws_mgr is a plain WebSocketManager (single-provider mode), it won't
                # call register_feed() itself (only DualFeedManager.start() does that).
                # Register it manually so get_ws_state('upstox') works and the admin
                # startup check doesn't see "offline" and tear down a working connection.
                if not hasattr(ws_mgr, 'upstox') and not hasattr(ws_mgr, 'dhan'):
                    try:
                        from hub.feed_registry import register_feed
                        register_feed('upstox', ws_mgr)
                        logger.info("[FeedServer] Registered plain WebSocketManager as 'upstox' in feed_registry.")
                    except Exception as _re:
                        logger.warning(f"[FeedServer] Could not register in feed_registry: {_re}")

                # Immediately replay any pending subscriptions queued while feed was transitioning
                self._replay_pending_subscriptions()

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
                # Start periodic status reporter — cancel previous one if it exists
                if self._status_loop_task and not self._status_loop_task.done():
                    self._status_loop_task.cancel()
                self._status_loop_task = asyncio.create_task(self._status_loop())
            except Exception as exc:
                logger.error(f"[FeedServer] Feed initialization failed: {exc}", exc_info=True)

    async def _status_loop(self) -> None:
        """Log FeedServer health every 60s so web-process logs show feed state."""
        while True:
            try:
                await asyncio.sleep(60)
                age = time.time() - self._last_broadcast_epoch if self._last_broadcast_epoch else None
                age_str = f"{age:.0f}s ago" if age is not None else "never"
                feed_ok = self._dual_feed is not None
                logger.info(
                    f"[FeedServer] Status — clients={len(self._writers)} | "
                    f"feed={'UP' if feed_ok else 'DOWN'} | "
                    f"last_tick={age_str} | "
                    f"subscribed={len(self._all_subscribed)}"
                )
            except asyncio.CancelledError:
                break
            except Exception:
                pass

    def _replay_pending_subscriptions(self) -> None:
        """Replay all subscriptions that were queued while DualFeedManager was transitioning."""
        if not self._pending_subscriptions:
            return
        if not self._dual_feed:
            logger.warning("[FeedServer] Tried to replay pending subscriptions but _dual_feed is still None")
            return

        logger.info(f"[FeedServer] Replaying {len(self._pending_subscriptions)} PENDING subscriptions queued during transition...")
        for instruments, mode in self._pending_subscriptions:
            try:
                self._dual_feed.subscribe(instruments, mode)
                logger.info(
                    f"[FeedServer] Replayed {len(instruments)} instruments (mode={mode}). "
                    f"Keys: {instruments[:3]}{'...' if len(instruments) > 3 else ''}"
                )
            except Exception as e:
                logger.error(f"[FeedServer] Failed to replay subscription for {instruments}: {e}")

        # Clear the queue after replaying
        self._pending_subscriptions.clear()
        logger.info("[FeedServer] Pending subscriptions queue cleared.")

    # ── Admin reconnect trigger ───────────────────────────────────────────────

    async def _shutdown_dual_feed(self) -> None:
        """Cancel ALL running WebSocket tasks in the current feed manager.

        Handles both DualFeedManager (has .upstox/.dhan) and a plain
        WebSocketManager used in single-provider mode.  Cancels listener_task
        AND all auxiliary tasks (watchdog, processor, latency) so no zombie tasks
        survive to force-reconnect with stale credentials.
        """
        if self._dual_feed is None:
            return
        old = self._dual_feed
        self._dual_feed = None

        # Build list of individual feed objects to shut down.
        feeds_to_kill = []
        for attr in ('upstox', 'dhan'):
            feed = getattr(old, attr, None)
            if feed is not None:
                feeds_to_kill.append((attr, feed))
        if not feeds_to_kill:
            # _dual_feed is a plain WebSocketManager or DhanWebSocketManager
            feeds_to_kill.append(('feed', old))

        for name, feed in feeds_to_kill:
            # Cancel ALL tasks the WS manager may have spawned — including watchdog
            # and processor tasks that would otherwise survive as zombies and
            # call _force_reconnect() at the 90s silence threshold.
            for task_attr in ('_listener_task', '_task', '_watchdog_task',
                              '_processor_task', '_latency_task'):
                task = getattr(feed, task_attr, None)
                if task and not task.done():
                    try:
                        task.cancel()
                        logger.info(f"[FeedServer] Cancelled {name}.{task_attr} during shutdown.")
                    except Exception:
                        pass
                try:
                    setattr(feed, task_attr, None)
                except Exception:
                    pass
            try:
                feed._running = False
            except Exception:
                pass

        # Unregister from feed_registry so admin status reflects the shutdown
        try:
            from hub.feed_registry import unregister_feed
            for provider in ('upstox', 'dhan'):
                unregister_feed(provider)
        except Exception:
            pass

        logger.info("[FeedServer] Old feed manager shut down.")

    async def reconnect_provider(self, provider: str) -> bool:
        """
        Called from admin 'Connect Now' after a token refresh — FORCES a clean restart of
        the upstream WebSocket regardless of its current state.

        Scenarios handled:
        - DualFeedManager never initialized (startup init failed) → re-run _init_dual_feed.
        - Feed task is alive but stuck in auth-retry loop → cancel and restart cleanly.
        - Feed task already exited (max retries hit) → start a new one.
        """
        if self._dual_feed is None:
            logger.info(f"[FeedServer] reconnect_provider({provider}) — no dual_feed, running full init.")
            asyncio.create_task(self._init_dual_feed())
            return True

        # Get the specific feed object.
        # DualFeedManager exposes .upstox and .dhan attributes.
        # A plain WebSocketManager (single Upstox provider) has no such attrs —
        # in that case _dual_feed itself IS the upstox feed.
        feed = None
        if provider == 'upstox':
            feed = getattr(self._dual_feed, 'upstox', None)
            if feed is None and not hasattr(self._dual_feed, 'dhan'):
                # Single-provider mode: _dual_feed is the plain Upstox WebSocketManager
                feed = self._dual_feed
                logger.info(f"[FeedServer] reconnect_provider({provider}) — plain WebSocketManager used as feed.")
        elif provider == 'dhan':
            feed = getattr(self._dual_feed, 'dhan', None)

        if feed is None:
            # Provider truly missing from the current manager — tear down and full reinit
            logger.info(
                f"[FeedServer] reconnect_provider({provider}) — feed missing in dual_feed; "
                "shutting down existing manager before re-init to avoid duplicate WS connections."
            )
            await self._shutdown_dual_feed()
            asyncio.create_task(self._init_dual_feed())
            return True

        # Cancel any existing listener/task so we can restart cleanly with the new credentials
        existing_task = getattr(feed, '_listener_task', None) or getattr(feed, '_task', None)
        if existing_task and not existing_task.done():
            try:
                existing_task.cancel()
                logger.info(f"[FeedServer] reconnect_provider({provider}) — cancelled stale listener task.")
            except Exception as _ce:
                logger.debug(f"[FeedServer] cancel error for {provider}: {_ce}")

        # Reset state so the new connect_and_listen() loop starts clean
        try:
            feed._disabled = False
        except Exception:
            pass
        try:
            feed._running = True
            feed.reconnect_attempts = 0
            feed.is_connected = False
            feed.websocket = None
        except Exception:
            pass
        try:
            feed.feed = None  # Dhan-specific
        except Exception:
            pass
        # Drop the cached task reference so start() creates a fresh one
        try:
            feed._listener_task = None
        except Exception:
            pass
        try:
            feed._task = None
        except Exception:
            pass

        # Sync the latest token from data_providers into the feed object before restarting.
        # This ensures the new connect_and_listen() task uses a fresh token, not the expired
        # one that was in memory when the old task failed.
        try:
            from web.db import db_fetchone as _dbf
            from web.auth import decrypt_secret as _dec
            _row = _dbf("SELECT access_token_encrypted FROM data_providers WHERE provider=?", (provider,))
            if _row and _row[0]:
                _fresh_token = _dec(_row[0])
                if _fresh_token and hasattr(feed, 'refresh_credentials'):
                    feed.refresh_credentials(_fresh_token)
                    logger.info(f"[FeedServer] reconnect_provider({provider}) — synced fresh token from DB into feed.")
                elif _fresh_token and hasattr(feed, 'access_token'):
                    feed.access_token = _fresh_token
                    logger.info(f"[FeedServer] reconnect_provider({provider}) — updated feed.access_token from DB.")
        except Exception as _tok_err:
            logger.debug(f"[FeedServer] reconnect_provider token sync skipped: {_tok_err}")

        # Now restart — DualFeedManager.start() re-registers the feed and calls feed.start()
        # which will create a new asyncio task running connect_and_listen().
        logger.info(f"[FeedServer] reconnect_provider({provider}) — starting fresh listener.")
        self._dual_feed.start()
        return True

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
            logger.warning(f"[FeedServer] Upstox message has no 'feeds' attribute: {type(feed_response).__name__}")
            return
        from hub.event_bus import event_bus
        now = datetime.datetime.now()
        _tick_batch = len(feed_response.feeds) if hasattr(feed_response, 'feeds') else 0
        _msg_type = getattr(feed_response, 'type', '?')
        if _tick_batch % 10 == 0 or _tick_batch == 1:
            logger.info(f"[FeedServer] Upstox raw: {_tick_batch} feeds (type={_msg_type})")
        for key, feed in feed_response.feeds.items():
            ltp = 0.0
            _has_field = 'none'
            try:
                if feed.HasField('ltpc'):
                    _has_field = 'ltpc'
                    ltp = float(feed.ltpc.ltp)
                elif feed.HasField('fullFeed'):
                    _has_field = 'fullFeed'
                    if feed.fullFeed.HasField('indexFF'):
                        ltp = float(feed.fullFeed.indexFF.ltpc.ltp)
                    elif feed.fullFeed.HasField('marketFF'):
                        ltp = float(feed.fullFeed.marketFF.ltpc.ltp)
                elif feed.HasField('firstLevelWithGreeks'):
                    _has_field = 'firstLevelWithGreeks'
                    ltp = float(feed.firstLevelWithGreeks.ltpc.ltp)
            except Exception as e:
                logger.warning(f"[FeedServer] LTP extraction failed ({_has_field}) for {key}: {e}")
                continue

            logger.info(f"[FeedServer] Tick: {key} ltp={ltp} ({_has_field})")
            # One-time detailed dump for the first index tick to verify protobuf field structure
            if 'INDEX' in key and not getattr(self, '_nifty_tick_logged', False):
                self._nifty_tick_logged = True
                try:
                    fields = []
                    if feed.HasField('ltpc'): fields.append(f"ltpc.ltp={feed.ltpc.ltp}")
                    if feed.HasField('fullFeed'):
                        if feed.fullFeed.HasField('indexFF'): fields.append(f"indexFF.ltp={feed.fullFeed.indexFF.ltpc.ltp}")
                        if feed.fullFeed.HasField('marketFF'): fields.append(f"marketFF.ltp={feed.fullFeed.marketFF.ltpc.ltp}")
                    if feed.HasField('firstLevelWithGreeks'): fields.append(f"greeks.ltp={feed.firstLevelWithGreeks.ltpc.ltp}")
                    logger.info(f"[FeedServer] INDEX first tick: key={key} | {' | '.join(fields) or 'NO_FIELDS_FOUND'}")
                except Exception as _dump_e:
                    logger.info(f"[FeedServer] INDEX first tick dump failed: {_dump_e}")
            if ltp <= 0:
                logger.warning(f"[FeedServer] Tick dropped (ltp={ltp}): {key}")
                continue

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
        self._last_broadcast_epoch = time.time()
        self._tick_count += 1

        # Log sample ticks for diagnostic purposes (every 10 ticks to avoid log spam)
        if self._tick_count % 10 == 0:
            logger.debug(
                f"[FeedServer] Tick #{self._tick_count}: {key} @ {ltp} "
                f"({len(self._writers)} clients subscribed)"
            )

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
                await w.drain()
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
                            # Forward immediately if feed is ready
                            self._dual_feed.subscribe(instruments, mode)
                            logger.info(
                                f"[FeedServer] Subscribed {len(instruments)} instruments from {peer} (mode={mode}). "
                                f"Keys: {instruments[:3]}{'...' if len(instruments) > 3 else ''}. "
                                f"Total tracked: {len(self._all_subscribed)}."
                            )
                        else:
                            # Queue for replay when _dual_feed becomes ready (during DualFeedManager transition)
                            self._pending_subscriptions.append((instruments, mode))
                            logger.warning(
                                f"[FeedServer] Subscription QUEUED (feed transitioning): {len(instruments)} instruments. "
                                f"Keys: {instruments[:3]}{'...' if len(instruments) > 3 else ''}. "
                                f"Pending queue size: {len(self._pending_subscriptions)}"
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
