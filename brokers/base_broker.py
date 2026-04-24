from abc import ABC, abstractmethod
import asyncio
from utils.trade_logger import TradeLogger

class BaseBroker(ABC):
    """
    Abstract base class for all broker clients.
    Defines the standard interface for broker-specific operations.
    """
    def __init__(self, instance_name, config_manager, user_id=None, db_config=None):
        self.instance_name = instance_name
        self.config_manager = config_manager
        self.user_id = user_id
        self.db_config = db_config # Sourced from database (multi-tenant)
        self.state_manager = None
        self.is_connected = False
        self.broker_name = None # Set by subclasses
        self.trade_logger = TradeLogger()
        self._load_config()

    def _load_config(self):
        """Loads broker-specific configuration from DB (multi-tenant) or INI (legacy)."""
        global_mode = self.config_manager.get('settings', 'trading_mode', fallback='paper')

        if self.db_config:
            # Multi-tenant DB path
            self.mode = self.db_config.get('mode', global_mode)
            self.paper_trade = str(self.mode).lower() == 'paper'
            settings = self.db_config.get('broker_settings', {})
            instruments_str = settings.get('instruments_to_trade', '')
            self.instruments = {i.strip().upper() for i in instruments_str.split(',') if i.strip()}
            self.api_key = self.db_config.get('api_key')
            self.api_secret = self.db_config.get('api_secret') # Already decrypted by Manager
        else:
            # Legacy INI path
            self.mode = self.config_manager.get(self.instance_name, 'mode', fallback=global_mode)
            self.paper_trade = str(self.mode).lower() == 'paper'
            instruments_str = self.config_manager.get(self.instance_name, 'instruments_to_trade', fallback='')
            self.instruments = {i.strip().upper() for i in instruments_str.split(',') if i.strip()}

    def is_configured_for_instrument(self, instrument_name):
        """Checks if this broker instance is configured to trade the given instrument."""
        return instrument_name.upper() in self.instruments

    def set_state_manager(self, state_manager):
        """Receives the shared StateManager instance."""
        self.state_manager = state_manager

    @abstractmethod
    def connect(self):
        """Connects to the broker's API."""
        pass

    @abstractmethod
    def start_data_feed(self):
        """Starts the real-time data feed (WebSocket) for this broker."""
        pass

    def start(self):
        """ProviderFactory compatibility: starts the data feed."""
        # In Client Mode, if this broker is used as the data provider, we MUST start it.
        logger.info(f"[{self.instance_name}] BaseBroker.start() called. Starting data feed...")
        self.start_data_feed()
        return None

    @abstractmethod
    def stop_data_feed(self):
        """Stops the real-time data feed for this broker."""
        pass

    async def close(self):
        """ProviderFactory compatibility: stops the data feed."""
        self.stop_data_feed()

    def register_message_handler(self, handler):
        """ProviderFactory compatibility: no-op for client-side unified providers."""
        pass

    def subscribe(self, instrument_list, mode='full'):
        """
        ProviderFactory compatibility: subscribes to a list of universal instrument keys.
        Translates to broker-specific format before calling internal subscribe_instruments.
        """
        if not instrument_list: return

        # We need a REST adapter to do the translation
        from utils.broker_rest_adapter import BrokerRestAdapter
        b_name = self.broker_name or self.instance_name.split('_')[-1].lower()
        adapter = BrokerRestAdapter(self, b_name)

        async def do_sub():
            sub_map = {}
            for ikey in instrument_list:
                broker_key = await adapter._translate_to_broker_key(ikey)
                if broker_key:
                    if b_name == 'dhan':
                        segment = 'NSE_FNO'
                        if 'INDEX' in ikey: segment = 'IDX_I'
                        elif 'NSE_EQ' in ikey: segment = 'NSE_EQ'
                        sub_map[ikey] = (segment, str(broker_key))
                    else:
                        sub_map[ikey] = broker_key

            if sub_map:
                self.subscribe_instruments(sub_map)

        # Run async in background
        asyncio.create_task(do_sub())

    def unsubscribe(self, instrument_list):
        """ProviderFactory compatibility: no-op for unified client providers."""
        pass

    @abstractmethod
    async def handle_entry_signal(self, **kwargs):
        """Handles a trade entry signal."""
        pass

    @abstractmethod
    async def handle_close_signal(self, **kwargs):
        """Handles a trade exit signal."""
        pass

    @abstractmethod
    def place_order(self, contract, transaction_type, quantity, expiry, product_type='NRML', market_protection=None):
        """
        Places an order with the broker. This method is responsible for
        generating the broker-specific symbol from the contract object.
        """
        pass

    @abstractmethod
    async def close_all_positions(self):
        """
        Industrial Standard: Squares off all open positions for this broker instance.
        Called when the client clicks 'STOP' to ensure zero risk remains.
        """
        pass

    @abstractmethod
    async def get_funds(self):
        """Returns the available funds/margin for the account."""
        pass

    @abstractmethod
    async def get_positions(self):
        """Returns all live positions for the account."""
        pass
