import asyncio
import datetime
from .base_broker import BaseBroker
from utils.logger import logger
from utils.rest_api_client import RestApiClient
from utils.websocket_manager import WebSocketManager
from hub.event_bus import event_bus

class UpstoxClient(BaseBroker):
    def __init__(self, broker_instance_name, config_manager, login_required=True, user_id=None, db_config=None):
        super().__init__(broker_instance_name, config_manager, user_id=user_id, db_config=db_config)
        self.broker_name = 'upstox'
        self.api_client = None
        self.ws_manager = None
        self.loop = None
        self._access_token = None  # Stored for order placement via SDK

        if login_required:
            if self.db_config:
                # Multi-tenant DB path
                try:
                    # For Upstox, we need an AuthHandler-like object to satisfy RestApiClient
                    class SimpleAuth:
                        def __init__(self, token, cm):
                            self.token = token
                            self.config_manager = cm
                        def get_access_token(self):
                            return self.token
                        def switch_client(self):
                            return False

                    access_token = None
                    # 1. Automated TOTP Login — skipped if a valid today's token exists in DB.
                    # This prevents burning a new TOTP token on every intraday bot restart,
                    # which would hit Upstox OTP rate limits and break the WebSocket connection.
                    if self.db_config.get('password') and self.db_config.get('totp'):
                        from utils.auth_manager_upstox import (
                            handle_upstox_login_automated, is_token_valid_today
                        )
                        if is_token_valid_today(self.db_config):
                            # Reuse the token already stored in db_config (loaded from DB)
                            access_token = (
                                self.db_config.get('access_token') or
                                self.db_config.get('api_secret')
                            )
                        else:
                            access_token = handle_upstox_login_automated(self.db_config)

                        # Persist the fresh token to the DB immediately so it survives even
                        # if RestApiClient construction fails below (e.g. static IP not bound).
                        # The WebSocket and any subsequent startup will then find a valid token.
                        if access_token:
                            try:
                                from web.db import db_execute
                                from web.auth import encrypt_secret
                                from datetime import datetime, timezone, timedelta
                                enc = encrypt_secret(access_token)
                                # Use IST timezone (same as Dhan) for consistency in timezone comparisons
                                _tz_ist = timezone(timedelta(hours=5, minutes=30))
                                now_ist = datetime.now(_tz_ist).isoformat()
                                _inst_id = self.db_config.get('id')
                                if _inst_id:
                                    db_execute(
                                        "UPDATE client_broker_instances "
                                        "SET access_token_encrypted=?, token_updated_at=? WHERE id=?",
                                        (enc, now_ist, _inst_id)
                                    )
                                    logger.info(f"[UpstoxClient] Fresh token saved to DB for user {self.user_id}.")
                                # Also update data_providers so the global FeedServer gets the fresh token
                                db_execute(
                                    "UPDATE data_providers SET access_token_encrypted=?, token_issued_at=? "
                                    "WHERE provider='upstox'",
                                    (enc, now_ist)
                                )
                                logger.info(f"[UpstoxClient] Fresh token propagated to data_providers.")
                            except Exception as _db_err:
                                logger.warning(f"[UpstoxClient] Could not persist fresh token to DB: {_db_err}")

                    # 2. Token Fallback — use stored token if automated login was skipped/failed
                    if not access_token:
                        access_token = self.db_config.get('access_token') or self.db_config.get('api_secret')

                    if access_token:
                        self._access_token = access_token
                        auth = SimpleAuth(access_token, self.config_manager)
                        # Pass source_ip so RestApiClient creates aiohttp session with
                        # TCPConnector(local_addr=...) — covers ALL async HTTP calls.
                        self.api_client = RestApiClient(auth, source_ip=self.source_ip)
                        logger.info(f"Upstox client initialized for User ID: {self.user_id}.")
                    else:
                        logger.error(f"Upstox: Missing credentials in DB config for user {self.user_id}.")
                except Exception as e:
                    logger.error(f"Failed to initialize Upstox client for user {self.user_id}: {e}")
            else:
                # Legacy INI path handled by ApiClientManager (usually)
                pass

    def connect(self):
        # Already connected if api_client is set
        pass

    def start_data_feed(self):
        """Starts the Upstox WebSocket feed."""
        if not self.api_client:
            logger.info(f"[{self.instance_name}] Skipping Upstox data feed (Not authenticated).")
            return

        try:
            self.loop = asyncio.get_event_loop()
        except RuntimeError:
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)

        logger.info(f"[{self.instance_name}] Initializing Upstox WebSocket Feed...")
        self.ws_manager = WebSocketManager(api_client=self.api_client)
        self.ws_manager.register_message_handler(self._handle_raw_message)
        self.ws_manager.start()

    def stop_data_feed(self):
        """Stops the Upstox WebSocket feed."""
        if self.ws_manager:
            logger.info(f"[{self.instance_name}] Stopping Upstox Data Feed...")
            asyncio.create_task(self.ws_manager.close())
            self.ws_manager = None

    async def _handle_raw_message(self, feed_response):
        """Standard Upstox Protobuf handler, normalized for multi-tenant bus."""
        if not feed_response or not feed_response.feeds:
            return

        for key, feed in feed_response.feeds.items():
            ltp = None
            if feed.HasField('ltpc'):
                ltp = feed.ltpc.ltp
            elif feed.HasField('fullFeed'):
                if feed.fullFeed.HasField('marketFF'):
                    ltp = feed.fullFeed.marketFF.ltpc.ltp
                elif feed.fullFeed.HasField('indexFF'):
                    ltp = feed.fullFeed.indexFF.ltpc.ltp

            if ltp is not None:
                normalized_tick = {
                    'user_id': self.user_id,
                    'instrument_key': key,
                    'ltp': ltp,
                    'broker': 'upstox',
                    'timestamp': datetime.datetime.now()
                }

                # Full Quotes for strategy V3
                if feed.HasField('fullFeed') and feed.fullFeed.HasField('marketFF'):
                    normalized_tick['atp'] = feed.fullFeed.marketFF.atp
                    normalized_tick['volume'] = feed.fullFeed.marketFF.vtt
                    normalized_tick['oi'] = feed.fullFeed.marketFF.oi

                await event_bus.publish('BROKER_TICK_RECEIVED', normalized_tick)

    def subscribe_instruments(self, instrument_list):
        """External method for SubscriptionManager."""
        if self.ws_manager:
            # instrument_list is actually a list of keys for Upstox
            keys = instrument_list if isinstance(instrument_list, list) else list(instrument_list.keys())
            self.ws_manager.subscribe(keys)

    async def handle_entry_signal(self, **kwargs):
        if self.paper_trade:
            return await self._handle_paper_entry(**kwargs)

        contract = kwargs.get('contract')
        instrument_name = kwargs.get('instrument_name')
        direction = kwargs.get('direction')        # CALL / PUT (strategy direction)
        entry_type = kwargs.get('entry_type', 'BUY')  # BUY / SELL (actual order side)
        signal_expiry_date = kwargs.get('signal_expiry_date')

        if not all([contract, instrument_name, direction, signal_expiry_date]):
            logger.error(f"[UpstoxClient] handle_entry_signal missing required args. Data: {kwargs}")
            return None

        broker_base_qty = self.config_manager.get_int(self.instance_name, 'quantity', 1)
        instrument_lot_size = getattr(contract, 'lot_size', 1) or 1
        quantity_multiplier = int(kwargs.get('quantity_multiplier', 1))
        final_qty = broker_base_qty * instrument_lot_size * quantity_multiplier
        transaction_type = "BUY" if entry_type == 'BUY' else "SELL"

        try:
            product_type = kwargs.get('product_type', 'NRML')
            order_id = self.place_order(contract, transaction_type, final_qty,
                                        expiry=signal_expiry_date, product_type=product_type)
            if order_id:
                logger.info(f"[UpstoxClient] Placed {transaction_type} ({direction}) order={order_id} qty={final_qty} user={self.user_id}")
                await event_bus.publish('TRADE_CONFIRMED', {
                    'user_id': self.user_id,
                    'instrument_name': instrument_name,
                    'direction': direction,
                    'trade_contract': contract,
                    'ltp': kwargs.get('ltp', 0),
                })
                return order_id
            else:
                logger.error(f"[UpstoxClient] Failed to place {transaction_type} ({direction}) for {contract.instrument_key} user={self.user_id}")
                return None
        except Exception as e:
            logger.error(f"[UpstoxClient] handle_entry_signal error for user {self.user_id}: {e}", exc_info=True)
            return None

    async def handle_close_signal(self, **kwargs):
        if self.paper_trade:
            return await self._handle_paper_exit(**kwargs)

        side = kwargs.get('side')
        instrument_name = kwargs.get('instrument_name')
        contract = kwargs.get('contract')
        position = self.state_manager.get_position(side) if self.state_manager else None

        if not contract and position:
            contract = position.get('contract')

        if not contract:
            logger.warning(f"[UpstoxClient] No active {side} position/contract to close. user={self.user_id}")
            return None

        broker_base_qty = self.config_manager.get_int(self.instance_name, 'quantity', 1)
        instrument_lot_size = getattr(contract, 'lot_size', 1) or 1
        final_qty = broker_base_qty * instrument_lot_size

        entry_type = position.get('entry_type', 'BUY') if position else 'BUY'
        exit_transaction_type = "SELL" if entry_type == 'BUY' else "BUY"

        try:
            signal_expiry_date = kwargs.get('signal_expiry_date')
            order_id = self.place_order(contract, exit_transaction_type, final_qty, expiry=signal_expiry_date)
            if order_id:
                logger.info(f"[UpstoxClient] Closed {side} position: {exit_transaction_type} qty={final_qty} order={order_id} user={self.user_id}")
            return order_id
        except Exception as e:
            logger.error(f"[UpstoxClient] handle_close_signal error for user {self.user_id}: {e}", exc_info=True)
            return None

    async def _handle_paper_entry(self, **kwargs):
        inst_name = kwargs.get('instrument_name')
        price = kwargs.get('ltp', 0)
        direction = kwargs.get('direction')

        logger.info(f"--- UPSTOX [PAPER] ENTRY ({direction}) for {inst_name} at {price} ---")

        await event_bus.publish('TRADE_CONFIRMED', {
            'user_id': self.user_id,
            'instrument_name': inst_name,
            'direction': direction,
            'trade_contract': kwargs.get('contract'),
            'ltp': price
        })

        self.trade_logger.log_entry(
            broker=self.instance_name,
            instrument_name=inst_name,
            instrument_symbol=kwargs.get('instrument_symbol', inst_name),
            trade_type=direction,
            price=price,
            user_id=self.user_id
        )
        return "PAPER_UPSTOX_ENTRY"

    async def _handle_paper_exit(self, **kwargs):
        inst_name = kwargs.get('instrument_name')
        price = kwargs.get('ltp', 0)
        direction = kwargs.get('side')

        logger.info(f"--- UPSTOX [PAPER] EXIT ({direction}) for {inst_name} at {price} ---")

        await event_bus.publish('TRADE_CLOSED', {
            'user_id': self.user_id,
            'instrument_name': inst_name,
            'direction': direction
        })
        return "PAPER_UPSTOX_EXIT"

    def place_order(self, contract, transaction_type: str, quantity: int, expiry=None,
                    product_type: str = "NRML", market_protection=None):
        """Places a real order via Upstox V2 SDK OrderApi.
        Upstox natively uses instrument_key (e.g. NSE_FO|...) — no symbol construction needed.
        """
        logger.info(f"[{self.instance_name}] place_order request: {transaction_type} {quantity} qty for {getattr(contract, 'instrument_key', 'UNKNOWN')} user={self.user_id}")
        if not self._access_token:
            logger.error(f"[UpstoxClient] No access token available. Cannot place order for user {self.user_id}.")
            return None
        try:
            import upstox_client

            instrument_token = getattr(contract, "instrument_key", None)
            if not instrument_token:
                logger.error(f"[UpstoxClient] Contract has no instrument_key. Cannot place order for user {self.user_id}.")
                return None

            tx_type = "BUY" if str(transaction_type).upper() == "BUY" else "SELL"
            # Upstox product codes: D = NRML (carry-forward), I = MIS (intraday)
            product = "I" if str(product_type).upper() == "MIS" else "D"

            cfg = upstox_client.Configuration()
            cfg.access_token = self._access_token
            api_client = upstox_client.ApiClient(cfg)
            order_api = upstox_client.OrderApi(api_client)

            req = upstox_client.PlaceOrderRequest(
                quantity=int(quantity),
                product=product,
                validity="DAY",
                price=0,
                tag="algosoft",
                instrument_token=instrument_token,
                order_type="MARKET",
                transaction_type=tx_type,
                disclosed_quantity=0,
                trigger_price=0,
                is_amo=False,
            )

            with self._scoped_ip_patch():
                resp = order_api.place_order(body=req, api_version="2.0")
            order_id = getattr(resp, "data", None)
            order_id = getattr(order_id, "order_id", None) if order_id else None

            if order_id:
                logger.info(f"[UpstoxClient] Order placed: {order_id} | {tx_type} {quantity} x {instrument_token} | user={self.user_id}")
                return order_id
            else:
                logger.error(f"[UpstoxClient] Order placement returned no order_id. Response: {resp}")
                return None
        except Exception as e:
            logger.error(f"[UpstoxClient] place_order error for user {self.user_id}: {e}", exc_info=True)
            return None

    # --- REST API Methods (delegated to api_client) ---

    async def get_ltp(self, instrument_key, silence_error=False):
        if not self.api_client: return 0.0
        return await self.api_client.get_ltp(instrument_key, silence_error=silence_error)

    async def get_ltps(self, instrument_keys, silence_error=False):
        if not self.api_client: return {}
        return await self.api_client.get_ltps(instrument_keys, silence_error=silence_error)

    async def get_historical_candle_data(self, instrument_key, interval, to_date, from_date):
        if not self.api_client: return pd.DataFrame()
        return await self.api_client.get_historical_candle_data(instrument_key, interval, to_date, from_date)

    async def get_option_contracts(self, instrument_key):
        if not self.api_client: return []
        return await self.api_client.get_option_contracts(instrument_key)

    async def get_expiring_option_contracts(self, instrument_key, expiry_date):
        if not self.api_client: return []
        return await self.api_client.get_expiring_option_contracts(instrument_key, expiry_date)

    async def close_all_positions(self):
        """Squares off positions in Upstox."""
        if not self.api_client or self.paper_trade: return
        try:
            positions_res = await self.api_client.get_positions()
            if not positions_res or 'data' not in positions_res: return

            data = positions_res['data']
            tasks = []
            for pos in data:
                qty = int(pos.get('quantity', 0))
                if qty == 0: continue

                # Opposite transaction
                trans_type = "SELL" if qty > 0 else "BUY"
                abs_qty = abs(qty)

                # Build a minimal contract-like object for place_order
                class _Contract:
                    def __init__(self, key): self.instrument_key = key

                logger.info(f"[{self.instance_name}] Squaring off {pos.get('tradingsymbol')} ({qty})")
                tasks.append(asyncio.to_thread(
                    self.place_order,
                    _Contract(pos.get('instrument_token')),
                    trans_type,
                    abs_qty,
                    product_type='MIS' if pos.get('product') == 'I' else 'NRML'
                ))

            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
                logger.info(f"[{self.instance_name}] Upstox positions squared off.")
        except Exception as e:
            logger.error(f"[{self.instance_name}] Upstox close_all error: {e}")

    async def get_funds(self):
        """Returns funds from Upstox."""
        if not self.api_client: return 0.0
        try:
            # Upstox has a dedicated margin endpoint
            funds = await self.api_client.get_user_fund_margin()
            if funds and 'data' in funds:
                equity = funds['data'].get('equity', {})
                return float(equity.get('available_margin', 0.0))
            return 0.0
        except: return 0.0

    async def get_positions(self):
        """Returns live positions from Upstox."""
        if not self.api_client: return []
        try:
            res = await self.api_client.get_positions()
            if res and 'data' in res:
                positions = []
                for p in res['data']:
                    if p.get('quantity', 0) == 0: continue
                    positions.append({
                        'symbol': p['tradingsymbol'],
                        'quantity': p['quantity'],
                        'average_price': p['average_price'],
                        'ltp': p['last_price'],
                        'pnl': p['pnl']
                    })
                return positions
            return []
        except: return []
