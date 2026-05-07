import asyncio
import websockets
import json
import ssl
import aiohttp
import inspect
from concurrent.futures import ThreadPoolExecutor
from .logger import logger
from . import MarketDataFeedV3_pb2 as pb
from hub.data_feed_base import DataFeed

class WebSocketManager(DataFeed):
    # Class-level lock to ensure multiple instances don't auth simultaneously
    _global_auth_lock = asyncio.Lock()

    def __init__(self, api_client_manager=None, api_client=None):
        self.api_client_manager = api_client_manager
        self.api_client = api_client # Direct RestApiClient for client-mode Upstox
        self.message_handlers = []
        self.websocket = None
        self.is_connected = False
        self.subscriptions = {}  # To track symbols and modes
        self.reconnect_attempts = 0
        self.max_reconnect_attempts = 1000 # Increased for all-day reliability
        self.reconnect_delay = 5  # seconds
        self._running = False
        self._listener_task = None
        self._watchdog_task = None
        self._processor_task = None
        self._latency_task = None
        self._message_queue = asyncio.Queue(maxsize=2000)
        self._handler_tasks = set() # Store handler tasks to prevent them from being garbage-collected
        # Thread pool for sync handlers
        self._executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="ws_handler")
        self._last_message_time = asyncio.get_event_loop().time()
        self._last_tick_epoch: float = 0.0  # Real Unix epoch seconds for health reporting

    def register_message_handler(self, handler):
        """Adds a new message handler to the list of handlers."""
        if handler not in self.message_handlers:
            self.message_handlers.append(handler)
            handler_name = getattr(handler, '__name__', type(handler).__name__)
            logger.info(f"Message handler {handler_name} registered.")

    async def _get_auth_uri(self):
        async with self._global_auth_lock:
            logger.info("Authorizing WebSocket feed...")
            max_retries = 15
            base_delay = 5
            
            for attempt in range(max_retries):
                try:
                    # Support both global manager and direct client
                    active_client = self.api_client
                    if not active_client and self.api_client_manager:
                        active_client = self.api_client_manager.get_active_client()

                    if not active_client:
                        raise ValueError("No active API client available for WebSocket authorization.")

                    url = "https://api.upstox.com/v3/feed/market-data-feed/authorize"
                    headers = {
                        "Authorization": f"Bearer {active_client.auth_handler.get_access_token()}",
                        "Accept": "application/json"
                    }

                    logger.info(f"Sending GET request to {url} (Attempt {attempt + 1})...")

                    async with aiohttp.ClientSession() as session:
                        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                            if resp.status == 429:
                                import random
                                delay = (base_delay ** attempt) + (random.random() * 2)
                                logger.warning(f"Upstox Rate Limit (429) hit during WS auth. Retrying in {delay:.1f}s...")
                                await asyncio.sleep(delay)
                                continue

                            resp.raise_for_status()
                            data = await resp.json()

                    uri = data.get("data", {}).get("authorized_redirect_uri")

                    if not uri:
                        raise ValueError("Could not extract authorized_redirect_uri from auth response.")

                    logger.info("WebSocket authorization successful.")
                    return uri

                except Exception as e:
                    # On 401, try to reload a fresher token from DB before retrying.
                    if "401" in str(e) and not active_client and hasattr(self, 'access_token'):
                        # FeedServer path: no api_client — reload directly from DB into self.access_token.
                        # Always re-query both tables on every 401: an identical token string may still be
                        # expired (Upstox tokens expire ~45 min after issue, not just when the value changes).
                        try:
                            from web.db import db_fetchone, db_execute
                            from web.auth import decrypt_secret, encrypt_secret
                            fresh = None
                            _best_updated = ''

                            # 1. Try data_providers first
                            row = db_fetchone(
                                "SELECT access_token_encrypted, updated_at FROM data_providers WHERE provider='upstox'",
                                ()
                            )
                            if row and row[0]:
                                candidate = decrypt_secret(row[0])
                                if candidate:
                                    fresh = candidate
                                    _best_updated = row.get('updated_at', '') or ''
                                    logger.info("[WSManager] 401 (FeedServer) — candidate token from data_providers.")

                            # 2. Check client_broker_instances — may have a MORE RECENT token
                            #    (ReconnectManager saves here before data_providers is updated)
                            try:
                                row2 = db_fetchone(
                                    "SELECT access_token_encrypted, updated_at FROM client_broker_instances "
                                    "WHERE broker='upstox' AND access_token_encrypted IS NOT NULL "
                                    "ORDER BY updated_at DESC LIMIT 1",
                                    ()
                                )
                                if row2 and row2[0]:
                                    candidate2 = decrypt_secret(row2[0])
                                    _inst_updated = row2.get('updated_at', '') or ''
                                    if candidate2 and _inst_updated > _best_updated:
                                        fresh = candidate2
                                        # Propagate back to data_providers so all paths stay in sync
                                        try:
                                            db_execute(
                                                "UPDATE data_providers SET access_token_encrypted=? WHERE provider='upstox'",
                                                (encrypt_secret(fresh),)
                                            )
                                        except Exception:
                                            pass
                                        logger.info(
                                            f"[WSManager] 401 (FeedServer) — fresher token from client_broker_instances "
                                            f"(updated {_inst_updated}) propagated to data_providers."
                                        )
                            except Exception:
                                pass

                            if fresh:
                                self.access_token = fresh
                            else:
                                logger.warning(
                                    "[WSManager] 401 on auth (FeedServer) — no token found in DB. "
                                    "Go to Admin → Data Providers → Upstox and click 'Connect Now'."
                                )
                        except Exception as _db_err:
                            logger.debug(f"[WSManager] Could not reload token from DB: {_db_err}")

                    if "401" in str(e) and active_client and hasattr(active_client, 'auth_handler'):
                        try:
                            from web.db import db_fetchone
                            from web.auth import decrypt_secret
                            current_tok = active_client.auth_handler.get_access_token()
                            fresh = None

                            # 1. Try data_providers (global feed token — admin-managed)
                            row = db_fetchone(
                                "SELECT access_token_encrypted FROM data_providers WHERE provider='upstox'",
                                ()
                            )
                            if row and row[0]:
                                candidate = decrypt_secret(row[0])
                                if candidate and candidate != current_tok:
                                    fresh = candidate

                            # 2. Fallback: try any Upstox broker instance token (covers the case
                            #    where the Upstox broker auto-logged-in but data_providers is stale
                            #    and only a non-Upstox broker bot is running today).
                            if not fresh:
                                row2 = db_fetchone(
                                    "SELECT access_token_encrypted FROM client_broker_instances "
                                    "WHERE broker='upstox' AND access_token_encrypted IS NOT NULL "
                                    "ORDER BY updated_at DESC LIMIT 1",
                                    ()
                                )
                                if row2 and row2[0]:
                                    candidate = decrypt_secret(row2[0])
                                    if candidate and candidate != current_tok:
                                        fresh = candidate
                                        # Propagate to data_providers so next attempt uses it
                                        try:
                                            from web.db import db_execute
                                            from web.auth import encrypt_secret
                                            db_execute(
                                                "UPDATE data_providers SET access_token_encrypted=? WHERE provider='upstox'",
                                                (encrypt_secret(fresh),)
                                            )
                                        except Exception:
                                            pass

                            if fresh:
                                # Update whichever attribute the auth_handler actually uses:
                                # AuthHandler (auth_manager.py) uses .access_token;
                                # inline handlers in upstox_client.py / provider_factory.py use .token
                                _ah = active_client.auth_handler
                                if hasattr(_ah, 'access_token'):
                                    _ah.access_token = fresh
                                elif hasattr(_ah, 'token'):
                                    _ah.token = fresh
                                elif hasattr(_ah, '_token'):
                                    _ah._token = fresh
                                logger.info("[WSManager] 401 on auth — reloaded fresh token from DB into auth_handler.")
                            else:
                                logger.warning(
                                    "[WSManager] 401 on auth — no fresher token found in DB. "
                                    "Go to Admin → Data Providers → Upstox and enter today's access token."
                                )
                        except Exception as _db_err:
                            logger.debug(f"[WSManager] Could not reload token from DB: {_db_err}")

                    if attempt == max_retries - 1:
                        logger.error(f"Failed to authorize WebSocket feed after {max_retries} attempts: {e}", exc_info=True)
                        raise

                    logger.warning(f"WebSocket auth attempt {attempt + 1} failed: {e}. Retrying...")
                    await asyncio.sleep(base_delay)
            
            raise RuntimeError("Exhausted retries for WebSocket authorization.")

    async def connect_and_listen(self):
        """
        Main loop to connect and reconnect to the WebSocket.
        """
        self._running = True
        logger.info("WebSocketManager starting connection loop.")
        while self._running and self.reconnect_attempts < self.max_reconnect_attempts:
            try:
                logger.info(f"Attempting to authorize and connect... (Attempt {self.reconnect_attempts + 1})")
                uri = await self._get_auth_uri()
                ssl_context = ssl.create_default_context()
                ssl_context.check_hostname = False
                ssl_context.verify_mode = ssl.CERT_NONE

                active_client = self.api_client
                if not active_client and self.api_client_manager:
                    active_client = self.api_client_manager.get_active_client()

                if not active_client:
                    logger.error("Cannot connect WebSocket: No active API client.")
                    await asyncio.sleep(self.reconnect_delay)
                    continue

                headers = {
                    'Authorization': f'Bearer {active_client.auth_handler.get_access_token()}'
                }

                logger.info(f"Opening WebSocket connection to {uri[:50]}...")
                async with websockets.connect(uri, ssl=ssl_context, additional_headers=headers, ping_interval=30, ping_timeout=20) as websocket:
                    self.websocket = websocket
                    self.is_connected = True
                    self.reconnect_attempts = 0
                    logger.info("WebSocket connection established successfully.")

                    if self.subscriptions:
                        logger.info(f"V2 WebSocket: Sending re-subscription request for {len(self.subscriptions)} instruments after reconnect.")
                        # Ensure we clear any stale state in handler tasks
                        self._handler_tasks.clear()
                        await self._send_subscription_request()
                        logger.info("V2 WebSocket: Re-subscription request SENT.")
                    else:
                        logger.warning("V2 WebSocket: No existing instruments to re-subscribe to.")

                    # Start proactive watchdog, message processor and latency monitor
                    import time as _time
                    self._last_message_time = asyncio.get_event_loop().time()
                    self._last_tick_epoch = _time.time()
                    self._watchdog_task = asyncio.create_task(self._run_watchdog())
                    self._processor_task = asyncio.create_task(self._message_processor())
                    self._latency_task = asyncio.create_task(self._run_latency_monitor())

                    try:
                        await self._listen_messages(websocket)
                    finally:
                        if self._watchdog_task:
                            self._watchdog_task.cancel()
                        if self._processor_task:
                            self._processor_task.cancel()
                        if self._latency_task:
                            self._latency_task.cancel()

            except (websockets.ConnectionClosed, AttributeError) as e:
                # Handle transient AttributeErrors that sometimes occur in websockets/aiohttp during disconnect
                if isinstance(e, AttributeError):
                    err_str = str(e)
                    if "resume_reading" in err_str or "NoneType" in err_str or "object has no attribute" in err_str:
                        # Reduced priority for disconnect cleanup
                        logger.debug(f"Caught transient AttributeError during WebSocket closure: {err_str}")
                    else:
                        logger.error(f"An unexpected critical AttributeError occurred: {e}", exc_info=True)
                        self._running = False
                        break
                else:
                    logger.warning(f"WebSocket connection issue: {type(e).__name__} ({e}). Attempting to reconnect...")
            except Exception as e:
                logger.error(f"An unexpected error occurred in the connection loop: {e}", exc_info=True)
            finally:
                self.is_connected = False
                self.websocket = None
                if self._running:
                    self.reconnect_attempts += 1
                    logger.info(f"Reconnect attempt {self.reconnect_attempts}/{self.max_reconnect_attempts} in {self.reconnect_delay} seconds...")
                    await asyncio.sleep(self.reconnect_delay)

        if not self._running:
            logger.info("WebSocket manager stopped.")
        else:
            logger.error("Failed to reconnect to WebSocket after multiple attempts. Please restart.")

    async def _run_latency_monitor(self):
        """Monitors the event loop for stalls and blocks."""
        last_time = asyncio.get_event_loop().time()
        while True:
            try:
                await asyncio.sleep(1.0)
                now = asyncio.get_event_loop().time()
                # Expected time is 1.0s, anything significantly more is a stall
                drift = now - last_time - 1.0
                # Increased threshold to 5.0s to reduce noise during high-volatility bursts (e.g. Market Open)
                if drift > 5.0:
                    logger.warning(f"EVENT LOOP STALL DETECTED: Loop blocked for {drift:.2f}s! This can cause WebSocket disconnects.")
                last_time = now
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in latency monitor: {e}")

    async def _run_watchdog(self):
        """Proactively monitors silence and forces reconnection."""
        while True:
            try:
                await asyncio.sleep(10)
                silence_duration = asyncio.get_event_loop().time() - self._last_message_time

                # HEARTBEAT & MONITORING
                qsize = self._message_queue.qsize()
                # Increased thresholds to reduce log noise: only log if queue > 500 or silence > 60s
                if qsize > 500 or silence_duration > 60:
                    logger.info(f"WS WATCHDOG: Silence: {silence_duration:.1f}s | Queue: {qsize} | Handlers: {len(self.message_handlers)}")

                # During pre-market hours (before 09:15 IST) exchanges don't stream ticks,
                # so silence is expected.  Use a longer threshold to avoid needless reconnects.
                import datetime as _dt, pytz as _ptz
                _now_ist = _dt.datetime.now(_ptz.timezone('Asia/Kolkata'))
                _market_open = _now_ist.replace(hour=9, minute=15, second=0, microsecond=0)
                _stale_threshold = 300 if _now_ist < _market_open else 45

                if silence_duration > _stale_threshold:
                    logger.error(f"WATCHDOG: Proactive detection of WebSocket SILENCE for {silence_duration:.0f}s. Forcing reconnect.")
                    if self.websocket:
                        # Closing the websocket will trigger an exception in the listener loop
                        # Use a timeout to avoid hanging the watchdog itself
                        try:
                            # Note: We don't break the loop here. We want the watchdog to keep trying
                            # if the connection stays silent and doesn't close successfully.
                            await asyncio.wait_for(self.websocket.close(code=1001, reason="Watchdog silence timeout"), timeout=5.0)
                        except Exception as e:
                            logger.warning(f"Watchdog: Forced close failed or timed out: {e}")
                            # Final fallback: attempt transport-level closure
                            try:
                                if hasattr(self.websocket, 'transport') and self.websocket.transport:
                                    self.websocket.transport.close()
                            except: pass
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in WebSocket watchdog: {e}")

    async def _listen_messages(self, websocket):
        """
        Inner loop to listen for messages on an active connection.
        Optimized to ONLY receive and queue messages, minimizing loop blockages.
        """
        while self.is_connected and websocket is not None:
            try:
                # 30s timeout to detect stale connections
                try:
                    message = await asyncio.wait_for(websocket.recv(), timeout=30.0)
                except AttributeError as ae:
                    # Specific hardening for 'NoneType' object has no attribute 'resume_reading'
                    # which can occur in some websockets/aiohttp versions during concurrent close.
                    if "resume_reading" in str(ae) or "NoneType" in str(ae):
                        # Reduced priority to debug for expected cleanup logs
                        logger.debug("Caught transient AttributeError in WebSocket recv loop. Connection likely closing.")
                        break
                    raise

                import time as _time
                self._last_message_time = asyncio.get_event_loop().time()
                self._last_tick_epoch = _time.time()

                try:
                    # Put into queue for background processing
                    self._message_queue.put_nowait(message)
                except asyncio.QueueFull:
                    # AGGRESSIVE RECOVERY: Clear 25% of the queue to make space for fresh ticks
                    # During massive volatility, dropping a few old ticks is better than lagging
                    try:
                        num_to_drop = self._message_queue.maxsize // 4
                        for _ in range(num_to_drop):
                            try: self._message_queue.get_nowait()
                            except asyncio.QueueEmpty: break
                            self._message_queue.task_done()

                        self._message_queue.put_nowait(message)
                        logger.warning(f"WebSocket queue OVERFLOW. Purged {num_to_drop} old packets.")
                    except Exception: pass

            except asyncio.TimeoutError:
                if not self.is_connected or websocket is None: break

                # WATCHDOG: Force reconnect on prolonged silence.
                # Pre-market: 300s threshold (no ticks expected before 09:15).
                # Market hours: 90s threshold (data should flow every few seconds).
                silence_duration = asyncio.get_event_loop().time() - self._last_message_time
                import datetime as _dt2, pytz as _ptz2
                _now2 = _dt2.datetime.now(_ptz2.timezone('Asia/Kolkata'))
                _stale2 = 300 if _now2 < _now2.replace(hour=9, minute=15, second=0, microsecond=0) else 45
                if silence_duration > _stale2:
                    logger.error(f"WATCHDOG: WebSocket SILENCE for {silence_duration:.0f}s. Forcing reconnect.")
                    break

                logger.warning("No WebSocket data for 30s, sending ping...")
                try:
                    pong = await websocket.ping()
                    await asyncio.wait_for(pong, timeout=10.0)
                except Exception:
                    logger.error("WebSocket ping failed, reconnecting...")
                    break
            except (websockets.ConnectionClosed, AttributeError):
                # Re-raise to be handled by the outer connection loop
                raise
            except Exception as e:
                logger.error(f"Error in listener loop: {e}", exc_info=True)
                break

    async def _message_processor(self):
        """Background task to parse and route messages from the queue."""
        logger.info("WebSocket message processor started.")
        first_message_received = False

        # PERFORMANCE: Pre-resolve sync/async handlers to avoid repeating checks in hot loop
        resolved_handlers = []
        for h in self.message_handlers:
            if hasattr(h, 'handle_message'):
                is_async = inspect.iscoroutinefunction(h.handle_message)
                resolved_handlers.append((h.handle_message, is_async, f"{type(h).__name__}.handle_message"))
            elif callable(h):
                is_async = inspect.iscoroutinefunction(h)
                resolved_handlers.append((h, is_async, getattr(h, '__name__', 'unnamed')))

        while self._running:
            try:
                q_size = self._message_queue.qsize()
                if q_size > 500:
                    logger.warning(f"WS PROCESSOR: Falling behind! Queue size: {q_size}")

                message = await self._message_queue.get()

                if not first_message_received:
                    logger.info("First market data packet processed from queue. Data feed is active.")
                    first_message_received = True

                try:
                    feed_response = pb.FeedResponse()
                    feed_response.ParseFromString(message)

                    # Debug logging for Upstox ticks
                    if feed_response.feeds:
                        for key, feed in feed_response.feeds.items():
                            ltp = 0
                            if feed.HasField('ltpc'): ltp = feed.ltpc.ltp
                            elif feed.HasField('fullFeed'):
                                if feed.fullFeed.HasField('indexFF'): ltp = feed.fullFeed.indexFF.ltpc.ltp
                                elif feed.fullFeed.HasField('marketFF'): ltp = feed.fullFeed.marketFF.ltpc.ltp
                            if ltp > 0:
                                logger.debug(f"[Upstox-DEBUG] Tick: Key={key}, LTP={ltp}")

                    # PERFORMANCE: Linear execution for zero overhead, or tasks for concurrency
                    # We avoid 'gather' which has significant allocation overhead in hot loops
                    for func, is_async, name in resolved_handlers:
                        try:
                            if is_async:
                                # We create a background task instead of awaiting to keep the processor moving
                                asyncio.create_task(func(feed_response))
                            else:
                                # Run sync handlers in thread pool
                                self._executor.submit(func, feed_response)
                        except Exception as e:
                            logger.error(f"Dispatch Error in {name}: {e}")

                except Exception as e:
                    logger.error(f"WebSocket processing error: {e}", exc_info=True)
                finally:
                    self._message_queue.task_done()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in message processor loop: {e}", exc_info=True)
                await asyncio.sleep(1)

    async def _run_handler_safe(self, handler, data):
        """Run handler with automatic sync/async detection and timeout"""
        try:
            start_time = asyncio.get_event_loop().time()

            # Get callable
            if callable(handler) and not hasattr(handler, 'handle_message'):
                func = handler
                name = getattr(handler, '__name__', 'handler')
            elif hasattr(handler, 'handle_message'):
                func = handler.handle_message
                name = f"{type(handler).__name__}.handle_message"
            else:
                return

            # Detect async vs sync
            if inspect.iscoroutinefunction(func):
                # ASYNC: Run directly
                await asyncio.wait_for(func(data), timeout=10.0)
            else:
                # SYNC: Run in thread pool to avoid blocking
                loop = asyncio.get_running_loop()
                await asyncio.wait_for(
                    loop.run_in_executor(self._executor, func, data),
                    timeout=10.0
                )

            elapsed = asyncio.get_event_loop().time() - start_time
            # Increased threshold to 5.0s to reduce noise during high-volatility bursts
            if elapsed > 5.0:
                logger.warning(f"Slow WebSocket handler {name}: {elapsed:.2f}s")

        except asyncio.TimeoutError:
            logger.error(f"TIMEOUT: WebSocket handler {name} blocked >10s")
        except Exception as e:
            logger.error(f"Handler error in {name}: {e}", exc_info=True)


    async def _send_subscription_request(self):
        if not self.is_connected or not self.websocket:
            logger.warning("Cannot subscribe, WebSocket is not connected.")
            return

        subs_by_mode = {}
        for symbol, mode in self.subscriptions.items():
            if mode not in subs_by_mode:
                subs_by_mode[mode] = []
            subs_by_mode[mode].append(symbol)

        for mode, symbols in subs_by_mode.items():
            data = {
                "guid": "guid-1",
                "method": "sub",
                "data": {
                    "mode": mode,
                    "instrumentKeys": symbols
                }
            }
            await self.websocket.send(json.dumps(data).encode('utf-8'))

    async def _send_unsubscription_request(self, symbols):
        if not self.is_connected or not self.websocket:
            logger.warning("Cannot unsubscribe, WebSocket is not connected.")
            return

        data = {
            "guid": "guid-1",
            "method": "unsub",
            "data": {
                "instrumentKeys": symbols
            }
        }
        await self.websocket.send(json.dumps(data).encode('utf-8'))

    def subscribe(self, symbols, mode='full'):
        new_subscriptions_by_mode = {}
        for symbol in symbols:
            if symbol not in self.subscriptions:
                self.subscriptions[symbol] = mode
                if mode not in new_subscriptions_by_mode:
                    new_subscriptions_by_mode[mode] = []
                new_subscriptions_by_mode[mode].append(symbol)

        if new_subscriptions_by_mode:
            logger.info(
                f"[WSManager] Subscribe request: {len(sum(new_subscriptions_by_mode.values(), []))} new symbols "
                f"(mode={mode}). Sample: {list(sum(new_subscriptions_by_mode.values(), []))[:3]}. "
                f"WS connected={self.is_connected}, total subscriptions={len(self.subscriptions)}"
            )
            if self.is_connected:
                for sub_mode, sub_symbols in new_subscriptions_by_mode.items():
                    asyncio.create_task(self._send_specific_subscription_request(sub_symbols, sub_mode))

    async def _send_specific_subscription_request(self, symbols, mode):
        if not self.is_connected or not self.websocket:
            logger.warning("Cannot subscribe, WebSocket is not connected.")
            return

        logger.info(
            f"[WSManager] Sending subscription to Upstox: {len(symbols)} symbols (mode={mode}). "
            f"Keys: {symbols[:3]}{'...' if len(symbols) > 3 else ''}"
        )

        data = {
            "guid": "guid-2",
            "method": "sub",
            "data": {
                "mode": mode,
                "instrumentKeys": symbols
            }
        }
        try:
            await self.websocket.send(json.dumps(data).encode('utf-8'))
            logger.debug(f"[WSManager] Subscription message sent successfully for {len(symbols)} symbols.")
        except Exception as _sub_err:
            logger.error(f"[WSManager] Failed to send subscription message: {_sub_err}")

    def unsubscribe(self, symbols):
        unsubscribed = False
        for symbol in symbols:
            if symbol in self.subscriptions:
                del self.subscriptions[symbol]
                unsubscribed = True

        if unsubscribed and self.is_connected:
            asyncio.create_task(self._send_unsubscription_request(symbols))

    def start(self):
        """
        Starts the WebSocket connection and listening loop in a background task.
        Returns the task object.
        """
        if not self._listener_task:
            self._listener_task = asyncio.create_task(self.connect_and_listen())
            logger.info("WebSocket listener task created.")
        return self._listener_task

    async def close(self):
        self._running = False
        self.is_connected = False
        self._executor.shutdown(wait=False)
        if self.websocket:
            await self.websocket.close()
            logger.info("WebSocket connection closed by client.")
        if self._listener_task:
            self._listener_task.cancel()
            try:
                await self._listener_task
            except asyncio.CancelledError:
                logger.info("WebSocket listen task cancelled.")

    def refresh_credentials(self, access_token: str, api_key: str = None) -> None:
        """
        Update the access token on the live auth object so the next reconnect uses it.
        RestApiClient stores credentials under auth_handler; WebSocketManager's _get_auth_uri
        reads active_client.auth_handler.get_access_token(). We update auth_handler.token in-place.
        """
        updated = False
        if self.api_client and hasattr(self.api_client, 'auth_handler'):
            auth = self.api_client.auth_handler
            if hasattr(auth, 'token'):
                auth.token = access_token
                updated = True
            elif hasattr(auth, '_token'):
                auth._token = access_token
                updated = True
            if updated:
                logger.info("[WebSocketManager] Upstox auth_handler token refreshed in-place.")
        if self.api_client_manager and hasattr(self.api_client_manager, 'set_access_token'):
            self.api_client_manager.set_access_token(access_token)
            logger.info("[WebSocketManager] Upstox api_client_manager token updated.")
        # Force reconnect so auth re-runs with the new token
        if self.websocket and self.is_connected:
            asyncio.create_task(self._force_reconnect())
        elif not self._listener_task or self._listener_task.done():
            # Connection loop has died — restart it with fresh credentials
            self._running = True
            self.reconnect_attempts = 0
            self._listener_task = asyncio.create_task(self.connect_and_listen())
            logger.info("[WebSocketManager] refresh_credentials — restarted dead connection task.")

    async def _force_reconnect(self):
        """Close the current WS so the reconnect loop re-establishes with the new token."""
        try:
            if self.websocket:
                await self.websocket.close()
                logger.info("[WebSocketManager] Forced reconnect for token refresh.")
        except Exception as e:
            logger.warning(f"[WebSocketManager] Error during forced reconnect: {e}")