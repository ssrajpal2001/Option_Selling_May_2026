import asyncio
import datetime
import json
import websockets
from .logger import logger
from hub.data_feed_base import DataFeed
from dhanhq import marketfeed
from hub.event_bus import event_bus
from types import ModuleType
import sys

class DhanWebSocketManager(DataFeed):
    """
    Global WebSocket Manager for Dhan.
    Provides a redundant market data feed for the entire bot.
    """
    def __init__(self, client_id, access_token):
        self.client_id = client_id
        self.access_token = access_token
        self.feed = None
        self.message_handlers = []
        self.subscriptions = set() # Set of (segment, security_id) tuples
        self._running = False
        self._disabled = False  # Set True when DhanFeed class unavailable; prevents watchdog retries
        self._task = None
        self._retry_delay = 2
        self._last_tick_time = 0
        self._last_tick_epoch: float = 0.0  # Real Unix epoch seconds

    @property
    def is_connected(self) -> bool:
        """True when the Dhan WebSocket is actively open."""
        return bool(self.feed and getattr(self.feed, 'ws', None) and getattr(self.feed.ws, 'open', False))

    def register_message_handler(self, handler):
        if handler not in self.message_handlers:
            self.message_handlers.append(handler)

    def _apply_websockets_patch(self):
        """Ensures compatibility between Dhan SDK and modern websockets library."""
        try:
            import websockets
            from websockets import State

            if not hasattr(websockets, 'protocol'):
                protocol_mod = ModuleType('websockets.protocol')
                websockets.protocol = protocol_mod
                sys.modules['websockets.protocol'] = protocol_mod

            if not hasattr(websockets.protocol, 'State'):
                class StateLegacy:
                    CONNECTING, OPEN, CLOSING, CLOSED = State.CONNECTING, State.OPEN, State.CLOSING, State.CLOSED
                websockets.protocol.State = StateLegacy
                logger.debug("[Global Dhan] Patched websockets.protocol.State")

            from websockets.asyncio.client import ClientConnection
            if not hasattr(ClientConnection, 'closed'):
                ClientConnection.closed = property(lambda self: self.state == State.CLOSED)
            if not hasattr(ClientConnection, 'open'):
                ClientConnection.open = property(lambda self: self.state == State.OPEN)
        except Exception as e:
            logger.warning(f"[Global Dhan] websockets patch failed: {e}")

    async def connect_and_listen(self):
        self._running = True
        self._apply_websockets_patch()

        # Resolve the correct feed class across dhanhq versions:
        #   2.0.x → marketfeed.DhanFeed
        #   2.1+  → class name may differ; fall back to scanning for any Feed-like class
        # Each named candidate is verified to be a class (isinstance(..., type)) to
        # guard against future SDK versions that export one of these names as a non-class.
        def _get_cls(mod, name):
            obj = getattr(mod, name, None)
            return obj if isinstance(obj, type) else None

        _DhanFeedCls = (
            _get_cls(marketfeed, 'DhanFeed') or
            _get_cls(marketfeed, 'Feed') or
            _get_cls(marketfeed, 'MarketFeed') or
            _get_cls(marketfeed, 'DhanMarketFeed') or
            _get_cls(marketfeed, 'DhanHQ') or
            next(
                (v for k, v in vars(marketfeed).items()
                 if isinstance(v, type) and ('feed' in k.lower() or 'Feed' in k)),
                None
            )
        )
        if _DhanFeedCls is None:
            available = [k for k in dir(marketfeed) if not k.startswith('_')]
            logger.error(
                "[Global Dhan] Cannot find a feed class in dhanhq.marketfeed. "
                f"Available names: {available}. "
                "Pin dhanhq to a known-good version or check the class name above. "
                "Dhan feed disabled."
            )
            self._disabled = True
            # Set epoch to inf so the watchdog never treats this as a stale feed
            self._last_tick_epoch = float('inf')
            self._running = False
            return

        logger.info(f"[Global Dhan] Using feed class: {_DhanFeedCls.__name__}")

        while self._running:
            try:
                logger.info(f"[Global Dhan] Connecting to Dhan Market Feed (v2)...")
                try:
                    from dhanhq import DhanContext
                except ImportError:
                    DhanContext = None

                if DhanContext:
                    ctx = DhanContext(self.client_id, self.access_token)
                    self.feed = _DhanFeedCls(
                        dhan_context=ctx,
                        instruments=[],
                        version='v2'
                    )
                else:
                    self.feed = _DhanFeedCls(
                        client_id=self.client_id,
                        access_token=self.access_token,
                        instruments=[],
                        version='v2'
                    )

                await self.feed.connect()
                logger.info(f"[Global Dhan] Connection established successfully.")

                # Re-subscribe
                if self.subscriptions:
                    subs_list = list(self.subscriptions)
                    logger.info(f"[Global Dhan] Re-subscribing to {len(subs_list)} instruments.")
                    self.feed.subscribe_symbols(subs_list)

                # Only reset backoff once the connection is genuinely stable — i.e., after
                # at least one valid market-data tick has been parsed by _process_raw_packet.
                # A connect that the server closes before any tick arrives must NOT reset
                # the delay; otherwise the rapid close→reconnect cycle resets to 2s each
                # time and hammers the server into HTTP 429.
                _tick_confirmed = False

                while self._running and self.feed.ws and getattr(self.feed.ws, 'open', False):
                    try:
                        # Use direct recv to avoid SDK callback issues
                        raw_message = await asyncio.wait_for(self.feed.ws.recv(), timeout=5.0)
                        if raw_message:
                            prev_epoch = self._last_tick_epoch
                            self._process_raw_packet(raw_message)
                            if not _tick_confirmed and self._last_tick_epoch != prev_epoch:
                                # _process_raw_packet updated the epoch → confirmed market-data tick
                                self._retry_delay = 2
                                _tick_confirmed = True
                    except asyncio.TimeoutError:
                        continue
                    except websockets.exceptions.ConnectionClosed:
                        logger.warning(f"[Global Dhan] WebSocket closed by server. Next retry in {self._retry_delay}s.")
                        break
                    except Exception as e:
                        if "no close frame" in str(e).lower(): break
                        logger.error(f"[Global Dhan] Loop error: {e}")
                        break

                # Inner loop exited (server close, protocol error, etc.).
                # Apply backoff before reconnecting — critical to prevent HTTP 429 bursts
                # when the server repeatedly closes the connection (e.g. expired token).
                if self._running:
                    await asyncio.sleep(self._retry_delay)
                    self._retry_delay = min(self._retry_delay * 2, 60)

            except Exception as e:
                if self._running:
                    logger.error(f"[Global Dhan] Connection failed: {e}. Retrying in {self._retry_delay}s...")
                    await asyncio.sleep(self._retry_delay)
                    self._retry_delay = min(self._retry_delay * 2, 60)

    def _process_raw_packet(self, raw_data):
        processed = None
        if isinstance(raw_data, bytes):
            try:
                processed = self.feed.process_data(raw_data)
            except: pass

        if not processed:
            try: processed = json.loads(raw_data)
            except: pass

        if processed and isinstance(processed, dict):
            import time as _time
            self._last_tick_epoch = _time.time()

            # Throttled debug logging for Dhan ticks (not on hot path)
            sid = processed.get('security_id') or processed.get('SecurityId')
            ltp = processed.get('LTP') or processed.get('last_price')
            if sid and ltp:
                logger.debug(f"[Dhan] Global tick: SID={sid}, LTP={ltp}")

            # Pass to handlers (e.g. DualFeedManager)
            for h in self.message_handlers:
                if asyncio.iscoroutinefunction(h):
                    asyncio.create_task(h('dhan', processed))
                else:
                    h('dhan', processed)

    def subscribe(self, symbols, mode='full'):
        """symbols: list of (segment, security_id) tuples"""
        new_items = [s for s in symbols if s not in self.subscriptions]
        if not new_items: return

        for s in new_items: self.subscriptions.add(s)

        if self.feed and getattr(self.feed.ws, 'open', False):
            logger.info(f"[Global Dhan] Subscribing to {len(new_items)} new items.")
            self.feed.subscribe_symbols(new_items)

    def unsubscribe(self, symbols):
        to_remove = [s for s in symbols if s in self.subscriptions]
        if not to_remove: return

        for s in to_remove: self.subscriptions.remove(s)

        if self.feed and getattr(self.feed.ws, 'open', False):
            self.feed.unsubscribe_symbols(to_remove)

    def start(self):
        if self._disabled:
            return None
        if not self._task or self._task.done():
            self._task = asyncio.create_task(self.connect_and_listen())
        return self._task

    async def close(self):
        self._running = False
        if self.feed:
            try: await self.feed.disconnect()
            except: pass
        if self._task:
            self._task.cancel()

    def refresh_credentials(self, access_token: str, api_key: str = None) -> None:
        """
        Update the access token and trigger a reconnect with the new credentials.
        The reconnect loop will use the updated token on the next DhanFeed creation.
        """
        self.access_token = access_token
        if api_key:
            self.client_id = api_key
        logger.info("[DhanWebSocketManager] Credentials refreshed. Triggering reconnect...")
        if self.feed:
            asyncio.create_task(self._close_for_reconnect())

    async def _close_for_reconnect(self):
        """Close the current WS connection so the reconnect loop re-establishes with new creds."""
        try:
            if self.feed:
                await self.feed.disconnect()
                logger.info("[DhanWebSocketManager] Forced disconnect for credential refresh.")
        except Exception as e:
            logger.warning(f"[DhanWebSocketManager] Error during forced disconnect: {e}")
