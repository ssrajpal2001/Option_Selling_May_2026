import pandas as pd
import os

atp_file = "atp_data_NIFTY_2026-03-25.csv"
market_file = "market_data_NIFTY_2026-03-25.csv"

if os.path.exists(atp_file):
    df_atp = pd.read_csv(atp_file)
    print("ATP Keys:", df_atp['instrument_key'].unique()[:10])
    print("ATP Strikes available:", df_atp['strike'].dropna().unique())

if os.path.exists(market_file):
    df_mkt = pd.read_csv(market_file)
    if 'ce_symbol' in df_mkt.columns:
        print("Market CE Keys:", df_mkt['ce_symbol'].unique()[:10])
    if 'ce_strike' in df_mkt.columns:
        print("Market CE Strikes:", df_mkt['ce_strike'].unique())
