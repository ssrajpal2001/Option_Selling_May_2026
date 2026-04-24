import datetime
import sys
import os

# Mock Contract class
class MockContract:
    def __init__(self, name, expiry, strike, instrument_type):
        self.name = name
        self.expiry = expiry
        self.strike_price = strike
        self.instrument_type = instrument_type

def test_symbols():
    # Adding parent dir to path to import brokers
    sys.path.append(os.getcwd())

    # We can't easily import ZerodhaClient because of its __init__ login,
    # but PaperTradeClient has the same logic and no complex init.
    from brokers.papertrade_client import PaperTradeClient
    from utils.config_manager import ConfigManager

    # Use config_trader.ini
    client = PaperTradeClient("PaperTrade", ConfigManager(config_file="config/config_trader.ini"))

    # Define test cases for March 2026
    # In March 2026, Holi (Mar 3), Ram Navami (Mar 26), and Mahavir Jayanti (Mar 31) are holidays.
    # Expiries usually shift to the previous day.

    test_cases = [
        # NIFTY Weekly
        {"name": "NIFTY 50", "expiry": datetime.date(2026, 3, 5), "strike": 23350, "type": "PUT", "expected": "NIFTY2630523350PE"},
        {"name": "NIFTY 50", "expiry": datetime.date(2026, 3, 12), "strike": 23350, "type": "PUT", "expected": "NIFTY2631223350PE"},
        {"name": "NIFTY 50", "expiry": datetime.date(2026, 3, 19), "strike": 23350, "type": "PUT", "expected": "NIFTY2631923350PE"},

        # NIFTY Monthly: Should be March 25 (Wednesday) because Mar 26 is holiday.
        {"name": "NIFTY 50", "expiry": datetime.date(2026, 3, 25), "strike": 23350, "type": "PUT", "expected": "NIFTY26MAR23350PE"},
        # Even if the expiry date is Mar 26 (e.g. metadata error), it's the last expiry of month.
        {"name": "NIFTY 50", "expiry": datetime.date(2026, 3, 26), "strike": 23350, "type": "PUT", "expected": "NIFTY26MAR23350PE"},

        # MIDCAP: March 30 (Monday) is likely the monthly expiry because Mar 31 is holiday.
        {"name": "MIDCAP", "expiry": datetime.date(2026, 3, 23), "strike": 13000, "type": "CALL", "expected": "MIDCPNIFTY2632313000CE"},
        {"name": "MIDCAP", "expiry": datetime.date(2026, 3, 30), "strike": 13000, "type": "CALL", "expected": "MIDCPNIFTY26MAR13000CE"},

        # October Weekly (Testing month char 'O')
        {"name": "NIFTY", "expiry": datetime.date(2026, 10, 1), "strike": 25000, "type": "CE", "expected": "NIFTY26O0125000CE"},
    ]

    success = True
    for tc in test_cases:
        contract = MockContract(tc['name'], tc['expiry'], tc['strike'], tc['type'])
        actual = client.construct_zerodha_symbol(contract)
        if actual == tc['expected']:
            print(f"PASS: {tc['name']} {tc['expiry']} -> {actual}")
        else:
            print(f"FAIL: {tc['name']} {tc['expiry']} | Expected: {tc['expected']} | Actual: {actual}")
            success = False

    return success

if __name__ == "__main__":
    if test_symbols():
        print("\nAll symbol tests passed!")
        sys.exit(0)
    else:
        print("\nSymbol tests failed!")
        sys.exit(1)
