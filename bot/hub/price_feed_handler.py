import asyncio
import datetime
import pytz
from collections import deque
from utils.logger import logger
from hub.event_bus import event_bus
from hub.tick_dispatcher import tick_dispatcher

class PriceFeedHandler:
    def __init__(self, state_manager, atm_manager, trade_orchestrator, entry_aggregator, exit_aggregator, one_min_aggregator, five_min_aggregator):
        self.state_manager = state_manager
        self.atm_manager = atm_manager
        self.trade_orchestrator = trade_orchestrator
        self.entry_aggregator = entry_aggregator
        self.exit_aggregator = exit_aggregator
        self.one_min_aggregator = one_min_aggregator
        self.five_min_aggregator = five_min_aggregator
        self._last_process_tick_time = 0

        # Dedicated worker for tick processing
        self._tick_event = asyncio.Event()
        self._worker_task = asyncio.create_task(self._tick_worker())
        self._cache_rebuild_task = asyncio.create_task(self._keys_rebuild_loop())

        self._relevant_keys_cache = set()
        self._last_keys_rebuild_time = 0
        self._kolkata_tz = pytz.timezone('Asia/Kolkata')

        # Get the keys for differentiation directly from the orchestrator
        # to ensure consistency with initial subscriptions.
        self.index_instrument_key = self.trade_orchestrator.index_instrument_key
        self.futures_instrument_key = self.trade_orchestrator.futures_instrument_key

        # Redundancy tracking: {instrument_key: last_received_time}
        self._last_tick_times_any_source = {}

        # Multi-broker normalized tick listener
        event_bus.subscribe('BROKER_TICK_RECEIVED', self.handle_normalized_tick)

        # Register standard handlers with the dispatcher
        # This ensures legacy Upstox ticks still work via handle_tick_dispatched
        # and new normalized ticks work via handle_tick_dispatched_normalized
        asyncio.create_task(tick_dispatcher.register(self.index_instrument_key, self.handle_tick_dispatched))
        asyncio.create_task(tick_dispatcher.register(self.futures_instrument_key, self.handle_tick_dispatched))

        # Register normalized handlers for global instruments
        asyncio.create_task(tick_dispatcher.register(self.index_instrument_key, self.handle_tick_dispatched_normalized))
        asyncio.create_task(tick_dispatcher.register(self.futures_instrument_key, self.handle_tick_dispatched_normalized))

    def _extract_timestamp(self, feed, fallback_ts):
        """Extracts the best available exchange timestamp (LTT) from a feed."""
        ltt_ms = 0
        if feed.HasField('ltpc'):
            ltt_ms = feed.ltpc.ltt
        elif feed.HasField('fullFeed'):
            if feed.fullFeed.HasField('indexFF'):
                ltt_ms = feed.fullFeed.indexFF.ltpc.ltt
            elif feed.fullFeed.HasField('marketFF'):
                ltt_ms = feed.fullFeed.marketFF.ltpc.ltt
        elif feed.HasField('firstLevelWithGreeks'):
             ltt_ms = feed.firstLevelWithGreeks.ltpc.ltt

        if ltt_ms > 0:
            # Upstox V3 uses milliseconds for currentTs and LTT
            ts_s = ltt_ms / 1000.0 if ltt_ms > 10**11 else ltt_ms
            return datetime.datetime.fromtimestamp(ts_s, tz=self._kolkata_tz)

        return fallback_ts

    async def handle_message(self, feed_response):
        """
        MODULAR ENTRY POINT:
        Only handles packet timestamping. Dispatches ticks via TickDispatcher.
        """
        if hasattr(feed_response, 'currentTs') and feed_response.currentTs > 0:
            ts_s = feed_response.currentTs / 1000.0 if feed_response.currentTs > 10**11 else feed_response.currentTs
            self.state_manager.last_exchange_time = datetime.datetime.fromtimestamp(ts_s, tz=self._kolkata_tz)

        if not feed_response.feeds: return

        # Dispatch each tick to registered orchestrators
        for key, feed in feed_response.feeds.items():
            tick_dispatcher.dispatch(key, feed)

    async def handle_normalized_tick(self, data):
        """
        Handler for standardized ticks from ANY broker.
        Enables High Availability Redundancy.
        """
        instrument_key = data.get('instrument_key')
        ltp = data.get('ltp')
        timestamp = data.get('timestamp') or datetime.datetime.now(self._kolkata_tz)
        broker_source = data.get('broker', 'unknown')
        user_id = data.get('user_id')

        # Keep last_exchange_time current so Feed Lag is shown in heartbeat
        if timestamp:
            ts = timestamp
            if getattr(ts, 'tzinfo', None) is None:
                ts = self._kolkata_tz.localize(ts)
            self.state_manager.last_exchange_time = ts

        # DUAL-FEED REDUNDANCY LOGIC (FAILOVER)
        # We only process this tick if it's "new" for this instrument.
        # This prevents slower redundant feeds from overriding faster ones.
        if instrument_key in self._last_tick_times_any_source:
            # If the tick is significantly old compared to the last one we saw, drop it
            last_ts = self._last_tick_times_any_source[instrument_key]
            if timestamp < last_ts:
                # logger.debug(f"[Redundancy] Dropped late tick from {broker_source} for {instrument_key}")
                return

        self._last_tick_times_any_source[instrument_key] = timestamp

        # Global redundancy uses 'GLOBAL' user_id
        target_user_id = data.get('user_id')

        # Route through dispatcher to ensure all registered user sessions get the update
        tick_dispatcher.dispatch(instrument_key, data, user_id=target_user_id)

    async def handle_tick_dispatched_normalized(self, instrument_key, data, user_id=None):
        """
        Final handler for ticks routed via Dispatcher (both Global and User-Scoped).
        Only processes normalized dict payloads — silently ignores Protobuf feed objects
        that arrive via the legacy handle_message → dispatch path.
        """
        if not isinstance(data, dict):
            return
        ltp = data.get('ltp')
        timestamp = data.get('timestamp') or datetime.datetime.now(self._kolkata_tz)

        if ltp is None:
            return

        # 1. Update Global State (Shared data)
        if instrument_key == self.futures_instrument_key:
            await self._update_spot_data(instrument_key, ltp, timestamp, is_futures=True, target_user_id=user_id)
        elif instrument_key == self.index_instrument_key:
            await self._update_spot_data(instrument_key, ltp, timestamp, is_futures=False, target_user_id=user_id)
        else:
            # 2. Update User-Specific State
            self._process_option_tick_normalized(user_id, instrument_key, ltp, timestamp, data)

        self._tick_event.set()

    async def _update_spot_data(self, instrument_key, ltp, timestamp, is_futures, target_user_id=None):
        """Unified spot/futures updater for normalized ticks."""
        self.entry_aggregator.add_tick(instrument_key, ltp, timestamp)
        self.exit_aggregator.add_tick(instrument_key, ltp, timestamp)
        self.one_min_aggregator.add_tick(instrument_key, ltp, timestamp)
        self.five_min_aggregator.add_tick(instrument_key, ltp, timestamp)

        is_mcx = any(x in self.trade_orchestrator.instrument_name.upper() for x in ['CRUDE', 'NATURAL', 'GOLD', 'SILVER'])

        if is_futures:
            self.state_manager.spot_price = ltp
            self._sync_market_data('spot_price', None, ltp, is_primitive=True)
            if is_mcx:
                await event_bus.publish('SPOT_PRICE_UPDATE', {'instrument': self.trade_orchestrator.instrument_name, 'ltp': ltp})
        else:
            self.state_manager.index_price = ltp
            self._sync_market_data('index_price', None, ltp, is_primitive=True)
            if not is_mcx:
                await event_bus.publish('SPOT_PRICE_UPDATE', {'instrument': self.trade_orchestrator.instrument_name, 'ltp': ltp})

    def _process_option_tick_normalized(self, user_id, instrument_key, ltp, timestamp, extra_data):
        """Processes an option tick from a specific broker/user source."""
        # Find the target StateManager (Global or User-Specific)
        is_global = (user_id == 'GLOBAL')
        target_sm = self.state_manager
        if user_id and user_id in self.trade_orchestrator.user_sessions:
            target_sm = self.trade_orchestrator.user_sessions[user_id].state_manager

        target_sm.option_prices[instrument_key] = ltp
        target_sm.last_tick_times[instrument_key] = timestamp

        # If this is a global redundant feed tick, sync it to all active user sessions
        if is_global:
            self._sync_market_data('option_prices', instrument_key, ltp)
            self._sync_market_data('last_tick_times', instrument_key, timestamp)

        atp = extra_data.get('atp')
        if atp:
            if not hasattr(target_sm, 'option_atps'): target_sm.option_atps = {}
            target_sm.option_atps[instrument_key] = atp

            # ATP History for Data Recording & VWAP Indicators
            minute_ts = timestamp.replace(second=0, microsecond=0) if hasattr(timestamp, 'replace') else None
            if minute_ts:
                if not hasattr(target_sm, 'atp_history'): target_sm.atp_history = {}
                if instrument_key not in target_sm.atp_history: target_sm.atp_history[instrument_key] = {}

                hist_for_key = target_sm.atp_history[instrument_key]
                prev_minute_ts = hist_for_key.get('_last_minute')
                hist_for_key[minute_ts] = atp

                if prev_minute_ts and prev_minute_ts != minute_ts:
                    prev_atp = hist_for_key.get(prev_minute_ts)
                    data_recorder = getattr(self.trade_orchestrator, 'data_recorder', None)
                    exchange_open = self.trade_orchestrator.get_exchange_open_time(timestamp)

                    if data_recorder and prev_atp and timestamp >= exchange_open:
                        atm_mgr = getattr(self.trade_orchestrator, 'atm_manager', None)
                        strike, side = None, None
                        if atm_mgr:
                            contract = atm_mgr.contracts.get(instrument_key)
                            if contract:
                                strike = getattr(contract, 'strike_price', None)
                                side = 'CE' if getattr(contract, 'instrument_type', '') in ('CE',) else 'PE'

                        spot = target_sm.index_price
                        futures = target_sm.spot_price
                        extra = {}
                        smv3 = getattr(self.trade_orchestrator, 'sell_manager', None)
                        # Prefer user-specific sell manager if available
                        if user_id and user_id in self.trade_orchestrator.user_sessions:
                             session_sm = self.trade_orchestrator.user_sessions[user_id].sell_manager
                             if session_sm: smv3 = session_sm

                        if smv3 and hasattr(smv3, 'v3_dashboard_data'):
                            extra['rsi'] = smv3.v3_dashboard_data.get('combined_rsi')
                            extra['roc'] = smv3.v3_dashboard_data.get('combined_roc')
                            extra['v_slope'] = smv3.v3_dashboard_data.get('curr_slope')
                            extra['combined_vwap'] = smv3.v3_dashboard_data.get('combined_vwap')

                        data_recorder.record_atp_snapshot(prev_minute_ts, instrument_key, strike, side, prev_atp, ltp, spot, futures, extra_indicators=extra)

                hist_for_key['_last_minute'] = minute_ts

        # Update target StateManager's PnL
        target_sm.update_pnl_for_tick(instrument_key, ltp)

        # Multi-broker: Trigger user-specific SellManager logic
        if is_global:
            # For global ticks, trigger all active sessions
            for session in self.trade_orchestrator.user_sessions.values():
                if session.sell_manager and not session.sell_manager.strangle_closed:
                    tick_dict = {instrument_key: {'ltp': float(ltp)}}
                    asyncio.create_task(session.sell_manager.on_tick(tick_dict, timestamp))
        elif user_id and user_id in self.trade_orchestrator.user_sessions:
            session = self.trade_orchestrator.user_sessions[user_id]
            if session.sell_manager and not session.sell_manager.strangle_closed:
                # Wrap tick in the format SellManager expects
                tick_dict = {instrument_key: {'ltp': float(ltp)}}
                # We use create_task to avoid blocking the hot tick processing path
                asyncio.create_task(session.sell_manager.on_tick(tick_dict, timestamp))

        # Feed aggregators (Global logic remains same, but technically we could isolate these per user if needed)
        self.entry_aggregator.add_tick(instrument_key, ltp, timestamp)
        self.exit_aggregator.add_tick(instrument_key, ltp, timestamp)
        self.one_min_aggregator.add_tick(instrument_key, ltp, timestamp)
        self.five_min_aggregator.add_tick(instrument_key, ltp, timestamp)

    async def handle_tick_dispatched(self, instrument_key, feed):
        """Legacy dispatched handler for individual ticks (Upstox Protobuf)."""
        if isinstance(feed, dict):
            return  # Normalized dict ticks are handled by handle_tick_dispatched_normalized
        packet_now = self.state_manager.last_exchange_time or self.trade_orchestrator._get_timestamp()
        now = self._extract_timestamp(feed, packet_now)

        # Redundancy check: ignore ticks that are older than the latest seen from any source
        if instrument_key in self._last_tick_times_any_source:
            if now < self._last_tick_times_any_source[instrument_key]:
                return
        self._last_tick_times_any_source[instrument_key] = now

        if instrument_key == self.futures_instrument_key:
            await self._handle_spot_feed(instrument_key, feed, now, is_futures=True)
        elif instrument_key == self.index_instrument_key:
            await self._handle_spot_feed(instrument_key, feed, now, is_futures=False)
        else:
            self._handle_option_feed_sync(instrument_key, feed, now)

        self._tick_event.set()

    def _rebuild_relevant_keys(self):
        """Rebuilds the cache of instrument keys this orchestrator cares about."""
        try:
            relevant_keys = {self.futures_instrument_key, self.index_instrument_key}

            for strike_info in self.atm_manager.contracts.values():
                for opt_type in ['CE', 'PE']:
                    contract = strike_info.get(opt_type, {})
                    key = contract.get('key') if isinstance(contract, dict) else getattr(contract, 'instrument_key', None)
                    if key: relevant_keys.add(key)

            # Multi-tenant: Check all user sessions for active trades or monitoring
            for session in self.trade_orchestrator.user_sessions.values():
                sm = session.state_manager
                expiry = self.atm_manager.signal_expiry_date

                for pos in [sm.call_position, sm.put_position]:
                    if pos:
                        relevant_keys.add(pos.get('instrument_key'))
                        # MANDATORY: Add instruments for S1LOW monitoring strike
                        sl_strike = pos.get('s1_monitoring_strike')
                        if sl_strike and expiry:
                            relevant_keys.add(self.atm_manager.find_instrument_key_by_strike(sl_strike, 'CALL', expiry))
                            relevant_keys.add(self.atm_manager.find_instrument_key_by_strike(sl_strike, 'PUT', expiry))

                # IMPORTANT: Include crossover monitoring instruments
                monitoring_data = sm.dual_sr_monitoring_data
                if monitoring_data:
                    for side_key in ['ce_data', 'pe_data']:
                        side_data = monitoring_data.get(side_key)
                        if side_data and side_data.get('instrument_key'):
                            relevant_keys.add(side_data['instrument_key'])

            # Legacy/Global check
            global_monitoring = self.state_manager.dual_sr_monitoring_data
            if global_monitoring:
                for side_key in ['ce_data', 'pe_data']:
                    side_data = global_monitoring.get(side_key)
                    if side_data and side_data.get('instrument_key'):
                        relevant_keys.add(side_data['instrument_key'])

            # Include SellManager strangle keys (SELL legs + HEDGE legs)
            if hasattr(self.trade_orchestrator, 'sell_manager'):
                sm = self.trade_orchestrator.sell_manager
                from hub.sell_manager_v3 import SellManagerV3
                if isinstance(sm, SellManagerV3):
                    # V3 logic: include ATM +/- 10 ITM strikes
                    interval = self.trade_orchestrator.config_manager.get_int(self.trade_orchestrator.instrument_name, 'strike_interval', 50)

                    # Resolve anchor price for subscription pool
                    idx_p = self.state_manager.index_price
                    fut_p = self.state_manager.spot_price
                    is_mcx = any(x in self.trade_orchestrator.instrument_name.upper() for x in ['CRUDE', 'NATURAL', 'GOLD', 'SILVER'])
                    # For MCX, anchor MUST be Futures price. For others, Index is preferred.
                    anchor_p = fut_p if is_mcx else idx_p

                    if anchor_p:
                        atm = int(round(anchor_p / interval) * interval)
                        expiry = self.atm_manager.get_expiry_by_mode('sell', 'signal')
                        if expiry:
                            for i in range(-10, 11):
                                strike = atm + i * interval
                                for side in ['CALL', 'PUT']:
                                    k = self.atm_manager.find_instrument_key_by_strike(strike, side, expiry)
                                    if k: relevant_keys.add(k)
                    # Also include active trades
                    for side in ['CE', 'PE']:
                        trade = sm.active_trades.get(side)
                        if trade and trade.get('key'): relevant_keys.add(trade['key'])
                else:
                    # Legacy SellManager
                    for k in [sm.sell_ce_key, sm.sell_pe_key, sm.buy_ce_key, sm.buy_pe_key]:
                        if k: relevant_keys.add(k)
                    for _, k in (sm.ce_candidates or []):
                        if k: relevant_keys.add(k)
                    for _, k in (sm.pe_candidates or []):
                        if k: relevant_keys.add(k)

            self._relevant_keys_cache = relevant_keys
            self._last_keys_rebuild_time = asyncio.get_event_loop().time()
        except Exception as e:
            logger.error(f"Error rebuilding keys cache: {e}")

    async def _keys_rebuild_loop(self):
        """Periodically rebuilds the keys cache to account for strike changes or new trades."""
        while True:
            try:
                self._rebuild_relevant_keys()
                await asyncio.sleep(10) # Every 10 seconds
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in keys rebuild loop: {e}")
                await asyncio.sleep(5)

    def _sync_market_data(self, attr_name, key, value, is_primitive=False):
        """Synchronizes market data updates across all active user sessions."""
        if not self.trade_orchestrator.user_sessions:
            return

        for session in self.trade_orchestrator.user_sessions.values():
            if is_primitive:
                setattr(session.state_manager, attr_name, value)
            else:
                # Defensive check for missing attributes in session state manager
                if not hasattr(session.state_manager, attr_name):
                    setattr(session.state_manager, attr_name, {})

                target_dict = getattr(session.state_manager, attr_name)
                target_dict[key] = value

        # logger.debug(f"Synced {attr_name} to {len(self.trade_orchestrator.user_sessions)} sessions.")

    async def _handle_spot_feed(self, instrument_key, feed, timestamp, is_futures):
        """Handles feed data for the spot (index) or futures instrument."""
        ltp = None

        # 1. Check for Full Feed
        if feed.HasField('fullFeed'):
            if feed.fullFeed.HasField('indexFF'):
                ff = feed.fullFeed.indexFF
                if ff and ff.ltpc and ff.ltpc.ltp > 0:
                    ltp = ff.ltpc.ltp
            elif feed.fullFeed.HasField('marketFF'):
                ff = feed.fullFeed.marketFF
                if ff and ff.ltpc and ff.ltpc.ltp > 0:
                    ltp = ff.ltpc.ltp

        # 2. Check for top-level LTPC (Direct fallback)
        if ltp is None and feed.HasField('ltpc'):
            if feed.ltpc.ltp > 0:
                ltp = feed.ltpc.ltp

        if ltp is not None:
            # logger.debug(f"[{self.trade_orchestrator.instrument_name}] Spot update for {instrument_key}: {ltp}")
            self.entry_aggregator.add_tick(instrument_key, ltp, timestamp)
            self.exit_aggregator.add_tick(instrument_key, ltp, timestamp)
            self.one_min_aggregator.add_tick(instrument_key, ltp, timestamp)
            self.five_min_aggregator.add_tick(instrument_key, ltp, timestamp)

            is_mcx = any(x in self.trade_orchestrator.instrument_name.upper() for x in ['CRUDE', 'NATURAL', 'GOLD', 'SILVER'])

            if is_futures:
                if self.state_manager.spot_price is None:
                    logger.info(f"V2: First FUTURES price received for {self.trade_orchestrator.instrument_name}: {ltp}")

                # This is the futures price. It updates the main spot_price and triggers ATM recalc.
                self.state_manager.spot_price = ltp
                self._sync_market_data('spot_price', None, ltp, is_primitive=True)

                # For MCX, Futures price triggers ATM updates.
                if is_mcx:
                    await event_bus.publish('SPOT_PRICE_UPDATE', {
                        'instrument': self.trade_orchestrator.instrument_name,
                        'ltp': ltp
                    })
            else:
                if self.state_manager.index_price is None:
                    logger.info(f"V2: First INDEX price (Spot) received for {self.trade_orchestrator.instrument_name}: {ltp}")

                # This is the index price. It's stored separately for trade initiation.
                self.state_manager.index_price = ltp
                self._sync_market_data('index_price', None, ltp, is_primitive=True)

                # For NSE/BSE, Index Price triggers ATM update.
                if not is_mcx:
                    await event_bus.publish('SPOT_PRICE_UPDATE', {
                        'instrument': self.trade_orchestrator.instrument_name,
                        'ltp': ltp
                    })

            # Underlying movement doesn't need to signal worker; handle_message does it once at the end.
            pass

    def _handle_option_feed_sync(self, instrument_key, feed, timestamp):
        """
        ULTRA-FAST OPTION PROCESSING:
        Synchronous path to avoid coroutine overhead. All updates are dictionary-based.
        """
        self.state_manager.last_tick_times[instrument_key] = timestamp
        self._sync_market_data('last_tick_times', instrument_key, timestamp)

        greeks, ltp, iv, volume, market_level, atp, oi_value = None, None, None, None, None, None, 0

        if feed.HasField('fullFeed'):
            ff = feed.fullFeed.marketFF
            greeks, ltp, iv, volume, market_level, atp = ff.optionGreeks, ff.ltpc.ltp, ff.iv, ff.vtt, ff.marketLevel, ff.atp
            oi_value = getattr(ff, 'oi', 0) or 0
        elif feed.HasField('ltpc'):
            ltp = feed.ltpc.ltp

        # --- Update StateManager ---
        if ltp and ltp > 0:
            self.state_manager.option_prices[instrument_key] = ltp
            self._sync_market_data('option_prices', instrument_key, ltp)

            if atp and atp > 0:
                if not hasattr(self.state_manager, 'option_atps'):
                    self.state_manager.option_atps = {}
                self.state_manager.option_atps[instrument_key] = atp
                self._sync_market_data('option_atps', instrument_key, atp)

                minute_ts = timestamp.replace(second=0, microsecond=0) if hasattr(timestamp, 'replace') else None
                if minute_ts:
                    if not hasattr(self.state_manager, 'atp_history'):
                        self.state_manager.atp_history = {}
                    if instrument_key not in self.state_manager.atp_history:
                        self.state_manager.atp_history[instrument_key] = {}

                    hist_for_key = self.state_manager.atp_history[instrument_key]
                    prev_minute_ts = hist_for_key.get('_last_minute')
                    hist_for_key[minute_ts] = atp

                    if prev_minute_ts and prev_minute_ts != minute_ts:
                        prev_atp = hist_for_key.get(prev_minute_ts)
                        data_recorder = getattr(self.trade_orchestrator, 'data_recorder', None)

                        # Gated by absolute exchange open time (09:15) for data recording
                        exchange_open = self.trade_orchestrator.get_exchange_open_time(timestamp)

                        if data_recorder and prev_atp and timestamp >= exchange_open:
                            atm_mgr = getattr(self.trade_orchestrator, 'atm_manager', None)
                            strike, side = None, None
                            if atm_mgr:
                                contract = atm_mgr.contracts.get(instrument_key)
                                if contract:
                                    strike = getattr(contract, 'strike_price', None)
                                    side = 'CE' if getattr(contract, 'instrument_type', '') in ('CE',) else 'PE'

                            spot = self.state_manager.index_price
                            futures = self.state_manager.spot_price

                            # Record snapshot asynchronously (non-blocking IO offloaded to thread)
                            extra = {}
                            smv3 = getattr(self.trade_orchestrator, 'sell_manager', None)
                            if smv3 and hasattr(smv3, 'v3_dashboard_data'):
                                extra['rsi'] = smv3.v3_dashboard_data.get('combined_rsi')
                                extra['roc'] = smv3.v3_dashboard_data.get('combined_roc')
                                extra['v_slope'] = smv3.v3_dashboard_data.get('curr_slope')
                                extra['combined_vwap'] = smv3.v3_dashboard_data.get('combined_vwap')

                            data_recorder.record_atp_snapshot(
                                prev_minute_ts, instrument_key, strike, side,
                                prev_atp, ltp, spot, futures,
                                extra_indicators=extra
                            )

                    hist_for_key['_last_minute'] = minute_ts


        # --- Throttle delta updates to once per 60 seconds per instrument ---
        if greeks:
            now = timestamp if isinstance(timestamp, datetime.datetime) else datetime.datetime.now()
            if not hasattr(self.state_manager, 'last_delta_update'):
                self.state_manager.last_delta_update = {}

            last_delta_update = self.state_manager.last_delta_update
            last_time = last_delta_update.get(instrument_key)

            if greeks.delta != 0.0:
                if last_time is None or (now - last_time).total_seconds() >= 60:
                    self.state_manager.option_deltas[instrument_key] = greeks.delta
                    self._sync_market_data('option_deltas', instrument_key, greeks.delta)
                    last_delta_update[instrument_key] = now

            if greeks.gamma != 0.0:
                self.state_manager.option_gammas[instrument_key] = greeks.gamma
                self._sync_market_data('option_gammas', instrument_key, greeks.gamma)
            if greeks.theta != 0.0:
                self.state_manager.option_thetas[instrument_key] = greeks.theta
                self._sync_market_data('option_thetas', instrument_key, greeks.theta)
            if greeks.vega != 0.0:
                self.state_manager.option_vegas[instrument_key] = greeks.vega
                self._sync_market_data('option_vegas', instrument_key, greeks.vega)

        if iv is not None and iv != 0.0:
            self.state_manager.option_ivs[instrument_key] = iv
            self._sync_market_data('option_ivs', instrument_key, iv)

        if oi_value and oi_value > 0:
            self.state_manager.option_oi[instrument_key] = oi_value
            self._sync_market_data('option_oi', instrument_key, oi_value)

        # Volume and incremental volume calculation
        volume_inc = 0
        if volume is not None:
            prev_volume = self.state_manager.option_volumes.get(instrument_key, 0)
            if volume > prev_volume and prev_volume > 0:
                volume_inc = volume - prev_volume

            self.state_manager.option_volumes[instrument_key] = volume
            self._sync_market_data('option_volumes', instrument_key, volume)

        # --- Update Aggregators ---
        if ltp and ltp > 0:
            tick_kwargs = {'volume_inc': volume_inc}
            self.entry_aggregator.add_tick(instrument_key, ltp, timestamp, **tick_kwargs)
            self.exit_aggregator.add_tick(instrument_key, ltp, timestamp, **tick_kwargs)
            self.one_min_aggregator.add_tick(instrument_key, ltp, timestamp, **tick_kwargs)
            self.five_min_aggregator.add_tick(instrument_key, ltp, timestamp, **tick_kwargs)

        if market_level and market_level.bidAskQuote:
            top_quote = market_level.bidAskQuote[0]
            quote_data = {'bid': top_quote.bidP, 'ask': top_quote.askP}
            self.state_manager.option_bid_ask[instrument_key] = quote_data
            self._sync_market_data('option_bid_ask', instrument_key, quote_data)

        if ltp and ltp > 0:
            self.state_manager.update_pnl_for_tick(instrument_key, ltp)
            for session in self.trade_orchestrator.user_sessions.values():
                session.state_manager.update_pnl_for_tick(instrument_key, ltp)

        # Startup data check (gated to prevent spam)
        if not self.atm_manager.initial_data_received.is_set():
            # Check asyncly to avoid blocking the hot loop
            asyncio.create_task(self._check_and_set_initial_data_event())

    async def _tick_worker(self):
        """Dedicated background task to run process_tick one at a time."""
        logger.info(f"Tick worker started for {self.trade_orchestrator.instrument_name}.")
        _last_tick_warn_time = 0.0
        _NO_TICK_WARN_INTERVAL = 30.0  # warn every 30s when no ticks arrive
        while True:
            # Wait for a tick, but wake every 30s to emit a dead-feed warning
            try:
                await asyncio.wait_for(self._tick_event.wait(), timeout=_NO_TICK_WARN_INTERVAL)
            except asyncio.TimeoutError:
                _now = asyncio.get_event_loop().time()
                import datetime as _dt, pytz as _ptz
                _now_ist = _dt.datetime.now(_ptz.timezone('Asia/Kolkata'))
                _mkt_open = _now_ist.replace(hour=9, minute=15, second=0, microsecond=0)
                _mkt_close = _now_ist.replace(hour=15, minute=30, second=0, microsecond=0)
                if _mkt_open <= _now_ist <= _mkt_close:
                    # Bot subprocesses use FeedClient → FeedServer → upstream WS.
                    # We can only see the FeedClient↔FeedServer link from here;
                    # upstream WS state lives in the web process (FeedServer).
                    _ws = getattr(self.trade_orchestrator, 'websocket', None)
                    _fc_connected = bool(getattr(_ws, 'is_connected', False))
                    _hint = (
                        "FeedServer reachable but no ticks — its upstream Upstox/Dhan WS is offline. "
                        "Open Admin → Data Providers and click 'Connect Now' for both Upstox and Dhan."
                    ) if _fc_connected else (
                        "Cannot reach FeedServer (web process). "
                        "Restart the web server (botrestart) or check that uvicorn is running."
                    )
                    logger.warning(
                        f"[{self.trade_orchestrator.instrument_name}] FEED SILENT: No ticks received in "
                        f"{_NO_TICK_WARN_INTERVAL:.0f}s during market hours. "
                        f"FeedClient↔FeedServer: {'connected' if _fc_connected else 'DISCONNECTED'}. "
                        f"{_hint}"
                    )
                continue
            self._tick_event.clear()

            # PROTECT: Ensure orchestrator is fully initialized before processing
            if not getattr(self.trade_orchestrator, 'tick_processor', None):
                continue

            try:
                # THROTTLE: Minimum delay between runs to prevent event loop saturation
                now_ts = asyncio.get_event_loop().time()
                elapsed = now_ts - self._last_process_tick_time

                if elapsed < 0.2: # 200ms min interval
                    await asyncio.sleep(0.2 - elapsed)

                self._last_process_tick_time = asyncio.get_event_loop().time()

                # Protect the entire tick processing flow with a 60s timeout
                try:
                    await asyncio.wait_for(self.trade_orchestrator.process_tick(), timeout=60.0)
                except asyncio.TimeoutError:
                    logger.error(f"V2: FATAL - Tick processing HANG detected for {self.trade_orchestrator.instrument_name}. Loop timed out after 60s.")
                except Exception as e:
                    logger.error(f"V2: Error during tick processing for {self.trade_orchestrator.instrument_name}: {e}", exc_info=True)

            except Exception as e:
                logger.error(f"Error in tick worker loop for {self.trade_orchestrator.instrument_name}: {e}", exc_info=True)

    def _log_missing_initial_data(self):
        """Logs which instruments are still missing baseline data."""
        missing_ltp, missing_delta = [], []
        for key in self.state_manager.initial_instrument_keys:
            if key not in self.state_manager.option_prices: missing_ltp.append(key)
            if self.state_manager.option_deltas.get(key) is None: missing_delta.append(key)
        
        # if missing_ltp or missing_delta:
        #     logger.debug(f"Waiting for baseline data. Missing LTP for: {missing_ltp}. Missing Delta for: {missing_delta}.")

    async def _check_and_set_initial_data_event(self):
        """
        Checks if the baseline data (LTP and Delta) for all initial instruments
        has been received. If so, it sets and publishes the INITIAL_DATA_COMPLETE event.
        """
        if self.state_manager.is_initial_data_complete():
            if not self.atm_manager.initial_data_received.is_set():
                logger.info("All initial instrument data, including deltas, has been received. Publishing event.")
                self.atm_manager.initial_data_received.set()
                await event_bus.publish('INITIAL_DATA_COMPLETE')
        else:
            self._log_missing_initial_data()
