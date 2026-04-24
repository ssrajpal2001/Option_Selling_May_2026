import sys
from kiteconnect import KiteConnect, KiteTicker
from .base_broker import BaseBroker
from utils.logger import logger
from utils.auth_manager_zerodha import handle_zerodha_login
from hub.event_bus import event_bus
import os
import threading
import asyncio

class ZerodhaClient(BaseBroker):
    def __init__(self, broker_instance_name, config_manager, login_required=True,
                 user_id=None, db_config=None, pre_auth_kite=None):
        super().__init__(broker_instance_name, config_manager, user_id=user_id, db_config=db_config)
        self.broker_name = 'zerodha'
        self.kite = None
        self.ticker = None
        self.token_to_key = {} # instrument_token (int) -> instrument_key (str)
        self.subscribed_tokens = set()
        self._pending_subscriptions = [] # List of tokens to subscribe when connected
        self._instrument_map = {} # (symbol, exchange) -> token
        self._master_instruments = None

        if pre_auth_kite is not None:
            self.kite = pre_auth_kite
            self.api_key = getattr(pre_auth_kite, 'api_key', self.api_key)
            logger.info(f"Zerodha client [{self.instance_name}] initialized with pre-authenticated kite object. API Key: {self.api_key[:4]}***")
        elif self.paper_trade:
            logger.info(f"Zerodha client [{self.instance_name}] operating in PAPER mode. Skipping API login.")
        elif login_required:
            if self.db_config and self.db_config.get('password') and self.db_config.get('totp'):
                # Handle automated login if credentials provided
                try:
                    from utils.auth_manager_zerodha import handle_zerodha_login_automated
                    # Ensure access_token is set in self.db_config if we want to use existing session
                    # But handle_zerodha_login_automated now generates a fresh one.
                    token = handle_zerodha_login_automated(self.db_config)
                    if token:
                        self.kite = KiteConnect(api_key=self.api_key)
                        self.kite.set_access_token(token)
                        profile = self.kite.profile()
                        logger.info(f"Zerodha automated authentication successful for user: {profile.get('user_id')} [{self.instance_name}].")
                    else:
                        raise Exception("Automated authentication failed.")
                except Exception as e:
                    logger.error(f"Zerodha automated login failed for {self.instance_name}: {e}. Falling back to standard login.")
                    login_required = True # Fallback

            if login_required and not self.kite:
                credentials_section = self.config_manager.get(broker_instance_name, 'credentials')
                try:
                    self.kite = handle_zerodha_login(credentials_section, self.config_manager)
                    if self.kite:
                        profile = self.kite.profile()
                        logger.info(f"Zerodha authentication successful for user: {profile.get('user_id')} [{self.instance_name}].")
                    else:
                        raise Exception("The authentication process failed and did not return a client.")
                except Exception as e:
                    logger.error(f"AUTHENTICATION FAILED for Zerodha account [{self.instance_name}]. Reason: {e}", exc_info=True)
                    raise RuntimeError(f"Failed to authenticate Zerodha client: {e}")

    def connect(self):
        # Established in init. But we also ensure master instruments are loaded.
        if not self.paper_trade and self.kite:
            asyncio.create_task(self.load_instrument_master())

    async def load_instrument_master(self):
        """Downloads and processes the Kite instrument master for key translation."""
        if self._master_instruments is not None: return
        try:
            logger.info(f"[{self.instance_name}] Loading Zerodha instrument master...")
            # Use to_thread for blocking SDK call
            instruments = await asyncio.to_thread(self.kite.instruments)
            self._master_instruments = instruments

            mapping = {}
            for inst in instruments:
                # Store by (tradingsymbol, exchange) for robust lookup
                mapping[(inst['tradingsymbol'], inst['exchange'])] = inst['instrument_token']
                # Also store by just tradingsymbol for common indices
                if inst['segment'] == 'INDICES':
                    mapping[inst['tradingsymbol']] = inst['instrument_token']

            self._instrument_map = mapping
            logger.info(f"[{self.instance_name}] Zerodha master loaded. {len(instruments)} instruments cached.")
        except Exception as e:
            logger.error(f"[{self.instance_name}] Failed to load Zerodha instruments: {e}")

    def get_token_by_symbol(self, symbol, exchange='NFO'):
        """Translates a trading symbol and exchange to an instrument token."""
        if not self._instrument_map: return None
        # Try exact, then uppercase, then just symbol
        res = self._instrument_map.get((symbol, exchange))
        if res: return res
        res = self._instrument_map.get((symbol.upper(), exchange))
        if res: return res
        res = self._instrument_map.get(symbol)
        if res: return res
        return self._instrument_map.get(symbol.upper())

    def start_data_feed(self):
        """Starts the KiteTicker WebSocket feed in a background thread."""
        if not self.kite:
            logger.info(f"[{self.instance_name}] Skipping data feed (Not authenticated).")
            return

        # Capture current event loop for thread-safe publishing from ticker thread
        try:
            self.loop = asyncio.get_event_loop()
        except RuntimeError:
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)

        # Robustness: ensure api_key and access_token are available and clean
        raw_api_key = getattr(self, 'api_key', None) or getattr(self.kite, 'api_key', None)
        raw_access_token = getattr(self.kite, 'access_token', None)

        # Log credentials for debugging (masked)
        logger.info(f"[{self.instance_name}] Data Feed Credentials Check: API_KEY={'PRESENT' if raw_api_key else 'MISSING'}, ACCESS_TOKEN={'PRESENT' if raw_access_token else 'MISSING'}")

        if not raw_api_key or not raw_access_token:
            logger.error(f"[{self.instance_name}] Cannot start data feed: Missing API Key ({'YES' if raw_api_key else 'NO'}) or Access Token ({'YES' if raw_access_token else 'NO'}).")
            return

        api_key = str(raw_api_key).strip()
        access_token = str(raw_access_token).strip()

        logger.info(f"[{self.instance_name}] Initializing Zerodha KiteTicker (Paper Mode: {self.paper_trade}). API Key: {api_key[:4]}***, Token: {access_token[:6]}***")
        self.ticker = KiteTicker(api_key, access_token)
        # self.ticker.debug = True # Enable for ultra-verbose websocket logs

        self.ticker.on_ticks = self._on_ticks
        self.ticker.on_connect = self._on_connect
        self.ticker.on_error = self._on_error
        self.ticker.on_close = self._on_close

        # Use KiteTicker's built-in threaded connection for better stability
        self.ticker.connect(threaded=True)
        logger.info(f"[{self.instance_name}] Zerodha Data Feed connection initiated (threaded).")

    def stop_data_feed(self):
        """Stops the KiteTicker WebSocket feed."""
        if self.ticker:
            logger.info(f"[{self.instance_name}] Stopping Zerodha Data Feed...")
            self.ticker.close()
            self.ticker = None

    def _on_ticks(self, ws, ticks):
        """
        Callback for incoming Zerodha ticks.
        Normalizes data and publishes to the event bus for multi-tenant routing.
        """
        if not ticks:
            return

        logger.info(f"[{self.instance_name}] DEBUG: Received {len(ticks)} ticks from Zerodha. Tokens: {[t.get('instrument_token') for t in ticks]}")

        for tick in ticks:
            token = tick.get('instrument_token')
            inst_key = self.token_to_key.get(token)

            if not inst_key:
                logger.info(f"[{self.instance_name}] DEBUG: Tick received for unknown token: {token} (LTP: {tick.get('last_price')})")
                continue

            # Standardized Tick Format for Strategy V3
            normalized_tick = {
                'user_id': self.user_id,
                'instrument_key': inst_key,
                'ltp': tick.get('last_price'),
                'volume': tick.get('volume', 0),
                'timestamp': tick.get('timestamp'),
                'broker': 'zerodha'
            }

            # Optional: Add full quotes if available
            if tick.get('ohlc'):
                normalized_tick['atp'] = tick.get('average_price')
                normalized_tick['oi'] = tick.get('oi')

            logger.debug(f"[{self.instance_name}] Dispatching normalized tick for {inst_key}: LTP={normalized_tick['ltp']}")

            # Inject into the system via Event Bus
            # This ensures PriceFeedHandler can update the CORRECT user's state
            asyncio.run_coroutine_threadsafe(
                event_bus.publish('BROKER_TICK_RECEIVED', normalized_tick),
                self.loop or asyncio.get_event_loop()
            )

    def _on_connect(self, ws, response):
        logger.info(f"[{self.instance_name}] Zerodha WebSocket connected.")

        # Merge pending subscriptions into the main set
        if self._pending_subscriptions:
            logger.info(f"[{self.instance_name}] Processing {len(self._pending_subscriptions)} pending subscriptions.")
            for token in self._pending_subscriptions:
                self.subscribed_tokens.add(token)
            self._pending_subscriptions = []

        if self.subscribed_tokens:
            logger.info(f"[{self.instance_name}] Subscribing to {len(self.subscribed_tokens)} tokens.")
            self.ticker.subscribe(list(self.subscribed_tokens))
            self.ticker.set_mode(self.ticker.MODE_FULL, list(self.subscribed_tokens))

    def _on_error(self, ws, code, reason):
        logger.error(f"[{self.instance_name}] Zerodha WebSocket error: {code} - {reason}")

    def _on_close(self, ws, code, reason):
        logger.warning(f"[{self.instance_name}] Zerodha WebSocket closed: {code} - {reason}")

    def subscribe_instruments(self, instrument_map):
        """
        External method to register instruments for this specific broker feed.
        instrument_map: { instrument_key (str): instrument_token (int) }
        """
        logger.info(f"[{self.instance_name}] subscribe_instruments called with {len(instrument_map)} instruments. Current mapped: {len(self.token_to_key)}")
        new_tokens = []
        for ikey, token in instrument_map.items():
            token = int(token)
            if token not in self.token_to_key:
                self.token_to_key[token] = ikey
                logger.info(f"[{self.instance_name}] DEBUG: Mapping token {token} to {ikey}")

            if token not in self.subscribed_tokens and token not in self._pending_subscriptions:
                new_tokens.append(token)

        if not new_tokens:
            logger.info(f"[{self.instance_name}] No NEW tokens to subscribe. Subscribed set size: {len(self.subscribed_tokens)}")
            return

        if not self.ticker or not getattr(self.ticker, 'ws', None):
            logger.info(f"[{self.instance_name}] Ticker not connected. Queueing {len(new_tokens)} tokens.")
            self._pending_subscriptions.extend(new_tokens)
            return

        # If already connected, subscribe immediately
        logger.info(f"[{self.instance_name}] Subscribing to {len(new_tokens)} new Zerodha tokens: {new_tokens}")
        for t in new_tokens: self.subscribed_tokens.add(t)
        self.ticker.subscribe(new_tokens)
        self.ticker.set_mode(self.ticker.MODE_FULL, new_tokens)

    async def handle_entry_signal(self, **kwargs):
        """Handles an entry signal by placing an order."""
        if not self.kite:
            logger.error(f"Kite connect object not initialized for '{self.instance_name}'. Cannot place order.")
            return

        contract = kwargs.get('contract')
        instrument_name = kwargs.get('instrument_name')
        direction = kwargs.get('direction')
        signal_expiry_date = kwargs.get('signal_expiry_date') # Extract the expiry date

        if not all([contract, instrument_name, direction, signal_expiry_date]):
            logger.error(f"handle_entry_signal missing required arguments. Data: {kwargs}")
            return

        broker_base_qty = self.config_manager.get_int(self.instance_name, 'quantity', 1)
        instrument_lot_size = contract.lot_size
        quantity_multiplier = int(kwargs.get('quantity_multiplier', 1))
        final_qty = broker_base_qty * instrument_lot_size * quantity_multiplier
        logger.info(f"Calculated trade quantity for {self.instance_name} on {instrument_name}: Broker Qty ({broker_base_qty}) * Lot Size ({instrument_lot_size}) * Multiplier ({quantity_multiplier}) = {final_qty}")

        try:
            order_id = None
            instrument_symbol = self.construct_zerodha_symbol(contract, signal_expiry_date)

            entry_type = kwargs.get('entry_type', 'BUY')
            transaction_type = "BUY" if entry_type == 'BUY' else "SELL"

            if self.paper_trade:
                logger.info(f"--- ZERODHA [PAPER TRADE] ENTRY ({direction} - {entry_type}) ---")
                logger.info(f"  Instrument: {instrument_symbol}")
                logger.info(f"  Quantity: {final_qty} | Price: {kwargs.get('ltp', 0)}")
                order_id = "PAPER_ZERODHA_ORDER"
            else:
                product_type = kwargs.get('product_type', 'NRML')
                order_id = self.place_order(contract, transaction_type, final_qty, signal_expiry_date, product_type=product_type)

            if order_id:
                if not self.paper_trade:
                    logger.info(f"Successfully placed {transaction_type} ({direction}) order for {instrument_symbol} with order_id: {order_id}")

                # Feedback to StateManager (Multi-Tenant aware)
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
            else:
                logger.error(f"Failed to place BUY ({direction}) order for {contract.instrument_key}.")
                return None
        except Exception as e:
            logger.error(f"Exception placing order for {contract.instrument_key}: {e}", exc_info=True)
            return None

    async def handle_close_signal(self, **kwargs):
        """Handles a close signal by closing the corresponding position."""
        if not self.kite:
            logger.error(f"Kite connect object not initialized for '{self.instance_name}'. Cannot close position.")
            return

        side = kwargs.get('side')
        instrument_name = kwargs.get('instrument_name')

        # Prioritize contract/position from payload, fallback to StateManager
        contract = kwargs.get('contract')
        position = self.state_manager.get_position(side)

        if not contract and position:
            contract = position.get('contract')

        if not contract:
            logger.warning(f"No active {side} position or contract found to close.")
            return

        # Calculate quantity
        broker_base_qty = self.config_manager.get_int(self.instance_name, 'quantity', 1)
        instrument_lot_size = contract.lot_size
        final_qty = broker_base_qty * instrument_lot_size

        try:
            order_id = None
            signal_expiry_date = kwargs.get('signal_expiry_date')
            instrument_symbol = self.construct_zerodha_symbol(contract, signal_expiry_date)

            entry_type = position.get('entry_type', 'BUY') if position else 'BUY'
            exit_transaction_type = "SELL" if entry_type == 'BUY' else "BUY"

            if self.paper_trade:
                logger.info(f"--- ZERODHA [PAPER TRADE] EXIT ({side} - {exit_transaction_type}) ---")
                logger.info(f"  Instrument: {instrument_symbol}")
                logger.info(f"  Quantity: {final_qty} | Price: {kwargs.get('ltp', 0)}")
                order_id = "PAPER_ZERODHA_EXIT"
            else:
                order_id = self.place_order(contract, exit_transaction_type, final_qty, signal_expiry_date)

            if order_id:
                if not self.paper_trade:
                    logger.info(f"Successfully placed {exit_transaction_type} order to close {side} position for {instrument_symbol} with order_id: {order_id}")

                # Feedback to StateManager
                await event_bus.publish('TRADE_CLOSED', {
                    'user_id': self.user_id,
                    'instrument_name': instrument_name,
                    'direction': side
                })

                # Log the live trade exit to the unified logger
                price = kwargs.get('ltp', 0)
                reason = kwargs.get('reason', 'UNKNOWN')
                entry_price = position.get('entry_price', 0) if position else 0
                pnl = (price - entry_price) if entry_price > 0 else 0

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
            else:
                logger.error(f"Failed to close {side} position for {contract.instrument_key}.")
        except Exception as e:
            logger.error(f"Exception closing position for {contract.instrument_key}: {e}", exc_info=True)

    def place_order(self, contract, transaction_type, quantity, signal_expiry_date, product_type='NRML', market_protection=None):
        """
        Places an order with Zerodha.
        Generates the Zerodha-specific symbol from the contract object.
        product_type: 'NRML' for carry-forward (sell strangle legs), 'MIS' for intraday (buy hedge legs).
        market_protection: Optional override for Zerodha's market protection feature (0-100 or -1).
        """
        symbol = self.construct_zerodha_symbol(contract, signal_expiry_date)
        if not symbol:
            logger.error(f"Could not construct a valid symbol for contract: {contract.__dict__}")
            return None

        if transaction_type == "BUY":
            zerodha_transaction_type = self.kite.TRANSACTION_TYPE_BUY
        else:
            zerodha_transaction_type = self.kite.TRANSACTION_TYPE_SELL

        # Resolve correct Zerodha exchange (NFO for NSE, BFO for BSE)
        exchange = self.kite.EXCHANGE_NFO
        if hasattr(contract, 'exchange') and contract.exchange == 'BSE':
            exchange = self.kite.EXCHANGE_BFO

        zerodha_product = self.kite.PRODUCT_NRML if product_type == 'NRML' else self.kite.PRODUCT_MIS

        try:
            # AMO Check: If market is closed, use variety=VARIETY_AMO and order_type=ORDER_TYPE_LIMIT
            import datetime
            import inspect
            now = datetime.datetime.now().time()
            variety = self.kite.VARIETY_REGULAR
            order_type = self.kite.ORDER_TYPE_MARKET
            price = 0

            # Zerodha API version check for market_protection support
            sig = inspect.signature(self.kite.place_order)
            supports_mprot = 'market_protection' in sig.parameters

            # Priority: 1. Passed argument (Strategy), 2. Broker Config (.ini), 3. Default (-1)
            if market_protection is not None:
                m_prot = market_protection
            else:
                m_prot_val = self.config_manager.get(self.instance_name, 'market_protection', -1)
                try:
                    m_prot = int(m_prot_val)
                except:
                    m_prot = -1

            # User Requirement: Use Market Orders for options but handle 'Market Protection' policy
            if exchange in (self.kite.EXCHANGE_NFO, self.kite.EXCHANGE_BFO):
                if supports_mprot:
                    # Best Case: Library supports new parameter. Use MARKET with Protection.
                    order_type = self.kite.ORDER_TYPE_MARKET
                    logger.info(f"[Zerodha] Options: Using MARKET order with Market Protection ({m_prot}).")
                else:
                    # Fallback: Library is old. Use LIMIT with 1-pt buffer to satisfy OMS protection.
                    order_type = self.kite.ORDER_TYPE_LIMIT
                    curr_ltp = self.get_ltp(symbol, exchange)
                    if not curr_ltp or curr_ltp <= 0:
                        curr_ltp = self.state_manager.get_ltp(contract.instrument_key) or 100.0

                    if transaction_type == "SELL":
                        price = round((curr_ltp - 1.0) * 20) / 20.0
                    else:
                        price = round((curr_ltp + 1.0) * 20) / 20.0

                    price = max(price, 0.05)
                    logger.info(f"[Zerodha] Options (Legacy Lib): Using LIMIT {price:.2f} (LTP: {curr_ltp:.2f})")

            # Zerodha AMO timings: typically after 3:45 PM and before 9:00 AM
            is_amo_requested = os.environ.get('ZERODHA_USE_AMO', 'false').lower() == 'true'
            if is_amo_requested or now >= datetime.time(15, 30) or now < datetime.time(9, 15):
                variety = self.kite.VARIETY_AMO
                order_type = self.kite.ORDER_TYPE_LIMIT
                if price == 0:
                    ltp = self.get_ltp(symbol, exchange) or 100.0
                    price = round(ltp * 1.05, 1) if transaction_type == "SELL" else round(ltp * 0.95, 1)
                logger.info(f"[Zerodha] AMO detected. Using Variety: AMO, Type: LIMIT, Price: {price}")

            # Construct order parameters
            place_params = {
                'variety': variety,
                'exchange': exchange,
                'tradingsymbol': symbol,
                'transaction_type': zerodha_transaction_type,
                'quantity': quantity,
                'product': zerodha_product,
                'order_type': order_type,
                'price': price
            }
            if supports_mprot:
                # Apply protection if MARKET order (docs specify MARKET and SL-M)
                if order_type == self.kite.ORDER_TYPE_MARKET:
                    place_params['market_protection'] = m_prot

            order_id = self.kite.place_order(**place_params)
            return order_id
        except Exception as e:
            logger.error(f"Error placing order with Zerodha. Symbol: {symbol}, Exchange: {exchange}, Type: {transaction_type}, Qty: {quantity}. API Error: {e}", exc_info=True)
            return None

    def construct_zerodha_symbol(self, contract, signal_expiry_date=None):
        """
        Constructs a Zerodha-compatible trading symbol for a given contract.
        - Monthly format: NIFTY<YY><MON><STRIKE>CE (e.g., NIFTY26MAR23350PE)
        - Weekly format:  NIFTY<YY><M><DD><STRIKE>CE (e.g., NIFTY2633023350PE)
        """
        import datetime

        # Robustness: Handle cases where 'contract' might be a string-serialized dictionary (from state JSON)
        if isinstance(contract, str) or (isinstance(contract, dict) and 'expiry' not in contract):
            # Attempt to resolve the real object via instrument_key or strike info
            ikey = contract if isinstance(contract, str) else contract.get('key')
            contract = self.orchestrator.atm_manager.get_contract_by_instrument_key(ikey)

        if not contract or not hasattr(contract, 'expiry'):
            logger.error(f"[Zerodha] Symbol construction failed: Invalid contract object {type(contract)}")
            return None

        # Dynamic prefix detection
        name_map = {
            "NIFTY 50": "NIFTY",
            "NIFTY BANK": "BANKNIFTY",
            "NIFTY FINANCIAL SERVICES": "FINNIFTY",
            "NIFTY MIDCAP SELECT": "MIDCPNIFTY",
            "MIDCAP": "MIDCPNIFTY",
            "NIFTY MID SELECT": "MIDCPNIFTY",
            "BANKNIFTY": "BANKNIFTY",
            "FINNIFTY": "FINNIFTY",
            "SENSEX": "SENSEX",
            "BANKEX": "BANKEX"
        }

        raw_name = str(getattr(contract, 'name', 'NIFTY') or 'NIFTY').upper()
        instrument_name = name_map.get(raw_name, raw_name)

        expiry = contract.expiry
        strike = int(contract.strike_price)
        option_type = str(getattr(contract, 'instrument_type', 'CE') or 'CE').upper()
        if option_type == "PUT": option_type = "PE"
        if option_type == "CALL": option_type = "CE"

        year_str = expiry.strftime('%y')

        # Normalize to date object
        expiry_date = expiry.date() if isinstance(expiry, datetime.datetime) else expiry

        # Robust Monthly Detection: Compare against the confirmed monthly_expiries list populated by ContractManager.
        # This accurately handles holiday shifts (like March 26, 2026) by looking at actual contract availability.
        is_monthly_expiry = False
        if self.state_manager and self.state_manager.monthly_expiries:
            is_monthly_expiry = (expiry_date in self.state_manager.monthly_expiries)
        else:
            # Fallback only if state_manager list is empty/missing
            next_week = expiry_date + datetime.timedelta(days=7)
            is_monthly_expiry = (next_week.month != expiry_date.month)

        logger.debug(f"construct_zerodha_symbol: name={instrument_name}, expiry={expiry_date}, is_monthly={is_monthly_expiry}")

        if is_monthly_expiry:
            # Monthly format: NIFTY26MAR23350PE
            month_names = {1:"JAN", 2:"FEB", 3:"MAR", 4:"APR", 5:"MAY", 6:"JUN",
                          7:"JUL", 8:"AUG", 9:"SEP", 10:"OCT", 11:"NOV", 12:"DEC"}
            month_str = month_names[expiry_date.month]
            return f"{instrument_name}{year_str}{month_str}{strike}{option_type}"
        else:
            # Weekly format: NIFTY2633023350PE
            month_val = expiry_date.month
            if month_val == 10: month_char = 'O'
            elif month_val == 11: month_char = 'N'
            elif month_val == 12: month_char = 'D'
            else: month_char = str(month_val)

            day_str = expiry_date.strftime('%d')
            return f"{instrument_name}{year_str}{month_char}{day_str}{strike}{option_type}"

    def get_ltp(self, symbol, exchange=None):
        """Gets the Last Traded Price for a symbol."""
        if not self.kite:
            return 0.0

        # Auto-detect exchange if not provided
        if exchange is None:
            exchange = 'BFO' if 'SENSEX' in symbol.upper() else 'NFO'

        try:
            # Format for LTP call: "EXCHANGE:TRADINGSYMBOL"
            instrument = f"{exchange}:{symbol}"
            quote = self.kite.ltp(instrument)
            return quote[instrument]['last_price']
        except Exception as e:
            logger.error(f"Error fetching LTP from Zerodha for {symbol} ({exchange}): {e}")
            return 0.0

    async def close_all_positions(self):
        """
        Squares off all open positions in Zerodha.
        Industrial standard safety measure.
        """
        if not self.kite or self.paper_trade:
            return

        try:
            positions = await asyncio.to_thread(self.kite.positions)
            net_positions = positions.get("net", [])

            close_tasks = []
            for pos in net_positions:
                qty = pos.get("quantity", 0)
                if qty == 0: continue

                # Opposite transaction
                trans_type = self.kite.TRANSACTION_TYPE_SELL if qty > 0 else self.kite.TRANSACTION_TYPE_BUY
                abs_qty = abs(qty)
                symbol = pos.get("tradingsymbol")
                exchange = pos.get("exchange")
                product = pos.get("product")

                logger.info(f"[{self.instance_name}] Squaring off {symbol} ({qty}) via {trans_type}")

                # Create task for parallel execution
                close_tasks.append(asyncio.to_thread(
                    self.kite.place_order,
                    variety=self.kite.VARIETY_REGULAR,
                    exchange=exchange,
                    tradingsymbol=symbol,
                    transaction_type=trans_type,
                    quantity=abs_qty,
                    order_type=self.kite.ORDER_TYPE_MARKET,
                    product=product
                ))

            if close_tasks:
                results = await asyncio.gather(*close_tasks, return_exceptions=True)
                for res in results:
                    if isinstance(res, Exception):
                        logger.error(f"[{self.instance_name}] Close position error: {res}")

            logger.info(f"[{self.instance_name}] All open positions squared off.")
        except Exception as e:
            logger.error(f"[{self.instance_name}] Error in close_all_positions: {e}")

    async def get_funds(self):
        """Returns available funds from Zerodha margins API."""
        if not self.kite: return 0.0
        try:
            margins = await asyncio.to_thread(self.kite.margins)
            # Zerodha margins contains 'equity' and 'commodity'
            # We typically care about 'equity' -> 'available' -> 'cash'
            equity = margins.get('equity', {})
            return float(equity.get('available', {}).get('cash', 0.0))
        except Exception as e:
            logger.error(f"[{self.instance_name}] Failed to get funds: {e}")
            return 0.0

    async def get_positions(self):
        """Returns live positions from Zerodha."""
        if not self.kite: return []
        try:
            raw = await asyncio.to_thread(self.kite.positions)
            net = raw.get('net', [])
            positions = []
            for p in net:
                if p.get('quantity', 0) == 0: continue
                positions.append({
                    'symbol': p['tradingsymbol'],
                    'quantity': p['quantity'],
                    'average_price': p['average_price'],
                    'ltp': p['last_price'],
                    'pnl': p['pnl'],
                    'm2m': p['m2m']
                })
            return positions
        except Exception as e:
            logger.error(f"[{self.instance_name}] Failed to get positions: {e}")
            return []
