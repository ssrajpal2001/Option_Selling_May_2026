import json
import sqlite3

def check():
    conn = sqlite3.connect('config/algosoft.db')
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT provider, status FROM data_providers").fetchall()
    print(json.dumps([dict(r) for r in rows], indent=2))
    conn.close()

if __name__ == "__main__":
    check()
