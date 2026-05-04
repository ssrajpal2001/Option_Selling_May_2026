import os
import datetime
import pandas as pd
from utils.logger import logger

class OptionContract:
    """A simple class to hold option contract data and parse the API response."""
    def __init__(self, data):
        self.instrument_key = data.get('instrument_key')
        self.exchange = data.get('exchange')
        self.trading_symbol = data.get('trading_symbol', data.get('tradingsymbol')) or self.instrument_key
        self.name = data.get('name') or self.trading_symbol.split(' ')[0] or 'NIFTY'
        self.instrument_type = data.get('instrument_type')

        # Robust type detection from symbol if missing
        symbol = str(data.get('tradingsymbol', data.get('trading_symbol', ''))).upper()
        if not self.instrument_type:
            if symbol.endswith('CE'): self.instrument_type = 'CE'
            elif symbol.endswith('PE'): self.instrument_type = 'PE'

        # Normalize instrument type (Upstox API vs CSV inconsistencies)
        if self.instrument_type:
            up_type = str(self.instrument_type).upper()
            opt_type = str(data.get('option_type', '')).upper()

            if 'CE' in up_type or 'CALL' in up_type or 'CE' in opt_type or 'CALL' in opt_type or symbol.endswith('CE'):
                self.instrument_type = 'CE'
            elif 'PE' in up_type or 'PUT' in up_type or 'PE' in opt_type or 'PUT' in opt_type or symbol.endswith('PE'):
                self.instrument_type = 'PE'

        self.strike_price = data.get('strike_price', data.get('strike'))
        self.lot_size = data.get('lot_size')
        self.expiry = self._parse_expiry(data.get('expiry'))

    def _parse_expiry(self, expiry_val):
        if not expiry_val:
            return None
        if isinstance(expiry_val, datetime.datetime):
            return expiry_val
        if isinstance(expiry_val, datetime.date):
            return datetime.datetime.combine(expiry_val, datetime.time.min)
        try:
            # Handle potential string format from some brokers or JSON
            # Kite sometimes returns "2024-10-24 00:00:00" or similar
            s_val = str(expiry_val).split(' ')[0]
            return datetime.datetime.strptime(s_val, '%Y-%m-%d')
        except:
            return None

class ContractManager:
    def __init__(self, rest_client, config_manager, atm_manager=None):
        self.rest_client = rest_client
        self.config_manager = config_manager
        self.atm_manager = atm_manager
        self.all_options = []
        self.near_expiry_date = None
        self.monthly_expiries = []

    def _get_project_root(self):
        return os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))

    async def load_contracts(self, instrument_key, discover_futures_func):
        is_backtest = self.config_manager.get_boolean('settings', 'backtest_enabled', fallback=False)
        has_api = self.rest_client and not hasattr(self.rest_client, 'called')

        if has_api:
            await discover_futures_func()

        if is_backtest:
            res = await self._load_contracts_from_csv()
            csv_success = res is not None
            if csv_success and self.all_options:
                return True, res
            if has_api:
                return await self._load_contracts_from_api(instrument_key), None
            return len(self.all_options) > 0, None
        else:
            return await self._load_contracts_from_api(instrument_key), None

    async def _load_contracts_from_csv(self):
        try:
            csv_filename = self.config_manager.get('settings', 'backtest_csv_path', fallback='tick_data_log.csv')
            csv_path = os.path.join(self._get_project_root(), csv_filename)

            if not os.path.isfile(csv_path):
                # Proactive Search for instrument-specific CSV
                inst = self.config_manager.get('settings', 'instrument_to_trade', fallback='NIFTY').split(',')[0].strip()
                bt_date = self.config_manager.get('settings', 'backtest_date', fallback='')
                if inst and bt_date:
                    for _md_dir in ["backtest_data", "."]:
                        specific_path = os.path.join(self._get_project_root(), _md_dir, f"market_data_{inst}_{bt_date}.csv")
                        if os.path.isfile(specific_path):
                            csv_path = specific_path
                            break

            if not os.path.isfile(csv_path): return None

            df = pd.read_csv(csv_path, on_bad_lines='warn', engine='python')
            if df.empty: return None

            df['timestamp'] = pd.to_datetime(df['timestamp'], errors='coerce', utc=True)
            df.dropna(subset=['timestamp'], inplace=True)
            df['timestamp'] = df['timestamp'].dt.tz_convert('Asia/Kolkata')
            df.set_index('timestamp', inplace=True)
            df.sort_index(inplace=True)
            fallback_date = df.index[0] if not df.empty else datetime.datetime.now()

            # Hybrid Model logic simplified here
            # In backtest, we FIRST try to extract contracts from the data CSV itself
            # as it contains the EXACT keys used in the simulation.
            extracted = []
            inst_name = self.atm_manager.instrument_name if self.atm_manager else self.config_manager.get('settings', 'instrument_to_trade').split(',')[0].strip()

            # Check for ce_symbol/pe_symbol/ce_strike/pe_strike (Long format CSV)
            if 'ce_symbol' in df.columns and 'ce_strike' in df.columns:
                for side in ['ce', 'pe']:
                    sym_col = f'{side}_symbol'
                    strike_col = f'{side}_strike'
                    if sym_col in df.columns and strike_col in df.columns:
                        unique = df[[sym_col, strike_col]].drop_duplicates().dropna()
                        for _, r in unique.iterrows():
                            extracted.append(OptionContract({
                                'name': inst_name,
                                'instrument_key': r[sym_col],
                                'strike_price': float(r[strike_col]),
                                'instrument_type': side.upper(),
                                'expiry': fallback_date.strftime('%Y-%m-%d'),
                                'lot_size': self.config_manager.get_int(inst_name, 'lot_size', 50)
                            }))

                # Ensure numeric types for strike and prices in backtest_df
                for col in ['strike_price', 'ce_strike', 'pe_strike', 'ce_ltp', 'pe_ltp', 'spot_price', 'index_price']:
                    if col in df.columns:
                        df[col] = pd.to_numeric(df[col], errors='coerce')

            if extracted:
                self.all_options = extracted
                logger.info(f"ContractManager: Extracted {len(extracted)} contracts from data CSV.")
            else:
                logger.debug("Backtest Hybrid Model: Fetching live option chain to ensure all expiries are available.")
                live_contracts = await self.get_live_option_contracts(self.config_manager.get_instrument_key_by_symbol(self.config_manager.get('settings', 'instrument_to_trade')))
                if live_contracts:
                    self.all_options = live_contracts

            if not self.all_options: return None
            self._determine_near_expiry_date()
            self._identify_monthly_expiries()
            return df
        except Exception as e:
            logger.error(f"Failed to load contracts from CSV: {e}", exc_info=True)
            return None

    async def _supplement_expiry_day_contracts(self, instrument_key):
        """On NSE expiry day, Upstox excludes today's expiring contracts from the standard
        /option/contract endpoint.  This method detects that and supplements all_options by
        fetching the /expired-instruments/option/contract endpoint."""
        # Use orchestrator timestamp if in backtest, otherwise today's date
        is_backtest = self.config_manager.get_boolean('settings', 'backtest_enabled', fallback=False)
        if is_backtest:
            # Try to get simulation date from settings first
            bt_date_str = self.config_manager.get('settings', 'backtest_date')
            if bt_date_str:
                today = datetime.datetime.strptime(bt_date_str, '%Y-%m-%d').date()
            elif self.atm_manager and self.atm_manager.orchestrator:
                ts = self.atm_manager.orchestrator._get_timestamp()
                today = ts.date() if ts else datetime.date.today()
            else:
                today = datetime.date.today()
        else:
            today = datetime.date.today()

        loaded_expiries = {c.expiry.date() for c in self.all_options if getattr(c, 'expiry', None) and c.instrument_type in ['CE', 'PE']}
        if today in loaded_expiries:
            return  # today's contracts already present — nothing to do

        logger.info(f"ContractManager: Expiry day detected ({today}) — today's contracts absent from standard endpoint. Fetching from expired-instruments API...")

        # Resolve which REST client to use for the expired-instruments call.
        # Priority: primary rest_client (if it supports the endpoint) → Upstox client broker
        # (fresh token, always supports the endpoint) → give up.
        supplement_client = None
        if hasattr(self.rest_client, 'get_expiring_option_contracts'):
            supplement_client = self.rest_client
        else:
            # Primary REST client (e.g. Dhan, Zerodha) doesn't have this endpoint.
            # Try to find a live Upstox client broker whose api_client is already set.
            try:
                orch = self.atm_manager.orchestrator if self.atm_manager else None
                bm = getattr(orch, 'broker_manager', None) if orch else None
                if bm and bm.brokers:
                    for _b in bm.brokers.values():
                        if getattr(_b, 'broker_name', '') == 'upstox':
                            _api_client = getattr(_b, 'api_client', None)
                            if _api_client:
                                from utils.rest_api_client import RestApiClient as _RAC
                                supplement_client = _RAC(_api_client.auth_handler)
                                logger.info("ContractManager: Using Upstox client broker for expired-instruments supplement.")
                                break
            except Exception as _se:
                logger.debug(f"ContractManager: Could not build Upstox supplement client: {_se}")

        if not supplement_client:
            logger.warning(
                f"ContractManager: No REST client supports get_expiring_option_contracts "
                f"(primary is {type(self.rest_client).__name__}, no live Upstox broker found). "
                "Bot will use next available expiry — today's expiry contracts unavailable."
            )
            return

        expiring_raw = await supplement_client.get_expiring_option_contracts(instrument_key, today)
        if not expiring_raw:
            logger.warning("ContractManager: expired-instruments API returned no contracts. Bot may be unable to trade today's expiry.")
            return

        today_contracts = [OptionContract(c) for c in expiring_raw
                           if c.get('expiry') == today.strftime('%Y-%m-%d')]
        if today_contracts:
            self.all_options.extend(today_contracts)
            logger.info(f"ContractManager: Supplemented with {len(today_contracts)} expiry-day contracts for {today}. Total option contracts: {len(self.all_options)}")
        else:
            logger.warning(f"ContractManager: expired-instruments API returned data but none matched today ({today}). Bot may be unable to trade today's expiry.")

    async def _load_contracts_from_api(self, instrument_key):
        try:
            if not instrument_key: return False
            raw_contracts = await self.rest_client.get_option_contracts(instrument_key)

            # If standard key (e.g., MCX_INDEX|CRUDEOIL) returns nothing,
            # try with the futures key (e.g., MCX_FO:CRUDEOIL26MARFUT).
            # Upstox API sometimes needs the specific future for commodity option chains.
            if not raw_contracts and self.atm_manager and self.atm_manager.orchestrator:
                f_key = self.atm_manager.orchestrator.futures_instrument_key
                if f_key and f_key != instrument_key:
                    logger.info(f"ContractManager: '{instrument_key}' returned no options. Retrying with futures_key '{f_key}'...")
                    raw_contracts = await self.rest_client.get_option_contracts(f_key)

                    # Extra level: try underlying_key from the future if available
                    if not raw_contracts:
                        _first_broker = next(iter(self.atm_manager.orchestrator.broker_manager.brokers.values()), None) if self.atm_manager.orchestrator.broker_manager.brokers else None
                        f_contract = _first_broker.get_contract_by_key(f_key) if _first_broker and hasattr(_first_broker, 'get_contract_by_key') else None
                        u_key = getattr(f_contract, 'underlying_key', None)
                        if u_key and u_key != f_key and u_key != instrument_key:
                            logger.info(f"ContractManager: Retrying with underlying_key '{u_key}' from future...")
                            raw_contracts = await self.rest_client.get_option_contracts(u_key)

            # Broker REST fallback: when primary REST client (e.g. global Upstox with stale token)
            # returns nothing, try logged-in client brokers before falling to the CSV snapshot.
            # This gives us live contracts (including May weekly expiries) that the CSV may lack.
            if not raw_contracts and self.atm_manager and self.atm_manager.orchestrator:
                _bm = getattr(self.atm_manager.orchestrator, 'broker_manager', None)
                if _bm and _bm.brokers:
                    _CAPABLE = ['upstox', 'zerodha', 'angelone', 'fyers', 'aliceblue', 'dhan']
                    for _pref in _CAPABLE:
                        _b = next(
                            (b for b in _bm.brokers.values()
                             if getattr(b, 'broker_name', '') == _pref),
                            None
                        )
                        if _b:
                            try:
                                from utils.broker_rest_adapter import BrokerRestAdapter as _BRA
                                _adapter = _BRA(_b, _pref)
                                _raw = await _adapter.get_option_contracts(instrument_key)
                                if _raw:
                                    raw_contracts = _raw
                                    logger.info(
                                        f"ContractManager: Got {len(raw_contracts)} contracts via {_pref} "
                                        "broker REST fallback (primary REST client had no data)."
                                    )
                                    break
                            except Exception as _be:
                                logger.debug(f"ContractManager: Broker REST fallback ({_pref}) failed: {_be}")

            if not raw_contracts:
                is_backtest = self.config_manager.get_boolean('settings', 'backtest_enabled', fallback=False)
                if not is_backtest:
                    # LIVE MODE: CSV fallback is DISABLED.
                    # The CSV is a public snapshot file that does NOT include near-weekly
                    # contracts (May 5, May 12, May 19, etc.). Using CSV causes:
                    #   • Wrong expiry resolution (e.g. May 26 instead of May 5)
                    #   • Position reconnect to wrong contracts after restart
                    #   • LTP stuck at 0 for the reconnected position
                    #   • Exit criteria unable to evaluate → trades frozen
                    # The admin MUST ensure the global data feed token is valid BEFORE
                    # starting the bot (Admin → Data Providers → refresh token).
                    logger.error(
                        f"[ContractManager] CRITICAL: No option contracts found for '{instrument_key}' "
                        "via any authenticated API. "
                        "CSV fallback is DISABLED in live mode to prevent wrong expiry resolution. "
                        "ACTION REQUIRED: Go to Admin → Data Providers and refresh the global Upstox "
                        "token, then restart the bot."
                    )
                    return False

                # BACKTEST MODE ONLY: Fall back to CSV snapshot
                logger.info(f"ContractManager: No options found via API for '{instrument_key}'. Attempting CSV fallback (backtest mode only)...")

                # Deduce exchange from symbol/instrument name
                instr_name = self.config_manager.get_instrument_by_symbol(instrument_key) or (self.atm_manager.instrument_name if self.atm_manager else "")
                name_up = instr_name.upper()
                if name_up == 'SENSEX': exchange = 'BSE_FO'
                elif any(x in name_up for x in ['CRUDE', 'NATURAL', 'GOLD', 'SILVER']): exchange = 'MCX'
                else: exchange = 'NSE_FO'

                csv_path = f"config/instruments_{exchange}.csv.gz"

                # Proactive CSV Download if missing or stale
                if not os.path.exists(csv_path) or (datetime.datetime.now().timestamp() - os.path.getmtime(csv_path)) > 86400:
                    try:
                        import aiohttp
                        url = f"https://assets.upstox.com/market-quote/instruments/exchange/{exchange}.csv.gz"
                        logger.info(f"ContractManager: Downloading {exchange} instruments from {url}...")
                        async with aiohttp.ClientSession() as session:
                            async with session.get(url, headers={"User-Agent": "Mozilla/5.0"}) as resp:
                                if resp.status == 200:
                                    os.makedirs('config', exist_ok=True)
                                    with open(csv_path, 'wb') as f: f.write(await resp.read())
                                    logger.info(f"ContractManager: Downloaded {csv_path}")
                    except Exception as de:
                        logger.error(f"ContractManager: Failed to download CSV: {de}")

                if os.path.exists(csv_path):
                    try:
                        df = pd.read_csv(csv_path, compression='gzip')
                        # Normalize names to handle spaces (e.g., "CRUDE OIL" vs "CRUDEOIL")
                        raw_sym = (instrument_key.split('|')[-1] if '|' in instrument_key else name_up).replace(" ", "").upper()

                        # Mapping for common indices where API name differs from CSV name
                        mapping = {
                            'NIFTY50': 'NIFTY',
                            'NIFTYBANK': 'BANKNIFTY',
                            'NIFTYFINSERVICE': 'FINNIFTY'
                        }
                        search_symbol = mapping.get(raw_sym, raw_sym)

                        df['name_norm'] = df['name'].str.replace(" ", "").str.upper()

                        # In CSV, instrument_type might be OPTCOM, OPTIDX, OPTFUT, etc.
                        # We search for anything that looks like an option or has an option_type.
                        # For MCX, we also check if tradingsymbol ends with CE/PE
                        # Handle both 'tradingsymbol' and 'trading_symbol' column names
                        sym_col = 'tradingsymbol' if 'tradingsymbol' in df.columns else 'trading_symbol'

                        options = df[((df['instrument_type'].str.contains('CE|PE|CALL|PUT|OPT', case=False, na=False)) |
                                     (df['option_type'].str.contains('CE|PE|CALL|PUT', case=False, na=False)) |
                                     (df[sym_col].str.endswith(('CE', 'PE'), na=False))) &
                                     (df['name_norm'] == search_symbol)]

                        logger.info(f"ContractManager: CSV search for '{search_symbol}' in {exchange} found {len(options)} options.")
                        if not options.empty:
                            logger.info(f"ContractManager: Sample CSV row: {options.iloc[0].to_dict()}")

                        if not options.empty:
                            self.all_options = [OptionContract(row.to_dict()) for _, row in options.iterrows()]
                            logger.info(f"ContractManager: CSV fallback success. Loaded {len(self.all_options)} option contracts for {search_symbol} from {exchange}.")
                            self._determine_near_expiry_date()
                            self._identify_monthly_expiries()
                            return True
                    except Exception as e:
                        logger.error(f"ContractManager: CSV fallback failed: {e}")

                self.all_options = []
                return True
            self.all_options = [OptionContract(c) for c in raw_contracts]
            await self._supplement_expiry_day_contracts(instrument_key)
            self._determine_near_expiry_date()
            self._identify_monthly_expiries()
            return True
        except Exception as e:
            logger.error(f"Error loading contracts from API: {e}", exc_info=True)
            return False

    def _determine_near_expiry_date(self):
        # Use orchestrator timestamp if in backtest, otherwise today's date
        is_backtest = self.config_manager.get_boolean('settings', 'backtest_enabled', fallback=False)
        if is_backtest:
            # Try to get simulation date from settings first
            bt_date_str = self.config_manager.get('settings', 'backtest_date')
            if bt_date_str:
                today = datetime.datetime.strptime(bt_date_str, '%Y-%m-%d').date()
            elif self.atm_manager and self.atm_manager.orchestrator:
                ts = self.atm_manager.orchestrator._get_timestamp()
                today = ts.date() if ts else datetime.date.today()
            else:
                today = datetime.date.today()
        else:
            today = datetime.date.today()

        unique_expiries = sorted(list(set(c.expiry.date() for c in self.all_options if getattr(c, 'expiry', None) and c.instrument_type in ['CE', 'PE'])))
        if not unique_expiries: return

        logger.info(f"ContractManager: All CE/PE expiry dates loaded ({len(unique_expiries)} unique): {unique_expiries[:10]}{'...' if len(unique_expiries) > 10 else ''}")

        trade_expiry_type = self.config_manager.get('settings', 'trade_expiry_type', fallback='WEEKLY').upper()
        if trade_expiry_type == 'WEEKLY':
            # Use the first available expiry >= today from the API-provided list.
            # This works regardless of which weekday NSE chooses as expiry day.
            # Expiry comes purely from the loaded contract data — no calendar math.
            for expiry_date in unique_expiries:
                if expiry_date >= today:
                    self.near_expiry_date = datetime.datetime.combine(expiry_date, datetime.time.min)
                    logger.info(f"ContractManager: Near weekly expiry resolved to {expiry_date} (day={expiry_date.strftime('%A')})")
                    return
        elif trade_expiry_type == 'MONTHLY':
            for expiry_date in self.monthly_expiries:
                if expiry_date >= today:
                    self.near_expiry_date = datetime.datetime.combine(expiry_date, datetime.time.min)
                    return

        if not self.near_expiry_date and unique_expiries:
            closest = min((d for d in unique_expiries if d >= today), default=unique_expiries[-1])
            self.near_expiry_date = datetime.datetime.combine(closest, datetime.time.min)

    def _identify_monthly_expiries(self):
        expiries_by_month = {}
        for contract in self.all_options:
            if contract.instrument_type not in ['CE', 'PE'] or not getattr(contract, 'expiry', None): continue
            expiry_date = contract.expiry.date()
            month_key = (expiry_date.year, expiry_date.month)
            if month_key not in expiries_by_month or expiry_date > expiries_by_month[month_key]:
                expiries_by_month[month_key] = expiry_date
        self.monthly_expiries = sorted(list(expiries_by_month.values()))

        # Sync to all state managers (Orchestrator and User Sessions)
        if self.atm_manager and self.atm_manager.orchestrator:
            orch = self.atm_manager.orchestrator
            orch.state_manager.monthly_expiries = self.monthly_expiries
            logger.debug(f"[{orch.instrument_name}] Monthly expiries synced to main state: {self.monthly_expiries}")

            for session in orch.user_sessions.values():
                session.state_manager.monthly_expiries = self.monthly_expiries
                logger.debug(f"[{orch.instrument_name}] Monthly expiries synced to user session: {session.email}")

    def get_contract_by_instrument_key(self, instrument_key):
        for c in self.all_options:
            if c.instrument_key == instrument_key: return c
        return None

    def find_instrument_key_by_strike(self, strike_price, option_type, expiry_date):
        api_type = 'CE' if option_type.upper() in ['CALL', 'CE'] else 'PE'
        target_expiry = expiry_date.date() if isinstance(expiry_date, datetime.datetime) else expiry_date

        matches = [c for c in self.all_options if getattr(c, 'expiry', None) and c.instrument_type == api_type and c.expiry.date() == target_expiry]
        if not matches: return None

        # Exact
        exact = next((c for c in matches if float(c.strike_price) == float(strike_price)), None)
        if exact: return exact.instrument_key

        # Closest
        closest = min(matches, key=lambda x: abs(float(x.strike_price) - float(strike_price)))
        # For Crude oil or other commodities, strikes might be further apart,
        # but 250 is already quite generous for a 100-point interval.
        if abs(float(closest.strike_price) - float(strike_price)) <= 250:
             return closest.instrument_key
        return None

    async def get_live_option_contracts(self, instrument_key):
        try:
            raw_contracts = await self.rest_client.get_option_contracts(instrument_key)
            return [OptionContract(c) for c in raw_contracts] if raw_contracts else []
        except Exception as e:
            logger.error(f"Error fetching live options: {e}")
            return []
