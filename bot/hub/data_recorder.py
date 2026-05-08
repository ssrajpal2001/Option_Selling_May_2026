import csv
import os
import datetime
import threading
import queue
from utils.logger import logger

class DataRecorder:
    """
    NON-BLOCKING DATA RECORDER:
    Offloads all disk I/O to a background thread to prevent asyncio loop stalls.
    Records market data for ATM +/- 10 strikes to date-named CSV files.
    """
    def __init__(self, instrument_name):
        self.instrument_name = instrument_name
        self._io_queue = queue.Queue()
        self._running = True

        # Start background worker thread
        self._worker_thread = threading.Thread(target=self._io_worker, daemon=True, name=f"IO_Recorder_{instrument_name}")
        self._worker_thread.start()

        logger.info(f"Initialized non-blocking DataRecorder for {instrument_name}")

    def _io_worker(self):
        """Dedicated thread for handling all disk writes."""
        while self._running:
            try:
                # Wait for data with a timeout to allow checking self._running
                item = self._io_queue.get(timeout=1.0)
                if not item: continue

                type, data = item
                if type == 'TICKS':
                    self._execute_record_ticks(data)
                elif type == 'ATP':
                    self._execute_record_atp(data)

                self._io_queue.task_done()
            except queue.Empty:
                continue
            except Exception as e:
                logger.error(f"IO Worker Error for {self.instrument_name}: {e}")

    def record_ticks(self, timestamp, spot_price, index_price, atm_strike, watchlist_data):
        """Public async-safe method: pushes data to queue and returns instantly."""
        # We take a snapshot of the data to ensure thread safety
        # Since watchlist_data usually contains simple dicts/floats, shallow copy is fine
        data_snapshot = {
            'ts': timestamp, 'spot': spot_price, 'idx': index_price,
            'atm': atm_strike, 'watchlist': watchlist_data
        }
        self._io_queue.put(('TICKS', data_snapshot))

    def record_atp_snapshot(self, minute_ts, instrument_key, strike, side, atp, ltp, spot_price, futures_price, extra_indicators=None):
        """Public async-safe method: pushes data to queue and returns instantly."""
        data_snapshot = {
            'ts': minute_ts, 'key': instrument_key, 'strike': strike, 'side': side,
            'atp': atp, 'ltp': ltp, 'spot': spot_price, 'futures': futures_price, 'extra': extra_indicators
        }
        self._io_queue.put(('ATP', data_snapshot))

    def _execute_record_ticks(self, d):
        # Use the data's own timestamp for the filename to support multi-day backtest recording
        data_date = d['ts'].date().isoformat() if hasattr(d['ts'], 'date') else datetime.date.today().isoformat()
        filename = f"market_data_{self.instrument_name}_{data_date}.csv"

        # Ensure path is absolute relative to bot root
        _bot_root = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
        _bt_dir = os.path.join(_bot_root, 'backtest_data')
        os.makedirs(_bt_dir, exist_ok=True)
        filepath = os.path.join(_bt_dir, filename)

        headers = [
            'timestamp', 'spot_price', 'index_price', 'atm_strike',
            'ce_strike', 'ce_symbol', 'ce_ltp', 'ce_delta', 'ce_vega', 'ce_theta', 'ce_gamma', 'ce_open', 'ce_high', 'ce_low', 'ce_close',
            'pe_strike', 'pe_symbol', 'pe_ltp', 'pe_delta', 'pe_vega', 'pe_theta', 'pe_gamma', 'pe_open', 'pe_high', 'pe_low', 'pe_close'
        ]

        if not os.path.exists(filepath):
            with open(filepath, 'w', newline='') as f:
                csv.writer(f).writerow(headers)

        try:
            with open(filepath, 'a', newline='') as f:
                writer = csv.writer(f)
                for strike, data in d['watchlist'].items():
                    row = [
                        d['ts'].isoformat(), d['spot'], d['idx'], d['atm'],
                        strike, data.get('ce_symbol', ''), data.get('ce_ltp', ''), data.get('ce_delta', ''),
                        data.get('ce_vega', ''), data.get('ce_theta', ''), data.get('ce_gamma', ''),
                        data.get('ce_open', ''), data.get('ce_high', ''), data.get('ce_low', ''), data.get('ce_close', ''),
                        strike, data.get('pe_symbol', ''), data.get('pe_ltp', ''), data.get('pe_delta', ''),
                        data.get('pe_vega', ''), data.get('pe_theta', ''), data.get('pe_gamma', ''),
                        data.get('pe_open', ''), data.get('pe_high', ''), data.get('pe_low', ''), data.get('pe_close', '')
                    ]
                    writer.writerow(row)
        except Exception as e:
            logger.error(f"Tick Write Failure: {e}")

    def _execute_record_atp(self, d):
        # Use the data's own timestamp for the filename
        data_date = d['ts'].date().isoformat() if hasattr(d['ts'], 'date') else datetime.date.today().isoformat()
        atp_filename = f"atp_data_{self.instrument_name}_{data_date}.csv"

        _bot_root = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
        _bt_dir = os.path.join(_bot_root, 'backtest_data')
        os.makedirs(_bt_dir, exist_ok=True)
        atp_filepath = os.path.join(_bt_dir, atp_filename)

        if not os.path.exists(atp_filepath):
            with open(atp_filepath, 'w', newline='') as f:
                csv.writer(f).writerow([
                    'minute_ts', 'instrument_key', 'strike', 'side',
                    'atp', 'ltp', 'spot_price', 'futures_price', 'rsi', 'roc', 'v_slope', 'combined_vwap'
                ])

        rsi_val = d['extra'].get('rsi', '') if d['extra'] else ''
        roc_val = d['extra'].get('roc', '') if d['extra'] else ''
        v_slope_val = d['extra'].get('v_slope', '') if d['extra'] else ''
        combined_vwap_val = d['extra'].get('combined_vwap', '') if d['extra'] else ''

        try:
            with open(atp_filepath, 'a', newline='') as f:
                csv.writer(f).writerow([
                    d['ts'].isoformat() if hasattr(d['ts'], 'isoformat') else str(d['ts']),
                    d['key'], d['strike'], d['side'], d['atp'], d['ltp'], d['spot'], d['futures'],
                    rsi_val, roc_val, v_slope_val, combined_vwap_val
                ])
        except Exception as e:
            logger.error(f"ATP Write Failure: {e}")

    def stop(self):
        self._running = False
        if self._worker_thread.is_alive():
            self._worker_thread.join(timeout=2.0)
