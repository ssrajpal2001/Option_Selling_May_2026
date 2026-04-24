import pandas as pd
import numpy as np
import sys
import os

# Add project root to path
sys.path.append(os.getcwd())

from hub.indicators.roc import ROCIndicator

def test_roc_calculation():
    print("Testing ROC Logic...")

    # Sample data: 100, 101, 102, ... 110
    data = pd.Series([100 + i for i in range(11)])
    length = 9

    # PineScript: roc = 100 * (source - source[length])/source[length]
    # At index 10 (current), source[9] is index 1.
    # val_current = 110
    # val_past = 101
    # expected = 100 * (110 - 101) / 101 = 100 * 9 / 101 = 8.9108...

    expected = 100 * (110 - 101) / 101
    actual = ROCIndicator.get_latest_value(data, length)

    print(f"Data: {data.tolist()}")
    print(f"Length: {length}")
    print(f"Expected ROC: {expected:.4f}")
    print(f"Actual ROC:   {actual:.4f}")

    if abs(actual - expected) < 0.0001:
        print("SUCCESS: ROC calculation matches PineScript formula.")
    else:
        print("FAILED: ROC calculation mismatch.")
        return False

    # Test decreasing case
    data_dec = pd.Series([100, 105, 104, 103, 102, 101, 100, 99, 98, 97, 96])
    # Index 10: 96
    # Index 1: 105
    # expected = 100 * (96 - 105) / 105 = -8.5714...
    expected_dec = 100 * (96 - 105) / 105
    actual_dec = ROCIndicator.get_latest_value(data_dec, length)

    print(f"\nDecreasing Data: {data_dec.tolist()}")
    print(f"Expected ROC: {expected_dec:.4f}")
    print(f"Actual ROC:   {actual_dec:.4f}")

    if abs(actual_dec - expected_dec) < 0.0001:
        print("SUCCESS: Decreasing ROC matches.")
    else:
        print("FAILED: Decreasing ROC mismatch.")
        return False

    return True

if __name__ == "__main__":
    if test_roc_calculation():
        sys.exit(0)
    else:
        sys.exit(1)
