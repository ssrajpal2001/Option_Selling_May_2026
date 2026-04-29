from utils.logger import logger
from utils.api_client_manager import ApiClientManager
from hub.csv_data_feeder import CSVDataFeeder
from utils.rest_api_client import RestApiClient
from utils.broker_rest_adapter import BrokerRestAdapter
import asyncio

class ProviderFactory:
    @staticmethod
    async def create_data_provider(api_client_manager, config_manager, is_backtest, contract_map=None, redis_manager=None, user_id=None, broker_manager=None):
        """
        Creates instances of the REST API client and WebSocket manager.
        In backtest mode, it prefers user-specific broker credentials if provided.
        """
        if is_backtest:
            logger.info("Backtest mode enabled. Using CSVDataFeeder for WebSocket and enabling REST client for historical data.")
            
            backtest_file = config_manager.get('settings', 'backtest_csv_path', fallback='tick_data_log.csv')
            websocket_manager = CSVDataFeeder(
                file_path=backtest_file,
                contract_map=contract_map,
                config_manager=config_manager
            )
            
            # 1. Try to create a REST client from User Credentials (Commercial Path)
            rest_client = None
            if user_id:
                try:
                    from web.db import db_fetchone
                    from web.auth import decrypt_secret
                    instance = db_fetchone("SELECT * FROM client_broker_instances WHERE client_id=? AND status != 'removed'", (user_id,))
                    if instance:
                        broker = instance['broker'].lower()
                        api_key = decrypt_secret(instance['api_key_encrypted'])
                        access_token = decrypt_secret(instance['access_token_encrypted'])

                        if broker == 'zerodha':
                            from kiteconnect import KiteConnect
                            kite = KiteConnect(api_key=api_key)
                            kite.set_access_token(access_token)
                            # Quick validation
                            try:
                                await asyncio.to_thread(kite.profile)
                                rest_client = BrokerRestAdapter(kite, 'zerodha')
                                logger.info(f"Backtest: User {user_id}'s Zerodha token is VALID.")
                            except Exception as ve:
                                logger.error(f"Backtest: User {user_id}'s Zerodha token is EXPIRED or INVALID: {ve}")
                                return None, websocket_manager

                        elif broker == 'dhan':
                            from dhanhq import dhanhq
                            try:
                                from dhanhq import DhanContext
                            except ImportError:
                                DhanContext = None
                            client = (dhanhq(DhanContext(api_key, access_token))
                                      if DhanContext else dhanhq(api_key, access_token))
                            # Dhan doesn't have a simple 'profile' that doesn't cost an API hit?
                            # Usually get_fund_limits is safe.
                            try:
                                await asyncio.to_thread(client.get_fund_limits)
                                rest_client = BrokerRestAdapter(client, 'dhan')
                                logger.info(f"Backtest: User {user_id}'s Dhan token is VALID.")
                            except Exception as ve:
                                logger.error(f"Backtest: User {user_id}'s Dhan token is EXPIRED or INVALID: {ve}")
                                return None, websocket_manager

                        elif broker == 'angelone':
                            from utils.auth_manager_angelone import handle_angelone_login
                            try:
                                client = await asyncio.to_thread(handle_angelone_login, instance)
                                if client:
                                    rest_client = BrokerRestAdapter(client, 'angelone')
                                    logger.info(f"Backtest: User {user_id}'s AngelOne session is VALID.")
                                else:
                                    logger.warning(f"Backtest: AngelOne login returned None for user {user_id}. Proceeding in OFFLINE mode.")
                            except Exception as ve:
                                logger.warning(f"Backtest: AngelOne login FAILED for user {user_id}: {ve}. Proceeding in OFFLINE mode.")
                    else:
                        logger.warning(f"Backtest: No active broker instance found for user {user_id}. Proceeding in OFFLINE mode.")
                except Exception as e:
                    logger.warning(f"Backtest: Failed to load user credentials for REST feed: {e}. Proceeding in OFFLINE mode.")

            # 2. Fallback to global active client (Legacy Path)
            if not rest_client and api_client_manager:
                raw_client = api_client_manager.get_active_client()
                if raw_client:
                    # In legacy mode, it's usually Upstox
                    rest_client = BrokerRestAdapter(raw_client, 'upstox')

            if not rest_client:
                logger.warning("No active API client found for backtest. Operating in OFFLINE mode (CSV only).")
                class MockRest:
                    def __init__(self):
                        self.is_mock = True
                    async def get_ltp(self, *args, **kwargs): return 0
                    async def get_ltps(self, *args, **kwargs): return {}
                    async def get_historical_candle_data(self, *args, **kwargs): return None
                    async def get_option_contracts(self, *args, **kwargs): return []
                    def get_active_client(self): return self
                rest_client = MockRest()

            return rest_client, websocket_manager

        # Commercial Client Mode: Force use of Global Redundant Feed (Upstox + Dhan) for Data
        # Execution remains with the primary client broker.
        if api_client_manager is None and broker_manager and broker_manager.brokers:
            primary_broker = next(iter(broker_manager.brokers.values()), None)
            if primary_broker:
                logger.info(f"Client Mode: Initializing Global Redundant Data Feeds for execution account {primary_broker.instance_name}")
        if is_backtest and user_id:
             # Already handled above in user_id block
             pass

        provider_list_str = config_manager.get('data_providers', 'provider_list', fallback='upstox').lower()
        provider_names = [p.strip() for p in provider_list_str.split(',')]
        logger.info(f"Live mode enabled. Initializing global data providers: {provider_names}")

        active_feeds = []
        rest_client = None

        # 0. Short-circuit for same-account setups: if the client's broker IS Upstox
        #    and already has a fresh api_client (token obtained during __init__), reuse
        #    that api_client directly for the global data WebSocket.
        #    This prevents the dual-TOTP token invalidation race where the client login
        #    (Task #114) fires AFTER the server's startup refresh, invalidating the
        #    global data provider's token.
        if 'upstox' in provider_names and broker_manager:
            try:
                from utils.websocket_manager import WebSocketManager as _WSM
                _upstox_broker = next(
                    (b for b in broker_manager.brokers.values()
                     if getattr(b, 'broker_name', '') == 'upstox' and getattr(b, 'api_client', None)),
                    None
                )
                if _upstox_broker:
                    _upstox_ws = _WSM(api_client=_upstox_broker.api_client)
                    active_feeds.append(('upstox', _upstox_ws))
                    if not rest_client:
                        # Build a source-IP-free RestApiClient for data reads.
                        # AWS Elastic IPs work via NAT at the gateway — the EIP is NOT
                        # a local interface address on the EC2 instance, so binding to it
                        # via socket.bind() raises EADDRNOTAVAIL.  All outbound traffic
                        # from EC2 already appears as the EIP to external servers
                        # automatically.  Only order-placement calls in the broker client
                        # itself need (or attempt) source-IP binding.
                        from utils.rest_api_client import RestApiClient as _RAC
                        rest_client = _RAC(_upstox_broker.api_client.auth_handler)
                    provider_names = [p for p in provider_names if p != 'upstox']
                    logger.info("[Global Upstox] Using client broker token for data feed (same-account mode).")
                else:
                    # Same-account shortcut skipped — warn if Upstox broker exists but api_client not ready yet
                    _upstox_broker_no_client = next(
                        (b for b in broker_manager.brokers.values()
                         if getattr(b, 'broker_name', '') == 'upstox'),
                        None
                    )
                    if _upstox_broker_no_client:
                        logger.warning(
                            "[Global Upstox] Upstox client broker found but api_client not yet set "
                            "(login may still be in progress). Falling back to DB-stored token for data feed."
                        )
            except (ImportError, AttributeError, TypeError, ValueError) as _e:
                logger.warning(f"[Global Upstox] Same-account short-circuit failed ({type(_e).__name__}: {_e}). Falling back to DB.")

        # 1. Initialize Upstox Feed (DB path — runs only when broker_manager has no Upstox client)
        #
        # DESIGN PRINCIPLE: Client subprocesses NEVER auto-refresh the global Upstox token.
        # Token refresh is the FeedServer/admin's exclusive responsibility. Client processes
        # simply read whatever token is stored in DB. If the token is stale the contract API
        # call will fail cleanly in ContractManager, which logs a clear "contact admin" error
        # instead of silently falling back to the CSV (which lacks near-weekly contracts and
        # causes wrong expiry, broken reconnects and LTP stuck at 0).
        if 'upstox' in provider_names:
            try:
                from web.db import db_fetchone
                from web.auth import decrypt_secret
                from utils.websocket_manager import WebSocketManager

                dp = db_fetchone("SELECT * FROM data_providers WHERE provider='upstox'")
                if dp and dp['status'] == 'configured':
                    api_key = decrypt_secret(dp['api_key_encrypted'])
                    access_token = decrypt_secret(dp['access_token_encrypted'])

                    # Minimal auth handler for global Upstox — satisfies RestApiClient + WebSocketManager
                    class _ConfigStub:
                        def get_boolean(self, *args, **kwargs): return False
                        def get(self, *args, **kwargs): return kwargs.get('fallback', None)

                    class GlobalUpstoxAuth:
                        config_manager = _ConfigStub()
                        def __init__(self, key, tok):
                            self.key = key
                            self.token = tok
                        def get_access_token(self): return self.token

                    global_auth = GlobalUpstoxAuth(api_key, access_token)
                    global_rest = RestApiClient(global_auth)

                    # WebSocket tick feed — FeedClient overrides this for client subprocesses.
                    upstox_ws = WebSocketManager(api_client=global_rest)
                    active_feeds.append(('upstox', upstox_ws))

                    # Global Upstox REST is used as last-resort for contract fetching only when
                    # no contract-capable execution broker is present (e.g. Dhan-only sessions).
                    # Contract-capable execution brokers (Zerodha, AngelOne) are always preferred
                    # — see the BROKER REST PREFERENCE section below.
                    if not rest_client:
                        rest_client = global_rest
                    logger.info("[Global Upstox] Data provider initialized from DB token (admin manages refresh).")
                else:
                    logger.warning("Global Upstox data provider requested but not configured in DB.")
            except Exception as e:
                logger.error(f"Failed to initialize Global Upstox feed: {e}")

        # 2. Initialize Dhan Feed
        if 'dhan' in provider_names:
            try:
                from web.db import db_fetchone
                from web.auth import decrypt_secret
                from utils.dhan_websocket_manager import DhanWebSocketManager

                dp = db_fetchone("SELECT * FROM data_providers WHERE provider='dhan'")
                if dp and dp['status'] == 'configured':
                    cid = decrypt_secret(dp['api_key_encrypted'])
                    access_token = decrypt_secret(dp['access_token_encrypted'])

                    # DESIGN PRINCIPLE: Client subprocesses NEVER auto-refresh the global Dhan token.
                    # Token refresh is the FeedServer/admin's exclusive responsibility.
                    # Validate only — if invalid, log a clear error and continue with the stale token.
                    # The Dhan WebSocket may disconnect immediately; tick data falls back to Upstox feed.
                    from utils.auth_manager_dhan import handle_dhan_login_automated

                    logger.info("[Global Dhan] Validating stored token...")
                    validated = handle_dhan_login_automated({
                        "api_key": cid,
                        "client_id": cid,
                        "access_token": access_token,
                    })

                    if validated:
                        access_token = validated
                        logger.info("[Global Dhan] Token valid.")
                    else:
                        logger.error(
                            "[Global Dhan] Token is INVALID. Contact administrator to refresh "
                            "the Dhan global feed token from Admin → Data Providers. "
                            "Dhan WebSocket will start with stale token and will disconnect immediately. "
                            "Tick data will fall back to Upstox feed via FeedServer."
                        )

                    dhan_ws = DhanWebSocketManager(cid, access_token)
                    active_feeds.append(('dhan', dhan_ws))
                    logger.info("[Global Dhan] Data provider initialized.")
                else:
                    logger.warning("Global Dhan data provider requested but not configured in DB.")
            except Exception as e:
                logger.error(f"Failed to initialize Global Dhan feed: {e}")

        if not active_feeds and (not broker_manager or not broker_manager.brokers):
            logger.error("No active data providers could be initialized.")
            raise RuntimeError("No data providers available.")

        # If multiple feeds, try FeedClient first in client-subprocess mode to avoid
        # the global Dhan/Upstox WebSocket eviction loop caused by 4 subprocesses
        # each opening their own connection with the same credentials (Task #152).
        if len(active_feeds) > 1:
            import os as _os
            _in_subprocess = bool(_os.environ.get('CLIENT_ID'))
            _feed_server_init = bool(_os.environ.get('_FEED_SERVER_INIT'))
            if _in_subprocess and not _feed_server_init:
                # Pre-build DualFeedManager objects as a runtime fallback (NOT started yet).
                # FeedClient will activate them only after _FALLBACK_TRIGGER_ROUNDS of
                # consecutive connection failures, giving FeedServer ample time to come up.
                from hub.dual_feed_manager import DualFeedManager as _DFM
                _upstox_feed = next((f[1] for f in active_feeds if f[0] == 'upstox'), None)
                _dhan_feed = next((f[1] for f in active_feeds if f[0] == 'dhan'), None)
                _fallback_dm = _DFM(_upstox_feed, _dhan_feed)

                # Always return FeedClient — let it own all retry and fallback logic.
                # A one-time probe is logged for diagnostics but never gates routing;
                # if FeedServer is not yet up, FeedClient will keep retrying in its
                # _connection_loop() before eventually activating the fallback DM.
                from hub.feed_client import FeedClient
                try:
                    _probe = FeedClient()
                    _server_up = await _probe.try_connect()
                    await _probe.close()
                    if _server_up:
                        logger.info(
                            "[ProviderFactory] FeedServer reachable — "
                            "FeedClient will use shared tick distribution."
                        )
                    else:
                        logger.info(
                            "[ProviderFactory] FeedServer not yet reachable — "
                            "FeedClient will retry until it comes up "
                            f"(fallback DualFeedManager held in reserve)."
                        )
                except Exception as _probe_err:
                    logger.info(
                        f"[ProviderFactory] FeedServer probe inconclusive ({_probe_err}); "
                        "FeedClient will retry on its own schedule."
                    )
                websocket_manager = FeedClient(fallback_feed=_fallback_dm)
            else:
                from hub.dual_feed_manager import DualFeedManager
                upstox_feed = next((f[1] for f in active_feeds if f[0] == 'upstox'), None)
                dhan_feed = next((f[1] for f in active_feeds if f[0] == 'dhan'), None)
                websocket_manager = DualFeedManager(upstox_feed, dhan_feed)
                logger.info("Redundant Dual-Feed mode ACTIVE (Upstox + Dhan).")
        elif len(active_feeds) == 1:
            websocket_manager = active_feeds[0][1]
        else:
            websocket_manager = None

        # REST CLIENT SELECTION: Must support get_option_contracts for expiry resolution.
        # Dhan is intentionally excluded — its BrokerRestAdapter.get_option_contracts()
        # returns [] (no implementation), which silently corrupts expiry resolution by
        # forcing a CSV fallback that never includes today's expiring contracts.
        _CONTRACT_CAPABLE = ['upstox', 'zerodha', 'angelone', 'fyers', 'aliceblue']

        if not rest_client:
            # For historical data and option contracts, we still need a rest_client.
            # ApiClientManager typically holds a live Upstox RestApiClient.
            if api_client_manager:
                _candidate = api_client_manager.get_active_client()
                # Guard: reject if it resolves to a Dhan adapter (no contract support)
                _candidate_broker = (
                    getattr(_candidate, 'broker_name', None) or
                    getattr(_candidate, '_broker_name', None) or ''
                )
                if _candidate and _candidate_broker not in ('dhan',):
                    rest_client = _candidate
                elif _candidate:
                    logger.warning(
                        f"[ProviderFactory] ApiClientManager returned a {_candidate_broker!r} client "
                        "which has no option-contract support — skipping."
                    )

        # BROKER REST PREFERENCE: For non-Upstox execution brokers (Zerodha, AngelOne, etc.),
        # ALWAYS prefer the execution broker over the global Upstox REST client for contract data.
        # These brokers have fresh authenticated sessions and their instrument masters contain
        # ALL current weekly expiries (May 5, May 12, May 19, etc.) — unlike the global Upstox
        # data provider which may have a stale token, and unlike the CSV which lacks near-weekly
        # contracts entirely and causes wrong expiry / LTP stuck at 0.
        #
        # Upstox is EXCLUDED here — Upstox sessions use the same-account RestApiClient set
        # earlier (via the same-account short-circuit block), which already uses the fresh
        # broker token and must not be overridden.
        # Dhan is also excluded — it has no option-contract API support.
        _PREFERRED_EXECUTION_BROKERS = ['zerodha', 'angelone', 'fyers', 'aliceblue']
        if broker_manager and broker_manager.brokers:
            for _preferred in _PREFERRED_EXECUTION_BROKERS:
                _b = next(
                    (b for b in broker_manager.brokers.values()
                     if getattr(b, 'broker_name', '') == _preferred),
                    None
                )
                if _b:
                    rest_client = BrokerRestAdapter(_b, _preferred)
                    logger.info(f"[ProviderFactory] Using {_preferred} execution broker as primary REST client for contracts.")
                    break

        # ENFORCEMENT: Websocket Data MUST come from Global Feeds (Upstox/Dhan)
        # We removed Zerodha as a data provider as per requirement.
        if not websocket_manager or (hasattr(websocket_manager, 'is_mock') and websocket_manager.is_mock):
            logger.warning("No global data feeder (Upstox/Dhan) is connected. Real-time data will be missing.")

        # Emit clear diagnostic so startup logs always show which REST client is used for contracts.
        if rest_client:
            _rc_type = type(rest_client).__name__
            _rc_broker = getattr(rest_client, 'broker_name', None) or getattr(rest_client, '_broker_name', None) or 'upstox'
            logger.info(f"[ProviderFactory] Contract REST client: {_rc_broker} ({_rc_type})")
        else:
            logger.warning("[ProviderFactory] No REST client available — contract loading will fail.")

        return rest_client, websocket_manager
