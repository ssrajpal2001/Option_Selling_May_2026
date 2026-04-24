from datetime import datetime


class LiveTradeLog:
    MAX_ENTRIES = 200

    def __init__(self):
        self.trades = []

    def add(self, trade_dict):
        self.trades.insert(0, trade_dict)
        if len(self.trades) > self.MAX_ENTRIES:
            self.trades = self.trades[:self.MAX_ENTRIES]

    def to_list(self, limit=50):
        return self.trades[:limit]

    @staticmethod
    def make_entry(trade_type, direction, strike, entry_price, exit_price,
                   pnl_pts, pnl_rs, reason, order_id='', timestamp=None, index_price=None,
                   entry_indicators=None, exit_indicators=None, entry_time=None):
        ts = timestamp or datetime.now()
        e_ts = entry_time or ts

        # Determine isoformat strings for DB-like consistency in UI
        opened_at = e_ts.isoformat() if hasattr(e_ts, 'isoformat') else str(e_ts)
        closed_at = ts.isoformat() if hasattr(ts, 'isoformat') else str(ts)

        return {
            'time': e_ts.strftime('%H:%M:%S') if hasattr(e_ts, 'strftime') else str(e_ts)[-8:],
            'date': e_ts.strftime('%Y-%m-%d') if hasattr(e_ts, 'strftime') else str(e_ts)[:10],
            'opened_at': opened_at,
            'closed_at': closed_at,
            'type': trade_type,
            'direction': direction,
            'strike': int(strike) if strike else 0,
            'entry_price': round(float(entry_price), 2) if entry_price else 0,
            'exit_price': round(float(exit_price), 2) if exit_price else 0,
            'pnl_pts': round(float(pnl_pts), 2) if pnl_pts is not None else 0,
            'pnl_rs': round(float(pnl_rs), 2) if pnl_rs is not None else 0,
            'entry_index_price': round(float(index_price), 2) if index_price else None,
            'reason': str(reason or ''),
            'order_id': str(order_id or ''),
            'entry_indicators': str(entry_indicators or '--'),
            'exit_indicators': str(exit_indicators or '--'),
        }
