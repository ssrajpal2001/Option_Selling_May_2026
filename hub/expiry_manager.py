import datetime
import calendar
from utils.logger import logger

class ExpiryManager:
    def __init__(self, all_contracts, near_expiry_date=None, monthly_expiries=None, config_manager=None):
        self.all_contracts = all_contracts
        self.near_expiry_date = near_expiry_date
        self.monthly_expiries = monthly_expiries or []
        self.config_manager = config_manager

    @staticmethod
    def get_last_thursday(year, month):
        """Returns the last Thursday (NIFTY monthly expiry) of the given month."""
        last_day = calendar.monthrange(year, month)[1]
        for day in range(last_day, last_day - 7, -1):
            candidate = datetime.date(year, month, day)
            if candidate.weekday() == 3:  # Thursday
                return candidate
        return None

    @staticmethod
    def is_monthly_expiry_date(d):
        """Returns True if date d is the last Thursday of its month (i.e., a monthly expiry)."""
        return d == ExpiryManager.get_last_thursday(d.year, d.month)

    def get_trade_expiry_date(self, today=None, mode='WEEKLY'):
        """Determines the target trade expiry date based on mode (WEEKLY, MONTHLY, EXPIRY, etc.)"""
        if today is None:
            today = datetime.date.today()
            # Backtest Heuristic: If all contracts are in the past, use the earliest expiry as "today"
            all_expiries = [c.expiry.date() for c in self.all_contracts if hasattr(c, 'expiry') and c.expiry]
            if all_expiries and max(all_expiries) < today:
                today = min(all_expiries)

        if hasattr(today, 'date'): today = today.date()

        # Filter to CE/PE only so that futures expiry dates don't pollute the list
        all_future_expiries = sorted(list({
            c.expiry.date() for c in self.all_contracts
            if hasattr(c, 'expiry') and c.expiry and c.expiry.date() >= today
            and getattr(c, 'instrument_type', '') in ['CE', 'PE']
        }))

        if not all_future_expiries:
            # Fallback for Backtest: If contracts exist but all are in the past (relative to today),
            # use the last available expiry as a proxy.
            all_expiries = sorted(list({
                c.expiry.date() for c in self.all_contracts
                if hasattr(c, 'expiry') and c.expiry and getattr(c, 'instrument_type', '') in ['CE', 'PE']
            }))
            if all_expiries:
                return all_expiries[-1]

            logger.error("No option expiries (CE/PE) found in contract master.")
            return None

        # Support hardcoded YYYY-MM-DD strings - validate that it has options!
        if isinstance(mode, str) and len(mode) == 10 and mode[4] == '-' and mode[7] == '-':
            try:
                dt = datetime.datetime.strptime(mode, '%Y-%m-%d').date()
                if dt in all_future_expiries:
                    return dt
                logger.warning(f"Requested trade expiry {mode} has no options (CE/PE). Falling back to nearest options expiry.")
                mode = 'WEEKLY' # Force fallback to nearest valid options expiry
            except ValueError:
                pass

        mode_upper = mode.upper()

        if mode_upper == 'MONTHLY':
            # Identify the latest available expiry in the current month as the "Monthly" expiry.
            # This handles both NSE (last Thursday) and MCX (various dates) correctly.
            first_month = all_future_expiries[0].month
            first_year = all_future_expiries[0].year
            month_expiries = [exp for exp in all_future_expiries if exp.month == first_month and exp.year == first_year]

            return month_expiries[-1] if month_expiries else all_future_expiries[0]

        # 'EXPIRY' is treated as 'WEEKLY' (nearest available expiry)
        expiry_map = {'WEEKLY': 0, 'EXPIRY': 0, 'NEXT_WEEK': 1, 'NEXT_TO_NEXT_WEEK': 2}
        idx = expiry_map.get(mode_upper, 0)

        return all_future_expiries[idx] if idx < len(all_future_expiries) else all_future_expiries[-1]

    def calculate_expiry_date(self, mode, today=None):
        """Helper to calculate expiry date for a given mode string."""
        if today is None:
            today = datetime.date.today()
            # Backtest Heuristic: If all contracts are in the past, use the earliest expiry as "today"
            all_expiries = [c.expiry.date() for c in self.all_contracts if hasattr(c, 'expiry') and c.expiry]
            if all_expiries and max(all_expiries) < today:
                today = min(all_expiries)

        if hasattr(today, 'date'): today = today.date()

        # CE/PE only — same filter as get_trade_expiry_date for consistency
        unique_weekly = sorted(list({
            c.expiry.date() for c in self.all_contracts
            if c.instrument_type in ['CE', 'PE'] and c.expiry.date() >= today
        }))

        if not unique_weekly:
            # Fallback for Backtest
            all_expiries = sorted(list({
                c.expiry.date() for c in self.all_contracts
                if getattr(c, 'instrument_type', '') in ['CE', 'PE']
            }))
            if all_expiries:
                return all_expiries[-1]

            logger.error("No option expiries (CE/PE) found in contract master.")
            return None

        # Support hardcoded YYYY-MM-DD strings - validate that it has options!
        if isinstance(mode, str) and len(mode) == 10 and mode[4] == '-' and mode[7] == '-':
            try:
                dt = datetime.datetime.strptime(mode, '%Y-%m-%d').date()
                if dt in unique_weekly:
                    return dt
                logger.warning(f"Requested signal expiry {mode} has no options (CE/PE). Falling back to nearest options expiry.")
                mode = 'WEEKLY' # Force fallback to nearest valid options expiry
            except ValueError:
                pass

        mode = mode.upper()

        # No inject-today: if today is an actual expiry day, the contract_manager supplement
        # will have already added today's contracts to all_contracts, so today will be in the list.
        # If today is NOT an expiry day, we must NOT inject it — use the real next expiry.
        expiry_map = {'CURRENT_WEEK': 0, 'WEEKLY': 0, 'EXPIRY': 0, 'NEXT_WEEK': 1, 'NEXT_TO_NEXT_WEEK': 2}
        if mode in expiry_map:
            idx = expiry_map[mode]
            return unique_weekly[idx] if idx < len(unique_weekly) else unique_weekly[-1]

        if mode == 'MONTHLY':
            curr_month, curr_year = unique_weekly[0].month, unique_weekly[0].year
            monthly_expiry = next((e for e in self.monthly_expiries if e.month == curr_month and e.year == curr_year), None)
            return monthly_expiry or self.near_expiry_date

        return self.near_expiry_date
