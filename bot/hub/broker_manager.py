import os
import importlib
import asyncio
import threading
from utils.logger import logger
from hub.event_bus import event_bus

from utils.encryption_manager import EncryptionManager
from utils.database_manager import DatabaseManager

class BrokerManager:
    def __init__(self, config_manager, db_manager=None):
        self.config_manager = config_manager
        self.db_manager = db_manager or DatabaseManager()
        self.encryption_manager = EncryptionManager()
        self.brokers = {} # instance_name -> broker_instance
        self.state_manager = None
        event_bus.subscribe('BROKER_TOKEN_UPDATED', self.handle_token_update)

    def set_state_manager(self, state_manager):
        """Receives the shared StateManager instance from the main application."""
        self.state_manager = state_manager
        # Pass the state_manager to already loaded brokers
        for broker in self.brokers.values():
            broker.set_state_manager(self.state_manager)

    def _get_broker_class(self, client_name):
        """Dynamically imports and returns a broker client class."""
        module_name = None
        class_name = None
        try:
            if not client_name:
                raise ValueError("client_name cannot be None or empty.")
            module_name = f"brokers.{client_name.lower()}_client"
            class_name = f"{client_name}Client"
            module = importlib.import_module(module_name)
            return getattr(module, class_name)
        except (ImportError, AttributeError, ValueError) as e:
            logger.error(f"Could not find or load the client class '{class_name}' from '{module_name}'. Please check the file and class names. Error: {e}")
            return None

    async def load_client_mode_broker(self, client_cfg):
        from kiteconnect import KiteConnect
        from brokers.zerodha_client import ZerodhaClient

        broker_name = client_cfg.broker.lower()
        instance_name = f"client_{client_cfg.client_id}_{broker_name}"

        db_config = {
            'id': client_cfg.instance_id,          # client_broker_instances.id — used by token DB save
            'client_id': client_cfg.client_id,
            'mode': client_cfg.trading_mode,
            'api_key': client_cfg.api_key,
            'api_secret': os.environ.get('CLIENT_API_SECRET', ''), # Injected in main.py
            'access_token': client_cfg.access_token,
            'password': client_cfg.password,
            'totp': client_cfg.totp,
            'broker_user_id': os.environ.get('CLIENT_BROKER_USER_ID', ''),
            'client_code':    os.environ.get('CLIENT_BROKER_USER_ID', ''),  # AngelOne login username alias
            'static_ip': client_cfg.static_ip or None,   # Elastic IP for source-IP binding
            'broker_settings': {
                'instruments_to_trade': client_cfg.instrument,
                'quantity': client_cfg.quantity,
            },
        }

        if broker_name == 'zerodha':
            # Attempt automated login if credentials provided
            broker_instance = ZerodhaClient(
                broker_instance_name=instance_name,
                config_manager=self.config_manager,
                login_required=True,
                user_id=client_cfg.client_id,
                db_config=db_config
            )

            if not broker_instance.kite and client_cfg.access_token:
                logger.info(f"[CLIENT MODE] Automated login failed or skipped. Trying with existing token...")
                kite = KiteConnect(api_key=client_cfg.api_key)
                kite.set_access_token(client_cfg.access_token)
                # Mount adapter BEFORE profile() so the validation call is IP-bound.
                if broker_instance.source_ip:
                    broker_instance._install_source_ip_adapter(
                        getattr(kite, 'reqsession', None)
                    )
                try:
                    await asyncio.to_thread(kite.profile)
                    broker_instance.kite = kite
                    logger.info(f"[CLIENT MODE] Zerodha authenticated via token.")
                except Exception as e:
                    logger.critical(
                        f"[CLIENT MODE] Zerodha token validation failed: {e}. "
                        f"Continuing in degraded mode (market data / paper trading only). "
                        f"Live orders will be blocked until reconnected via Settings."
                    )
                    # Do NOT raise — allow the subprocess to continue for paper trading
                    # and market data. Live order placement is independently gated by
                    # paper_trade mode and per-broker trading_active checks.
        elif broker_name == 'dhan':
            from brokers.dhan_client import DhanClient
            broker_instance = DhanClient(
                broker_instance_name=instance_name,
                config_manager=self.config_manager,
                user_id=client_cfg.client_id,
                db_config=db_config,
                login_required=True # Force initialization from db_config
            )
        elif broker_name == 'angelone':
            from brokers.angelone_client import AngelOneClient
            broker_instance = AngelOneClient(
                broker_instance_name=instance_name,
                config_manager=self.config_manager,
                user_id=client_cfg.client_id,
                db_config=db_config,
                login_required=True # Force initialization from db_config
            )
        elif broker_name == 'upstox':
            from brokers.upstox_client import UpstoxClient
            broker_instance = UpstoxClient(
                broker_instance_name=instance_name,
                config_manager=self.config_manager,
                user_id=client_cfg.client_id,
                db_config=db_config,
                login_required=True
            )
        elif broker_name == 'fyers':
            from brokers.fyers_client import FyersClient
            broker_instance = FyersClient(
                broker_instance_name=instance_name,
                config_manager=self.config_manager,
                user_id=client_cfg.client_id,
                db_config=db_config,
                login_required=True
            )
        elif broker_name == 'aliceblue':
            from brokers.aliceblue_client import AliceblueClient
            broker_instance = AliceblueClient(
                broker_instance_name=instance_name,
                config_manager=self.config_manager,
                user_id=client_cfg.client_id,
                db_config=db_config,
                login_required=True
            )
        elif broker_name == 'groww':
            from brokers.groww_client import GrowwClient
            broker_instance = GrowwClient(
                broker_instance_name=instance_name,
                config_manager=self.config_manager,
                user_id=client_cfg.client_id,
                db_config=db_config,
                login_required=True
            )
        else:
            raise ValueError(f"Unsupported broker: {broker_name}")

        if self.state_manager:
            broker_instance.set_state_manager(self.state_manager)
        self.brokers[instance_name] = broker_instance
        logger.info(f"[CLIENT MODE] Loaded {broker_name} broker instance: {instance_name}")

    async def load_brokers(self):
        """
        Commercial Path: Loads all active broker instances for all users from the Database.
        Fallback Path: Loads from .ini if DB is not populated or configured.
        """
        if self.brokers:
            logger.info("BrokerManager: Brokers already loaded, skipping discovery.")
            return

        try:
            if not self.db_manager or not getattr(self.db_manager, 'pool', None):
                logger.info("BrokerManager: Database not connected. Skipping DB discovery.")
                rows = []
            else:
                logger.debug("BrokerManager: Discovering multi-tenant brokers from database...")
                rows = await self.db_manager.get_active_users_and_brokers()

            if rows:
                for row in rows:
                    try:
                        # Decrypt secret
                        decrypted_secret = self.encryption_manager.decrypt(row['api_secret_encrypted'])
                        row['api_secret'] = decrypted_secret

                        client_name = row['broker_name']
                        broker_class = self._get_broker_class(client_name)

                        if broker_class:
                            # Instantiate with user_id and db_sourced config
                            broker_instance = broker_class(
                                broker_instance_name=row['instance_name'],
                                config_manager=self.config_manager,
                                user_id=row['user_id'],
                                db_config=row
                            )
                            if self.state_manager:
                                broker_instance.set_state_manager(self.state_manager)
                            self.brokers[row['instance_name']] = broker_instance
                            logger.info(f"Loaded DB Broker: User={row['email']} | {row['instance_name']} ({client_name})")
                    except Exception as e:
                        logger.error(f"Failed to load DB broker {row.get('instance_name')}: {e}")

                if self.brokers:
                    return # Successfully loaded from DB

        except Exception as e:
            logger.warning(f"Database broker discovery skipped or failed: {e}. Falling back to .ini")

        # --- FALLBACK TO LEGACY INI LOADING ---
        active_broker_sections_str = self.config_manager.get('settings', 'active_broker', fallback='')
        active_broker_sections = [b.strip() for b in active_broker_sections_str.split(',') if b.strip()]

        if not active_broker_sections:
            logger.warning("No active brokers are defined in the [settings] section under 'active_broker'.")
            return

        for broker_section in active_broker_sections:
            if not self.config_manager.has_section(broker_section):
                logger.error(f"Configuration section '[{broker_section}]' not found for the active broker.")
                continue

            client_name = self.config_manager.get(broker_section, 'client')
            broker_class = self._get_broker_class(client_name)

            if broker_class:
                try:
                    broker_instance = broker_class(broker_section, self.config_manager)
                    if self.state_manager:
                        broker_instance.set_state_manager(self.state_manager)
                    self.brokers[broker_section] = broker_instance
                    logger.info(f"Successfully loaded broker: {broker_section} (Client: {client_name})")
                except Exception as e:
                    logger.error(f"An unexpected error occurred while loading broker {broker_section}: {e}", exc_info=True)


    async def handle_execute_trade_request(self, trade_data):
        """
        Commercial Route: Sends the trade request only to the brokers belonging to the specific user.
        """
        raw_instrument_name = trade_data.get("instrument_name")
        target_user_id = trade_data.get("user_id")

        if not raw_instrument_name:
            logger.error(f"Trade request is missing 'instrument_name'. Cannot route.")
            return

        # Use normalized name for routing checks to ensure "NIFTY 50" matches "NIFTY" config
        from brokers.base_broker import BaseBroker
        # Temporary instance for normalization if needed, or just use the static mapping
        # Actually, let's just use the shared mapping directly if possible or a helper.
        # BaseBroker is an ABC, but we can call the helper if it was static.
        # Since it's an instance method, let's just do a quick normalization here.
        from brokers.base_broker import _INSTRUMENT_NAME_MAP
        instrument_name = _INSTRUMENT_NAME_MAP.get(raw_instrument_name.upper(), raw_instrument_name.upper())

        logger.info(f"BrokerManager routing {instrument_name} signal for user_id={target_user_id}")

        tasks = []
        for broker in self.brokers.values():
            # Check if this broker instance belongs to the target user
            # and if it is configured for this instrument
            is_user_match = (target_user_id is None) or (broker.user_id == target_user_id)

            if is_user_match and broker.is_configured_for_instrument(raw_instrument_name):
                logger.info(f"Routing trade to broker '{broker.instance_name}' (User: {broker.user_id})")
                tasks.append(broker.handle_entry_signal(**trade_data))
            else:
                if is_user_match:
                    logger.debug(f"Broker '{broker.instance_name}' NOT configured for {instrument_name}. Configured: {broker.instruments}")

        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for i, res in enumerate(results):
                if isinstance(res, Exception):
                    logger.error(f"Error executing trade on a broker: {res}", exc_info=True)

    async def handle_exit_request(self, exit_data):
        """
        Commercial Route: Sends the exit request only to the brokers belonging to the specific user.
        """
        raw_instrument_name = exit_data.get("instrument_name")
        target_user_id = exit_data.get("user_id")

        if not raw_instrument_name:
            logger.error(f"Exit request is missing 'instrument_name'. Cannot route.")
            return

        from brokers.base_broker import _INSTRUMENT_NAME_MAP
        instrument_name = _INSTRUMENT_NAME_MAP.get(raw_instrument_name.upper(), raw_instrument_name.upper())

        logger.info(f"BrokerManager routing exit signal for {instrument_name} for user_id={target_user_id}")

        tasks = []
        for broker in self.brokers.values():
            is_user_match = (target_user_id is None) or (broker.user_id == target_user_id)

            if is_user_match and broker.is_configured_for_instrument(raw_instrument_name):
                logger.info(f"Routing exit to broker '{broker.instance_name}' (User: {broker.user_id})")
                tasks.append(broker.handle_close_signal(**exit_data))

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def broadcast_entry_signal(self, **kwargs):
        """Helper to initiate an entry signal to all brokers."""
        logger.info(f"DIAGNOSTIC: Broadcasting entry signal with data: {kwargs}")
        if not self.brokers:
            logger.error("DIAGNOSTIC: No brokers loaded. Cannot broadcast entry signal.")
            return

        tasks = []
        for broker in self.brokers.values():
            logger.info(f"DIAGNOSTIC: Queueing handle_entry_signal for broker '{broker.instance_name}'...")
            tasks.append(broker.handle_entry_signal(**kwargs))

        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for i, res in enumerate(results):
                if isinstance(res, Exception):
                    b_name = list(self.brokers.keys())[i]
                    logger.error(f"DIAGNOSTIC: Error broadcasting entry signal to broker '{b_name}': {res}")

    async def broadcast_close_signal(self, **kwargs):
        """Helper to initiate a close signal to all brokers."""
        logger.info(f"Broadcasting close signal with data: {kwargs}")
        tasks = []
        for broker in self.brokers.values():
            tasks.append(broker.handle_close_signal(**kwargs))

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def close_all_positions(self):
        """
        Industrial Standard: Instructs all brokers to close any open positions.
        Used during graceful shutdown to ensure zero remaining risk.
        """
        logger.info(f"BrokerManager: Squaring off all positions for {len(self.brokers)} brokers...")
        tasks = []
        for broker in self.brokers.values():
            if hasattr(broker, 'close_all_positions'):
                # Some brokers might be sync, some async.
                # Our new implementations are async.
                tasks.append(broker.close_all_positions())

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        logger.info("BrokerManager: All square-off commands dispatched.")

    def get_all_unique_instruments(self):
        """
        Scans all loaded broker instances to find all the unique instruments they are configured to trade.
        """
        unique_instruments = set()

        # 1. Primary path: Use actually loaded broker instances (Client Mode / Multi-tenant)
        if self.brokers:
            for broker in self.brokers.values():
                unique_instruments.update(broker.instruments)
            if unique_instruments:
                return unique_instruments

        # 2. Fallback path: Scan configuration file (Legacy / INI Mode)
        active_broker_sections_str = self.config_manager.get('settings', 'active_broker', fallback='')
        active_broker_sections = [b.strip() for b in active_broker_sections_str.split(',') if b.strip()]

        if not active_broker_sections:
            # Final default if nothing else is specified
            logger.warning("No active brokers or configuration found. Defaulting to NIFTY.")
            return {'NIFTY'}

        for section in active_broker_sections:
            instruments_str = self.config_manager.get(section, 'instruments_to_trade', fallback='')
            if instruments_str:
                instruments = [i.strip().upper() for i in instruments_str.split(',')]
                unique_instruments.update(instruments)
            else:
                logger.warning(f"Broker section '[{section}]' is active but has no 'instruments_to_trade' defined.")

        if not unique_instruments:
            unique_instruments.add('NIFTY')

        return unique_instruments

    def get_broker_instance(self, broker_section_name):
        """
        Factory method to create a temporary, non-trading instance of a broker client.
        This is used by the reporting module to access broker-specific formatting
        without interfering with live trading instances.
        """
        if not self.config_manager.has_section(broker_section_name):
            logger.error(f"Configuration section '[{broker_section_name}]' not found for the requested broker instance.")
            return None

        client_name = self.config_manager.get(broker_section_name, 'client')
        broker_class = self._get_broker_class(client_name)

        if broker_class:
            try:
                # Instantiate the client without requiring a login, safe for reporting.
                broker_instance = broker_class(broker_section_name, self.config_manager, login_required=False)
                return broker_instance
            except Exception as e:
                logger.error(f"Failed to create temporary instance of broker {broker_section_name}: {e}", exc_info=True)
                return None
        return None

    async def handle_token_update(self, data):
        """
        Triggered when a new OAuth token is captured.
        Automatically starts the WebSocket for that specific broker.
        """
        user_id = data.get('user_id')
        broker_name = data.get('broker')

        logger.info(f"BrokerManager: Received token update for User {user_id} ({broker_name}). Auto-connecting...")

        # Find the existing broker instance or create one
        target_broker = None
        for b in self.brokers.values():
            if b.user_id == user_id and b.instance_name.lower().endswith(broker_name.lower()):
                target_broker = b
                break

        if target_broker:
            # Update token if already loaded
            if broker_name == 'zerodha' and hasattr(target_broker, 'kite'):
                target_broker.kite.set_access_token(data['access_token'])

            # Restart/Start Data Feed
            target_broker.stop_data_feed()
            target_broker.start_data_feed()
            logger.info(f"BrokerManager: Data feed restarted for {target_broker.instance_name}")
        else:
            logger.warning(f"BrokerManager: No active instance found for user {user_id} to auto-connect.")

    def shutdown(self):
        """Gracefully shuts down all broker instances and clears the list."""
        logger.debug("BrokerManager shutdown called.")
        for broker in self.brokers.values():
            try:
                # If the broker client has a shutdown method, call it
                if hasattr(broker, 'shutdown'):
                    broker.shutdown()

                # Stop data feeds
                if hasattr(broker, 'stop_data_feed'):
                    broker.stop_data_feed()
            except Exception as e:
                logger.error(f"Error shutting down broker: {e}")
        self.brokers.clear()
