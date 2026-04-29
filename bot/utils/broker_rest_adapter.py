import asyncio
import datetime
import pandas as pd
from utils.logger import logger

class BrokerRestAdapter:
    """
    Standardization layer for Broker REST APIs.
    Ensures that the bot logic can call 'get_ltp', 'get_historical_candle_data', etc.
    regardless of whether the underlying broker is Upstox, Zerodha, or Dhan.
    """

    # Class-level shared state to avoid redundant failed API calls across instances
    _blocked_zerodha_api_keys = set()

    def __init__(self, broker_client, broker_name):
        self.client = broker_client
        self.broker_name = broker_name.lower()
        # Mirror the Upstox-style method names that the bot logic already uses
        self.is_mock = getattr(broker_client, 'is_mock', False)

    async def _translate_to_broker_key(self, instrument_key):
        """Translates universal keys (e.g. NSE_INDEX|Nifty 50) to broker-specific keys."""
        if self.broker_name == 'upstox':
            return instrument_key

        elif self.broker_name == 'zerodha':
            if isinstance(instrument_key, int): return instrument_key
            if isinstance(instrument_key, str):
                # Format check: "NSE_FO|12345" -> Extract numeric part
                if '|' in instrument_key:
                    parts = instrument_key.split('|')
                    # If it's an index key, handle mapping
                    if 'INDEX' in parts[0]:
                        index_name = parts[1]
                        # 1. Hardcoded Fast Path for Common Indices (Zerodha Tokens)
                        hardcoded = {
                            'Nifty 50': 256265,
                            'NIFTY 50': 256265,
                            'Nifty Bank': 260105,
                            'NIFTY BANK': 260105,
                            'Nifty Fin Service': 257801,
                            'NIFTY FIN SERVICE': 257801,
                            'SENSEX': 265
                        }
                        if index_name in hardcoded: return hardcoded[index_name]

                        # Handle alternative names from INI (Nifty 50 vs NIFTY 50)
                        index_name_clean = index_name.upper().strip()

                        # 2. Wrapper Method Path
                        index_map = {
                            'NIFTY 50': 'NIFTY 50',
                            'NIFTY BANK': 'NIFTY BANK',
                            'NIFTY FIN SERVICE': 'NIFTY FIN SERVICE',
                            'SENSEX': 'SENSEX',
                            'NIFTY MIDCAP SELECT': 'NIFTY MIDCAP SELECT',
                            'NIFTY MID SELECT': 'NIFTY MIDCAP SELECT'
                        }
                        symbol = index_map.get(index_name_clean, index_name_clean)
                        exchange = 'NSE' if 'SENSEX' not in symbol else 'BSE'

                        if hasattr(self.client, 'load_instrument_master'):
                            await self.client.load_instrument_master()

                        if hasattr(self.client, 'get_token_by_symbol'):
                            token = self.client.get_token_by_symbol(symbol, exchange)
                            if token: return int(token)
                    else:
                        # For regular FO keys like NSE_FO|66691, the 2nd part is usually the token
                        try: return int(parts[1])
                        except: pass

                # Fallback: attempt to convert whole string to int
                try: return int(instrument_key)
                except:
                    if hasattr(self.client, 'get_token_by_symbol'):
                        token = self.client.get_token_by_symbol(instrument_key)
                        if token: return int(token)
            return instrument_key

        elif self.broker_name == 'dhan':
            if isinstance(instrument_key, str) and '|' in instrument_key:
                sid_map = {
                    'NSE_INDEX|Nifty 50': '13',
                    'NSE_INDEX|Nifty Bank': '25',
                    'NSE_INDEX|Nifty Fin Service': '27',
                    'BSE_INDEX|SENSEX': '1'
                }
                if instrument_key in sid_map: return sid_map[instrument_key]
                return instrument_key.split('|')[1]
            return instrument_key

        elif self.broker_name == 'angelone':
            if hasattr(self.client, 'get_token_by_universal_key'):
                if hasattr(self.client, '_load_token_map'):
                    await self.client._load_token_map()
                token = self.client.get_token_by_universal_key(instrument_key)
                if token: return token
            return instrument_key

        return instrument_key

    async def get_ltp(self, instrument_key, silence_error=False):
        if self.is_mock: return 0.0
        try:
            broker_key = await self._translate_to_broker_key(instrument_key)

            if self.broker_name == 'upstox':
                return await self.client.get_ltp(broker_key, silence_error=silence_error)

            elif self.broker_name == 'zerodha':
                # Handle both wrapper client and raw KiteConnect object
                kite = getattr(self.client, 'kite', self.client)
                query_key = str(broker_key)
                res = await asyncio.to_thread(kite.ltp, [query_key])
                return res.get(query_key, {}).get('last_price', 0.0)

            elif self.broker_name == 'dhan':
                # Handle both wrapper client and raw dhanhq object
                dhan = getattr(self.client, 'dhan', self.client)
                res = await asyncio.to_thread(dhan.quote_data, broker_key)
                if res and res.get('status') == 'success':
                    return float(res.get('data', {}).get('last_price', 0.0))
                return 0.0

            elif self.broker_name == 'angelone':
                # Handle both wrapper client and raw SmartApi object
                smart_api = getattr(self.client, 'smart_api', self.client)
                res = await asyncio.to_thread(smart_api.ltpData, "NSE", "", str(broker_key))
                if res and res.get('status'):
                    return float(res.get('data', {}).get('lastTradedPrice', 0.0))
                return 0.0

            return 0.0
        except Exception as e:
            if not silence_error: logger.warning(f"[{self.broker_name}] get_ltp failed for {instrument_key} (as {broker_key if 'broker_key' in locals() else 'N/A'}): {e}")
            return 0.0

    async def get_ltps(self, instrument_keys, silence_error=False):
        if self.is_mock: return {}
        if not instrument_keys: return {}
        try:
            if self.broker_name == 'upstox':
                return await self.client.get_ltps(instrument_keys, silence_error=silence_error)

            elif self.broker_name == 'zerodha':
                # Translate all keys
                translated_map = {}
                for ikey in instrument_keys:
                    translated_map[str(await self._translate_to_broker_key(ikey))] = ikey

                query_keys = list(translated_map.keys())
                kite = getattr(self.client, 'kite', self.client)
                res = await asyncio.to_thread(kite.ltp, query_keys)

                # Map back to original keys
                return {translated_map[k]: v.get('last_price', 0.0) for k, v in res.items() if k in translated_map}

            elif self.broker_name == 'dhan':
                # Dhan batch quotes
                return {}

            elif self.broker_name == 'angelone':
                # AngelOne doesn't have a simple batch LTP REST call in standard SDK
                # We could iterate or use websocket (which is already running)
                results = {}
                for ikey in instrument_keys:
                    results[ikey] = await self.get_ltp(ikey, silence_error=True)
                return results

            return {}
        except Exception as e:
            if not silence_error: logger.warning(f"[{self.broker_name}] get_ltps failed: {e}")
            return {}

    async def get_historical_candle_data(self, instrument_key, interval, to_date, from_date):
        if self.is_mock: return pd.DataFrame()

        # Fast exit if we know history is blocked for this account (Insufficient Permission)
        if self.broker_name == 'zerodha':
            api_key = getattr(self.client, 'api_key', 'unknown')
            if api_key in self._blocked_zerodha_api_keys:
                return pd.DataFrame()

        try:
            broker_key = await self._translate_to_broker_key(instrument_key)

            # interval conversion (Bot uses '1minute', '5minute', etc.)
            if self.broker_name == 'upstox':
                return await self.client.get_historical_candle_data(broker_key, interval, to_date, from_date)

            elif self.broker_name == 'zerodha':
                k_interval = interval.replace('1minute', 'minute')
                token = broker_key
                if not isinstance(token, int):
                    try: token = int(token)
                    except:
                        logger.error(f"[Zerodha] Historical data requires numeric token. Got: {token}")
                        return pd.DataFrame()

                kite = getattr(self.client, 'kite', self.client)
                res = await asyncio.to_thread(kite.historical_data, token, from_date, to_date, k_interval)
                if not res: return pd.DataFrame()
                df = pd.DataFrame(res)
                df.set_index('date', inplace=True)
                df.index = pd.to_datetime(df.index)
                return df

            elif self.broker_name == 'dhan':
                sid = broker_key
                segment = 'NSE_FNO'
                if isinstance(instrument_key, str) and '|' in instrument_key:
                    parts = instrument_key.split('|')
                    segment = 'NSE_FNO' if 'FO' in parts[0] else 'NSE_EQ'

                dhan = getattr(self.client, 'dhan', self.client)
                # If from_date is today, use intraday
                if str(from_date) == datetime.date.today().strftime('%Y-%m-%d'):
                    res = await asyncio.to_thread(dhan.intraday_minute_data,
                                                 security_id=sid, exchange_segment=segment, instrument_type='INDEX' if 'INDEX' in str(instrument_key) else 'OPTIDX')
                else:
                    # Dhan historical uses different params
                    res = await asyncio.to_thread(dhan.historical_minute_data,
                                                 security_id=sid, exchange_segment=segment,
                                                 instrument_type='INDEX' if 'INDEX' in str(instrument_key) else 'OPTIDX',
                                                 from_date=str(from_date), to_date=str(to_date))

                if res and res.get('status') == 'success':
                    data = res.get('data', [])
                    if not data: return pd.DataFrame()
                    df = pd.DataFrame(data)
                    # Dhan uses 'start_Time'
                    ts_col = 'start_Time' if 'start_Time' in df.columns else 'timestamp'
                    df['timestamp'] = pd.to_datetime(df[ts_col])
                    df.set_index('timestamp', inplace=True)
                    df = df.rename(columns={'open_Price': 'open', 'high_Price': 'high', 'low_Price': 'low', 'close_Price': 'close', 'volume': 'volume'})
                    return df
                return pd.DataFrame()

            elif self.broker_name == 'angelone':
                # AngelOne historical data
                # interval: ONE_MINUTE, FIVE_MINUTE, etc.
                # Bot uses '1minute', '5minute'
                a_interval = "ONE_MINUTE" if interval == '1minute' else "FIVE_MINUTE"

                params = {
                    "exchange": "NSE" if "INDEX" in str(instrument_key) else "NFO",
                    "symboltoken": str(broker_key),
                    "interval": a_interval,
                    "fromdate": f"{from_date} 09:15",
                    "todate": f"{to_date} 15:30"
                }
                smart_api = getattr(self.client, 'smart_api', self.client)
                res = await asyncio.to_thread(smart_api.getCandleData, params)
                if res and res.get('status'):
                    data = res.get('data', [])
                    if not data: return pd.DataFrame()
                    df = pd.DataFrame(data, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                    df['timestamp'] = pd.to_datetime(df['timestamp'])
                    df.set_index('timestamp', inplace=True)
                    return df
                return pd.DataFrame()

            return pd.DataFrame()
        except Exception as e:
            err_str = str(e).lower()
            if self.broker_name == 'zerodha' and "insufficient permission" in err_str:
                api_key = getattr(self.client, 'api_key', 'unknown')
                if api_key not in self._blocked_zerodha_api_keys:
                    logger.error(f"[Zerodha] REST API history addon missing for account '{api_key}'. "
                                 f"Check your Kite developer portal and ensure the Rs. 2000/month history addon is active.")
                    self._blocked_zerodha_api_keys.add(api_key)
                return pd.DataFrame()

            logger.warning(f"[{self.broker_name}] get_historical failed for {instrument_key}: {e}")
            return pd.DataFrame()

    async def get_option_contracts(self, instrument_key):
        if self.is_mock: return []
        try:
            if self.broker_name == 'upstox':
                return await self.client.get_option_contracts(instrument_key)

            elif self.broker_name == 'zerodha':
                # instrument_key is something like "NSE_INDEX|Nifty 50"
                # Zerodha master instruments are in self.client._master_instruments
                if hasattr(self.client, 'load_instrument_master'):
                    await self.client.load_instrument_master()

                # _master_instruments is initialized to None so getattr fallback [] is unused;
                # use `or []` to safely handle both None (load failed) and missing attribute.
                master = getattr(self.client, '_master_instruments', None) or []
                if not master: return []

                # Extract index name from key
                index_name = instrument_key.split('|')[1].upper()
                name_map = {
                    "NIFTY 50": "NIFTY",
                    "NIFTY BANK": "BANKNIFTY",
                    "NIFTY FIN SERVICE": "FINNIFTY",
                    "NIFTY MID SELECT": "MIDCPNIFTY"
                }
                z_name = name_map.get(index_name, index_name)

                contracts = []
                for inst in master:
                    if inst['name'] == z_name and inst['segment'] == 'NFO-OPT':
                        # Zerodha instrument_token IS the NSE exchange token used by
                        # Upstox too — format as "NSE_FO|{token}" so the rest of the
                        # system (LTP lookup, WebSocket subscriptions) can use these
                        # contracts without key mismatches.
                        contracts.append({
                            'instrument_key': f"NSE_FO|{inst['instrument_token']}",
                            'tradingsymbol': inst['tradingsymbol'],
                            'expiry': inst['expiry'],
                            'strike_price': float(inst['strike']),
                            'instrument_type': 'CE' if inst['instrument_type'] == 'CE' else 'PE',
                            'lot_size': int(inst['lot_size'])
                        })
                return contracts

            elif self.broker_name == 'angelone':
                if hasattr(self.client, '_load_token_map'):
                    await self.client._load_token_map()

                # _token_map is initialized to None so getattr fallback {} is never used;
                # use `or {}` to safely handle both None and missing attribute.
                token_map = getattr(self.client, '_token_map', None) or {}
                if not token_map: return []

                index_name = instrument_key.split('|')[1].upper()
                name_map = {
                    "NIFTY 50": "NIFTY",
                    "NIFTY BANK": "BANKNIFTY",
                    "NIFTY FIN SERVICE": "FINNIFTY"
                }
                a_name = name_map.get(index_name, index_name)

                contracts = []
                for key, info in token_map.items():
                    # key is (name, expiry_date, strike, type)
                    if key[0] == a_name:
                        # Format instrument_key as NSE_FO|<exchange_token> — AngelOne's token
                        # field IS the NSE exchange token (same as Zerodha's instrument_token
                        # and Upstox's exchange_token). This format is required for the
                        # FeedServer tick subscription system to route LTP correctly.
                        contracts.append({
                            'instrument_key': f"NSE_FO|{info['token']}",
                            'tradingsymbol': info['tradingsymbol'],
                            'expiry': key[1],
                            'strike_price': key[2],
                            'instrument_type': key[3],
                            'lot_size': info['lotsize']
                        })
                return contracts

            return []
        except Exception as e:
            logger.error(f"[{self.broker_name}] get_option_contracts failed: {e}")
            return []

    def get_active_client(self):
        return self
