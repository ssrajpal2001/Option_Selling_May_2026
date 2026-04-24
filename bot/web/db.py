import os
import sqlite3
from pathlib import Path
from utils.logger import logger

_BASE_DIR = Path(__file__).parent.parent
DB_PATH = os.environ.get("ALGOSOFT_DB_PATH", str(_BASE_DIR / "config" / "algosoft.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    email TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT DEFAULT 'client',
    is_active INTEGER DEFAULT 0,
    subscription_tier TEXT DEFAULT 'FREE',
    max_broker_instances INTEGER DEFAULT 1,
    created_at TEXT DEFAULT (datetime('now')),
    activated_at TEXT,
    activated_by INTEGER,
    full_name TEXT,
    phone_number TEXT,
    static_ip TEXT,
    telegram_chat_id TEXT,
    referral_code TEXT UNIQUE,
    referred_by_id INTEGER REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS platform_settings (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at TEXT DEFAULT (datetime('now')),
    updated_by INTEGER
);

CREATE TABLE IF NOT EXISTS data_providers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL UNIQUE,
    api_key_encrypted TEXT,
    api_secret_encrypted TEXT,
    access_token_encrypted TEXT,
    status TEXT DEFAULT 'not_configured',
    updated_at TEXT DEFAULT (datetime('now')),
    updated_by INTEGER
);

CREATE TABLE IF NOT EXISTS client_broker_instances (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id INTEGER NOT NULL REFERENCES users(id),
    broker TEXT NOT NULL,
    instance_label TEXT,
    api_key_encrypted TEXT,
    api_secret_encrypted TEXT,
    access_token_encrypted TEXT,
    token_updated_at TEXT,
    trading_mode TEXT DEFAULT 'paper',
    instrument TEXT DEFAULT 'NIFTY',
    quantity INTEGER DEFAULT 25,
    strategy_version TEXT DEFAULT 'V3',
    status TEXT DEFAULT 'idle',
    bot_pid INTEGER,
    last_heartbeat TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE(client_id, broker)
);

CREATE TABLE IF NOT EXISTS trade_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    instance_id INTEGER NOT NULL REFERENCES client_broker_instances(id),
    client_id INTEGER NOT NULL REFERENCES users(id),
    trade_type TEXT,
    direction TEXT,
    strike INTEGER,
    entry_price REAL,
    exit_price REAL,
    pnl_pts REAL,
    pnl_rs REAL,
    quantity INTEGER,
    broker TEXT,
    exit_reason TEXT,
    instrument TEXT,
    trading_mode TEXT,
    opened_at TEXT,
    closed_at TEXT DEFAULT (datetime('now')),
    entry_index_price REAL
);

CREATE TABLE IF NOT EXISTS order_failures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    instance_id INTEGER NOT NULL REFERENCES client_broker_instances(id),
    client_id INTEGER NOT NULL REFERENCES users(id),
    order_side TEXT,
    broker_error TEXT,
    failure_reason TEXT,
    retry_attempt INTEGER DEFAULT 0,
    paired_leg_closed INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_id INTEGER,
    actor_role TEXT,
    action TEXT,
    target_type TEXT,
    target_id INTEGER,
    details TEXT,
    ip_address TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS broker_change_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id INTEGER NOT NULL REFERENCES users(id),
    current_broker TEXT NOT NULL,
    requested_broker TEXT NOT NULL,
    reason TEXT DEFAULT '',
    status TEXT DEFAULT 'pending',
    created_at TEXT DEFAULT (datetime('now')),
    resolved_at TEXT,
    resolved_by_id INTEGER
);

CREATE TABLE IF NOT EXISTS subscription_plans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_name TEXT UNIQUE NOT NULL,
    display_name TEXT NOT NULL,
    max_broker_instances INTEGER DEFAULT 1,
    description TEXT DEFAULT '',
    is_active INTEGER DEFAULT 1,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);
"""

_conn = None


def get_db() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA foreign_keys=ON")
        _migrate(_conn)
        _seed(_conn)
        logger.info(f"[DB] SQLite connected: {DB_PATH}")
    return _conn


def _migrate(conn: sqlite3.Connection):
    conn.executescript(SCHEMA)
    user_cols = [row[1] for row in conn.execute("PRAGMA table_info(users)").fetchall()]
    if "full_name" not in user_cols:
        conn.execute("ALTER TABLE users ADD COLUMN full_name TEXT")
    if "phone_number" not in user_cols:
        conn.execute("ALTER TABLE users ADD COLUMN phone_number TEXT")

    dp_cols = [row[1] for row in conn.execute("PRAGMA table_info(data_providers)").fetchall()]
    if "api_secret_encrypted" not in dp_cols:
        conn.execute("ALTER TABLE data_providers ADD COLUMN api_secret_encrypted TEXT")
    if "user_id_encrypted" not in dp_cols:
        conn.execute("ALTER TABLE data_providers ADD COLUMN user_id_encrypted TEXT")
    if "password_encrypted" not in dp_cols:
        conn.execute("ALTER TABLE data_providers ADD COLUMN password_encrypted TEXT")
    if "totp_encrypted" not in dp_cols:
        conn.execute("ALTER TABLE data_providers ADD COLUMN totp_encrypted TEXT")
    if "token_issued_at" not in dp_cols:
        # Tracks when the access_token was first obtained — separate from updated_at.
        # For Dhan (30-day tokens): used to compute remaining life; never reset by validation-only runs.
        # For Upstox (daily tokens): reset each time a new token is fetched.
        conn.execute("ALTER TABLE data_providers ADD COLUMN token_issued_at TEXT")

    # One-time backfill: for existing records that have a token but no token_issued_at,
    # seed token_issued_at from updated_at so expiry calculations are stable immediately.
    conn.execute(
        "UPDATE data_providers SET token_issued_at = updated_at "
        "WHERE access_token_encrypted IS NOT NULL AND token_issued_at IS NULL"
    )

    existing = [row[1] for row in conn.execute("PRAGMA table_info(client_broker_instances)").fetchall()]
    if "api_secret_encrypted" not in existing:
        conn.execute("ALTER TABLE client_broker_instances ADD COLUMN api_secret_encrypted TEXT")
    if "token_updated_at" not in existing:
        conn.execute("ALTER TABLE client_broker_instances ADD COLUMN token_updated_at TEXT")
    if "password_encrypted" not in existing:
        conn.execute("ALTER TABLE client_broker_instances ADD COLUMN password_encrypted TEXT")
    if "totp_encrypted" not in existing:
        conn.execute("ALTER TABLE client_broker_instances ADD COLUMN totp_encrypted TEXT")
    if "broker_user_id_encrypted" not in existing:
        conn.execute("ALTER TABLE client_broker_instances ADD COLUMN broker_user_id_encrypted TEXT")

    trade_cols = [row[1] for row in conn.execute("PRAGMA table_info(trade_history)").fetchall()]
    if "entry_index_price" not in trade_cols:
        conn.execute("ALTER TABLE trade_history ADD COLUMN entry_index_price REAL")
    if "entry_indicators" not in trade_cols:
        conn.execute("ALTER TABLE trade_history ADD COLUMN entry_indicators TEXT")
    if "exit_indicators" not in trade_cols:
        conn.execute("ALTER TABLE trade_history ADD COLUMN exit_indicators TEXT")

    # Migration: add subscription_plans table columns safety check
    plan_cols = [row[1] for row in conn.execute("PRAGMA table_info(subscription_plans)").fetchall()]
    if plan_cols and "description" not in plan_cols:
        conn.execute("ALTER TABLE subscription_plans ADD COLUMN description TEXT DEFAULT ''")
    if plan_cols and "is_active" not in plan_cols:
        conn.execute("ALTER TABLE subscription_plans ADD COLUMN is_active INTEGER DEFAULT 1")
    if plan_cols and "price_monthly" not in plan_cols:
        conn.execute("ALTER TABLE subscription_plans ADD COLUMN price_monthly REAL DEFAULT 0")

    # Migration: add plan_id and plan_expiry_date to users
    if "plan_id" not in user_cols:
        conn.execute("ALTER TABLE users ADD COLUMN plan_id INTEGER REFERENCES subscription_plans(id)")
    if "plan_expiry_date" not in user_cols:
        conn.execute("ALTER TABLE users ADD COLUMN plan_expiry_date TEXT")

    # Migration: user profile extensions
    for col, defn in [
        ("static_ip",       "TEXT"),
        ("telegram_chat_id","TEXT"),
        ("referral_code",   "TEXT"),
        ("referred_by_id",  "INTEGER"),
    ]:
        if col not in user_cols:
            conn.execute(f"ALTER TABLE users ADD COLUMN {col} {defn}")

    # Migration: broker instance risk/lock columns
    inst_cols = {row[1] for row in conn.execute("PRAGMA table_info(client_broker_instances)")}
    for col, defn in [
        ("client_strategy_overrides", "TEXT"),
        ("trading_locked_until",      "TEXT"),
        ("daily_loss_limit",          "REAL DEFAULT 0"),
    ]:
        if col not in inst_cols:
            conn.execute(f"ALTER TABLE client_broker_instances ADD COLUMN {col} {defn}")

    # Ensure platform_settings table exists (created in SCHEMA but guard for old DBs)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS platform_settings (
            key TEXT PRIMARY KEY,
            value TEXT,
            updated_at TEXT DEFAULT (datetime('now')),
            updated_by INTEGER
        )
    """)

    conn.commit()


def _seed(conn: sqlite3.Connection):
    from web.auth import hash_password
    existing = conn.execute("SELECT id FROM users WHERE role='admin'").fetchone()
    if not existing:
        ph = hash_password("Admin@123")
        conn.execute(
            "INSERT INTO users (username, email, password_hash, role, is_active) VALUES (?,?,?,?,?)",
            ("admin", "admin@algosoft.com", ph, "admin", 1)
        )
        conn.commit()
        logger.info("[DB] Default admin created (admin / Admin@123)")

    conn.execute(
        "INSERT OR IGNORE INTO data_providers (provider, status) VALUES ('upstox', 'not_configured')"
    )
    conn.execute(
        "INSERT OR IGNORE INTO data_providers (provider, status) VALUES ('dhan', 'not_configured')"
    )

    # Seed default subscription plans (use REPLACE only for new seeds; keep existing data intact)
    for row in [
        ('BASIC',      'Basic',      1,   0,    'Starter — 1 broker connection'),
        ('STANDARD',   'Standard',   2,   999,  'Standard — up to 2 broker connections'),
        ('PREMIUM',    'Premium',    3,   1999, 'Premium — up to 3 simultaneous brokers'),
        ('ENTERPRISE', 'Enterprise', 999, 4999, 'Enterprise — unlimited broker connections'),
        # Legacy names kept for backward compat
        ('FREE', 'Free', 1, 0, 'Basic access with 1 broker connection'),
        ('PRO',  'Pro',  5, 2999, 'Pro access with up to 5 simultaneous brokers'),
    ]:
        conn.execute("""
            INSERT OR IGNORE INTO subscription_plans
                (plan_name, display_name, max_broker_instances, price_monthly, description)
            VALUES (?,?,?,?,?)
        """, row)

    # Ensure price_monthly column exists on plans already seeded without it
    try:
        conn.execute("UPDATE subscription_plans SET price_monthly=999 WHERE plan_name='STANDARD' AND price_monthly=0 AND plan_name!='FREE'")
    except Exception:
        pass

    # Backfill: assign plan_id to clients that don't have one yet
    # Match on subscription_tier → plan_name; fall back to BASIC
    conn.execute("""
        UPDATE users SET plan_id = (
            SELECT sp.id FROM subscription_plans sp
            WHERE sp.plan_name = users.subscription_tier AND sp.is_active = 1
            LIMIT 1
        )
        WHERE plan_id IS NULL AND role = 'client'
    """)
    # Any still-null → assign BASIC
    conn.execute("""
        UPDATE users SET plan_id = (
            SELECT id FROM subscription_plans WHERE plan_name = 'BASIC' LIMIT 1
        )
        WHERE plan_id IS NULL AND role = 'client'
    """)
    conn.commit()


def db_fetchone(sql: str, params=()):
    row = get_db().execute(sql, params).fetchone()
    return dict(row) if row else None


def db_fetchall(sql: str, params=()):
    rows = get_db().execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def db_execute(sql: str, params=()):
    conn = get_db()
    try:
        cur = conn.execute(sql, params)
        conn.commit()
        return cur.lastrowid
    except Exception as e:
        conn.rollback()
        logger.error(f"[DB] Execute failed: {sql} | Error: {e}")
        raise
