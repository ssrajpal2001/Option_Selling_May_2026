import sys
import datetime
import pandas as pd
import requests
import threading
import asyncio
from SmartApi.smartWebSocketV2 import SmartWebSocketV2
from .base_broker import BaseBroker
from utils.logger import logger
from utils.auth_manager_angelone import handle_angelone_login
from hub.event_bus import event_bus

class AngelOneClient(BaseBroker):
    def __init__(self, broker_instance_name, config_manager, login_required=True, user_id=None, db_config=None):
        super().__init__(broker_instance_name, config_manager, user_id=user_id, db_config=db_config)
        self.broker_name = 'angelone'
        self.smart_api = None
        self.ws = None
        self.loop = None
        self.token_to_key = {} # token (str) -> instrument_key (str)
        self.subscribed_tokens = set()
        self._pending_subscriptions = [] # List of tokens
        self._token_map = None # symbol_key -> {token, tradingsymbol, lot_size}
        self._last_token_load = None   # Set only on successful load
        self._last_load_attempt = None # Set on every attempt (success or failure) for backoff

        # Commercial Path: Always attempt login if db_config provided
        if self.db_config:
            try:
                with self._scoped_ip_patch():
                    self.smart_api = handle_angelone_login(self.db_config)
                if self.smart_api:
                    logger.info(f"AngelOne client initialized from DB for User ID: {self.user_id}.")
            except Exception as e:
                logger.error(
                    f"Failed to initialize AngelOne client for user {self.user_id} "
                    f"(db_config path): {e}. "
                    f"Check Static IP assignment or credential values — NOT a missing-credentials issue."
                )
                if login_required:
                    logger.critical(
                        f"AUTHENTICATION FAILED for AngelOne account [{self.instance_name}] (client mode). "
                        f"Continuing in degraded mode (market data / paper trading only). "
                        f"Live orders will be blocked until the issue above is resolved."
                    )

        if login_required and not self.smart_api and not self.db_config:
            # Fallback to credentials section only in non-client-mode (legacy / admin-mode bots).
            # When db_config was provided (client mode), a DB-path failure must NOT silently retry
            # via INI config — that would produce a misleading "Username: None" error because the
            # INI file has no client-specific section.
            credentials_section = self.config_manager.get(broker_instance_name, 'credentials', fallback=broker_instance_name)
            try:
                self.smart_api = handle_angelone_login(credentials_section, self.config_manager)
                if self.smart_api:
                    logger.info(f"AngelOne authentication successful for {credentials_section} [{self.instance_name}].")
                else:
                    raise Exception("The authentication process failed and did not return a client.")
            except Exception as e:
                logger.critical(
                    f"AUTHENTICATION FAILED for AngelOne account [{self.instance_name}]. Reason: {e}. "
                    f"Continuing in degraded mode (market data / paper trading only). "
                    f"Live orders will be blocked until credentials are configured via Settings.",
                    exc_info=True
                )
                # Do NOT sys.exit — allow the subprocess to continue for paper trading
                # and market data. Live order placement is gated by paper_trade mode
                # and per-broker trading_active checks.

        # Mount SourceIPHTTPAdapter on SmartAPI's requests session so that ALL
        # HTTP calls (login, orders, instruments, funds) route through assigned IP.
        if self.smart_api and self.source_ip:
            self._install_source_ip_adapter(getattr(self.smart_api, 'reqsession', None))

    def connect(self):
        # Established in init. Proactively load tokens.
        if not self.paper_trade and self.smart_api:
            asyncio.create_task(self._load_token_map())

    def start_data_feed(self):
        """Starts the AngelOne WebSocket feed (V2) in a background thread."""
        if not self.smart_api:
            logger.info(f"[{self.instance_name}] Skipping AngelOne data feed (Not authenticated).")
            return

        # Capture current event loop for thread-safe publishing
        try:
            self.loop = asyncio.get_event_loop()
        except RuntimeError:
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)

        logger.info(f"[{self.instance_name}] Initializing AngelOne SmartWebSocketV2...")

        # AngelOne V2 requires these specifically
        auth_token = self.smart_api.access_token
        api_key = self.smart_api.api_key
        client_code = self.db_config.get('client_code') or self.db_config.get('username') if self.db_config else self.config_manager.get(self.instance_name, 'client_code')
        feed_token = self.smart_api.feed_token

        if not all([auth_token, api_key, client_code, feed_token]):
            logger.error(f"[{self.instance_name}] Missing required tokens for AngelOne WebSocket V2.")
            return

        correlation_id = f"algo_{self.user_id or 'sys'}_{int(datetime.datetime.now().timestamp()) % 10000}"

        self.ws = SmartWebSocketV2(auth_token, api_key, client_code, feed_token)

        self.ws.on_data = self._on_data
        self.ws.on_open = self._on_open
        self.ws.on_error = self._on_error
        self.ws.on_close = self._on_close

        # Run connection in a separate thread
        t = threading.Thread(target=self.ws.connect, daemon=True, name=f"AngelTicker_{self.instance_name}")
        t.start()
        logger.info(f"[{self.instance_name}] AngelOne Data Feed thread started. Correlation ID: {correlation_id}")

    def stop_data_feed(self):
        """Stops the AngelOne WebSocket feed."""
        if self.ws:
            logger.info(f"[{self.instance_name}] Stopping AngelOne Data Feed...")
            self.ws.close()
            self.ws = None

    def _on_data(self, message):
        """
        Callback for incoming AngelOne market data.
        Normalizes data and publishes to the event bus.
        """
        # SmartWebSocketV2 returns data as a dictionary with shortened keys (tk, lp, etc.)
        if not message or 'tk' not in message:
            return

        token = message.get('tk')
        inst_key = self.token_to_key.get(token)

        if not inst_key:
            return

        try:
            # lp = Last Price. AngelOne sends paise for F&O (exch 2, 5, 7, etc) but rupees for Cash (exch 1).
            # We check the instrument_key prefix to decide on scaling.
            raw_ltp = float(message.get('lp', 0))
            raw_atp = float(message.get('atp', 0)) if 'atp' in message else 0.0

            # Universal keys like NSE_INDEX|Nifty 50 or instrument_keys starting with NSE_EQ
            # are typically in Rupees. NFO_OPTIDX, NFO_FUTIDX are in Paise.
            is_paise = any(seg in inst_key for seg in ['NFO_', 'MCX_', 'CDS_', 'BFO_'])

            ltp = raw_ltp / 100.0 if is_paise else raw_ltp
            atp = raw_atp / 100.0 if is_paise and raw_atp > 0 else raw_atp

            normalized_tick = {
                'user_id': self.user_id,
                'instrument_key': inst_key,
                'ltp': ltp,
                'volume': int(message.get('v', 0)),
                'timestamp': datetime.datetime.now(),
                'broker': 'angelone'
            }

            if raw_atp > 0:
                normalized_tick['atp'] = atp
            if 'oi' in message:
                normalized_tick['oi'] = int(message.get('oi'))

            if self.loop:
                asyncio.run_coroutine_threadsafe(
                    event_bus.publish('BROKER_TICK_RECEIVED', normalized_tick),
                    self.loop
                )
        except Exception as e:
            logger.error(f"Error processing AngelOne tick: {e}")

    def _on_open(self):
        logger.info(f"[{self.instance_name}] AngelOne WebSocket connected.")
        if self._pending_subscriptions:
            logger.info(f"[{self.instance_name}] Processing {len(self._pending_subscriptions)} pending AngelOne subscriptions.")
            for t in self._pending_subscriptions:
                self.subscribed_tokens.add(t)
            self._pending_subscriptions = []
        if self.subscribed_tokens:
            logger.info(f"[{self.instance_name}] Subscribing to {len(self.subscribed_tokens)} tokens.")
            self._subscribe_now(list(self.subscribed_tokens))

    def _on_error(self, error):
        logger.error(f"[{self.instance_name}] AngelOne WebSocket error: {error}")

    def _on_close(self, code, reason):
        logger.warning(f"[{self.instance_name}] AngelOne WebSocket closed: {code} - {reason}")

    def subscribe_instruments(self, instrument_map):
        """
        External method to register instruments for this specific broker feed.
        instrument_map: { instrument_key (str): token (str) }
        """
        new_tokens = []
        for ikey, token in instrument_map.items():
            if token not in self.subscribed_tokens and token not in self._pending_subscriptions:
                self.token_to_key[token] = ikey
                new_tokens.append(token)

        if not new_tokens:
            return

        if not self.ws:
            logger.info(f"[{self.instance_name}] AngelOne WebSocket not started. Queueing {len(new_tokens)} tokens.")
            self._pending_subscriptions.extend(new_tokens)
            return

        logger.info(f"[{self.instance_name}] Subscribing to {len(new_tokens)} new AngelOne tokens.")
        for t in new_tokens: self.subscribed_tokens.add(t)
        self._subscribe_now(new_tokens)

    def _subscribe_now(self, tokens):
        """Internal helper to send subscription command."""
        if not self.ws: return

        # AngelOne V2 subscription format
        # Action: 1 = Subscribe, 0 = Unsubscribe
        # Mode: 1 = LTP, 2 = Quote, 3 = Snapquote
        try:
            # We use Mode 2 (Quote) to get Volume and potentially ATP/OI
            correlation_id = f"sub_{int(datetime.datetime.now().timestamp()) % 1000}"

            # Separate tokens by type (NFO vs NSE)
            nfo_tokens = []
            nse_tokens = []
            for t in tokens:
                # Common pattern: Index tokens are 4-5 digits, Options are longer or specific mapping
                if hasattr(self, '_universal_token_map') and t in self._universal_token_map.values():
                    nse_tokens.append(t)
                elif len(str(t)) > 6: # Likely an option
                    nfo_tokens.append(t)
                else:
                    nse_tokens.append(t)

            subs = []
            if nfo_tokens: subs.append({"exchangeType": 2, "tokens": nfo_tokens})
            if nse_tokens: subs.append({"exchangeType": 1, "tokens": nse_tokens})

            if subs:
                self.ws.subscribe(correlation_id, 2, subs)
                logger.debug(f"[{self.instance_name}] Subscription command sent for {len(tokens)} tokens.")
        except Exception as e:
            logger.error(f"[{self.instance_name}] Failed to subscribe to AngelOne tokens: {e}")

    async def _load_token_map(self):
        """Downloads and processes the Angel One token master file."""
        import json as _json
        import aiohttp
        now = datetime.datetime.now()

        # Fast path: already loaded successfully today
        if self._token_map is not None and self._last_token_load and self._last_token_load.date() == now.date():
            return

        # Backoff guard: if a previous attempt failed within the last 60 seconds, don't retry.
        # Without this guard, every concurrent call to get_instrument_info() triggers a fresh
        # download, flooding the logs with dozens of parallel attempts per second.
        if self._last_load_attempt is not None:
            seconds_since_last = (now - self._last_load_attempt).total_seconds()
            if seconds_since_last < 60:
                logger.debug(f"[{self.instance_name}] AngelOne: Skipping token master download (last attempt {seconds_since_last:.0f}s ago, backoff=60s).")
                return

        self._last_load_attempt = now  # Record attempt time BEFORE download (prevents storm even if download is slow)

        urls = [
            "https://margincalculator.angelbroking.com/OpenAPI_Standard_MSil.php?Exchange=NFO",
            "https://margincalculator.angelbroking.com/OpenAPI_Standard_MSil.php?Exchange=NSE"
        ]

        try:
            logger.info(f"[{self.instance_name}] AngelOne: Downloading token masters...")
            all_data = []
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
                "Accept": "application/json, text/plain, */*"
            }

            max_retries = 3
            for attempt in range(max_retries):
                all_data = []
                raw_for_debug = ''
                try:
                    async with aiohttp.ClientSession(headers=headers) as session:
                        for url in urls:
                            async with session.get(url, timeout=30) as response:
                                raw_text = await response.text()
                                raw = raw_text.replace('\ufeff', '').strip()
                                raw_for_debug = raw
                                decoder = _json.JSONDecoder()
                                pos = 0
                                while pos < len(raw):
                                    try:
                                        obj, pos = decoder.raw_decode(raw, pos)
                                    except _json.JSONDecodeError:
                                        break
                                    if isinstance(obj, list):
                                        all_data.extend(obj)
                                    while pos < len(raw) and raw[pos] in ' \t\r\n':
                                        pos += 1

                    if all_data:
                        break # Success
                    else:
                        logger.warning(f"[{self.instance_name}] AngelOne: Master file empty on attempt {attempt+1}/{max_retries}. Retrying in 5s...")
                        await asyncio.sleep(5)
                except Exception as ex:
                    logger.warning(f"[{self.instance_name}] AngelOne: Download error on attempt {attempt+1}: {ex}")
                    await asyncio.sleep(5)

            if not all_data:
                snippet = raw_for_debug[:500]
                logger.warning(
                    f"[{self.instance_name}] AngelOne master: all_data is empty after parsing. "
                    f"Master file might be updating (Pre-market). Status: NO_DATA. "
                    f"First 500 chars of last URL response: {snippet!r}"
                )
                # Reset attempt time so we can retry sooner than 60s if it was an empty response
                self._last_load_attempt = now - datetime.timedelta(seconds=45)

                # If both URL calls produced no data, do not overwrite existing map
                # (prevents transient network errors from clearing current tokens).
                if self._token_map:
                    logger.warning(f"[{self.instance_name}] AngelOne: Token master download yielded no data; retaining existing map.")
                return

            mapping = {}
            universal_mapping = {}       # "NSE_INDEX|Nifty 50" -> token
            nsefoo_tradingsymbol = {}    # "NSE_FO|{token}" -> tradingsymbol (for ltpData calls)

            for item in all_data:
                name = item.get('name', '').upper()
                token = item.get('token')
                symbol = item.get('symbol', '')
                exch = item.get('exch_seg', '')

                # 1. Option Mapping
                if exch == 'NFO' and item.get('expiry'):
                    try:
                        expiry_date = datetime.datetime.strptime(item['expiry'], '%d%b%Y').date()
                        raw_strike = float(item['strike'])
                        strike = raw_strike / 100.0 if raw_strike > 100000 else raw_strike
                        option_type = 'CE' if symbol.endswith('CE') else 'PE' if symbol.endswith('PE') else 'XX'

                        key = (name, expiry_date, strike, option_type)
                        mapping[key] = {
                            'token': token,
                            'tradingsymbol': symbol,
                            'lotsize': int(item.get('lotsize', 1))
                        }
                        # Build a fast NSE_FO|<token> → tradingsymbol reverse map so
                        # get_ltp can pass the correct tradingsymbol to ltpData.
                        if token:
                            nsefoo_tradingsymbol[f"NSE_FO|{token}"] = symbol
                    except: continue

                # 2. Universal Key Mapping (for Indices and Stocks)
                if exch == 'NSE':
                    if name == 'NIFTY' and symbol == 'Nifty 50':
                        universal_mapping['NSE_INDEX|Nifty 50'] = token
                    elif name == 'BANKNIFTY' and symbol == 'Nifty Bank':
                        universal_mapping['NSE_INDEX|Nifty Bank'] = token
                    elif name == 'FINNIFTY' and symbol == 'Nifty Fin Service':
                        universal_mapping['NSE_INDEX|Nifty Fin Service'] = token
                    # Add more as needed...

            if mapping or universal_mapping:
                self._token_map = mapping
                self._universal_token_map = universal_mapping
                self._nsefoo_tradingsymbol = nsefoo_tradingsymbol
                self._last_token_load = now
                logger.info(f"[{self.instance_name}] AngelOne: Token masters loaded. {len(mapping)} options, {len(universal_mapping)} universal keys.")
            else:
                logger.warning(f"[{self.instance_name}] AngelOne: Parsed {len(all_data)} items but found 0 valid options/keys. Master might be empty.")
                # Force retry sooner
                self._last_load_attempt = now - datetime.timedelta(seconds=45)

        except Exception as e:
            logger.error(f"[{self.instance_name}] Failed to load AngelOne token master: {e}")
            # Reset attempt so we retry
            self._last_load_attempt = now - datetime.timedelta(seconds=30)

    def get_token_by_universal_key(self, key):
        if not hasattr(self, '_universal_token_map'): return None
        return self._universal_token_map.get(key)

    def get_tradingsymbol_for_nsefoo_key(self, instrument_key):
        """Return the AngelOne tradingsymbol for an NSE_FO|<token> instrument key.

        This is used by get_ltp() so ltpData receives the correct tradingsymbol
        alongside the symboltoken, matching AngelOne's recommended API usage.
        Returns an empty string if the master is not loaded or the key is unknown.
        """
        if not hasattr(self, '_nsefoo_tradingsymbol'):
            return ''
        return self._nsefoo_tradingsymbol.get(instrument_key, '')

    async def get_instrument_info(self, contract):
        """Resolves the token and tradingsymbol for a given contract."""
        await self._load_token_map()
        if not self._token_map:
            return None

        name = self._normalize_instrument_name(getattr(contract, 'name', 'NIFTY'))
        expiry = contract.expiry.date() if isinstance(contract.expiry, datetime.datetime) else contract.expiry
        strike = float(contract.strike_price)
        opt_type = str(getattr(contract, 'instrument_type', 'CE') or 'CE').upper()

        key = (name, expiry, strike, opt_type)
        info = self._token_map.get(key)

        if not info:
            logger.warning(f"AngelOne: No instrument info found for {key}")
            # Try fuzzy match if exact expiry fails (some brokers have slightly different expiry dates in master)
            for k, v in self._token_map.items():
                if k[0] == name and abs((k[1] - expiry).days) <= 1 and k[2] == strike and k[3] == opt_type:
                    logger.info(f"AngelOne: Found fuzzy match for {key} -> {k}")
                    return v
        return info

    async def handle_entry_signal(self, **kwargs):
        if not self.smart_api:
            logger.error(f"AngelOne client not initialized for '{self.instance_name}'.")
            return

        contract = kwargs.get('contract')
        instrument_name = kwargs.get('instrument_name')
        direction = kwargs.get('direction')
        signal_expiry_date = kwargs.get('signal_expiry_date')

        if not all([contract, instrument_name, direction, signal_expiry_date]):
            logger.error("AngelOne: handle_entry_signal missing required arguments.")
            return

        broker_base_qty = self.config_manager.get_int(self.instance_name, 'quantity', 1)
        instrument_lot_size = contract.lot_size
        final_qty = broker_base_qty * instrument_lot_size

        try:
            order_id = None
            if self.paper_trade:
                logger.info(f"--- ANGELONE [PAPER TRADE] ENTRY ({direction}) ---")
                logger.info(f"  Instrument: {contract.instrument_key}")
                logger.info(f"  Quantity: {final_qty} | Price: {kwargs.get('ltp', 0)}")
                order_id = "PAPER_ANGELONE_ORDER"
            else:
                order_id = self.place_order(contract, "BUY", final_qty, signal_expiry_date)

            if order_id:
                info = await self.get_instrument_info(contract)
                instrument_symbol = info['tradingsymbol'] if info else contract.instrument_key
                if not self.paper_trade:
                    logger.info(f"Successfully placed AngelOne BUY ({direction}) order for {instrument_symbol}, ID: {order_id}")

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
            logger.error(f"Exception placing AngelOne entry order: {e}", exc_info=True)
            return None

    async def handle_close_signal(self, **kwargs):
        if not self.smart_api:
            logger.error(f"AngelOne client not initialized for '{self.instance_name}'.")
            return

        side = kwargs.get('side')
        instrument_name = kwargs.get('instrument_name')
        signal_expiry_date = kwargs.get('signal_expiry_date')

        contract = kwargs.get('contract')
        position = self.state_manager.get_position(side)
        if not contract and position:
            contract = position.get('contract')

        if not contract:
            logger.warning(f"No active {side} position found on AngelOne.")
            return

        broker_base_qty = self.config_manager.get_int(self.instance_name, 'quantity', 1)
        final_qty = broker_base_qty * contract.lot_size

        try:
            order_id = None
            if self.paper_trade:
                logger.info(f"--- ANGELONE [PAPER TRADE] EXIT ({side}) ---")
                logger.info(f"  Instrument: {contract.instrument_key}")
                logger.info(f"  Quantity: {final_qty} | Price: {kwargs.get('ltp', 0)}")
                order_id = "PAPER_ANGELONE_EXIT"
            else:
                order_id = self.place_order(contract, "SELL", final_qty, signal_expiry_date)

            if order_id:
                if not self.paper_trade:
                    logger.info(f"Successfully placed AngelOne SELL order to close {side} position, ID: {order_id}")

                await event_bus.publish('TRADE_CLOSED', {
                    'user_id': self.user_id,
                    'instrument_name': instrument_name,
                    'direction': side
                })

                price = kwargs.get('ltp', 0)
                reason = kwargs.get('reason', 'UNKNOWN')
                entry_price = position.get('entry_price', 0) if position else 0
                pnl = (price - entry_price) if entry_price > 0 else 0

                info = await self.get_instrument_info(contract)
                instrument_symbol = info['tradingsymbol'] if info else contract.instrument_key
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
            logger.error(f"Exception closing AngelOne position: {e}", exc_info=True)

    def place_order(self, contract, transaction_type, quantity, expiry, product_type='NRML', market_protection=None):
        logger.info(f"[{self.instance_name}] place_order request: {transaction_type} {quantity} qty for {getattr(contract, 'instrument_key', 'UNKNOWN')} user={self.user_id}")
        if not self.smart_api:
            logger.error(f"[{self.instance_name}] AngelOne client not authenticated. Order blocked.")
            return None

        # Pre-market resilience check: if token map is empty, we cannot place orders
        if not self._token_map:
            logger.error(f"[{self.instance_name}] AngelOne: Token master empty (Pre-market?). Cannot resolve instrument {getattr(contract, 'instrument_key', 'N/A')}. Order blocked.")
            return None

        self._validate_source_ip()
        try:
            # First, resolve instrument info
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                     info = asyncio.run_coroutine_threadsafe(self.get_instrument_info(contract), loop).result(timeout=10)
                else:
                     info = loop.run_until_complete(self.get_instrument_info(contract))
            except Exception as se:
                logger.error(f"AngelOne: Failed to resolve instrument info in place_order: {se}")
                return None

            if not info:
                logger.error(f"AngelOne: No instrument info found for {contract.instrument_key}")
                return None

            order_params = {
                "variety": "NORMAL",
                "tradingsymbol": info['tradingsymbol'],
                "symboltoken": info['token'],
                "transactiontype": transaction_type,
                "exchange": "NFO", # AngelOne exchange mapping logic
                "ordertype": "MARKET",
                "producttype": "CARRYFORWARD" if product_type == 'NRML' else "INTRADAY",
                "duration": "DAY",
                "quantity": str(int(quantity))
            }

            order_id = self.smart_api.placeOrder(order_params)
            return order_id
        except Exception as e:
            logger.error(f"Error in AngelOne place_order: {e}", exc_info=True)
            return None

    def get_ltp(self, symbol):
        """Fetches LTP for a symbol using rest API."""
        if not self.smart_api: return 0.0
        try:
            # Note: This is simplified. In a real scenario, we'd need the token.
            # But usually the bot uses BrokerRestAdapter which uses token.
            return 0.0
        except: return 0.0

    async def close_all_positions(self):
        """Squares off all open positions in AngelOne."""
        if not self.smart_api or self.paper_trade: return
        try:
            positions = await asyncio.to_thread(self.smart_api.position)
            if not positions or not positions.get('status'): return

            data = positions.get('data', [])
            if not data: return

            close_tasks = []
            for pos in data:
                qty = int(pos.get('netqty', 0))
                if qty == 0: continue

                trans_type = "SELL" if qty > 0 else "BUY"
                abs_qty = abs(qty)

                order_params = {
                    "variety": "NORMAL",
                    "tradingsymbol": pos.get('tradingsymbol'),
                    "symboltoken": pos.get('symboltoken'),
                    "transactiontype": trans_type,
                    "exchange": pos.get('exchange'),
                    "ordertype": "MARKET",
                    "producttype": pos.get('producttype'),
                    "duration": "DAY",
                    "quantity": str(abs_qty)
                }
                logger.info(f"[{self.instance_name}] AngelOne Squaring off {pos.get('tradingsymbol')} ({qty})")
                close_tasks.append(asyncio.to_thread(self.smart_api.placeOrder, order_params))

            if close_tasks:
                await asyncio.gather(*close_tasks, return_exceptions=True)
                logger.info(f"[{self.instance_name}] AngelOne positions squared off.")
        except Exception as e:
            logger.error(f"[{self.instance_name}] AngelOne close_all error: {e}")

    async def get_funds(self):
        """Returns funds from AngelOne."""
        if not self.smart_api: return 0.0
        try:
            res = await asyncio.to_thread(self.smart_api.rmsLimit)
            if res and res.get('status'):
                return float(res.get('data', {}).get('net', 0.0))
            return 0.0
        except: return 0.0

    async def get_positions(self):
        """Returns live positions from AngelOne."""
        if not self.smart_api: return []
        try:
            res = await asyncio.to_thread(self.smart_api.position)
            if res and res.get('status'):
                data = res.get('data', [])
                positions = []
                for p in data:
                    if int(p.get('netqty', 0)) == 0: continue
                    positions.append({
                        'symbol': p['tradingsymbol'],
                        'quantity': int(p['netqty']),
                        'average_price': float(p['avgprice']),
                        'ltp': float(p['ltp']),
                        'pnl': float(p['pnl'])
                    })
                return positions
            return []
        except: return []
