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

def test_symbols_v2():
    # Adding parent dir to path to import brokers
    sys.path.append(os.getcwd())

    from brokers.papertrade_client import PaperTradeClient
    from utils.config_manager import ConfigManager
    from hub.state_manager import StateManager

    # Initialize config
    config = ConfigManager(config_file="config/config_trader.ini")

    # Initialize client
    client = PaperTradeClient("PaperTrade", config)

    # Manually initialize state_manager (normally done by Orchestrator)
    client.state_manager = StateManager(config, "NIFTY", is_backtest=True)

    # CASE 1: Test March 2026 Monthly Expiry
    # Mocking the monthly_expiries list to simulate March 30 as monthly.
    march_monthly = datetime.date(2026, 3, 30)
    client.state_manager.monthly_expiries = [march_monthly]

    # Test March 25 (Weekly)
    c_weekly = MockContract("NIFTY 50", datetime.date(2026, 3, 25), 23350, "PUT")
    sym_weekly = client.construct_zerodha_symbol(c_weekly)
    if sym_weekly == "NIFTY2632523350PE":
        print(f"PASS: March 25 is Weekly -> {sym_weekly}")
    else:
        print(f"FAIL: March 25 Weekly format wrong | Actual: {sym_weekly}")

    # Test March 30 (Monthly)
    c_monthly = MockContract("NIFTY 50", march_monthly, 23350, "PUT")
    sym_monthly = client.construct_zerodha_symbol(c_monthly)
    if sym_monthly == "NIFTY26MAR23350PE":
        print(f"PASS: March 30 is Monthly -> {sym_monthly}")
    else:
        print(f"FAIL: March 30 Monthly format wrong | Actual: {sym_monthly}")

    # CASE 2: Test mapping logic
    test_mappings = [
        ("NIFTY MID SELECT", "MIDCPNIFTY"),
        ("MIDCAP", "MIDCPNIFTY"),
        ("NIFTY FINANCIAL SERVICES", "FINNIFTY")
    ]
    for raw, expected in test_mappings:
        c = MockContract(raw, datetime.date(2026, 1, 1), 100, "CE")
        sym = client.construct_zerodha_symbol(c)
        if expected in sym:
            print(f"PASS: {raw} maps to {expected}")
        else:
            print(f"FAIL: {raw} mapping wrong | Actual: {sym}")

    # Final result check
    return ("NIFTY26325" in sym_weekly and "NIFTY26MAR" in sym_monthly)

if __name__ == "__main__":
    if test_symbols_v2():
        print("\nAll symbol V2 tests passed!")
        sys.exit(0)
    else:
        print("\nSymbol V2 tests failed!")
        sys.exit(1)
