import asyncio
import datetime
from utils.logger import logger
from hub.data_feed_base import DataFeed
from hub.event_bus import event_bus

class DualFeedManager(DataFeed):
    """
    Manages concurrent feeds from Upstox and Dhan for global data redundancy.
    Ensures that ticks from any provider are normalized and dispatched to the bot.
    """
    def __init__(self, upstox_feed, dhan_feed):
        self.upstox = upstox_feed
        self.dhan = dhan_feed
        self.message_handlers = []

        # Internal map: Universal Key -> (Dhan Segment, Dhan ID)
        self._key_to_dhan = {}
        # Internal map: Dhan ID -> Universal Key
        self._dhan_id_to_key = {}

    def register_message_handler(self, handler):
        self.message_handlers.append(handler)

        # Upstox feed stays standard
        if self.upstox:
            self.upstox.register_message_handler(handler)

        # Dhan feed gets our custom normalizer
        if self.dhan:
            self.dhan.register_message_handler(self._on_dhan_tick)

    async def _on_dhan_tick(self, source, data):
        """Normalizes ticks from global Dhan feed."""
        if not isinstance(data, dict): return

        sid = str(data.get('security_id') or data.get('SecurityId') or '')
        if not sid: return

        inst_key = self._dhan_id_to_key.get(sid)
        if not inst_key: return

        ltp = data.get('LTP') or data.get('last_price') or data.get('lp')
        if ltp is None: return

        # Normalize to internal format
        tick = {
            'user_id': 'GLOBAL', # Flag as master feed
            'instrument_key': inst_key,
            'ltp': float(ltp),
            'volume': int(data.get('volume') or data.get('vtt') or 0),
            'timestamp': datetime.datetime.now(),
            'broker': 'dhan_redundant'
        }

        if 'OI' in data or 'oi' in data: tick['oi'] = int(data.get('OI') or data.get('oi'))
        if 'atp' in data or 'avg_price' in data:
            tick['atp'] = float(data.get('atp') or data.get('avg_price'))

        # Publish to Event Bus. PriceFeedHandler listens for this.
        logger.debug(f"DualFeedManager dispatching global Dhan tick for {inst_key}: LTP={ltp}")
        await event_bus.publish('BROKER_TICK_RECEIVED', tick)

    def subscribe(self, symbols, mode='full'):
        """symbols: list of universal Upstox-style keys."""
        if self.upstox:
            self.upstox.subscribe(symbols, mode)

        if self.dhan:
            # Kick off async translation and subscription
            asyncio.create_task(self._subscribe_dhan_async(symbols))

    async def _subscribe_dhan_async(self, symbols):
        from utils.broker_rest_adapter import BrokerRestAdapter
        # Dhan Segment Constants
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
                    # Determine numeric segment
                    segment = NSE_FNO
                    if 'INDEX' in key: segment = IDX
                    elif 'NSE_EQ' in key: segment = NSE_EQ
                    elif 'MCX' in key: segment = MCX_COMM

                    item = (segment, str(sid))
                    self._key_to_dhan[key] = item
                    self._dhan_id_to_key[str(sid)] = key
                    dhan_list.append(item)
            except: pass

        if dhan_list:
            self.dhan.subscribe(dhan_list)

    def unsubscribe(self, symbols):
        if self.upstox:
            self.upstox.unsubscribe(symbols)
        if self.dhan:
            to_unsub = [self._key_to_dhan[s] for s in symbols if s in self._key_to_dhan]
            if to_unsub: self.dhan.unsubscribe(to_unsub)

    def start(self):
        from hub.feed_registry import register_feed
        tasks = []
        if self.upstox:
            register_feed('upstox', self.upstox)
            tasks.append(self.upstox.start())
        if self.dhan:
            register_feed('dhan', self.dhan)
            tasks.append(self.dhan.start())

        async def wait_all():
            if tasks: await asyncio.gather(*tasks)
        return asyncio.create_task(wait_all())

    async def close(self):
        from hub.feed_registry import unregister_feed
        if self.upstox:
            unregister_feed('upstox')
            await self.upstox.close()
        if self.dhan:
            unregister_feed('dhan')
            await self.dhan.close()
