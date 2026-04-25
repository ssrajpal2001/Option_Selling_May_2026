import requests
from .base_broker import BaseBroker, SourceIPHTTPAdapter
from utils.logger import logger
from utils.auth_manager_groww import handle_groww_login


class GrowwClient(BaseBroker):
    """
    Execution-only Groww broker client (no WebSocket data feed).
    Groww's trading API requires a Bearer access token obtained from the Groww
    developer portal. Token is stored and validated on bot start.

    Uses a persistent requests.Session with SourceIPHTTPAdapter mounted so that
    ALL HTTP calls (auth, orders, positions, funds) route through the client's
    assigned Elastic IP.
    """

    def __init__(self, broker_instance_name, config_manager, login_required=True, user_id=None, db_config=None):
        super().__init__(broker_instance_name, config_manager, user_id=user_id, db_config=db_config)
        self.broker_name = "groww"
        self.access_token = None
        self.client_id = None

        # Persistent session — adapter is mounted below if source_ip is set
        self._session = requests.Session()
        if self.source_ip and SourceIPHTTPAdapter is not None:
            self._install_source_ip_adapter(self._session)

        if self.db_config:
            try:
                self._set_source_ip()
                try:
                    token = handle_groww_login(self.db_config)
                finally:
                    self._clear_source_ip()
                if token:
                    self.access_token = token
                    self.client_id = (
                        self.db_config.get("broker_user_id") or
                        self.db_config.get("client_id") or
                        self.db_config.get("api_key")
                    )
                    logger.info(f"[GrowwClient] Initialised for user {self.user_id}.")
                else:
                    logger.warning(f"[GrowwClient] Token invalid/missing for user {self.user_id}. Bot will run in limited mode.")
            except Exception as e:
                logger.error(f"[GrowwClient] Init error for user {self.user_id}: {e}")

    def connect(self):
        pass

    def start_data_feed(self):
        logger.info(f"[GrowwClient:{self.instance_name}] Execution-only. Data feed skipped (using Upstox/Dhan global feed).")

    def stop_data_feed(self):
        pass

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
        }

    def place_order(self, contract, transaction_type: str, quantity: int, expiry=None,
                    product_type: str = "NRML", market_protection=None):
        if not self.access_token:
            logger.error(f"[GrowwClient] No access token. Cannot place order.")
            return None
        try:
            symbol = self._resolve_symbol(contract)
            if not symbol:
                logger.error(f"[GrowwClient] Could not resolve symbol for {contract.instrument_key}")
                return None

            payload = {
                "tradingsymbol": symbol,
                "exchange": "NFO",
                "transaction_type": transaction_type.upper(),
                "order_type": "MARKET",
                "product": "NRML" if product_type == "NRML" else "MIS",
                "quantity": int(quantity),
                "validity": "DAY",
            }

            resp = self._session.post(
                "https://groww.in/v1/api/trade/v1/order/place",
                json=payload,
                headers=self._headers(),
                timeout=15,
            )

            if resp.status_code in (200, 201):
                data = resp.json()
                order_id = data.get("order_id") or data.get("data", {}).get("order_id")
                logger.info(f"[GrowwClient] Order placed: {order_id}")
                return order_id

            logger.error(f"[GrowwClient] Order failed: HTTP {resp.status_code} — {resp.text[:200]}")
            return None
        except Exception as e:
            logger.error(f"[GrowwClient] place_order error: {e}", exc_info=True)
            return None

    def get_positions(self) -> list:
        if not self.access_token:
            return []
        try:
            resp = self._session.get(
                "https://groww.in/v1/api/trade/v1/portfolio/positions",
                headers=self._headers(),
                timeout=10,
            )
            if resp.status_code == 200:
                return resp.json().get("positions", [])
            return []
        except Exception as e:
            logger.error(f"[GrowwClient] get_positions error: {e}")
            return []

    def get_funds(self) -> dict:
        if not self.access_token:
            return {}
        try:
            resp = self._session.get(
                "https://groww.in/v1/api/trade/v1/user/trading_balance",
                headers=self._headers(),
                timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json()
                return {"balance": float(data.get("available_margin", 0))}
            return {}
        except Exception as e:
            logger.error(f"[GrowwClient] get_funds error: {e}")
            return {}

    async def close_all_positions(self):
        logger.info(f"[GrowwClient:{self.instance_name}] close_all_positions called.")

    async def handle_entry_signal(self, **kwargs):
        pass

    async def handle_close_signal(self, **kwargs):
        pass

    def _resolve_symbol(self, contract) -> str | None:
        """Converts contract to Groww NFO symbol string.
        Format: NIFTY25APR202624500CE  (name + DD + MON + YYYY + strike + CE/PE)
        """
        try:
            import datetime as _dt
            raw_name = str(getattr(contract, "name", "NIFTY") or "NIFTY")
            name = self._normalize_instrument_name(raw_name)
            expiry = contract.expiry
            if isinstance(expiry, _dt.datetime):
                expiry = expiry.date()
            expiry_str = expiry.strftime("%d%b%Y").upper()
            strike = int(float(contract.strike_price))
            opt_type = str(getattr(contract, "instrument_type", "CE") or "CE").upper()
            if opt_type == "CALL": opt_type = "CE"
            if opt_type == "PUT": opt_type = "PE"
            symbol = f"{name}{expiry_str}{strike}{opt_type}"
            logger.debug(f"[GrowwClient] Resolved symbol: {symbol}")
            return symbol
        except Exception as e:
            logger.error(f"[GrowwClient] Symbol resolution error for {getattr(contract, 'instrument_key', 'unknown')}: {e}", exc_info=True)
            return None
