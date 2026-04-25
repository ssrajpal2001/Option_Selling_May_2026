from abc import ABC, abstractmethod
import asyncio
import socket
import threading
from utils.trade_logger import TradeLogger
from utils.logger import logger

# ── Per-thread source IP binding ─────────────────────────────────────────────
# Two complementary patches applied once at import time:
#
#  1. socket.create_connection  — covers http.client and any code that calls
#     socket.create_connection directly.
#
#  2. urllib3.util.connection.create_connection — urllib3 (and therefore
#     requests) uses its OWN create_connection that does NOT delegate to
#     socket.create_connection; it binds via sock.bind(source_address).
#     Patching at the urllib3 level is the only way to inject a source
#     address into bare requests.get/post calls (e.g. pya3 AliceBlue SDK)
#     that have no accessible session to mount an HTTPAdapter on.
#
# The preferred method for brokers whose SDK exposes a requests.Session is
# SourceIPHTTPAdapter.init_poolmanager (urllib3-native, mount once at init).
# These patches are a defense-in-depth fallback used only where a session
# cannot be accessed (pya3, fyers-apiv3 SDK).

_tls = threading.local()

# Patch 1 — socket.create_connection
_orig_socket_create_connection = socket.create_connection

def _source_ip_aware_create_connection(address, timeout=socket.getdefaulttimeout(),
                                        source_address=None):
    src = getattr(_tls, 'source_ip', None)
    if src and not source_address:
        source_address = (src, 0)
    return _orig_socket_create_connection(address, timeout, source_address)

socket.create_connection = _source_ip_aware_create_connection

# Patch 2 — urllib3.util.connection.create_connection
try:
    import urllib3.util.connection as _urllib3_conn
    _orig_urllib3_create_connection = _urllib3_conn.create_connection

    def _source_ip_aware_urllib3_create_connection(
            address, timeout=socket.getdefaulttimeout(),
            source_address=None, socket_options=None):
        src = getattr(_tls, 'source_ip', None)
        if src and not source_address:
            source_address = (src, 0)
        return _orig_urllib3_create_connection(
            address, timeout, source_address, socket_options)

    _urllib3_conn.create_connection = _source_ip_aware_urllib3_create_connection
except Exception:
    pass  # urllib3 not available — no-op


# ── SourceIPHTTPAdapter ───────────────────────────────────────────────────────
try:
    from requests.adapters import HTTPAdapter as _HTTPAdapter

    class SourceIPHTTPAdapter(_HTTPAdapter):
        """
        Requests adapter that routes ALL outbound connections through a specific
        local IP address by configuring urllib3's connection pool with
        source_address=(ip, 0).

        This is the correct urllib3-native approach: urllib3 does NOT call
        socket.create_connection — it uses its own create_connection that
        only honours source_address when set on the pool manager.  Overriding
        init_poolmanager / proxy_manager_for is the only reliable method.

        Mount on a broker SDK's requests.Session at __init__ time and every
        HTTP call (auth, instruments, orders, funds) will automatically use
        the assigned Elastic IP — no per-call wrapping required.
        """
        def __init__(self, source_ip: str, **kwargs):
            self.source_ip = source_ip
            super().__init__(**kwargs)

        def init_poolmanager(self, *args, **kwargs):
            kwargs['source_address'] = (self.source_ip, 0)
            super().init_poolmanager(*args, **kwargs)

        def proxy_manager_for(self, proxy, **kwargs):
            kwargs['source_address'] = (self.source_ip, 0)
            return super().proxy_manager_for(proxy, **kwargs)

except ImportError:
    SourceIPHTTPAdapter = None  # type: ignore


# ── Instrument name normalisation map ────────────────────────────────────────
_INSTRUMENT_NAME_MAP = {
    "NIFTY 50": "NIFTY",
    "NIFTY50": "NIFTY",
    "NIFTY": "NIFTY",
    "NIFTY BANK": "BANKNIFTY",
    "NIFTYBANK": "BANKNIFTY",
    "BANKNIFTY": "BANKNIFTY",
    "NIFTY FINANCIAL SERVICES": "FINNIFTY",
    "NIFTY FIN SERVICE": "FINNIFTY",
    "FINNIFTY": "FINNIFTY",
    "NIFTY MIDCAP SELECT": "MIDCPNIFTY",
    "NIFTY MID SELECT": "MIDCPNIFTY",
    "MIDCAP NIFTY": "MIDCPNIFTY",
    "MIDCPNIFTY": "MIDCPNIFTY",
    "SENSEX": "SENSEX",
    "BANKEX": "BANKEX",
}

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
            self.source_ip = self.db_config.get('static_ip') or None
        else:
            # Legacy INI path
            self.mode = self.config_manager.get(self.instance_name, 'mode', fallback=global_mode)
            self.paper_trade = str(self.mode).lower() == 'paper'
            instruments_str = self.config_manager.get(self.instance_name, 'instruments_to_trade', fallback='')
            self.instruments = {i.strip().upper() for i in instruments_str.split(',') if i.strip()}
            self.source_ip = None

    def _install_source_ip_adapter(self, session):
        """
        Mount a SourceIPHTTPAdapter on a requests.Session so that every HTTP
        call routed through that session (auth, data, orders, etc.) automatically
        uses self.source_ip as the outbound local address.

        Safe to call with session=None or when source_ip is not set.
        """
        if not self.source_ip or not session or SourceIPHTTPAdapter is None:
            return
        try:
            adapter = SourceIPHTTPAdapter(self.source_ip)
            session.mount("https://", adapter)
            session.mount("http://", adapter)
            logger.info(
                f"[{self.instance_name}] SourceIPHTTPAdapter mounted "
                f"(source IP: {self.source_ip})"
            )
        except Exception as exc:
            logger.warning(
                f"[{self.instance_name}] Could not mount SourceIPHTTPAdapter: {exc}"
            )

    def _set_source_ip(self):
        """
        Fallback: set thread-local source IP for a single SDK call block.
        Use this for brokers whose HTTP client is not a requests.Session
        (e.g. Upstox swagger client, raw http.client usage).
        """
        if self.source_ip:
            _tls.source_ip = self.source_ip

    def _clear_source_ip(self):
        """Companion to _set_source_ip(): clear after the SDK call."""
        _tls.source_ip = None

    def is_configured_for_instrument(self, instrument_name):
        """Checks if this broker instance is configured to trade the given instrument."""
        return instrument_name.upper() in self.instruments

    def set_state_manager(self, state_manager):
        """Receives the shared StateManager instance."""
        self.state_manager = state_manager

    def _normalize_instrument_name(self, raw_name: str) -> str:
        """Normalize an instrument name to the short broker-compatible form.
        e.g. 'NIFTY 50' -> 'NIFTY', 'NIFTY BANK' -> 'BANKNIFTY'
        """
        upper = (raw_name or "NIFTY").strip().upper()
        return _INSTRUMENT_NAME_MAP.get(upper, upper)

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
