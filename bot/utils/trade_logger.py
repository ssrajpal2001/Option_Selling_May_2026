import csv
from datetime import date, datetime
import os
from threading import Lock


class TradeLogger:
    _instance = None
    _lock = Lock()

    def __new__(cls):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super(TradeLogger, cls).__new__(cls)
                cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        with self._lock:
            if self._initialized:
                return
            self.log_files = {}  # key -> {handle, writer, date}
            self.lock = Lock()
            self.config_manager = None
            self._initialized = True

    def setup(self, config_manager):
        with self.lock:
            self.config_manager = config_manager

    def _get_writer_for_instrument(self, instrument_name, user_id=None, broker=None):
        """
        Returns (writer, handle) for today's trade log for this instrument/user/broker.
        Creates a new file each calendar day and rotates automatically at midnight.
        Filename: trades_{INSTRUMENT}_user_{user_id}_{broker}_{YYYYMMDD}.csv
        """
        instrument_name = instrument_name.upper()
        today = date.today()
        today_str = today.strftime('%Y%m%d')
        broker_part = (broker or 'unknown').lower()
        user_part = str(user_id) if user_id else 'unknown'
        log_key = f"{instrument_name}_{user_part}_{broker_part}"

        entry = self.log_files.get(log_key)
        # Rotate if the stored entry is from a previous date
        if entry and entry.get('date') != today:
            try:
                entry['handle'].close()
            except Exception:
                pass
            del self.log_files[log_key]
            entry = None

        if entry is None:
            filename = f"trades_{instrument_name}_user_{user_part}_{broker_part}_{today_str}.csv"
            file_exists = os.path.exists(filename) and os.path.getsize(filename) > 0
            file_handle = open(filename, 'a', newline='')
            writer = csv.writer(file_handle)
            if not file_exists:
                writer.writerow([
                    "Timestamp", "Broker", "InstrumentSymbol", "TradeType",
                    "Price", "PNL", "Reason", "StrategyLog"
                ])
                file_handle.flush()
            self.log_files[log_key] = {'handle': file_handle, 'writer': writer, 'date': today}

        return self.log_files[log_key]['writer'], self.log_files[log_key]['handle']

    def log_entry(self, broker, instrument_name, instrument_symbol, trade_type, price, strategy_log="", user_id=None):
        with self.lock:
            writer, handle = self._get_writer_for_instrument(instrument_name, user_id, broker)
            writer.writerow([
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                broker,
                instrument_symbol,
                trade_type,
                price,
                "",
                "",
                strategy_log
            ])
            handle.flush()

    def log_exit(self, broker, instrument_name, instrument_symbol, trade_type, price, pnl, reason, strategy_log="", user_id=None):
        with self.lock:
            writer, handle = self._get_writer_for_instrument(instrument_name, user_id, broker)
            writer.writerow([
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                broker,
                instrument_symbol,
                trade_type,
                price,
                f"{pnl:.2f}",
                reason,
                strategy_log
            ])
            handle.flush()

    def shutdown(self):
        with self.lock:
            for data in self.log_files.values():
                if data.get('handle'):
                    data['handle'].close()
            self.log_files.clear()
