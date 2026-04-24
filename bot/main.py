import asyncio
import datetime
import json
import sys
import os
import time
from hub.provider_factory import ProviderFactory
from hub.broker_manager import BrokerManager
from utils.logger import logger, configure_logger
from utils.config_manager import ConfigManager
from utils.config_validator import ConfigValidator
from utils.api_client_manager import ApiClientManager
from hub.data_manager import DataManager
from hub.display_manager import DisplayManager
from hub.lifecycle_manager import LifecycleManager
from hub.engine_manager import EngineManager
from hub.portfolio_manager import PortfolioManager
from hub.backtest_orchestrator import BacktestOrchestrator
from hub.strike_manager import StrikeManager
from utils.trade_logger import TradeLogger
from hub.live_orchestrator import LiveOrchestrator
from hub.event_bus import event_bus

import argparse
import signal

async def monitor_event_loop_health():
    """
    EC2 RESOURCE OPTIMIZATION:
    Detects event loop stalls and reports them. If a major stall occurs,
    this provides diagnostic data to prevent instance hangs.
    """
    while True:
        start = time.perf_counter()
        await asyncio.sleep(1.0)
        delay = time.perf_counter() - start - 1.0
        if delay > 2.0:
            logger.warning(f"CRITICAL: Event Loop STALL Detected: {delay:.2f}s. System load is too high!")
        await asyncio.sleep(5)

async def main():
    """
    Main entry point for the multi-broker, multi-instrument trading application.
    Initializes and orchestrates all the major components.
    """
    # Start loop health monitor
    asyncio.create_task(monitor_event_loop_health())

    # --- 0. Graceful Shutdown Setup ---
    stop_event = asyncio.Event()

    # --- 1. Configuration and Logging ---
    parser = argparse.ArgumentParser(description="AlgoSoft Trading Bot")
    parser.add_argument('--config', type=str, default='config/config_trader.ini', help='Path to the configuration file.')
    parser.add_argument('--entry_tf', type=int, help='Override entry timeframe (minutes)')
    parser.add_argument('--exit_tf', type=int, help='Override exit timeframe (minutes)')
    parser.add_argument('--provider', type=str, help='Specific data provider credential section to use')
    parser.add_argument('--client_mode', action='store_true', help='Run in multi-tenant client mode (reads config from env vars)')
    args = parser.parse_args()

    script_dir = os.path.dirname(os.path.abspath(__file__))
    config_path = os.path.join(script_dir, args.config)
    config_manager = ConfigManager(config_file=config_path)

    if args.client_mode:
        from hub.client_config import load_client_config
        client_cfg = load_client_config()
        # Override log file for client mode BEFORE configuring logger
        config_manager.set_override('app', 'log_file', f"logs/client_{client_cfg.client_id}_{client_cfg.broker}.log")

        # Absolute path hardening
        log_dir = os.path.join(os.getcwd(), "logs")
        os.makedirs(log_dir, exist_ok=True)

        _status_path = os.path.join(os.getcwd(), "config", f"bot_status_client_{client_cfg.client_id}.json")
        os.makedirs(os.path.dirname(_status_path), exist_ok=True)

        # Ensure trading toggle is initialized to FALSE on start (User must explicitly click Start Trading)
        # Commercial Requirement: "CONNECT TO BROKER" (starting bot) should not automatically start trading.
        _toggle_path = os.path.join(os.getcwd(), "config", f"trading_enabled_{client_cfg.client_id}.json")
        if not os.path.exists(_toggle_path):
            with open(_toggle_path, "w") as _tf:
                json.dump({"enabled": False, "updated_at": time.time()}, _tf)

        with open(_status_path, "w") as _sf:
            json.dump({
                "bot_running": True,
                "trading_active": False, # Initialize status with trading OFF
                "pid": os.getpid(),
                "mode": client_cfg.trading_mode.upper(),
                "instrument": client_cfg.instrument,
                "broker": client_cfg.broker,
                "heartbeat": time.time(),
                "updated_at": datetime.datetime.now().isoformat(),
                "session_pnl": 0,
                "trade_count": 0,
                "trade_history": [],
                "log_tail": ["Bot starting in client mode. Connection active. Waiting for 'Start Trading' toggle..."]
            }, _sf)

    # Apply runtime overrides for parallel variant testing
    if args.entry_tf:
        config_manager.set_override('settings', 'entry_timeframe_minutes', args.entry_tf)
        config_manager.set_override('settings', 'entry_pattern_tf', args.entry_tf)
    if args.exit_tf:
        config_manager.set_override('settings', 'exit_timeframe_minutes', args.exit_tf)
        config_manager.set_override('settings', 'exit_pattern_tf', args.exit_tf)
    if args.provider: config_manager.set_override('data_providers', 'provider_list', args.provider)

    # Automatically generate a unique log file if overrides are used to prevent file locking issues
    if not args.client_mode and (args.entry_tf or args.exit_tf):
        etf = config_manager.get('settings', 'entry_timeframe_minutes')
        xtf = config_manager.get('settings', 'exit_timeframe_minutes')
        config_manager.set_override('app', 'log_file', f"app_{etf}m_{xtf}m.log")

    configure_logger(config_manager)

    if args.client_mode:
        logger.info(f"[CLIENT MODE] Starting bot for {client_cfg}")

    # Log the active strategy parameters for visual confirmation
    entry_p_tf = config_manager.get('settings', 'entry_pattern_tf', fallback=config_manager.get('settings', 'entry_timeframe_minutes', fallback='1'))
    exit_p_tf = config_manager.get('settings', 'exit_pattern_tf', fallback=config_manager.get('settings', 'exit_timeframe_minutes', fallback='1'))
    logger.info(f"ACTIVE STRATEGY: Entry TF: {entry_p_tf}m | Exit TF: {exit_p_tf}m")

    # Initialize TradeLogger with config for dynamic filename generation
    TradeLogger().setup(config_manager)
    logger.info("Starting the multi-broker trading application...")
    logger.info(f"Using configuration file: {args.config}")

    validator = ConfigValidator(config_manager)
    if not validator.validate(context='trader'):
        logger.critical("Invalid configuration. Exiting.")
        sys.exit(1)

    # --- 2. API and Core Component Setup ---
    is_backtest = config_manager.get_boolean('settings', 'backtest_enabled', fallback=False)
    is_client_mode = args.client_mode
    api_client_manager = None

    # Global ApiClientManager (typically Upstox) is only needed for:
    # 1. Admin/Global Trading Mode
    # 2. Backtest mode where user credentials aren't provided (as a fallback)
    # In 'client_mode', we skip global init and let the client broker handle both data and trades.
    if not is_backtest and not is_client_mode:
        api_client_manager = ApiClientManager(config_manager)
        await api_client_manager.async_init()

    # Redis initialization
    redis_manager = None
    if config_manager.get_boolean('redis', 'enabled', fallback=False):
        from hub.state_redis import RedisStateManager
        redis_manager = RedisStateManager(
            host=config_manager.get('redis', 'host', fallback='localhost'),
            port=config_manager.get_int('redis', 'port', fallback=6379),
            db=config_manager.get_int('redis', 'db', fallback=0)
        )
        await redis_manager.connect()

    from utils.database_manager import DatabaseManager
    db_manager = DatabaseManager()

    db_connected = False
    # Only attempt PostgreSQL connection if explicitly enabled
    db_enabled = config_manager.get_boolean('database', 'enabled', fallback=False)
    if db_enabled:
        try:
            await db_manager.connect(config_manager=config_manager)
            db_connected = True
        except Exception as e:
            db_required = config_manager.get_boolean('database', 'required', fallback=False)
            if db_required:
                logger.critical(f"DATABASE ERROR: Could not connect to PostgreSQL. {e}")
                logger.info("Setup Help: Ensure PostgreSQL is running on the host/port specified in config_trader.ini.")
                logger.info("If you are running in Cloud9, you may need to start the service: 'sudo service postgresql start'")
                sys.exit(1)
            else:
                logger.warning(f"DATABASE WARNING: Connection failed ({e}). Proceeding without database.")

    broker_manager = BrokerManager(config_manager, db_manager=db_manager if db_connected else None)

    if args.client_mode:
        await broker_manager.load_client_mode_broker(client_cfg)

    # Register shutdown handlers after managers are initialized
    async def handle_shutdown(sig_name):
        logger.info(f"Received {sig_name}. Initiating industrial square-off and shutdown...")
        # 1. Stop all trading logic processing
        for orch in engine_manager.orchestrators.values():
            orch.is_active = False

        # 2. Industrial Square-off: Close all positions across all active brokers
        await broker_manager.close_all_positions()

        # 3. Signal the main loop to exit
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda s=sig: asyncio.create_task(handle_shutdown(s.name)))

    # --- 3. Engine Management ---
    engine_manager = EngineManager(config_manager, api_client_manager, broker_manager,
                                   db_manager if db_connected else None, redis_manager,
                                   DisplayManager({}, config_manager))
    engine_manager.is_client_mode = is_client_mode

    try:
        if is_backtest:
            await broker_manager.load_brokers()
            instruments = [i.strip() for i in config_manager.get('settings', 'instrument_to_trade', fallback='NIFTY').upper().split(',')]
            user_id = os.environ.get('CLIENT_ID')

            for inst in instruments:
                rest, ws = await ProviderFactory.create_data_provider(
                    api_client_manager, config_manager, True, redis_manager=redis_manager, user_id=user_id
                )

                # Check for mandatory client credentials in backtest mode
                # The ProviderFactory is responsible for returning either a valid rest_client
                # or a MockRest object. If a user_id is present, we expect a real client.
                # Logic fix: In user-driven backtests, if rest is Mock, it means credentials validation failed.
                # We stop to prevent confusing "No Data" errors if the user expected live REST history.
                if user_id and (not rest or getattr(rest, 'is_mock', False)):
                     logger.critical(f"[Backtest] User {user_id} credentials could not be validated for REST API data. Fix settings or use global config.")
                     sys.exit(1)

                orch = await engine_manager.create_orchestrator(inst, rest, ws, True)

                # Identify user for session
                bt_uid, bt_email = "backtest_user", "backtest_user@local"
                if user_id:
                    from web.db import db_fetchone
                    u = db_fetchone("SELECT username, email FROM users WHERE id=?", (user_id,))
                    if u: bt_uid, bt_email = int(user_id), u['email']

                await orch.add_user_session(bt_uid, bt_email)

                lm = LifecycleManager(orch, ws, broker_manager, DisplayManager(orch, config_manager), True)

                # --- Multi-day Backtest Date Parsing ---
                backtest_date_raw = config_manager.get('settings', 'backtest_date', fallback='')
                backtest_dates = []

                if " to " in backtest_date_raw:
                    try:
                        start_str, end_str = backtest_date_raw.split(" to ")
                        start_dt = datetime.datetime.strptime(start_str.strip(), '%Y-%m-%d').date()
                        end_dt = datetime.datetime.strptime(end_str.strip(), '%Y-%m-%d').date()
                        current = start_dt
                        while current <= end_dt:
                            backtest_dates.append(current)
                            current += datetime.timedelta(days=1)
                    except Exception as e:
                        logger.error(f"Failed to parse backtest date range: {e}")
                        backtest_dates = [backtest_date_raw]
                elif "," in backtest_date_raw:
                    backtest_dates = [d.strip() for d in backtest_date_raw.split(',')]
                else:
                    backtest_dates = [backtest_date_raw]

                lm.backtest_dates = backtest_dates
                await lm.start()
        else:
            event_bus.subscribe('EXECUTE_TRADE_REQUEST', broker_manager.handle_execute_trade_request)
            event_bus.subscribe('EXIT_TRADE_REQUEST', broker_manager.handle_exit_request)
            await engine_manager.run_engines()

            # Keep the bot running until a shutdown signal is received
            await stop_event.wait()

    except asyncio.CancelledError:
        logger.info("Main task cancelled, shutting down.")
    except Exception as e:
        logger.critical(f"An unhandled error occurred in main: {e}", exc_info=True)
    finally:
        logger.info("Shutting down all application components...")
        for instrument, manager in engine_manager.lifecycle_managers.items():
            logger.info(f"Stopping lifecycle manager for {instrument}...")
            await manager.stop()

        if api_client_manager and hasattr(api_client_manager, 'close'):
            await api_client_manager.close()

        if redis_manager:
            await redis_manager.close()

        # Ensure all trade logs are closed gracefully
        TradeLogger().shutdown()


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Application interrupted by user. Shutting down.")
