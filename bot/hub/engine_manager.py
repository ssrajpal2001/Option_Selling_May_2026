import asyncio
import datetime
import os
from utils.logger import logger
from hub.provider_factory import ProviderFactory
from hub.lifecycle_manager import LifecycleManager
from hub.data_manager import DataManager
from hub.backtest_orchestrator import BacktestOrchestrator
from hub.live_orchestrator import LiveOrchestrator

class EngineManager:
    def __init__(self, config_manager, api_client_manager, broker_manager, db_manager, redis_manager, shared_display_manager):
        self.config_manager = config_manager
        self.api_client_manager = api_client_manager
        self.broker_manager = broker_manager
        self.db_manager = db_manager
        self.redis_manager = redis_manager
        self.shared_display_manager = shared_display_manager
        self.lifecycle_managers = {}
        self.orchestrators = {}

    async def create_orchestrator(self, instrument_name, rest_client, websocket_manager, is_backtest):
        instrument_name = instrument_name.upper().strip()
        symbol = self.config_manager.get(instrument_name, 'instrument_symbol')

        # ALWAYS apply universal name mapping to convert bare names (e.g., 'NIFTY')
        # to proper Upstox format (e.g., 'NSE_INDEX|Nifty 50'). This ensures DataManager
        # is initialized with the correctly mapped key, not the raw config value.
        # This applies both when symbol is empty AND when it's a bare name from config.
        fallbacks = {
            'NIFTY': 'NSE_INDEX|Nifty 50',
            'BANKNIFTY': 'NSE_INDEX|Nifty Bank',
            'FINNIFTY': 'NSE_INDEX|Nifty Fin Service',
            'SENSEX': 'BSE_INDEX|SENSEX',
            'MIDCAP': 'NSE_INDEX|NIFTY MID SELECT',
            'CRUDEOIL': 'MCX_INDEX|CRUDE OIL',
            'NATURALGAS': 'MCX_INDEX|NATURAL GAS'
        }
        # If symbol is a bare name (in the fallbacks map), map it; otherwise use as-is
        if symbol:
            symbol = fallbacks.get(symbol.upper(), symbol)
        else:
            # If no symbol in INI, use instrument_name as key for fallback
            symbol = fallbacks.get(instrument_name, 'NSE_INDEX|Nifty 50')

        # Broker-Specific Overrides for Backtests or direct SDK calls (optional)
        # But BrokerRestAdapter now handles translation, so we stick to Universal Keys.

        data_manager = DataManager(rest_client, symbol, self.config_manager)
        OrchestratorClass = BacktestOrchestrator if is_backtest else LiveOrchestrator
        orch = OrchestratorClass(instrument_name=instrument_name, rest_client=rest_client, websocket_manager=websocket_manager, broker_manager=self.broker_manager, config_manager=self.config_manager, data_manager=data_manager, redis_manager=self.redis_manager)

        orch.atm_manager.data_manager = data_manager
        data_manager.atm_manager = orch.atm_manager
        if not await data_manager.load_contracts(): raise RuntimeError(f"Failed to load contracts for {instrument_name}.")

        orch.atm_manager.all_contracts = data_manager.all_options
        orch.atm_manager.near_expiry_date = data_manager.near_expiry_date
        orch.atm_manager.monthly_expiries = data_manager.monthly_expiries
        orch.atm_manager._build_contract_lookup_table()
        orch.finalize_initialization()
        orch.atm_manager._determine_expiries()
        return orch

    def _get_sell_start_time(self, orch):
        """Read sell start time from strategy_logic.json with fallback."""
        raw = orch.get_strat_cfg("sell.start_time")
        # BaseOrchestrator already provides defaults "09:00" (MCX) / "09:15" (NSE)
        time_str = str(raw)
        try:
            t_str = time_str.replace('.', ':')
            parts = t_str.split(':')
            if len(parts) >= 2:
                return datetime.time(int(parts[0]), int(parts[1]), int(parts[2]) if len(parts) == 3 else 0)
        except Exception:
            pass
        return datetime.time(9, 0) if orch.is_mcx else datetime.time(9, 15)

    async def run_engines(self):
        # We start by evaluating which instruments are enabled on brokers
        if not self.broker_manager.brokers:
            await self.broker_manager.load_brokers()
        instruments = self.broker_manager.get_all_unique_instruments()

        # Overall session window determined by all enabled instruments
        def get_t(s, k, d):
            # 0. Try instrument-specific JSON config (strategy_logic.json)
            from utils.json_config_manager import JsonConfigManager
            jc = JsonConfigManager()

            # Helper to check JSON with NIFTY fallback
            def _get_json_cfg(inst, key):
                val = jc.get_value(f"{inst}.sell.{key}")
                if val is None: val = jc.get_value(f"{inst}.buy.{key}")

                # Timing fallbacks: MCX instruments should NOT fall back to NIFTY 09:15 defaults
                is_mcx = any(x in inst.upper() for x in ['CRUDE', 'NATURAL', 'GOLD', 'SILVER'])
                is_timing_cfg = any(x in key for x in ['start_time', 'end_time', 'close_time'])

                if val is None and not (is_mcx and is_timing_cfg):
                    val = jc.get_value(f"NIFTY.sell.{key}")
                    if val is None: val = jc.get_value(f"NIFTY.buy.{key}")
                return val

            if k == 'start_time':
                strat_val = _get_json_cfg(s, 'start_time')
                if strat_val:
                    try:
                        # Robust parsing for HH:MM, HH.MM, HH:MM:SS
                        t_str = str(strat_val).replace('.', ':')
                        parts = t_str.split(':')
                        if len(parts) == 2:
                            return datetime.datetime.strptime(t_str, '%H:%M').time()
                        elif len(parts) == 3:
                            return datetime.datetime.strptime(t_str, '%H:%M:%S').time()
                    except: pass

            # 1. Try instrument-specific section first (INI)
            inst_val = self.config_manager.get(s, k)
            if inst_val:
                try:
                    return datetime.datetime.strptime(inst_val, '%H:%M:%S').time()
                except:
                    try:
                        return datetime.datetime.strptime(inst_val, '%H:%M').time()
                    except: pass

            # 2. Instrument-specific defaults (PRIORITY over global settings for start_time)
            is_mcx = any(x in s.upper() for x in ['CRUDE', 'NATURAL', 'GOLD', 'SILVER'])
            if k == 'start_time' and is_mcx:
                return datetime.time(9, 0)

            # 3. Fallback to global settings
            val = self.config_manager.get('settings', k, fallback=None)
            if val:
                try:
                    return datetime.datetime.strptime(val, '%H:%M:%S').time()
                except:
                    try:
                        return datetime.datetime.strptime(val, '%H:%M').time()
                    except: pass

            # 4. Final fallback to provided default
            if isinstance(d, str):
                try:
                    return datetime.datetime.strptime(d, '%H:%M:%S').time()
                except:
                    return datetime.datetime.strptime(d, '%H:%M').time()
            return d

        start_time = datetime.time(23, 59, 59)
        end_time = datetime.time(0, 0, 0)

        # If no specific instruments found, use defaults from settings
        if not instruments:
            start_time = get_t('settings', 'start_time', '09:15:00')
            end_time = get_t('settings', 'end_time', '15:25:00')
        else:
            for inst in instruments:
                is_mcx = any(x in str(inst).upper() for x in ['CRUDE', 'NATURAL', 'GOLD', 'SILVER'])
                default_start = '09:00:00' if is_mcx else '09:15:00'
                s_t = get_t(str(inst), 'start_time', default_start)
                e_t = get_t(inst, 'end_time', '15:25:00')
                if s_t < start_time: start_time = s_t
                if e_t > end_time: end_time = e_t

        logger.info(f"[EngineManager] Global session window calculated: {start_time} to {end_time}")

        is_initialized, is_trading_active = False, False
        ws_mgr, ws_task, disp_task = None, None, None
        self.orchestrators.clear()

        # Track which orchestrators have had candidates built (avoid repeat calls)
        _sell_candidates_built = set()
        _init_retry_count = 0
        _init_max_wait = 120  # seconds

        while True:
            now_dt = datetime.datetime.now()
            now = now_dt.time()

            # 1. Pre-Market Initialization: Allow bot to connect and sync before start_time
            if now < end_time:
                if not is_initialized:
                    try:
                        if not self.broker_manager.brokers:
                            await self.broker_manager.load_brokers()
                        instruments = self.broker_manager.get_all_unique_instruments()
                        if not instruments:
                            await asyncio.sleep(60); continue

                        rest, ws_mgr = await ProviderFactory.create_data_provider(
                            self.api_client_manager, self.config_manager, False,
                            redis_manager=self.redis_manager, broker_manager=self.broker_manager
                        )
                        ws_task, disp_task = ws_mgr.start(), asyncio.create_task(self.shared_display_manager.start_display_loop())

                        self.orchestrators.clear()
                        for inst in instruments:
                            orch = await self.create_orchestrator(inst, rest, ws_mgr, False)
                            self.orchestrators[inst] = orch

                        # Add Users to relevant Orchestrators
                        client_id_env = os.environ.get('CLIENT_ID')
                        db_users = await self.db_manager.get_active_users_and_brokers() if self.db_manager else []
                        unique_users = {u['user_id']: u for u in db_users}

                        if client_id_env:
                            for inst_name, orch in self.orchestrators.items():
                                logger.info(f"[EngineManager] Adding client session for ID: {client_id_env} to {inst_name}")
                                await orch.add_user_session(int(client_id_env), os.environ.get('CLIENT_USERNAME', 'client@local'))
                        elif unique_users:
                            for user_id, user_data in unique_users.items():
                                for inst_name, orch in self.orchestrators.items():
                                    if any(b.user_id == user_id and b.is_configured_for_instrument(inst_name) for b in self.broker_manager.brokers.values()):
                                        await orch.add_user_session(user_id, user_data['email'], await self.db_manager.get_user_strategy(user_id, inst_name))
                        else:
                            for inst_name, orch in self.orchestrators.items():
                                await orch.add_user_session(None, "default_user@local")

                        self.shared_display_manager.orchestrators.update(self.orchestrators)
                        for inst, orch in self.orchestrators.items():
                            self.lifecycle_managers[inst] = LifecycleManager(orch, ws_mgr, self.broker_manager, self.shared_display_manager, False)

                        subs = []
                        for orch in self.orchestrators.values(): subs.extend(orch.get_initial_subscriptions())
                        if ws_mgr and subs: ws_mgr.subscribe(list(set(subs)))
                        is_initialized = True
                        _init_retry_count = 0
                        logger.info(f"[EngineManager] Bot initialized successfully. Waiting for trading window ({start_time}).")
                    except Exception as e:
                        _init_retry_count += 1
                        # Exponential backoff: 5s, 10s, 20s, 40s … capped at _init_max_wait
                        wait = min(5 * (2 ** (_init_retry_count - 1)), _init_max_wait)
                        logger.error(
                            f"[EngineManager] Init error (attempt {_init_retry_count}): {e}. "
                            f"Retrying in {wait}s. "
                            "If this is a token error, refresh the global Upstox/Dhan token via Admin → Data Providers."
                        )
                        await asyncio.sleep(wait); continue

                # 2. Trading Logic Gating
                if is_initialized and not is_trading_active and now >= start_time:
                    await asyncio.gather(*(m.start(False) for m in self.lifecycle_managers.values()))
                    is_trading_active = True
                    logger.info(f"[EngineManager] Trading window opened. Signal monitoring active.")
                    # Force immediate status write to update UI
                    for orch in self.orchestrators.values():
                        if hasattr(orch, 'status_writer'):
                            orch.status_writer.maybe_write(now_dt, orch.state_manager.atm_strike, force=True)

                if is_initialized and is_trading_active:
                    for inst_name, orch in self.orchestrators.items():
                        if not hasattr(orch, 'sell_manager'):
                            continue
                        sell_enabled = orch.json_config.get_value(
                            f"{orch.instrument_name}.sell.enabled")
                        if sell_enabled is False or sell_enabled == 'false':
                            continue

                        sell_start = self._get_sell_start_time(orch)

                        # Build candidates once when sell start time is reached
                        if now >= sell_start and inst_name not in _sell_candidates_built:
                            if not orch.sell_manager.strangle_closed:
                                logger.info(
                                    f"[EngineManager] {inst_name} sell start time "
                                    f"{sell_start} reached.")
                                # V2 managers require explicit candidate building,
                                # but V3 managers handle it dynamically in on_tick.
                                if hasattr(orch.sell_manager, 'build_candidates_for_all_sides'):
                                    success = await orch.sell_manager.build_candidates_for_all_sides(
                                        datetime.datetime.now())
                                    if success:
                                        _sell_candidates_built.add(inst_name)
                                else:
                                    # Already ready for V3
                                    _sell_candidates_built.add(inst_name)
            else:
                if is_trading_active:
                    for orch in self.orchestrators.values():
                        if hasattr(orch, 'sell_manager') and not orch.sell_manager.strangle_closed:
                            await orch.sell_manager.close_all(datetime.datetime.now())
                    await asyncio.gather(*(m.stop() for m in self.lifecycle_managers.values()))
                    is_trading_active = False
                if is_initialized:
                    if ws_mgr: await ws_mgr.close()
                    if ws_task: ws_task.cancel()
                    if disp_task: disp_task.cancel()
                    self.broker_manager.shutdown(); self.lifecycle_managers.clear()
                    self.shared_display_manager.orchestrators.clear()
                    is_initialized = False
                    _sell_candidates_built.clear()
            # 4. Persistent Heartbeat: Keep UI status files fresh even during pre-market wait
            if is_initialized:
                for orch in self.orchestrators.values():
                    if hasattr(orch, 'status_writer'):
                        # Throttled internally to 5s, ensuring heartbeat timestamp is always current
                        orch.status_writer.maybe_write(now_dt, orch.state_manager.atm_strike)

            await asyncio.sleep(5)
