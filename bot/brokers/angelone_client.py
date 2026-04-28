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
        self._last_token_load = None

        # Commercial Path: Always attempt login if db_config provided
        if self.db_config:
            try:
                with self._scoped_ip_patch():
                    self.smart_api = handle_angelone_login(self.db_config)
                if self.smart_api:
                    logger.info(f"AngelOne client initialized from DB for User ID: {self.user_id}.")
            except Exception as e:
                logger.error(f"Failed to initialize AngelOne client for user {self.user_id}: {e}")

        if login_required and not self.smart_api:
            # Fallback to credentials section if not initialized from DB
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
        now = datetime.datetime.now()
        if self._token_map is not None and self._last_token_load and self._last_token_load.date() == now.date():
            return

        urls = [
            "https://margincalculator.angelbroking.com/OpenAPI_Standard_MSil.php?Exchange=NFO",
            "https://margincalculator.angelbroking.com/OpenAPI_Standard_MSil.php?Exchange=NSE"
        ]

        try:
            logger.info(f"[{self.instance_name}] AngelOne: Downloading token masters...")
            import aiohttp
            all_data = []
            async with aiohttp.ClientSession() as session:
                for url in urls:
                    async with session.get(url, timeout=30) as response:
                        all_data.extend(await response.json())

            mapping = {}
            universal_mapping = {} # "NSE_INDEX|Nifty 50" -> token

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

            self._token_map = mapping
            self._universal_token_map = universal_mapping
            self._last_token_load = now
            logger.info(f"[{self.instance_name}] AngelOne: Token masters loaded. {len(mapping)} options, {len(universal_mapping)} universal keys.")
        except Exception as e:
            logger.error(f"[{self.instance_name}] Failed to load AngelOne token master: {e}")

    def get_token_by_universal_key(self, key):
        if not hasattr(self, '_universal_token_map'): return None
        return self._universal_token_map.get(key)

    async def get_instrument_info(self, contract):
        """Resolves the token and tradingsymbol for a given contract."""
        await self._load_token_map()
        if not self._token_map:
            return None

        name = str(getattr(contract, 'name', 'NIFTY') or 'NIFTY').upper()
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
        if not self.smart_api: return None
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
