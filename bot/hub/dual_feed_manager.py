import asyncio
import time
import datetime
import pytz
from utils.logger import logger
from hub.data_feed_base import DataFeed
from hub.event_bus import event_bus

_KOLKATA = pytz.timezone('Asia/Kolkata')

# How long (seconds) a feed can be silent before the watchdog considers it stale
_STALE_THRESHOLD_SECS = 60
# How often (seconds) the watchdog checks feed health
_WATCHDOG_INTERVAL_SECS = 30


class DualFeedManager(DataFeed):
    """
    Manages concurrent feeds from Upstox and Dhan for global data redundancy.

    Both feeds run simultaneously.  If either goes silent the other continues
    delivering ticks uninterrupted.  A background watchdog detects a silent
    feed and attempts to reconnect + re-subscribe it automatically.
    """

    def __init__(self, upstox_feed, dhan_feed):
        self.upstox = upstox_feed
        self.dhan = dhan_feed
        self.message_handlers = []

        # Internal map: Universal Key -> (Dhan Segment, Dhan ID)
        self._key_to_dhan: dict = {}
        # Internal map: Dhan ID -> Universal Key
        self._dhan_id_to_key: dict = {}

        # Remember every subscription so feeds can be re-subscribed after reconnect
        self._subscribed_symbols: list = []
        self._subscribed_mode: str = 'full'

        # Watchdog task handle
        self._watchdog_task = None
        self._start_time = time.time()

    # ──────────────────────────────────────────────────────────────────────────
    # Handler registration
    # ──────────────────────────────────────────────────────────────────────────

    def register_message_handler(self, handler):
        self.message_handlers.append(handler)

        if self.upstox:
            self.upstox.register_message_handler(handler)

        if self.dhan:
            self.dhan.register_message_handler(self._on_dhan_tick)

    # ──────────────────────────────────────────────────────────────────────────
    # Dhan tick normaliser
    # ──────────────────────────────────────────────────────────────────────────

    async def _on_dhan_tick(self, source, data):
        if not isinstance(data, dict):
            return

        sid = str(data.get('security_id') or data.get('SecurityId') or '')
        if not sid:
            return

        inst_key = self._dhan_id_to_key.get(sid)
        if not inst_key:
            return

        ltp = data.get('LTP') or data.get('last_price') or data.get('lp')
        if ltp is None:
            return

        tick = {
            'user_id': 'GLOBAL',
            'instrument_key': inst_key,
            'ltp': float(ltp),
            'volume': int(data.get('volume') or data.get('vtt') or 0),
            'timestamp': datetime.datetime.now(_KOLKATA),
            'broker': 'dhan_redundant'
        }

        if 'OI' in data or 'oi' in data:
            tick['oi'] = int(data.get('OI') or data.get('oi'))
        atp_raw = data.get('atp') or data.get('avg_price') or data.get('average_price')
        if atp_raw:
            tick['atp'] = float(atp_raw)

        logger.debug(f"DualFeedManager dispatching global Dhan tick for {inst_key}: LTP={ltp}")
        await event_bus.publish('BROKER_TICK_RECEIVED', tick)

    # ──────────────────────────────────────────────────────────────────────────
    # Subscribe / unsubscribe
    # ──────────────────────────────────────────────────────────────────────────

    def subscribe(self, symbols, mode='full'):
        """symbols: list of universal Upstox-style keys."""
        # Track so we can re-subscribe after a reconnect
        for s in symbols:
            if s not in self._subscribed_symbols:
                self._subscribed_symbols.append(s)
        self._subscribed_mode = mode

        if self.upstox:
            self.upstox.subscribe(symbols, mode)

        if self.dhan:
            asyncio.create_task(self._subscribe_dhan_async(symbols))

    async def _subscribe_dhan_async(self, symbols):
        from utils.broker_rest_adapter import BrokerRestAdapter
        IDX = 0
        NSE_EQ = 1
        NSE_FNO = 2
        MCX_COMM = 5

        adapter = BrokerRestAdapter(None, 'dhan')

        dhan_list = []
        for key in symbols:
            if key in self._key_to_dhan:
                dhan_list.append(self._key_to_dhan[key])
                continue
            try:
                sid = await adapter._translate_to_broker_key(key)
                if sid:
                    segment = NSE_FNO
                    if 'INDEX' in key:   segment = IDX
                    elif 'NSE_EQ' in key: segment = NSE_EQ
                    elif 'MCX' in key:    segment = MCX_COMM

                    item = (segment, str(sid))
                    self._key_to_dhan[key] = item
                    self._dhan_id_to_key[str(sid)] = key
                    dhan_list.append(item)
            except Exception:
                pass

        if dhan_list:
            self.dhan.subscribe(dhan_list)

    def unsubscribe(self, symbols):
        for s in symbols:
            if s in self._subscribed_symbols:
                self._subscribed_symbols.remove(s)

        if self.upstox:
            self.upstox.unsubscribe(symbols)
        if self.dhan:
            to_unsub = [self._key_to_dhan[s] for s in symbols if s in self._key_to_dhan]
            if to_unsub:
                self.dhan.unsubscribe(to_unsub)

    # ──────────────────────────────────────────────────────────────────────────
    # Start / stop
    # ──────────────────────────────────────────────────────────────────────────

    def start(self):
        from hub.feed_registry import register_feed
        tasks = []
        if self.upstox:
            register_feed('upstox', self.upstox)
            tasks.append(self.upstox.start())
        if self.dhan:
            register_feed('dhan', self.dhan)
            tasks.append(self.dhan.start())

        # Launch the health watchdog
        self._watchdog_task = asyncio.create_task(self._feed_watchdog())

        async def wait_all():
            if tasks:
                await asyncio.gather(*tasks)
        return asyncio.create_task(wait_all())

    async def close(self):
        from hub.feed_registry import unregister_feed
        if self._watchdog_task and not self._watchdog_task.done():
            self._watchdog_task.cancel()
        if self.upstox:
            unregister_feed('upstox')
            await self.upstox.close()
        if self.dhan:
            unregister_feed('dhan')
            await self.dhan.close()

    # ──────────────────────────────────────────────────────────────────────────
    # Failover watchdog
    # ──────────────────────────────────────────────────────────────────────────

    async def _feed_watchdog(self):
        """
        Runs every _WATCHDOG_INTERVAL_SECS seconds.
        If a feed has not received a tick for _STALE_THRESHOLD_SECS seconds it is
        considered stale.  The watchdog logs a warning and kicks a reconnect on
        that feed.  Because both feeds run concurrently, the other feed continues
        delivering ticks uninterrupted — no manual intervention is required.
        """
        logger.info("[DualFeedWatchdog] Started. Monitoring Upstox + Dhan feeds.")
        while True:
            try:
                await asyncio.sleep(_WATCHDOG_INTERVAL_SECS)
                now = time.time()

                for name, feed in (('upstox', self.upstox), ('dhan', self.dhan)):
                    if feed is None:
                        continue

                    # 2-minute startup grace — don't evaluate staleness before feeds have time to connect
                    if (now - self._start_time) < 120:
                        continue

                    last = getattr(feed, '_last_tick_epoch', None)
                    is_conn = getattr(feed, 'is_connected', False)

                    # Treat 0.0 (default epoch) same as None — feed has never ticked
                    if last is None or last == 0.0:
                        if not is_conn:
                            logger.info(f"[DualFeedWatchdog] {name}: not yet connected.")
                        continue

                    age = now - last
                    if age > _STALE_THRESHOLD_SECS:
                        logger.warning(
                            f"[DualFeedWatchdog] {name} STALE — last tick {age:.0f}s ago. "
                            f"Other feeder continues. Attempting reconnect..."
                        )
                        await self._reconnect_feed(name, feed)
                    else:
                        logger.debug(
                            f"[DualFeedWatchdog] {name} healthy — last tick {age:.1f}s ago."
                        )

            except asyncio.CancelledError:
                logger.info("[DualFeedWatchdog] Stopped.")
                break
            except Exception as e:
                logger.error(f"[DualFeedWatchdog] Error: {e}", exc_info=True)

    async def _reconnect_feed(self, name: str, feed):
        """Attempt a soft reconnect on the given feed, then re-subscribe symbols."""
        if getattr(feed, '_disabled', False):
            logger.debug(f"[DualFeedWatchdog] {name} is disabled — skipping reconnect.")
            return
        try:
            # Refresh Upstox token from DB before reconnecting to avoid 401 loops
            if name == 'upstox' and hasattr(feed, 'refresh_credentials'):
                try:
                    from web.db import db_fetch_one
                    from web.auth import decrypt_secret
                    row = db_fetch_one(
                        "SELECT access_token_encrypted FROM data_providers WHERE provider='upstox'",
                        ()
                    )
                    if row and row[0]:
                        fresh_token = decrypt_secret(row[0])
                        if fresh_token:
                            feed.refresh_credentials(fresh_token)
                            logger.info("[DualFeedWatchdog] upstox token refreshed from DB before reconnect.")
                            return  # refresh_credentials already calls _force_reconnect internally
                except Exception as _tok_err:
                    logger.warning(f"[DualFeedWatchdog] Could not refresh upstox token: {_tok_err}")

            # Force-close the existing connection so the feed's own reconnect loop
            # re-runs _get_auth_uri and reconnects with current credentials.
            if hasattr(feed, '_force_reconnect'):
                await feed._force_reconnect()
                logger.info(f"[DualFeedWatchdog] {name} force-reconnect triggered.")
            elif hasattr(feed, 'close') and hasattr(feed, 'start'):
                await feed.close()
                feed.start()  # start() already creates its own asyncio.Task internally
                logger.info(f"[DualFeedWatchdog] {name} restarted.")

            # After a short delay, re-subscribe so ticks resume
            await asyncio.sleep(5)
            if self._subscribed_symbols:
                if name == 'upstox' and self.upstox:
                    self.upstox.subscribe(self._subscribed_symbols, self._subscribed_mode)
                    logger.info(f"[DualFeedWatchdog] upstox re-subscribed {len(self._subscribed_symbols)} symbols.")
                elif name == 'dhan' and self.dhan:
                    asyncio.create_task(self._subscribe_dhan_async(self._subscribed_symbols))
                    logger.info(f"[DualFeedWatchdog] dhan re-subscription queued for {len(self._subscribed_symbols)} symbols.")

        except Exception as e:
            logger.error(f"[DualFeedWatchdog] Reconnect failed for {name}: {e}", exc_info=True)
