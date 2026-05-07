from utils.logger import logger
from abc import ABC, abstractmethod
import asyncio
import datetime

from .state_manager import StateManager
from .atm_manager import AtmManager
from .data_manager import DataManager
from .price_feed_handler import PriceFeedHandler
from .trade_execution_manager import TradeExecutionManager
from .event_bus import event_bus
from .strike_manager import StrikeManager
from .backtest_pnl_tracker import BacktestPnLTracker
from .live_trade_log import LiveTradeLog
from .orchestrator_state import OrchestratorState
from .tick_processor import TickProcessor
from .signal_monitor import SignalMonitor
from .trade_executor import TradeExecutor
from .position_manager import PositionManager
from .user_session import UserSession
from .data_recorder import DataRecorder
from .indicator_manager import IndicatorManager
from utils.ohlc_aggregator import OHLCAggregator
from utils.common_models import AppConfig, MarketData
from utils.json_config_manager import JsonConfigManager

class BaseOrchestrator(ABC):
    def __init__(self, instrument_name, rest_client, websocket_manager, broker_manager, config_manager, data_manager=None, contract_map=None, redis_manager=None, is_backtest=None):
        self.instrument_name = instrument_name
        self.rest_client = rest_client
        self.websocket = websocket_manager
        self.config_manager = config_manager
        self.json_config = JsonConfigManager()
        self.contract_map = contract_map or {}
        self.redis_manager = redis_manager

        # Priority: Explicit argument > Config setting
        if is_backtest is not None:
            self.is_backtest = is_backtest
        else:
            self.is_backtest = self.config_manager.get_boolean('settings', 'backtest_enabled', fallback=False)

        # 1. SHARED STATE (One per instrument)
        # StateManager must be created first as AtmManager depends on it.
        self.state_manager = StateManager(self.config_manager, instrument_name=self.instrument_name, redis_manager=self.redis_manager, is_backtest=self.is_backtest)
        self.atm_manager = AtmManager(self.config_manager, websocket_manager, self.state_manager, rest_client, instrument_name=self.instrument_name, orchestrator=self)

        self.entry_timeframe = self.config_manager.get_int('settings', 'entry_timeframe_minutes', 1)
        self.exit_timeframe = self.config_manager.get_int('settings', 'exit_timeframe_minutes', 5)

        # Configurable S1LOW Timeframes
        # Priority: JSON config (indicators/s1_confirm) > INI instrument section > INI settings > Defaults
        def _get_json_cfg(path):
            val = self.json_config.get_value(f"{self.instrument_name}.{path}")
            if val is None:
                val = self.json_config.get_value(f"NIFTY.{path}")
            return val

        json_fast = _get_json_cfg("buy.exit_indicators.s1_confirm.fast_tf")
        json_slow = _get_json_cfg("buy.exit_indicators.s1_confirm.slow_tf")

        def _parse_tf(val):
            if val is None or str(val).strip() == "":
                return None
            try:
                return int(val)
            except (ValueError, TypeError):
                return None

        json_fast_val = _parse_tf(json_fast)
        json_slow_val = _parse_tf(json_slow)

        self.s1_low_fast_tf = json_fast_val if json_fast_val is not None else \
                             self.config_manager.get_int(self.instrument_name, 's1_low_fast_tf',
                                                         self.config_manager.get_int('settings', 's1_low_fast_tf', 1))
        self.s1_low_slow_tf = json_slow_val if json_slow_val is not None else \
                             self.config_manager.get_int(self.instrument_name, 's1_low_slow_tf',
                                                         self.config_manager.get_int('settings', 's1_low_slow_tf', 5))

        # Increased history limit to 2000 to support multi-day S&R/Pattern analysis (approx 5 days of 1-min data)
        self.entry_aggregator = OHLCAggregator(interval_minutes=1, history_limit=2000, name="Pattern_Entry") # Fixed 1-min for Pattern Recognition
        self.exit_aggregator = OHLCAggregator(interval_minutes=1, history_limit=2000, name="Pattern_Exit")  # Fixed 1-min for Pattern Recognition
        self.one_min_aggregator = OHLCAggregator(interval_minutes=self.s1_low_fast_tf, history_limit=2000, name="S1LOW_Fast") # S1LOW Fast TF
        self.five_min_aggregator = OHLCAggregator(interval_minutes=self.s1_low_slow_tf, history_limit=2000, name="S1LOW_Slow") # S1LOW Slow TF

        # 2. MULTI-TENANT USER SESSIONS
        self.user_sessions = {} # user_id -> UserSession

        self.broker_manager = broker_manager
        if self.broker_manager:
            self.broker_manager.set_state_manager(self.state_manager)

        # --- Instrument Configuration ---
        # The primary instrument is now passed in and set directly.
        self.primary_instrument = self.instrument_name

        # Read the futures_instrument_key from the specific instrument's section.
        self.futures_instrument_key = self.config_manager.get(self.primary_instrument, 'futures_instrument_key')

        # Guard: if the config mistakenly sets futures_instrument_key to an index key
        # (e.g. "NSE_INDEX|Nifty 50" instead of a real futures contract), clear it.
        # An index key used as futures_key causes handle_tick_dispatched_normalized to route
        # the NIFTY tick as is_futures=True → sets spot_price but never sets index_price → Spot=None.
        _INDEX_PREFIXES = ('NSE_INDEX|', 'BSE_INDEX|', 'MCX_INDEX|')
        if self.futures_instrument_key and any(self.futures_instrument_key.startswith(p) for p in _INDEX_PREFIXES):
            logger.warning(
                f"[Orchestrator] futures_instrument_key='{self.futures_instrument_key}' looks like an INDEX key, "
                "not a futures contract. Clearing it — auto-discovery will resolve the real futures key. "
                "Fix your config: set futures_instrument_key to a contract like NSE_FO:NIFTY25MAYFUT or leave blank."
            )
            self.futures_instrument_key = None

        if not self.futures_instrument_key:
            # Auto-discovery for MCX if missing in INI
            if self.is_mcx:
                self.futures_instrument_key = f"MCX_FO:{self.instrument_name}FUT"
            else:
                logger.warning(f"futures_instrument_key not defined in config for {self.primary_instrument}. Relying on auto-discovery.")

        # Use data_manager's pre-mapped instrument_key (already normalized by EngineManager)
        # instead of re-reading from config to avoid bare names like 'NIFTY'
        if data_manager and hasattr(data_manager, 'instrument_key'):
            self.index_instrument_key = data_manager.instrument_key
        else:
            # Fallback: map bare names to universal Upstox format if not already mapped
            raw_symbol = self.config_manager.get(self.primary_instrument, 'instrument_symbol')
            if raw_symbol:
                # Apply mapping if raw symbol is a bare name
                name_map = {
                    'NIFTY': 'NSE_INDEX|Nifty 50',
                    'BANKNIFTY': 'NSE_INDEX|Nifty Bank',
                    'FINNIFTY': 'NSE_INDEX|Nifty Fin Service',
                    'SENSEX': 'BSE_INDEX|SENSEX',
                    'MIDCAP': 'NSE_INDEX|NIFTY MID SELECT',
                    'CRUDEOIL': 'MCX_INDEX|CRUDE OIL',
                    'NATURALGAS': 'MCX_INDEX|NATURAL GAS'
                }
                self.index_instrument_key = name_map.get(raw_symbol.upper(), raw_symbol)
            else:
                # Default fallback
                self.index_instrument_key = name_map.get(self.primary_instrument.upper(), 'NSE_INDEX|Nifty 50')

        # If a DataManager is not passed in, it's created using the correct instrument symbol.
        self.data_manager = data_manager or DataManager(self.rest_client, self.index_instrument_key, self.config_manager, is_backtest=self.is_backtest)

        self.atm_manager.spot_instrument_key = self.futures_instrument_key

        self.strike_manager = StrikeManager(self.state_manager, self.atm_manager, self.config_manager, instrument_name=self.instrument_name)
        self.price_feed_handler = PriceFeedHandler(self.state_manager, self.atm_manager, self, self.entry_aggregator, self.exit_aggregator, self.one_min_aggregator, self.five_min_aggregator)
        self.trade_execution_manager = TradeExecutionManager(self.broker_manager, self.state_manager, self.atm_manager, self.config_manager)

        self.pnl_tracker = BacktestPnLTracker(self.instrument_name, self.config_manager) if self.is_backtest else None
        self.trade_log = LiveTradeLog()

        # Initialize DataRecorder for live/paper V2 recording (Default: Disabled in Bot)
        # Recording is now handled by a standalone script scripts/run_recorder.py
        should_record = self.config_manager.get_boolean('settings', 'record_data', fallback=False)
        self.data_recorder = DataRecorder(self.instrument_name) if (not self.is_backtest and should_record) else None

        self.orchestrator_state = OrchestratorState()
        self.is_active = False

        self.websocket.register_message_handler(self.price_feed_handler.handle_message)

        # --- V2 Logic Component Initializations ---
        logger.debug("Initializing V2 Logic Components...")
        self.indicator_manager = IndicatorManager(self)
        logger.info(f"[{self.instrument_name}] IndicatorManager initialized on orchestrator.")
        self.signal_monitor = SignalMonitor(self)
        self.trade_executor = TradeExecutor(self)
        self.position_manager = PositionManager(self)

        # The TickProcessor is initialized separately after all other components are set up.
        self.tick_processor = None

    @property
    def is_mcx(self):
        return any(x in self.instrument_name.upper() for x in ['CRUDE', 'NATURAL', 'GOLD', 'SILVER'])

    def get_anchor_price(self):
        """Returns the appropriate anchor price (Futures for MCX, Index for others)."""
        if self.is_mcx:
            return self.state_manager.spot_price
        return self.state_manager.index_price or self.state_manager.spot_price

    def get_strat_cfg(self, path, default=None, type_func=None):
        """Retrieves a value from JSON config with instrument-specific priority and NIFTY fallback."""
        val = self.json_config.get_value(f"{self.instrument_name}.{path}")

        # Robustness: Treat empty strings as None to allow fallback logic to proceed
        if isinstance(val, str) and val.strip() == "":
            val = None

        # Timing fallbacks: MCX instruments should NOT fall back to NIFTY 09:15 defaults
        is_timing_cfg = any(x in path for x in ['start_time', 'end_time', 'close_time'])

        if val is None:
            if not (is_timing_cfg and self.is_mcx):
                val = self.json_config.get_value(f"NIFTY.{path}")
                if isinstance(val, str) and val.strip() == "":
                    val = None

        if val is None:
            return default

        if type_func:
            try:
                if type_func == bool:
                    return str(val).lower() == 'true'
                return type_func(val)
            except:
                return default
        return val

    def get_exchange_open_time(self, timestamp):
        """Returns the absolute market open time (NSE 09:15, MCX 09:00) for data recording."""
        hour = 9
        minute = 0 if self.is_mcx else 15
        return timestamp.replace(hour=hour, minute=minute, second=0, microsecond=0)

    def get_market_open_time(self, timestamp):
        """Returns the USER CONFIGURED market open time for strategy start."""
        # 1. Priority: Strategy JSON sell.start_time (with NIFTY fallback)
        strat_start = self.get_strat_cfg("sell.start_time")
        if not strat_start:
            strat_start = self.get_strat_cfg("buy.start_time")

        if strat_start:
            try:
                t_str = str(strat_start).replace('.', ':')
                parts = t_str.split(':')
                if len(parts) == 2:
                    t = datetime.datetime.strptime(t_str, '%H:%M').time()
                else:
                    t = datetime.datetime.strptime(t_str, '%H:%M:%S').time()
                return timestamp.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
            except: pass

        # 2. Priority: instrument-specific INI config start_time
        inst_start = self.config_manager.get(self.instrument_name, 'start_time')
        if inst_start:
            try:
                if len(str(inst_start).split(':')) == 2:
                    t = datetime.datetime.strptime(str(inst_start), '%H:%M').time()
                else:
                    t = datetime.datetime.strptime(str(inst_start), '%H:%M:%S').time()
                return timestamp.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
            except: pass

        return self.get_exchange_open_time(timestamp)

    def finalize_initialization(self):
        """
        Finalizes the initialization by creating components that depend on
        all other modules being in place.
        """
        self.tick_processor = TickProcessor(self)
        self.atm_manager.set_ready()

        # SellManager reconnection
        if hasattr(self, 'sell_manager') and hasattr(self.sell_manager, 'reconnect_positions'):
            self.sell_manager.reconnect_positions()

        logger.debug("Orchestrator initialization finalized.")

    @abstractmethod
    def _get_timestamp(self):
        """Returns the current timestamp (live or backtest)."""
        pass

    @abstractmethod
    def _is_trade_active(self):
        """Checks if a trade is currently active."""
        raise NotImplementedError

    @abstractmethod
    async def _check_backtest_exit_conditions(self, timestamp):
        """Checks for exit conditions during a backtest."""
        pass

    async def add_user_session(self, user_id, email, strategy_config=None):
        """Adds a new isolated user session to this orchestrator."""
        if user_id not in self.user_sessions:
            session = UserSession(user_id, email, self.instrument_name, self, strategy_config)
            await session.state_manager.load_state()

            # Ensure new session receives the current monthly expiries list from the main orchestrator state
            if self.state_manager.monthly_expiries:
                session.state_manager.monthly_expiries = list(self.state_manager.monthly_expiries)

            self.user_sessions[user_id] = session
            logger.debug(f"[{self.instrument_name}] User {email} added to monitoring (monthly_expiries={session.state_manager.monthly_expiries}).")

    async def broadcast_signal(self, direction, instrument_key, signal_ltp, strike_price, timestamp, strategy_log, entry_type='BUY'):
        """Broadcasts a validated market signal to all isolated user sessions."""
        if getattr(self, 'profit_target_hit', False):
            logger.debug(f"[{self.instrument_name}] Profit target already hit — signal skipped.")
            return

        quantity_multiplier = self.config_manager.get_int(
            self.instrument_name, 'hedge_quantity_multiplier', 2)

        logger.debug(f"[{self.instrument_name}] Broadcasting {direction} signal ({entry_type}) to {len(self.user_sessions)} users.")
        tasks = []
        for session in self.user_sessions.values():
            tasks.append(session.evaluate_signal(
                direction=direction,
                instrument_key=instrument_key,
                signal_ltp=signal_ltp,
                strike_price=strike_price,
                timestamp=timestamp,
                strategy_log=strategy_log,
                entry_type=entry_type,
                quantity_multiplier=quantity_multiplier
            ))

        if tasks:
            await asyncio.gather(*tasks)

    def get_current_tick_data(self, ce_strike, pe_strike, is_backtest, backtest_current_tick=None):
        """
        Delegates the fetching of the current tick data to the TickProcessor,
        which is now the central authority for this logic.
        """
        return self.tick_processor.get_tick_data(ce_strike, pe_strike, is_backtest, backtest_current_tick)

    def get_initial_subscriptions(self):
        """
        Returns a list of instrument keys that this orchestrator needs for its initial data feed.
        """
        if self.is_backtest:
            return []

        # Use the properly mapped index_instrument_key (e.g., NSE_INDEX|Nifty 50)
        # instead of raw config value (e.g., NIFTY), which Upstox doesn't recognize
        instruments = [self.futures_instrument_key, self.index_instrument_key]
        logger.debug(f"[{self.instrument_name}] Requesting initial subscriptions for: {instruments}")
        return instruments

    async def process_tick(self, backtest_previous_tick=None, backtest_current_tick=None):
        await self.tick_processor.process_tick(backtest_previous_tick, backtest_current_tick)

    def stop(self):
        logger.info("Stopping Orchestrator's components...")
        # No action needed here anymore as the scheduler is removed.
        pass

    def clear_for_new_run(self):
        """Clears all state and aggregators for a fresh run (e.g. multi-day backtest)."""
        # Ensure fresh strategy logic from JSON
        self.json_config.load()
        logger.debug(f"[{self.instrument_name}] Clearing orchestrator state for a fresh run.")
        self.state_manager.clear_all_state()
        for session in self.user_sessions.values():
            session.state_manager.clear_all_state()

        self.entry_aggregator.clear()
        self.exit_aggregator.clear()
        self.one_min_aggregator.clear()
        self.five_min_aggregator.clear()

        if self.pnl_tracker:
            self.pnl_tracker.trade_history = []
            self.pnl_tracker.active_call_trade = None
            self.pnl_tracker.active_put_trade = None

        if hasattr(self.data_manager, 'clear_caches'):
            self.data_manager.clear_caches()

        if hasattr(self, 'sell_manager'):
            v3_mode = self.json_config.get_value(f"{self.instrument_name}.v3_mode")
            if v3_mode is None: v3_mode = self.json_config.get_value("NIFTY.v3_mode")

            if str(v3_mode).lower() == 'true':
                from hub.sell_manager_v3 import SellManagerV3
                self.sell_manager = SellManagerV3(self)
            else:
                from hub.sell_manager import SellManager
                self.sell_manager = SellManager(self)
        if hasattr(self, '_backtest_strangle_triggered'):
            self._backtest_strangle_triggered = False
