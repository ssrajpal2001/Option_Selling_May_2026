from .base_broker import BaseBroker
from utils.logger import logger
from utils.auth_manager_fyers import handle_fyers_login


class FyersClient(BaseBroker):
    """
    Execution-only Fyers broker client (no WebSocket data feed).
    Authentication: OAuth via fyers-apiv3. Token valid ~24 hours.
    """

    def __init__(self, broker_instance_name, config_manager, login_required=True, user_id=None, db_config=None):
        super().__init__(broker_instance_name, config_manager, user_id=user_id, db_config=db_config)
        self.broker_name = "fyers"
        self.fyers = None

        if self.db_config:
            try:
                self.fyers = handle_fyers_login(self.db_config)
                if self.fyers:
                    logger.info(f"[FyersClient] Initialised for user {self.user_id}.")
                else:
                    logger.warning(f"[FyersClient] Token validation failed for user {self.user_id}. Bot will run in limited mode.")
            except Exception as e:
                logger.error(f"[FyersClient] Init error for user {self.user_id}: {e}")

    def connect(self):
        pass

    def start_data_feed(self):
        logger.info(f"[FyersClient:{self.instance_name}] Fyers is execution-only. Data feed skipped (using Upstox/Dhan global feed).")

    def stop_data_feed(self):
        pass

    def place_order(self, contract, transaction_type: str, quantity: int, expiry=None,
                    product_type: str = "NRML", market_protection=None):
        if not self.fyers:
            logger.error(f"[FyersClient] Not initialised. Cannot place order.")
            return None
        try:
            app_id = self.db_config.get("broker_user_id") or self.db_config.get("api_key", "")
            symbol = self._resolve_symbol(contract)
            if not symbol:
                logger.error(f"[FyersClient] Could not resolve symbol for {contract.instrument_key}")
                return None

            data = {
                "symbol": symbol,
                "qty": int(quantity),
                "type": 2,
                "side": 1 if transaction_type.upper() == "BUY" else -1,
                "productType": "MARGIN" if product_type == "NRML" else "INTRADAY",
                "limitPrice": 0,
                "stopPrice": 0,
                "validity": "DAY",
                "disclosedQty": 0,
                "offlineOrder": False,
                "orderTag": "algosoft",
            }
            resp = self.fyers.place_order(data=data)
            if resp and (resp.get("s") == "ok" or resp.get("code") == 200):
                order_id = resp.get("id") or resp.get("data", {}).get("id")
                logger.info(f"[FyersClient] Order placed: {order_id}")
                return order_id
            logger.error(f"[FyersClient] Order failed: {resp}")
            return None
        except Exception as e:
            logger.error(f"[FyersClient] place_order error: {e}", exc_info=True)
            return None

    def get_positions(self) -> list:
        if not self.fyers:
            return []
        try:
            resp = self.fyers.positions()
            if resp and resp.get("s") == "ok":
                return resp.get("netPositions", [])
            return []
        except Exception as e:
            logger.error(f"[FyersClient] get_positions error: {e}")
            return []

    def get_funds(self) -> dict:
        if not self.fyers:
            return {}
        try:
            resp = self.fyers.funds()
            if resp and resp.get("s") == "ok":
                fund_data = resp.get("fund_limit", [])
                for item in fund_data:
                    if item.get("title") == "Total Balance":
                        return {"balance": item.get("equityAmount", 0)}
            return {}
        except Exception as e:
            logger.error(f"[FyersClient] get_funds error: {e}")
            return {}

    async def close_all_positions(self):
        logger.info(f"[FyersClient:{self.instance_name}] close_all_positions called (Fyers execution mode).")

    async def handle_entry_signal(self, **kwargs):
        pass

    async def handle_close_signal(self, **kwargs):
        pass

    def _resolve_symbol(self, contract) -> str | None:
        """Converts contract to Fyers NSE symbol format (e.g. NSE:NIFTY25APR24500CE)."""
        try:
            name = str(getattr(contract, "name", "NIFTY") or "NIFTY").upper()
            expiry = contract.expiry
            if hasattr(expiry, "strftime"):
                expiry_str = expiry.strftime("%d%b%y").upper()
            else:
                expiry_str = str(expiry).upper()
            strike = int(float(contract.strike_price))
            opt_type = str(getattr(contract, "instrument_type", "CE") or "CE").upper()
            return f"NSE:{name}{expiry_str}{strike}{opt_type}"
        except Exception as e:
            logger.error(f"[FyersClient] Symbol resolution error: {e}")
            return None
