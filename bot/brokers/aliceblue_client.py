from .base_broker import BaseBroker
from utils.logger import logger
from utils.auth_manager_alice import handle_alice_login


class AliceblueClient(BaseBroker):
    """
    Execution-only Alice Blue broker client (no WebSocket data feed).
    Authentication: Client ID + API Key + PIN + TOTP via pya3.
    Background auto-refresh supported when PIN and TOTP seed are provided.
    """

    def __init__(self, broker_instance_name, config_manager, login_required=True, user_id=None, db_config=None):
        super().__init__(broker_instance_name, config_manager, user_id=user_id, db_config=db_config)
        self.broker_name = "aliceblue"
        self.alice = None

        if self.db_config:
            try:
                self.alice = handle_alice_login(self.db_config)
                if self.alice:
                    logger.info(f"[AliceblueClient] Initialised for user {self.user_id}.")
                else:
                    logger.warning(f"[AliceblueClient] Init failed for user {self.user_id}. Bot will run in limited mode.")
            except Exception as e:
                logger.error(f"[AliceblueClient] Init error for user {self.user_id}: {e}")

    def connect(self):
        pass

    def start_data_feed(self):
        logger.info(f"[AliceblueClient:{self.instance_name}] Execution-only. Data feed skipped (using Upstox/Dhan global feed).")

    def stop_data_feed(self):
        pass

    def place_order(self, contract, transaction_type: str, quantity: int, expiry=None,
                    product_type: str = "NRML", market_protection=None):
        if not self.alice:
            logger.error(f"[AliceblueClient] Not initialised. Cannot place order.")
            return None
        try:
            symbol = self._resolve_symbol(contract)
            if not symbol:
                logger.error(f"[AliceblueClient] Could not resolve symbol for {contract.instrument_key}")
                return None

            from pya3 import TransactionType, OrderType, ProductType, Exchange
            tx = TransactionType.Buy if transaction_type.upper() == "BUY" else TransactionType.Sell
            prod = ProductType.Normal if product_type == "NRML" else ProductType.Intraday

            self._set_source_ip()
            try:
                order_id = self.alice.place_order(
                    transaction_type=tx,
                    instrument=self.alice.get_instrument_by_symbol("NFO", symbol),
                    quantity=int(quantity),
                    order_type=OrderType.Market,
                    product_type=prod,
                )
            finally:
                self._clear_source_ip()
            if order_id:
                logger.info(f"[AliceblueClient] Order placed: {order_id}")
                return order_id
            logger.error(f"[AliceblueClient] Order placement returned None.")
            return None
        except Exception as e:
            logger.error(f"[AliceblueClient] place_order error: {e}", exc_info=True)
            return None

    def get_positions(self) -> list:
        if not self.alice:
            return []
        try:
            positions = self.alice.get_netwise_positions_list()
            return positions if isinstance(positions, list) else []
        except Exception as e:
            logger.error(f"[AliceblueClient] get_positions error: {e}")
            return []

    def get_funds(self) -> dict:
        if not self.alice:
            return {}
        try:
            balance = self.alice.get_balance()
            if isinstance(balance, list) and balance:
                for item in balance:
                    if "Net" in str(item.get("type", "")):
                        return {"balance": float(item.get("net", 0))}
            return {}
        except Exception as e:
            logger.error(f"[AliceblueClient] get_funds error: {e}")
            return {}

    async def close_all_positions(self):
        logger.info(f"[AliceblueClient:{self.instance_name}] close_all_positions called.")

    async def handle_entry_signal(self, **kwargs):
        pass

    async def handle_close_signal(self, **kwargs):
        pass

    def _resolve_symbol(self, contract) -> str | None:
        """Converts contract to Alice Blue NFO symbol string for pya3.
        Format expected by pya3 get_instrument_by_symbol('NFO', symbol):
          NIFTY25APR26C24000   (name + DD + MON + YY + C/P + strike)
        Note: pya3 uses single-letter C/P (not CE/PE) and 2-digit year.
        """
        try:
            import datetime as _dt
            raw_name = str(getattr(contract, "name", "NIFTY") or "NIFTY")
            name = self._normalize_instrument_name(raw_name)
            expiry = contract.expiry
            if isinstance(expiry, _dt.datetime):
                expiry = expiry.date()
            expiry_str = expiry.strftime("%d%b%y").upper()
            strike = int(float(contract.strike_price))
            raw_type = str(getattr(contract, "instrument_type", "CE") or "CE").upper()
            opt_char = "C" if raw_type in ("CE", "CALL") else "P"
            symbol = f"{name}{expiry_str}{opt_char}{strike}"
            logger.debug(f"[AliceblueClient] Resolved symbol: {symbol}")
            return symbol
        except Exception as e:
            logger.error(f"[AliceblueClient] Symbol resolution error for {getattr(contract, 'instrument_key', 'unknown')}: {e}", exc_info=True)
            return None
