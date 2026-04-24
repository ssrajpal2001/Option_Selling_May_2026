import asyncio
from collections import defaultdict
from utils.logger import logger

class TickDispatcher:
    """
    ULTRA-HIGH SPEED TICK ROUTER:
    Maintains a global mapping of instrument keys to their target orchestrators.
    Ensures each tick is dispatched directly without redundant searching.
    """
    def __init__(self):
        # instrument_key -> list of handler functions
        self._route_map = defaultdict(list)
        # (user_id, instrument_key) -> list of handler functions
        self._user_route_map = defaultdict(list)
        self._lock = asyncio.Lock()

    async def register(self, instrument_key, handler, user_id=None):
        """
        Registers a handler for an instrument.
        If user_id is provided, only ticks from that user's broker feed will trigger this handler.
        """
        async with self._lock:
            if user_id:
                route_key = (user_id, instrument_key)
                if handler not in self._user_route_map[route_key]:
                    self._user_route_map[route_key].append(handler)
                    logger.debug(f"[Dispatcher] Registered user-scoped route for {instrument_key} (User: {user_id})")
            else:
                if handler not in self._route_map[instrument_key]:
                    self._route_map[instrument_key].append(handler)
                    logger.debug(f"[Dispatcher] Registered global route for {instrument_key}")

    async def unregister(self, instrument_key, handler, user_id=None):
        async with self._lock:
            if user_id:
                route_key = (user_id, instrument_key)
                if route_key in self._user_route_map:
                    try:
                        self._user_route_map[route_key].remove(handler)
                        if not self._user_route_map[route_key]:
                            del self._user_route_map[route_key]
                    except ValueError: pass
            else:
                if instrument_key in self._route_map:
                    try:
                        self._route_map[instrument_key].remove(handler)
                        if not self._route_map[instrument_key]:
                            del self._route_map[instrument_key]
                    except ValueError: pass

    def dispatch(self, instrument_key, packet, user_id=None):
        """
        Zero-latency dispatch.
        If user_id is provided, it triggers user-scoped handlers first, then global ones.
        """
        # logger.debug(f"[Dispatcher] Dispatching {instrument_key} (User: {user_id})")
        # 1. User-scoped routing (Private Data)
        if user_id:
            user_handlers = self._user_route_map.get((user_id, instrument_key))
            if user_handlers:
                logger.info(f"DEBUG: Dispatcher found {len(user_handlers)} user-scoped handlers for {instrument_key} (User: {user_id})")
                for h in user_handlers:
                    asyncio.create_task(h(instrument_key, packet, user_id=user_id))
            else:
                logger.info(f"DEBUG: Dispatcher found NO user-scoped handlers for {instrument_key} (User: {user_id})")

        # 2. Global routing (Shared Data like Index/Futures from master feed)
        global_handlers = self._route_map.get(instrument_key)
        if global_handlers:
            for h in global_handlers:
                asyncio.create_task(h(instrument_key, packet))

# Global Singleton
tick_dispatcher = TickDispatcher()
