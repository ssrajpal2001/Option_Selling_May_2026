from abc import ABC, abstractmethod
import asyncio
import socket
import threading
from contextlib import contextmanager
from utils.trade_logger import TradeLogger
from utils.logger import logger

# ── Source-IP helpers ─────────────────────────────────────────────────────────
#
# Two strategies are used, in order of preference:
#
# 1. SourceIPHTTPAdapter (urllib3-native, safest)
#    Mount on a broker SDK's requests.Session at __init__ time.  urllib3 will
#    then use source_address on every connection it opens through that session.
#    Used for: Zerodha (kiteconnect.reqsession), Dhan, AngelOne, Groww.
#
# 2. _scoped_ip_patch() context manager (mutex-scoped temporary patch)
#    For SDKs that do NOT expose a requests.Session (pya3 / AliceBlue,
#    fyers-apiv3, upstox auth path via requests).  The context manager:
#      a. Acquires a process-wide lock so only one such patched call runs at
#         a time (prevents cross-thread interference).
#      b. Temporarily replaces socket.create_connection AND
#         urllib3.util.connection.create_connection for the duration of the
#         block, then atomically restores both.
#    Callers wrap just the SDK call — no global state persists after the block.

# Save originals once at import time (never overwritten at module level).
_orig_socket_cc = socket.create_connection
try:
    import urllib3.util.connection as _urllib3_conn
    _orig_urllib3_cc = _urllib3_conn.create_connection
    _HAS_URLLIB3 = True
except Exception:
    _urllib3_conn = None
    _orig_urllib3_cc = None
    _HAS_URLLIB3 = False

# Serialises all scoped patches so that concurrent broker initialisations with
# different static IPs cannot interfere with each other's temporary patches.
_SOURCE_IP_PATCH_LOCK = threading.Lock()

# ── AWS NAT / Elastic IP detection ────────────────────────────────────────────
#
# On AWS EC2 the Elastic IP is NOT bound to a local interface — outbound traffic
# is NAT'd to the EIP at the VPC level. socket.bind(EIP) therefore raises
# [Errno 99] EADDRNOTAVAIL even though all outbound packets DO leave via the EIP.
#
# `_nat_detected` is a module-level result cache so the IMDS call is not made
# on every order placement when NAT is already confirmed.
#
#   Key present (True)  — confirmed behind AWS NAT; result cached permanently.
#   Key absent          — result unknown; False is never cached so transient
#                         IMDS failures are retried on the next call.
#
# Keyed by source_ip so processes that manage brokers with different static IPs
# (uncommon but possible) each get an independent IMDS result.

_nat_detected: 'dict[str, bool]' = {}


def _check_aws_nat(source_ip: str) -> bool:
    """
    Return True if we are running on an AWS EC2 instance whose public Elastic IP
    matches *source_ip*.

    Method: query the Instance Metadata Service at the link-local address
    169.254.169.254 with a tight 0.3-second timeout.  IMDSv2 is tried first
    (required on instances with enforce-IMDSv2 enabled); falls back to a plain
    IMDSv1 GET if the PUT token exchange fails.  If the returned public-ipv4
    equals source_ip we are behind NAT and socket.bind() will always fail for
    that IP — but orders still route through the EIP correctly via AWS NAT.

    Only True results are cached per source_ip.  False (IMDS failure / IP
    mismatch) is NOT cached so transient failures are retried on the next call.
    Thread-safe for CPython (dict key assignment is atomic).
    """
    if _nat_detected.get(source_ip):
        return True

    result = False
    try:
        import urllib.request as _ureq

        # Step 1 — try IMDSv2 (PUT for session token, then GET with token header)
        meta_req = None
        try:
            token_req = _ureq.Request(
                'http://169.254.169.254/latest/api/token',
                data=b'',
                method='PUT',
                headers={'X-aws-ec2-metadata-token-ttl-seconds': '21600'},
            )
            with _ureq.urlopen(token_req, timeout=0.3) as tr:
                imds_token = tr.read().decode().strip()
            meta_req = _ureq.Request(
                'http://169.254.169.254/latest/meta-data/public-ipv4',
                headers={'X-aws-ec2-metadata-token': imds_token},
            )
        except Exception:
            # IMDSv2 token fetch failed — fall back to IMDSv1 plain GET
            meta_req = _ureq.Request(
                'http://169.254.169.254/latest/meta-data/public-ipv4',
            )

        # Step 2 — fetch public-ipv4 and compare
        with _ureq.urlopen(meta_req, timeout=0.3) as resp:
            public_ip = resp.read().decode().strip()
        result = (public_ip == source_ip)
    except Exception:
        # IMDS not reachable (non-EC2) or timed out — assume not behind NAT
        result = False

    # Only cache positive confirmations; False is retried on the next call.
    if result:
        _nat_detected[source_ip] = True
    return result


def _is_bindable(ip: str) -> bool:
    """Quick non-raising test: can we bind a UDP socket to *ip* locally?"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.bind((ip, 0))
        return True
    except OSError:
        return False
    finally:
        s.close()


@contextmanager
def _scoped_socket_patch(source_ip: str):
    """
    Temporarily patch socket.create_connection (and urllib3's equivalent) to
    bind outbound connections to *source_ip*.  The patch is active only for the
    duration of the ``with`` block and is protected by a process-wide lock so
    that concurrent callers with different IPs do not interfere.
    """
    def _patched_socket_cc(address, timeout=socket.getdefaulttimeout(),
                           source_address=None):
        return _orig_socket_cc(address, timeout, source_address or (source_ip, 0))

    def _patched_urllib3_cc(address, timeout=socket.getdefaulttimeout(),
                            source_address=None, socket_options=None):
        return _orig_urllib3_cc(address, timeout,
                                source_address or (source_ip, 0),
                                socket_options)

    with _SOURCE_IP_PATCH_LOCK:
        socket.create_connection = _patched_socket_cc
        if _HAS_URLLIB3:
            _urllib3_conn.create_connection = _patched_urllib3_cc
        try:
            yield
        finally:
            socket.create_connection = _orig_socket_cc
            if _HAS_URLLIB3:
                _urllib3_conn.create_connection = _orig_urllib3_cc


# ── SourceIPHTTPAdapter ───────────────────────────────────────────────────────
try:
    from requests.adapters import HTTPAdapter as _HTTPAdapter

    class SourceIPHTTPAdapter(_HTTPAdapter):
        """
        Requests adapter that routes ALL outbound connections through a specific
        local IP address by configuring urllib3's connection pool with
        source_address=(ip, 0).

        This is the correct urllib3-native approach: mount once on the broker
        SDK's requests.Session and every HTTP call (auth, instruments, orders,
        funds) will automatically use the assigned Elastic IP — no per-call
        wrapping required.

        For SDKs that bypass requests entirely, use _scoped_ip_patch() instead.
        """
        def __init__(self, source_ip: str, **kwargs):
            self.source_ip = source_ip
            super().__init__(**kwargs)

        def init_poolmanager(self, *args, **kwargs):
            kwargs['source_address'] = (self.source_ip, 0)
            super().init_poolmanager(*args, **kwargs)

        def proxy_manager_for(self, proxy, **kwargs):
            kwargs['source_address'] = (self.source_ip, 0)
            return super().proxy_manager_for(proxy, **kwargs)

except ImportError:
    SourceIPHTTPAdapter = None  # type: ignore


# ── Instrument name normalisation map ────────────────────────────────────────
_INSTRUMENT_NAME_MAP = {
    "NIFTY 50": "NIFTY",
    "NIFTY50": "NIFTY",
    "NIFTY": "NIFTY",
    "NIFTY BANK": "BANKNIFTY",
    "NIFTYBANK": "BANKNIFTY",
    "BANKNIFTY": "BANKNIFTY",
    "NIFTY FINANCIAL SERVICES": "FINNIFTY",
    "NIFTY FIN SERVICE": "FINNIFTY",
    "FINNIFTY": "FINNIFTY",
    "NIFTY MIDCAP SELECT": "MIDCPNIFTY",
    "NIFTY MID SELECT": "MIDCPNIFTY",
    "MIDCAP NIFTY": "MIDCPNIFTY",
    "MIDCPNIFTY": "MIDCPNIFTY",
    "SENSEX": "SENSEX",
    "BANKEX": "BANKEX",
}

class BaseBroker(ABC):
    """
    Abstract base class for all broker clients.
    Defines the standard interface for broker-specific operations.
    """
    def __init__(self, instance_name, config_manager, user_id=None, db_config=None):
        self.instance_name = instance_name
        self.config_manager = config_manager
        self.user_id = user_id
        self.db_config = db_config
        self.state_manager = None
        self.is_connected = False
        self.broker_name = None
        self.trade_logger = TradeLogger()
        self._load_config()

    def _load_config(self):
        """Loads broker-specific configuration from DB (multi-tenant) or INI (legacy)."""
        global_mode = self.config_manager.get('settings', 'trading_mode', fallback='paper')

        if self.db_config:
            self.mode = self.db_config.get('mode', global_mode)
            self.paper_trade = str(self.mode).lower() == 'paper'
            settings = self.db_config.get('broker_settings', {})
            instruments_str = settings.get('instruments_to_trade', '')
            self.instruments = {i.strip().upper() for i in instruments_str.split(',') if i.strip()}
            self.api_key = self.db_config.get('api_key')
            self.api_secret = self.db_config.get('api_secret')
            self.source_ip = self.db_config.get('static_ip') or None
        else:
            self.mode = self.config_manager.get(self.instance_name, 'mode', fallback=global_mode)
            self.paper_trade = str(self.mode).lower() == 'paper'
            instruments_str = self.config_manager.get(self.instance_name, 'instruments_to_trade', fallback='')
            self.instruments = {i.strip().upper() for i in instruments_str.split(',') if i.strip()}
            self.source_ip = None

    def _install_source_ip_adapter(self, session):
        """
        Mount a SourceIPHTTPAdapter on a requests.Session so that every HTTP
        call routed through that session uses self.source_ip as the local
        outbound address.  Safe when source_ip is None or session is None.

        On AWS EC2 with an Elastic IP the IP is NOT a local interface, so
        urllib3 socket.bind() inside SourceIPHTTPAdapter would fail with
        [Errno -9] when attempting DNS resolution (address family mismatch).
        We skip the adapter in that case — orders still route via the EIP
        through AWS NAT without any explicit source binding.
        """
        if not self.source_ip or not session or SourceIPHTTPAdapter is None:
            return
        # Skip adapter if the IP is not locally bindable (e.g. AWS NAT / EIP).
        # Mounting SourceIPHTTPAdapter when the IP isn't a local interface causes
        # urllib3 to fail at DNS resolution with [Errno -9] instead of connecting.
        if not _is_bindable(self.source_ip):
            if _check_aws_nat(self.source_ip):
                logger.info(
                    f"[{self.instance_name}] Behind AWS NAT — SourceIPHTTPAdapter "
                    f"skipped; orders route via EIP {self.source_ip}."
                )
            else:
                logger.info(
                    f"[{self.instance_name}] {self.source_ip} is not bindable locally "
                    f"— SourceIPHTTPAdapter skipped."
                )
            return
        try:
            adapter = SourceIPHTTPAdapter(self.source_ip)
            session.mount("https://", adapter)
            session.mount("http://", adapter)
            logger.info(
                f"[{self.instance_name}] SourceIPHTTPAdapter mounted "
                f"(source IP: {self.source_ip})"
            )
        except Exception as exc:
            logger.warning(
                f"[{self.instance_name}] Could not mount SourceIPHTTPAdapter: {exc}"
            )

    def _validate_source_ip(self) -> None:
        """
        Lightweight pre-flight check: confirm that self.source_ip is either
        directly bindable on this machine, or routed via AWS NAT (Elastic IP).

        On AWS EC2 the Elastic IP is NOT a local interface — the socket.bind()
        attempt will raise [Errno 99] EADDRNOTAVAIL.  When this happens we
        consult the AWS Instance Metadata Service (IMDS) to verify that the
        instance's public-ipv4 matches self.source_ip.  If it does, the
        pre-flight check passes (NAT will route orders through the EIP).

        If the bind fails AND IMDS doesn't confirm a matching EIP:
          1. Logs the error at ERROR level.
          2. Fires a Telegram alert to the admin.
          3. Raises RuntimeError so the caller can block the order.

        No-op when source_ip is not configured.
        """
        if not self.source_ip:
            return

        import time
        t0 = time.monotonic()
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                s.bind((self.source_ip, 0))
            finally:
                s.close()
            elapsed_ms = (time.monotonic() - t0) * 1000
            logger.debug(
                f"[{self.instance_name}] Static IP {self.source_ip} validated "
                f"({elapsed_ms:.1f} ms)."
            )
            self._clear_ip_failure()  # clear any previous failure record
        except OSError as exc:
            import errno as _errno
            elapsed_ms = (time.monotonic() - t0) * 1000
            if exc.errno in (_errno.EADDRNOTAVAIL, _errno.EAFNOSUPPORT, 99, -9):
                # bind() failed — check whether we're behind AWS NAT
                if _check_aws_nat(self.source_ip):
                    logger.info(
                        f"[{self.instance_name}] Running behind AWS NAT. "
                        f"EIP {self.source_ip} validated via instance metadata "
                        f"— order routing OK ({elapsed_ms:.1f} ms)."
                    )
                    self._clear_ip_failure()  # clear any previous failure record
                    return  # NAT confirmed — do NOT raise
            # Not behind NAT (or IMDS unreachable / IP mismatch) — block the order
            err_msg = (
                f"[{self.instance_name}] Static IP {self.source_ip} is NOT bound "
                f"to this machine — order blocked ({elapsed_ms:.1f} ms). Error: {exc}"
            )
            logger.error(err_msg)
            self._alert_admin_ip_failure(exc)
            raise RuntimeError(err_msg) from exc

    def _clear_ip_failure(self) -> None:
        """Clear ip_last_failed_at in DB after a successful IP validation."""
        if not (self.user_id and self.broker_name):
            return
        try:
            from web.db import db_execute
            db_execute(
                "UPDATE client_broker_instances SET ip_last_failed_at=NULL "
                "WHERE client_id=? AND broker=?",
                (self.user_id, self.broker_name),
            )
        except Exception as db_exc:
            logger.debug(f"[IP-conflict] DB clear failed: {db_exc}")

    def _alert_admin_ip_failure(self, exc: Exception) -> None:
        """
        Send a Telegram alert to the admin when the static IP binding check
        fails.  Uses force=True so the alert fires even when Telegram alerts
        are globally disabled.
        """
        # Record the failure timestamp in the DB so the admin dashboard can
        # surface an IP-conflict warning banner (Task #151).
        # Use plain UTC format so SQLite datetime() comparisons work correctly.
        try:
            from web.db import db_execute
            from datetime import datetime
            _now = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
            if self.user_id and self.broker_name:
                db_execute(
                    "UPDATE client_broker_instances SET ip_last_failed_at=? "
                    "WHERE client_id=? AND broker=?",
                    (_now, self.user_id, self.broker_name),
                )
        except Exception as db_exc:
            logger.warning(f"[IP-conflict] DB write failed: {db_exc}")

        try:
            from utils.notifier import send_telegram, get_admin_chat_id
            chat_id = get_admin_chat_id()
            if not chat_id:
                logger.warning(
                    "[Telegram] Static IP failure alert: no admin chat_id configured — skipping."
                )
                return
            alert = (
                f"🚨 <b>Static IP Binding Failed — AlgoSoft</b>\n\n"
                f"<b>Instance:</b> <code>{self.instance_name}</code>\n"
                f"<b>Expected IP:</b> <code>{self.source_ip}</code>\n"
                f"<b>Error:</b> {exc}\n\n"
                f"The Elastic IP is <b>not attached</b> to this machine. "
                f"All orders for this instance are <b>blocked</b> until the "
                f"IP is re-attached.\n\n"
                f"<i>Action: AWS Console → EC2 → Elastic IPs → re-associate.</i>"
            )
            send_telegram(chat_id, alert, force=True)
        except Exception as notify_exc:
            logger.warning(
                f"[Telegram] Static IP alert dispatch failed: {notify_exc}"
            )

    @contextmanager
    def _scoped_ip_patch(self):
        """
        Context manager for broker SDK calls that do NOT go through a
        requests.Session (e.g. pya3 / AliceBlue, fyers-apiv3, upstox auth).

        When source_ip is set:
          1. Validates the IP is actually bound to this machine via
             _validate_source_ip() — raises RuntimeError and alerts admin if not.
          2. Temporarily patches socket.create_connection and urllib3's
             create_connection for the duration of the ``with`` block using a
             process-wide lock.

        No-op when source_ip is None.
        """
        if self.source_ip:
            self._validate_source_ip()
            with _scoped_socket_patch(self.source_ip):
                yield
        else:
            yield

    def is_configured_for_instrument(self, instrument_name):
        """Checks if this broker instance is configured to trade the given instrument."""
        return instrument_name.upper() in self.instruments

    def set_state_manager(self, state_manager):
        """Receives the shared StateManager instance."""
        self.state_manager = state_manager

    def _normalize_instrument_name(self, raw_name: str) -> str:
        """Normalize an instrument name to the short broker-compatible form."""
        upper = (raw_name or "NIFTY").strip().upper()
        return _INSTRUMENT_NAME_MAP.get(upper, upper)

    @abstractmethod
    def connect(self):
        pass

    @abstractmethod
    def start_data_feed(self):
        pass

    def start(self):
        """ProviderFactory compatibility: starts the data feed."""
        logger.info(f"[{self.instance_name}] BaseBroker.start() called. Starting data feed...")
        self.start_data_feed()
        return None

    @abstractmethod
    def stop_data_feed(self):
        pass

    async def close(self):
        """ProviderFactory compatibility: stops the data feed."""
        self.stop_data_feed()

    def register_message_handler(self, handler):
        """ProviderFactory compatibility: no-op for client-side unified providers."""
        pass

    def subscribe(self, instrument_list, mode='full'):
        """
        ProviderFactory compatibility: subscribes to a list of universal
        instrument keys.  Translates to broker-specific format before calling
        internal subscribe_instruments.
        """
        if not instrument_list:
            return

        from utils.broker_rest_adapter import BrokerRestAdapter
        b_name = self.broker_name or self.instance_name.split('_')[-1].lower()
        adapter = BrokerRestAdapter(self, b_name)

        async def do_sub():
            sub_map = {}
            for ikey in instrument_list:
                broker_key = await adapter._translate_to_broker_key(ikey)
                if broker_key:
                    if b_name == 'dhan':
                        segment = 'NSE_FNO'
                        if 'INDEX' in ikey:
                            segment = 'IDX_I'
                        elif 'NSE_EQ' in ikey:
                            segment = 'NSE_EQ'
                        sub_map[ikey] = (segment, str(broker_key))
                    else:
                        sub_map[ikey] = broker_key

            if sub_map:
                self.subscribe_instruments(sub_map)

        asyncio.create_task(do_sub())

    def unsubscribe(self, instrument_list):
        """ProviderFactory compatibility: no-op for unified client providers."""
        pass

    @abstractmethod
    async def handle_entry_signal(self, **kwargs):
        pass

    @abstractmethod
    async def handle_close_signal(self, **kwargs):
        pass

    @abstractmethod
    def place_order(self, contract, transaction_type, quantity, expiry,
                    product_type='NRML', market_protection=None):
        pass

    @abstractmethod
    async def close_all_positions(self):
        pass

    @abstractmethod
    async def get_funds(self):
        pass

    @abstractmethod
    async def get_positions(self):
        pass
