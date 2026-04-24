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

        while self._running:
            try:
                logger.info(f"[Global Dhan] Connecting to Dhan Market Feed (v2)...")
                self.feed = marketfeed.DhanFeed(
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

                self._retry_delay = 2

                while self._running and self.feed.ws and getattr(self.feed.ws, 'open', False):
                    try:
                        # Use direct recv to avoid SDK callback issues
                        raw_message = await asyncio.wait_for(self.feed.ws.recv(), timeout=5.0)
                        if raw_message:
                            self._process_raw_packet(raw_message)
                    except asyncio.TimeoutError:
                        continue
                    except websockets.exceptions.ConnectionClosed:
                        logger.warning("[Global Dhan] WebSocket closed by server.")
                        break
                    except Exception as e:
                        if "no close frame" in str(e).lower(): break
                        logger.error(f"[Global Dhan] Loop error: {e}")
                        break

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

            # Debug logging for Dhan ticks
            sid = processed.get('security_id') or processed.get('SecurityId')
            ltp = processed.get('LTP') or processed.get('last_price')
            if sid and ltp:
                logger.info(f"[Dhan-DEBUG] Received global tick: SID={sid}, LTP={ltp}")

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
