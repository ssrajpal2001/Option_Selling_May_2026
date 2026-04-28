import sys
import os
import datetime
import warnings
import asyncio
import pandas as pd
from dhanhq import dhanhq, marketfeed as market_feed
from .base_broker import BaseBroker
from utils.logger import logger
from utils.auth_manager_dhan import handle_dhan_login
from hub.event_bus import event_bus
import threading

class DhanClient(BaseBroker):
    # Class-level shared state to avoid redundant downloads across multiple client instances
    _shared_security_list_df = None
    _last_shared_load_time = None
    _shared_load_lock = asyncio.Lock()
    _DISK_CACHE_PATH = 'config/dhan_security_list.csv'

    def __init__(self, broker_instance_name, config_manager, login_required=True, user_id=None, db_config=None):
        super().__init__(broker_instance_name, config_manager, user_id=user_id, db_config=db_config)
        self.broker_name = 'dhan'
        self.dhan = None
        self.feed = None
        self._security_id_cache = {} # (name, expiry, strike, type) -> security_id
        self.subscribed_instruments = [] # list of tuples for Dhan (segment, security_id, type)
        self.ikey_to_sid = {} # instrument_key -> security_id
        self._pending_subscriptions = [] # List of tuples for Dhan (segment, security_id, type)
        self._last_conn_id = None # Track connection to avoid redundant subs

        if login_required:
            if self.db_config:
                # Multi-tenant DB path
                try:
                    client_id = self.db_config.get('api_key', '')

                    # 1. Attempt Automated Token Generation if Password/TOTP exist
                    if self.db_config.get('password') and self.db_config.get('totp'):
                        from utils.auth_manager_dhan import handle_dhan_login_automated
                        with self._scoped_ip_patch():
                            token = handle_dhan_login_automated(self.db_config)
                        if token:
                            self.dhan = dhanhq(client_id, token)
                            logger.info(f"Dhan automated client initialized for User ID: {self.user_id}.")

                    # 2. Fallback to existing access token
                    if not self.dhan:
                        access_token = self.db_config.get('access_token') or self.db_config.get('api_secret')
                        if client_id and access_token:
                            self.dhan = dhanhq(client_id, access_token)
                            logger.info(f"Dhan client initialized from token for User ID: {self.user_id}.")
                        else:
                            logger.error(f"Dhan: Missing credentials in DB config for user {self.user_id}.")
                except Exception as e:
                    logger.error(f"Failed to initialize Dhan client for user {self.user_id}: {e}")
            else:
                # Legacy INI path
                credentials_section = self.config_manager.get(broker_instance_name, 'credentials')
                try:
                    self.dhan = handle_dhan_login(credentials_section, self.config_manager)
                    logger.info(f"Dhan authentication successful for {credentials_section} [{self.instance_name}].")
                except Exception as e:
                    logger.critical(
                        f"AUTHENTICATION FAILED for Dhan account [{self.instance_name}]. Reason: {e}. "
                        f"Continuing in degraded mode (market data / paper trading only). "
                        f"Live orders will be blocked until credentials are configured via Settings.",
                        exc_info=True
                    )
                    # Do NOT sys.exit — allow the subprocess to continue for paper trading
                    # and market data. Live order placement is gated by paper_trade mode
                    # and per-broker trading_active checks.

            # Proactively trigger security list loading in the background AFTER dhan is initialized
            if not self.paper_trade and self.dhan:
                asyncio.create_task(self._load_security_list())

        # Mount SourceIPHTTPAdapter on Dhan's requests session so that ALL HTTP
        # calls (auth, orders, funds, security list) route through the assigned IP.
        if self.dhan and self.source_ip:
            self._install_source_ip_adapter(getattr(self.dhan, 'session', None))

    def connect(self):
        # Established in __init__
        pass

    def _apply_websockets_patch(self):
        """
        Monkey-patches the 'websockets' library to maintain compatibility
        between Dhan SDK (expecting v13 legacy API) and environment (v14+ asyncio API).
        """
        try:
            import websockets
            import sys
            from websockets import State

            # 1. Provide websockets.protocol.State (Dhan SDK uses this for connection checks)
            if not hasattr(websockets, 'protocol'):
                from types import ModuleType
                protocol_mod = ModuleType('websockets.protocol')
                websockets.protocol = protocol_mod
                sys.modules['websockets.protocol'] = protocol_mod

            if not hasattr(websockets.protocol, 'State'):
                class StateLegacy:
                    CONNECTING = State.CONNECTING
                    OPEN = State.OPEN
                    CLOSING = State.CLOSING
                    CLOSED = State.CLOSED
                websockets.protocol.State = StateLegacy
                logger.info("Dhan: Patched websockets.protocol.State successfully.")

            # 2. Patch ClientConnection classes to provide .closed and .open properties
            classes_to_patch = []
            try:
                from websockets.asyncio.client import ClientConnection
                classes_to_patch.append(ClientConnection)
            except ImportError: pass

            try:
                from websockets.legacy.protocol import WebSocketCommonProtocol
                classes_to_patch.append(WebSocketCommonProtocol)
            except ImportError: pass

            for cls in classes_to_patch:
                if not hasattr(cls, 'closed'):
                    def get_closed(self):
                        try: return self.state.value == State.CLOSED.value
                        except: return False
                    cls.closed = property(get_closed)
                    logger.info(f"Dhan: Patched {cls.__name__}.closed")

                if not hasattr(cls, 'open'):
                    def get_open(self):
                        try: return self.state.value == State.OPEN.value
                        except: return False
                    cls.open = property(get_open)
                    logger.info(f"Dhan: Patched {cls.__name__}.open")

        except Exception as e:
            logger.warning(f"Dhan: Failed to apply websockets compatibility patch: {e}")

    def start_data_feed(self):
        """Starts the Dhan WebSocket feed in a background thread."""
        if not self.dhan:
            logger.info(f"[{self.instance_name}] Skipping Dhan data feed (Not authenticated).")
            return

        self._apply_websockets_patch()

        # Capture current event loop for thread-safe publishing
        try:
            self.loop = asyncio.get_event_loop()
        except RuntimeError:
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)

        # Dhan SDK 'market_feed' needs credentials
        raw_access_token = self.db_config.get('access_token') or self.db_config.get('api_secret') if self.db_config else self.config_manager.get_credential(self.instance_name, 'access_token')
        raw_client_id = self.db_config.get('api_key') if self.db_config else self.config_manager.get_credential(self.instance_name, 'client_id')

        logger.info(f"[{self.instance_name}] Dhan Feed Credentials Check: CLIENT_ID={'PRESENT' if raw_client_id else 'MISSING'}, ACCESS_TOKEN={'PRESENT' if raw_access_token else 'MISSING'}")

        if not all([raw_client_id, raw_access_token]):
            logger.error(f"[{self.instance_name}] Missing credentials for Dhan WebSocket.")
            return

        client_id = str(raw_client_id).strip()
        access_token = str(raw_access_token).strip()

        logger.info(f"[{self.instance_name}] Initializing Dhan Market Feed (Paper Mode: {self.paper_trade}). Client ID: {client_id[:4]}***, Token: {access_token[:6]}***")

        # Dhan WebSocket requires an initial list of instruments.
        # In v2.0.2, the constructor only takes client_id, access_token, and instruments.
        # We specify version='v2' to avoid HTTP 400 errors.
        # Resolve feed class across dhanhq versions (2.0.x → DhanFeed; later may differ)
        # Each named candidate is verified to be a class (isinstance(..., type)) to
        # guard against future SDK versions that export one of these names as a non-class.
        def _get_feed_cls(mod, name):
            obj = getattr(mod, name, None)
            return obj if isinstance(obj, type) else None

        _DhanFeedCls = (
            _get_feed_cls(market_feed, 'DhanFeed') or
            _get_feed_cls(market_feed, 'Feed') or
            _get_feed_cls(market_feed, 'MarketFeed') or
            _get_feed_cls(market_feed, 'DhanMarketFeed') or
            _get_feed_cls(market_feed, 'DhanHQ') or
            next(
                (v for k, v in vars(market_feed).items()
                 if isinstance(v, type) and ('feed' in k.lower() or 'Feed' in k)),
                None
            )
        )
        if _DhanFeedCls is None:
            available = [k for k in dir(market_feed) if not k.startswith('_')]
            logger.error(
                f"[{self.instance_name}] Cannot find a feed class in dhanhq.marketfeed. "
                f"Available names: {available}. Feed not initialized."
            )
            return
        self.feed = _DhanFeedCls(
            client_id=client_id,
            access_token=access_token,
            instruments=self.subscribed_instruments,
            version='v2'
        )

        # Start the connection and data retrieval loop in a background task
        asyncio.run_coroutine_threadsafe(self._run_feed_loop(), self.loop)
        logger.info(f"[{self.instance_name}] Dhan Data Feed loop scheduled.")

    async def _run_feed_loop(self):
        """Internal loop to connect and continuously fetch ticks from Dhan."""
        import websockets
        from websockets import State
        import json

        retry_delay = 2
        while self.feed:
            try:
                # Check connection state
                is_connected = False
                if self.feed.ws:
                    # Robust state check: Try our patched .open first, fallback to numeric state comparison
                    is_connected = getattr(self.feed.ws, 'open', False)
                    if not is_connected and hasattr(self.feed.ws, 'state'):
                        # 1 is State.OPEN in nearly all websockets versions
                        try: is_connected = (int(self.feed.ws.state) == 1)
                        except: pass

                if not is_connected:
                    logger.info(f"[{self.instance_name}] Dhan: Connecting to feed...")
                    # ensure cleanup of old ws if exists
                    if self.feed.ws:
                        try: await self.feed.ws.close()
                        except: pass
                    self.feed.ws = None

                    await self.feed.connect()

                    # Reset retry delay on successful connection
                    retry_delay = 2
                    self._on_connect(self.feed)

                    # Mandatory small sleep after connection to let it stabilize
                    await asyncio.sleep(0.5)

                # Directly use ws.recv() to avoid SDK bugs (like missing on_message_received in v2.0.2)
                # Use wait_for to allow the loop to check for shutdown
                raw_message = await asyncio.wait_for(self.feed.ws.recv(), timeout=5.0)
                if not raw_message:
                    continue

                # The raw_message can be bytes (binary protocol) or string (JSON protocol)
                processed_data = None
                if isinstance(raw_message, bytes):
                    try:
                        # Use SDK's binary processor if it looks like binary
                        processed_data = self.feed.process_data(raw_message)
                    except Exception as pe:
                        logger.debug(f"[{self.instance_name}] Dhan binary parse fail: {pe}")

                # If binary parsing failed or produced nothing, try JSON
                if not processed_data:
                    try:
                        processed_data = json.loads(raw_message)
                    except:
                        pass

                if processed_data:
                    self._on_message(processed_data)

            except (websockets.exceptions.ConnectionClosed, asyncio.TimeoutError):
                # Timeout is normal if no ticks are arriving, just loop and check connection
                pass
            except Exception as e:
                # Catch "no close frame" and other transient errors
                err_str = str(e).lower()
                if "no close frame" in err_str or "attribute" in err_str or "rejected" in err_str:
                    # Exponential backoff for rejected/429
                    if "429" in err_str or "rejected" in err_str:
                        logger.warning(f"[{self.instance_name}] Dhan connection REJECTED (Too Many Requests). Waiting {retry_delay}s...")
                        await asyncio.sleep(retry_delay)
                        retry_delay = min(retry_delay * 2, 60) # Max 1 min backoff
                    else:
                        logger.warning(f"[{self.instance_name}] Dhan connection issue: {e}. Reconnecting in 2s...")
                        await asyncio.sleep(2)

                    if self.feed:
                        try: self.feed.ws = None
                        except: pass
                else:
                    logger.error(f"[{self.instance_name}] Error in Dhan data retrieval: {e}")
                    await asyncio.sleep(2)

    def stop_data_feed(self):
        """Stops the Dhan WebSocket feed."""
        if self.feed:
            logger.info(f"[{self.instance_name}] Stopping Dhan Data Feed...")
            if self.loop:
                asyncio.run_coroutine_threadsafe(self.feed.disconnect(), self.loop)
            self.feed = None

    def _on_message(self, message):
        """
        Callback for incoming Dhan ticks.
        Normalization: Convert Dhan's message to standardized tick format.
        """
        import json
        if not message:
            return

        # V2 can send JSON strings or bytes
        if isinstance(message, (str, bytes)):
            try:
                message = json.loads(message)
            except:
                pass

        if not isinstance(message, dict):
            return

        logger.debug(f"[{self.instance_name}] Received Dhan tick: {message}")

        # Dhan messages can be dictionaries. Handle both SDK binary-parsed and V2 JSON formats.
        # Possible keys: security_id (Binary), SecurityId (JSON), etc.
        sid = message.get('security_id') or message.get('SecurityId') or message.get('si')
        if sid is None:
            return

        sid = str(sid)
        # Reverse map sid to instrument_key
        inst_key = None
        for ikey, stored_sid in self.ikey_to_sid.items():
            if str(stored_sid) == sid:
                inst_key = ikey
                break

        if not inst_key:
            return

        # Handle Dhan's varied key naming (LTP vs last_price vs lp)
        ltp = message.get('LTP') or message.get('last_price') or message.get('lp')
        volume = message.get('volume') or message.get('vtt') or message.get('v') or 0
        oi = message.get('OI') or message.get('oi')
        atp = message.get('avg_price') or message.get('average_price') or message.get('atp')

        if ltp is None:
            return

        normalized_tick = {
            'user_id': self.user_id,
            'instrument_key': inst_key,
            'ltp': float(ltp),
            'volume': int(volume),
            'timestamp': datetime.datetime.now(), # Dhan might not provide exch timestamp in Ticker mode
            'broker': 'dhan'
        }

        if oi is not None: normalized_tick['oi'] = int(oi)
        if atp is not None: normalized_tick['atp'] = float(atp)

        # Inject into Event Bus
        asyncio.run_coroutine_threadsafe(
            event_bus.publish('BROKER_TICK_RECEIVED', normalized_tick),
            self.loop or asyncio.get_event_loop()
        )

    def _on_connect(self, instance):
        # instance is the DhanFeed object. its .ws is the actual connection.
        current_ws_id = id(instance.ws) if instance and instance.ws else None
        if current_ws_id == self._last_conn_id:
            return

        self._last_conn_id = current_ws_id
        logger.info(f"[{self.instance_name}] Dhan WebSocket connected (Conn ID: {current_ws_id}).")

        if self._pending_subscriptions:
            logger.info(f"[{self.instance_name}] Processing {len(self._pending_subscriptions)} pending Dhan subscriptions.")
            self.feed.subscribe_symbols(self._pending_subscriptions)
            for sub in self._pending_subscriptions:
                if sub not in self.subscribed_instruments:
                    self.subscribed_instruments.append(sub)
            self._pending_subscriptions = []

        if self.subscribed_instruments:
             # Ensure everything is subscribed on reconnect
             self.feed.subscribe_symbols(self.subscribed_instruments)

    def _on_error(self, instance, error):
        logger.error(f"[{self.instance_name}] Dhan WebSocket error: {error}")

    def _on_close(self, instance):
        logger.warning(f"[{self.instance_name}] Dhan WebSocket closed.")

    def subscribe_instruments(self, instrument_map):
        """
        External method to register instruments for this Dhan feed.
        instrument_map: { instrument_key (str): (exchange_segment, security_id) }
        """
        from websockets import State
        new_subs = []
        for ikey, info in instrument_map.items():
            segment, sid = info
            if ikey not in self.ikey_to_sid:
                self.ikey_to_sid[ikey] = str(sid)
                # Dhan sub format: (segment, security_id, subscription_type)
                # Dhan SDK constants: Ticker=15, Quote=17, Depth=19, Full=21
                sub_tuple = (segment, str(sid), market_feed.Quote)
                new_subs.append(sub_tuple)

        if not new_subs:
            return

        # Check if feed exists and is actually connected
        is_connected = False
        if self.feed and self.feed.ws:
            if hasattr(self.feed.ws, 'state'):
                is_connected = (self.feed.ws.state == State.OPEN)
            else:
                is_connected = getattr(self.feed.ws, 'open', False)

        if not is_connected:
            logger.info(f"[{self.instance_name}] Dhan Feed not connected. Queueing {len(new_subs)} instruments.")
            self._pending_subscriptions.extend(new_subs)
            return

        logger.info(f"[{self.instance_name}] Subscribing to {len(new_subs)} new Dhan instruments.")
        self.feed.subscribe_symbols(new_subs)
        for s in new_subs:
            if s not in self.subscribed_instruments:
                self.subscribed_instruments.append(s)

    async def _load_security_list(self):
        """Downloads and loads the Dhan security list into a DataFrame with disk caching."""
        async with self._shared_load_lock:
            now = datetime.datetime.now()

            # 1. Check if already loaded in memory today
            if self._shared_security_list_df is not None and self._last_shared_load_time and self._last_shared_load_time.date() == now.date():
                return

            # 2. Check disk cache
            if os.path.exists(self._DISK_CACHE_PATH):
                mtime = datetime.datetime.fromtimestamp(os.path.getmtime(self._DISK_CACHE_PATH))
                if mtime.date() == now.date():
                    try:
                        logger.info(f"Dhan: Loading security list from disk cache...")
                        self._shared_security_list_df = await asyncio.to_thread(pd.read_csv, self._DISK_CACHE_PATH, low_memory=False)
                        self._last_shared_load_time = mtime
                        logger.info(f"Dhan: Security list loaded from cache. Total records: {len(self._shared_security_list_df)}")
                        return
                    except Exception as e:
                        logger.warning(f"Dhan: Failed to read security list cache: {e}")

            # 3. Download fresh list
            if not self.dhan:
                return

            try:
                logger.info("Dhan: Downloading fresh security list...")
                # mode='compact' should be enough for basic info
                # Suppress DtypeWarning from dhanhq's internal read_csv
                with warnings.catch_warnings():
                    warnings.filterwarnings("ignore", category=pd.errors.DtypeWarning)
                    # Use to_thread since fetch_security_list is likely blocking (sync)
                    df = await asyncio.to_thread(self.dhan.fetch_security_list, mode='compact')

                if df is not None and not df.empty:
                    self._shared_security_list_df = df
                    self._last_shared_load_time = now
                    logger.info(f"Dhan: Security list downloaded. Total records: {len(df)}")

                    # Save to disk cache
                    os.makedirs(os.path.dirname(self._DISK_CACHE_PATH), exist_ok=True)
                    await asyncio.to_thread(df.to_csv, self._DISK_CACHE_PATH, index=False)
                    logger.debug(f"Dhan: Security list cached to {self._DISK_CACHE_PATH}")
            except Exception as e:
                logger.error(f"Failed to download Dhan security list: {e}")

    async def get_security_id(self, contract):
        """Finds the security_id for a given contract."""
        # Use our own logic to map contract info to Dhan's security list
        strike = float(contract.strike_price)
        expiry = contract.expiry.date() if isinstance(contract.expiry, datetime.datetime) else contract.expiry
        opt_type = contract.instrument_type.upper() # 'CE' or 'PE'
        name = contract.name.upper()

        cache_key = (name, expiry, strike, opt_type)
        if cache_key in self._security_id_cache:
            return self._security_id_cache[cache_key]

        await self._load_security_list()

        if self._shared_security_list_df is None:
            return None

        try:
            df = self._shared_security_list_df

            # Dhan Expiry is usually in 'YYYY-MM-DD HH:MM:SS' format in the CSV
            # Let's convert SEM_EXPIRY_DATE to date
            if 'SEM_EXPIRY_DATE' in df.columns:
                # Use errors='coerce' to handle '0001-01-01' and other malformed dates
                df['expiry_date'] = pd.to_datetime(df['SEM_EXPIRY_DATE'], errors='coerce').dt.date
            else:
                logger.error("Dhan: SEM_EXPIRY_DATE column missing in security list.")
                return None

            # Filter conditions
            # For Options (OPTIDX), Dhan uses SEM_TRADING_SYMBOL which starts with Name
            # e.g. NIFTY-Feb2026-25000-CE

            # Strategy 1: Trading Symbol Match (e.g. NIFTY-Feb2026-25000-CE)
            mask1 = (
                (df['SEM_TRADING_SYMBOL'].str.startswith(name + "-")) &
                (df['expiry_date'] == expiry) &
                (df['SEM_OPTION_TYPE'].str.upper() == opt_type) &
                (df['SEM_STRIKE_PRICE'].astype(float) == strike)
            )

            # Strategy 2: Custom Symbol Match (e.g. NIFTY 24 FEB 25000 CALL)
            # We don't know the exact format of SEM_CUSTOM_SYMBOL but it usually contains these parts
            mask2 = (
                (df['SEM_CUSTOM_SYMBOL'].str.contains(name, case=False, na=False)) &
                (df['SEM_CUSTOM_SYMBOL'].str.contains(str(int(strike)), na=False)) &
                (df['expiry_date'] == expiry) &
                (df['SEM_OPTION_TYPE'].str.upper() == opt_type)
            )

            result = df[mask1]
            if result.empty:
                logger.debug(f"Dhan: Strategy 1 (Trading Symbol) failed for {name}. Trying Strategy 2 (Custom Symbol)...")
                result = df[mask2]

            if result.empty:
                # Strategy 3: Fuzzy Expiry Match (Same strike, type, symbol name, but +/- 2 days on expiry)
                logger.debug(f"Dhan: Strategy 2 failed. Trying Strategy 3 (Fuzzy Expiry)...")
                mask3 = (
                    (df['SEM_TRADING_SYMBOL'].str.startswith(name + "-")) &
                    (pd.to_datetime(df['expiry_date']).dt.date >= expiry - datetime.timedelta(days=2)) &
                    (pd.to_datetime(df['expiry_date']).dt.date <= expiry + datetime.timedelta(days=2)) &
                    (df['SEM_OPTION_TYPE'].str.upper() == opt_type) &
                    (df['SEM_STRIKE_PRICE'].astype(float) == strike)
                )
                result = df[mask3]

            if not result.empty:
                security_id = str(result.iloc[0]['SEM_SMST_SECURITY_ID'])
                self._security_id_cache[cache_key] = security_id
                logger.info(f"Dhan: Found SecurityID {security_id} for {name} {strike} {opt_type} {expiry}")
                return security_id
            else:
                logger.warning(f"Dhan: No match found in security list for {name} {strike} {opt_type} {expiry}")
                return None

        except Exception as e:
            logger.error(f"Error searching Dhan security list: {e}", exc_info=True)
            return None

    async def handle_entry_signal(self, **kwargs):
        if not self.dhan:
            logger.error(f"Dhan client not initialized for '{self.instance_name}'.")
            return

        contract = kwargs.get('contract')
        instrument_name = kwargs.get('instrument_name')
        direction = kwargs.get('direction')
        signal_expiry_date = kwargs.get('signal_expiry_date')

        if not all([contract, instrument_name, direction, signal_expiry_date]):
            logger.error("Dhan: handle_entry_signal missing required arguments.")
            return

        broker_base_qty = self.config_manager.get_int(self.instance_name, 'quantity', 1)
        instrument_lot_size = contract.lot_size
        final_qty = broker_base_qty * instrument_lot_size

        try:
            order_id = None
            if self.paper_trade:
                logger.info(f"--- DHAN [PAPER TRADE] ENTRY ({direction}) ---")
                logger.info(f"  Instrument: {contract.instrument_key}")
                logger.info(f"  Quantity: {final_qty} | Price: {kwargs.get('ltp', 0)}")
                order_id = "PAPER_DHAN_ORDER"
            else:
                order_id = self.place_order(contract, "BUY", final_qty, signal_expiry_date)

            if order_id:
                instrument_symbol = self.construct_dhan_symbol(contract)
                if not self.paper_trade:
                    logger.info(f"Successfully placed Dhan BUY ({direction}) order for {instrument_symbol}, ID: {order_id}")

                await event_bus.publish('TRADE_CONFIRMED', {
                    'user_id': self.user_id,
                    'instrument_name': instrument_name,
                    'direction': direction,
                    'trade_contract': contract,
                    'instrument_symbol': instrument_symbol,
                    'ltp': kwargs.get('ltp', 0)
                })

                self.trade_logger.log_entry(
                    broker=self.instance_name,
                    instrument_name=instrument_name,
                    instrument_symbol=instrument_symbol,
                    trade_type=direction,
                    price=kwargs.get('ltp', 0),
                    strategy_log=kwargs.get('strategy_log', ""),
                    user_id=self.user_id
                )
                return order_id
        except Exception as e:
            logger.error(f"Exception placing Dhan entry order: {e}", exc_info=True)
            return None

    async def handle_close_signal(self, **kwargs):
        if not self.dhan:
            logger.error(f"Dhan client not initialized for '{self.instance_name}'.")
            return

        side = kwargs.get('side')
        instrument_name = kwargs.get('instrument_name')
        signal_expiry_date = kwargs.get('signal_expiry_date')

        # Prioritize contract/position from payload, fallback to StateManager
        contract = kwargs.get('contract')
        position = self.state_manager.get_position(side)

        if not contract and position:
            contract = position.get('contract')

        if not contract:
            logger.warning(f"No active {side} position found on Dhan.")
            return
        broker_base_qty = self.config_manager.get_int(self.instance_name, 'quantity', 1)
        final_qty = broker_base_qty * contract.lot_size

        try:
            order_id = None
            if self.paper_trade:
                logger.info(f"--- DHAN [PAPER TRADE] EXIT ({side}) ---")
                logger.info(f"  Instrument: {contract.instrument_key}")
                logger.info(f"  Quantity: {final_qty} | Price: {kwargs.get('ltp', 0)}")
                order_id = "PAPER_DHAN_EXIT"
            else:
                order_id = self.place_order(contract, "SELL", final_qty, signal_expiry_date)

            if order_id:
                if not self.paper_trade:
                    logger.info(f"Successfully placed Dhan SELL order to close {side} position, ID: {order_id}")

                await event_bus.publish('TRADE_CLOSED', {
                    'user_id': self.user_id,
                    'instrument_name': instrument_name,
                    'direction': side
                })

                price = kwargs.get('ltp', 0)
                reason = kwargs.get('reason', 'UNKNOWN')
                entry_price = position.get('entry_price', 0) if position else 0
                pnl = (price - entry_price) if entry_price > 0 else 0

                instrument_symbol = self.construct_dhan_symbol(contract)
                self.trade_logger.log_exit(
                    broker=self.instance_name,
                    instrument_name=instrument_name,
                    instrument_symbol=instrument_symbol,
                    trade_type=f"EXIT_{side}",
                    price=price,
                    pnl=pnl,
                    reason=reason,
                    strategy_log=kwargs.get('strategy_log', ""),
                    user_id=self.user_id
                )
        except Exception as e:
            logger.error(f"Exception closing Dhan position: {e}", exc_info=True)

    def place_order(self, contract, transaction_type, quantity, expiry, product_type='NRML', market_protection=None):
        if not self.dhan: return None
        self._validate_source_ip()

        if transaction_type == "BUY":
            dhan_transaction_type = self.dhan.BUY
        else:
            dhan_transaction_type = self.dhan.SELL

        # Map Exchange
        exchange = contract.exchange.upper() if hasattr(contract, 'exchange') else 'NSE'
        if 'NSE' in exchange:
            segment = self.dhan.NSE_FNO
        elif 'BSE' in exchange:
            segment = self.dhan.BSE_FNO
        else:
            segment = self.dhan.NSE_FNO

        try:
            # Resolve security_id — short-circuit via ikey_to_sid cache first (dual-role:
            # when Dhan is both the data feed AND the execution broker, the feed's
            # ikey_to_sid map already has every subscribed contract's security_id).
            ikey = getattr(contract, 'instrument_key', None)
            security_id = self.ikey_to_sid.get(ikey) if ikey else None

            if not security_id:
                # Fallback: look up from security list CSV (slower, but complete)
                try:
                    loop = asyncio.get_event_loop()
                    if loop.is_running():
                        security_id = asyncio.run_coroutine_threadsafe(
                            self.get_security_id(contract), loop
                        ).result(timeout=10)
                    else:
                        security_id = loop.run_until_complete(self.get_security_id(contract))
                except Exception as se:
                    logger.error(f"[DhanClient] Failed to resolve security_id in place_order for {ikey}: {se}")
                    return None

            if not security_id:
                logger.error(f"[DhanClient] No security_id found for {ikey}. Cannot place order.")
                return None

            # Dhan API expects string security_id
            # Price is required even for MARKET orders (use 0)
            response = self.dhan.place_order(
                security_id=str(security_id),
                exchange_segment=segment,
                transaction_type=dhan_transaction_type,
                quantity=int(quantity),
                order_type=self.dhan.MARKET,
                product_type=self.dhan.MARGIN, # Carry Forward for Options
                price=0.0,
                validity='DAY'
            )

            if response.get('status') == 'success':
                return response.get('data', {}).get('orderId')
            else:
                logger.error(f"Dhan order failed. Resp: {response}")
                return None
        except Exception as e:
            logger.error(f"Error in Dhan place_order: {e}", exc_info=True)
            return None

    def get_ltp(self, symbol):
        return 0.0

    def construct_dhan_symbol(self, contract):
        """Constructs a readable symbol for Dhan contracts."""
        strike = int(contract.strike_price)
        expiry = contract.expiry.date() if isinstance(contract.expiry, datetime.datetime) else contract.expiry
        opt_type = str(getattr(contract, 'instrument_type', 'CE') or 'CE').upper()
        name = str(getattr(contract, 'name', 'NIFTY') or 'NIFTY').upper()
        # Format: NIFTY 24 FEB 25000 CE
        return f"{name} {expiry.strftime('%d %b').upper()} {strike} {opt_type}"

    async def close_all_positions(self):
        """
        Squares off all open positions in Dhan.
        Industrial standard safety measure.
        """
        if not self.dhan or self.paper_trade:
            return

        try:
            # Dhan doesn't have a single 'square_off_all' in standard SDK
            # Fetch positions and iterate
            positions = await asyncio.to_thread(self.dhan.get_positions)
            if not positions or not isinstance(positions, list):
                return

            close_tasks = []
            for pos in positions:
                qty = pos.get('netQty', 0)
                if qty == 0: continue

                trans_type = self.dhan.SELL if qty > 0 else self.dhan.BUY
                abs_qty = abs(qty)
                security_id = pos.get('securityId')
                segment = pos.get('exchangeSegment')
                product = pos.get('productType')

                logger.info(f"[{self.instance_name}] Dhan Squaring off {security_id} ({qty})")

                close_tasks.append(asyncio.to_thread(
                    self.dhan.place_order,
                    security_id=str(security_id),
                    exchange_segment=segment,
                    transaction_type=trans_type,
                    quantity=int(abs_qty),
                    order_type=self.dhan.MARKET,
                    product_type=product,
                    price=0.0
                ))

            if close_tasks:
                await asyncio.gather(*close_tasks, return_exceptions=True)
                logger.info(f"[{self.instance_name}] Dhan positions squared off.")
        except Exception as e:
            logger.error(f"[{self.instance_name}] Dhan close_all error: {e}")

    async def get_funds(self):
        """Returns available funds from Dhan."""
        if not self.dhan: return 0.0
        try:
            funds = await asyncio.to_thread(self.dhan.get_fund_limits)
            if funds and funds.get('status') == 'success':
                # 'dhanCash' is the usual field for available balance
                return float(funds.get('data', {}).get('dhanCash', 0.0))
            return 0.0
        except Exception as e:
            logger.error(f"[{self.instance_name}] Dhan funds error: {e}")
            return 0.0

    async def get_positions(self):
        """Returns live positions from Dhan."""
        if not self.dhan: return []
        try:
            raw = await asyncio.to_thread(self.dhan.get_positions)
            if not raw or not isinstance(raw, list): return []
            positions = []
            for p in raw:
                if p.get('netQty', 0) == 0: continue
                positions.append({
                    'symbol': p.get('tradingSymbol', str(p.get('securityId'))),
                    'quantity': p.get('netQty'),
                    'average_price': p.get('avgCostPrice'),
                    'ltp': p.get('lastPrice'),
                    'pnl': p.get('realizedProfit', 0) + p.get('unrealizedProfit', 0)
                })
            return positions
        except Exception as e:
            logger.error(f"[{self.instance_name}] Dhan positions error: {e}")
            return []
