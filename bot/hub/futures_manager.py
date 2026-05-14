import os
import datetime
import pandas as pd
from utils.logger import logger

class FuturesManager:
    def __init__(self, rest_client, config_manager, atm_manager=None):
        self.rest_client = rest_client
        self.config_manager = config_manager
        self.atm_manager = atm_manager

    async def discover_futures_key(self, instrument_key, update_callback):
        if not instrument_key: return
        instrument_name = self.config_manager.get_instrument_by_symbol(instrument_key)
        if not instrument_name:
            if self.atm_manager: instrument_name = self.atm_manager.instrument_name
            else: return

        configured_key = self.config_manager.get(instrument_name, 'futures_instrument_key')
        if configured_key:
            try:
                ltp = await self.rest_client.get_ltp(configured_key, silence_error=True)
                if ltp > 0:
                    update_callback(configured_key)
                    return
            except Exception: pass

        if await self._discover_futures_from_csv(instrument_name, instrument_key, configured_key, update_callback):
            return

        guessed_key = self._guess_futures_symbol(instrument_name)
        if guessed_key:
            try:
                ltp = await self.rest_client.get_ltp(guessed_key, silence_error=True)
                if ltp > 0:
                    update_callback(guessed_key)
                    return
            except Exception: pass

        await self._probe_futures_near_key(instrument_name, instrument_key, configured_key, update_callback)

    def _guess_futures_symbol(self, instrument_name):
        today = datetime.date.today()
        mon = today.strftime('%b').upper()
        yy = today.strftime('%y')
        symbol = instrument_name.upper()
        if symbol == "NIFTY BANK": symbol = "BANKNIFTY"
        if symbol == "NIFTY 50": symbol = "NIFTY"
        if symbol in ["NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY"]:
            return f"NSE_FO:{symbol}{yy}{mon}FUT"
        elif symbol == "SENSEX":
            return f"BSE_FO:{symbol}{yy}{mon}FUT"
        elif symbol in ["CRUDEOIL", "NATURALGAS", "GOLD", "SILVER"]:
            return f"MCX_FO:{symbol}{yy}{mon}FUT"
        return None

    async def _probe_futures_near_key(self, instrument_name, instrument_key, base_key, update_callback):
        if not base_key or '|' not in base_key: return
        prefix, start_id = base_key.split('|')
        try:
            start_id = int(start_id)
        except ValueError: return
        candidate_keys = [f"{prefix}|{i}" for i in range(start_id - 200, start_id + 201)]
        try:
            ltps = await self.rest_client.get_ltps(candidate_keys, silence_error=True)
            valid_keys = [k for k in candidate_keys if ltps.get(k, 0) > 0]
            if valid_keys:
                # Use orchestrator to get proper anchor (Futures for MCX, Index for others)
                anchor_price = 0
                if self.atm_manager and self.atm_manager.orchestrator:
                    anchor_price = self.atm_manager.orchestrator.get_anchor_price() or 0
                else:
                    anchor_price = await self.rest_client.get_ltp(instrument_key)

                best_match = valid_keys[0]
                if anchor_price > 0:
                    min_dist = float('inf')
                    for k in valid_keys:
                        dist = abs(ltps[k] - anchor_price)
                        if dist < (anchor_price * 0.01) and dist < min_dist:
                            min_dist = dist
                            best_match = k
                update_callback(best_match)
        except Exception as e:
            logger.error(f"Probing failed for {instrument_name}: {e}")

    async def _discover_futures_from_csv(self, instrument_name, instrument_key, configured_key, update_callback):
        try:
            name_up = instrument_name.upper()
            if name_up == 'SENSEX':
                exchange = 'BSE_FO'
            elif any(x in name_up for x in ['CRUDE', 'NATURAL', 'GOLD', 'SILVER']):
                exchange = 'MCX'
            else:
                exchange = 'NSE_FO'
            url = f"https://assets.upstox.com/market-quote/instruments/exchange/{exchange}.csv.gz"
            cache_file = f"config/instruments_{exchange}.csv.gz"
            if not os.path.exists(cache_file) or (datetime.datetime.now().timestamp() - os.path.getmtime(cache_file)) > 86400:
                import aiohttp
                async with aiohttp.ClientSession() as session:
                    async with session.get(url, headers={"User-Agent": "Mozilla/5.0"}) as resp:
                        if resp.status == 200:
                            os.makedirs('config', exist_ok=True)
                            with open(cache_file, 'wb') as f: f.write(await resp.read())
                        else: return False
            df = pd.read_csv(cache_file, compression='gzip')
            # FUTCOM = MCX commodity futures; FUT/FUTIDX/FUTSTK = NSE/BSE equity/index futures
            fut_types = ['FUT', 'FUTIDX', 'FUTSTK', 'FUTCOM']
            # Column names differ across exchange CSVs: MCX uses 'tradingsymbol', NSE_FO uses 'trading_symbol'
            sym_col = 'trading_symbol' if 'trading_symbol' in df.columns else 'tradingsymbol'
            futures = pd.DataFrame()
            if 'underlying_key' in df.columns:
                futures = df[(df['instrument_type'].isin(fut_types)) & (df['underlying_key'] == instrument_key)].copy()
            if futures.empty:
                search_term = "bank" if "bank" in instrument_name.lower() else ("nifty" if "nifty" in instrument_name.lower() else instrument_name.lower())
                futures = df[(df['instrument_type'].isin(fut_types)) & (df[sym_col].str.lower().str.contains(search_term, na=False))].copy()
            if not futures.empty:
                futures['expiry'] = pd.to_datetime(futures['expiry'])
                futures = futures[futures['expiry'].dt.date >= datetime.date.today()].sort_values(by='expiry')
                # Prefer full-size contracts over mini (lot_size >= 100 vs 10)
                if 'lot_size' in futures.columns:
                    full = futures[futures['lot_size'] >= 100]
                    if not full.empty:
                        futures = full
                if not futures.empty:
                    new_key = futures.iloc[0]['instrument_key'] or f"{exchange}:{futures.iloc[0].get(sym_col)}"
                    if new_key:
                        logger.info(f"[FuturesManager] MCX CSV: discovered futures key {new_key} for {instrument_name}")
                        update_callback(new_key)
                        return True
        except Exception as e: logger.debug(f"CSV Discovery failed: {e}")
        return False
