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
                            client = dhanhq(api_key, access_token)
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
                                else: raise Exception("AngelOne login returned None")
                            except Exception as ve:
                                logger.error(f"Backtest: User {user_id}'s AngelOne login FAILED: {ve}")
                                return None, websocket_manager
                    else:
                        logger.error(f"Backtest: No active broker instance found for user {user_id}")
                        return None, websocket_manager
                except Exception as e:
                    logger.warning(f"Failed to load user credentials for backtest REST feed: {e}")
                    return None, websocket_manager

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

        # 1. Initialize Upstox Feed
        if 'upstox' in provider_names:
            try:
                from web.db import db_fetchone, db_execute
                from web.auth import decrypt_secret, encrypt_secret
                from utils.websocket_manager import WebSocketManager
                from datetime import datetime, timezone

                dp = db_fetchone("SELECT * FROM data_providers WHERE provider='upstox'")
                if dp and dp['status'] == 'configured':
                    api_key = decrypt_secret(dp['api_key_encrypted'])
                    access_token = decrypt_secret(dp['access_token_encrypted'])
                    updated_at = dp.get('updated_at')

                    # Daily Token Refresh check for Global Upstox
                    needs_refresh = True
                    if updated_at:
                        try:
                            # Simple check: same day?
                            upd_dt = datetime.fromisoformat(updated_at).date()
                            if upd_dt == datetime.now(timezone.utc).date():
                                needs_refresh = False
                        except: pass

                    if needs_refresh:
                        logger.info("Global Upstox token expired or fresh day started. Attempting auto-refresh...")
                        creds = {
                            "api_key": api_key,
                            "api_secret": decrypt_secret(dp.get("api_secret_encrypted", "")),
                            "user_id": decrypt_secret(dp.get("user_id_encrypted", "")),
                            "password": decrypt_secret(dp.get("password_encrypted", "")),
                            "totp": decrypt_secret(dp.get("totp_encrypted", ""))
                        }
                        from utils.auth_manager_upstox import handle_upstox_login_automated
                        new_token = handle_upstox_login_automated(creds)
                        if new_token:
                            access_token = new_token
                            enc_token = encrypt_secret(new_token)
                            now_str = datetime.now(timezone.utc).isoformat()
                            db_execute("UPDATE data_providers SET access_token_encrypted=?, updated_at=? WHERE provider='upstox'", (enc_token, now_str))
                            logger.info("Global Upstox token auto-refreshed successfully.")

                    # Create a specialized Upstox REST client for this global feed
                    class GlobalUpstoxAuth:
                        def __init__(self, key, token):
                            self.key = key
                            self.token = token
                        def get_access_token(self): return self.token

                    global_auth = GlobalUpstoxAuth(api_key, access_token)
                    global_rest = RestApiClient(global_auth)

                    # WebSocketManager expects an api_client_manager usually,
                    # but we can pass our custom global_rest if we refactor or mock
                    # For now, if api_client_manager is None, we need to ensure it works.
                    upstox_ws = WebSocketManager(api_client_manager or global_rest)
                    active_feeds.append(('upstox', upstox_ws))

                    if not rest_client: rest_client = global_rest
                    logger.info("Global Upstox data provider initialized from DB.")
                else:
                    logger.warning("Global Upstox data provider requested but not configured in DB.")
            except Exception as e:
                logger.error(f"Failed to initialize Global Upstox feed: {e}")

        # 2. Initialize Dhan Feed
        if 'dhan' in provider_names:
            try:
                from web.db import db_fetchone, db_execute
                from web.auth import decrypt_secret, encrypt_secret
                from utils.dhan_websocket_manager import DhanWebSocketManager
                from datetime import datetime, timezone

                dp = db_fetchone("SELECT * FROM data_providers WHERE provider='dhan'")
                if dp and dp['status'] == 'configured':
                    cid = decrypt_secret(dp['api_key_encrypted'])
                    access_token = decrypt_secret(dp['access_token_encrypted'])
                    updated_at = dp.get('updated_at')

                    # Daily Token Refresh check for Global Dhan
                    needs_refresh = True
                    if updated_at:
                        try:
                            upd_dt = datetime.fromisoformat(updated_at).date()
                            if upd_dt == datetime.now(timezone.utc).date():
                                needs_refresh = False
                        except: pass

                    if needs_refresh:
                        logger.info("Global Dhan token fresh day started. Attempting auto-refresh...")
                        creds = {
                            "api_key": cid,
                            "user_id": decrypt_secret(dp.get("user_id_encrypted", "")),
                            "password": decrypt_secret(dp.get("password_encrypted", "")),
                            "totp": decrypt_secret(dp.get("totp_encrypted", ""))
                        }
                        from utils.auth_manager_dhan import handle_dhan_login_automated
                        new_token = handle_dhan_login_automated(creds)
                        if new_token:
                            access_token = new_token
                            enc_token = encrypt_secret(new_token)
                            now_str = datetime.now(timezone.utc).isoformat()
                            db_execute("UPDATE data_providers SET access_token_encrypted=?, updated_at=? WHERE provider='dhan'", (enc_token, now_str))
                            logger.info("Global Dhan token auto-refreshed successfully.")

                    dhan_ws = DhanWebSocketManager(cid, access_token)
                    active_feeds.append(('dhan', dhan_ws))
                    logger.info("Global Dhan data provider initialized.")
                else:
                    logger.warning("Global Dhan data provider requested but not configured in DB.")
            except Exception as e:
                logger.error(f"Failed to initialize Global Dhan feed: {e}")

        if not active_feeds and (not broker_manager or not broker_manager.brokers):
            logger.error("No active data providers could be initialized.")
            raise RuntimeError("No data providers available.")

        # If multiple feeds, wrap in DualFeedManager
        if len(active_feeds) > 1:
            from hub.dual_feed_manager import DualFeedManager
            upstox_feed = next((f[1] for f in active_feeds if f[0] == 'upstox'), None)
            dhan_feed = next((f[1] for f in active_feeds if f[0] == 'dhan'), None)
            websocket_manager = DualFeedManager(upstox_feed, dhan_feed)
            logger.info("Redundant Dual-Feed mode ACTIVE (Upstox + Dhan).")
        elif len(active_feeds) == 1:
            websocket_manager = active_feeds[0][1]
        else:
            websocket_manager = None

        # Use Upstox as the REST client if available, otherwise Dhan
        if not rest_client:
            # For historical data and option contracts, we still need a rest_client.
            # We'll try to get it from the ApiClientManager or just use the first provider.
            if api_client_manager:
                rest_client = api_client_manager.get_active_client()

            if not rest_client and 'dhan' in [f[0] for f in active_feeds]:
                # Try to create a Dhan rest adapter from the same credentials
                try:
                    from web.db import db_fetchone
                    from web.auth import decrypt_secret
                    dp = db_fetchone("SELECT * FROM data_providers WHERE provider='dhan'")
                    if dp and dp['status'] == 'configured':
                        from dhanhq import dhanhq
                        cid = decrypt_secret(dp['api_key_encrypted'])
                        token = decrypt_secret(dp['access_token_encrypted'])
                        client = dhanhq(cid, token)
                        rest_client = BrokerRestAdapter(client, 'dhan')
                        logger.info("Using Dhan as Global REST data provider.")
                except: pass

        # FINAL FALLBACK: Ensure we have a REST client for indicators/history
        # We prefer Global feeds (Upstox/Dhan) but can use primary broker REST for simple queries if needed.
        if not rest_client and broker_manager and broker_manager.brokers:
            primary_broker = next(iter(broker_manager.brokers.values()), None)
            if primary_broker:
                b_name = getattr(primary_broker, 'broker_name', 'upstox')
                rest_client = BrokerRestAdapter(primary_broker, b_name)
                logger.info(f"Using {b_name} as fallback REST data provider.")

        # ENFORCEMENT: Websocket Data MUST come from Global Feeds (Upstox/Dhan)
        # We removed Zerodha as a data provider as per requirement.
        if not websocket_manager or (hasattr(websocket_manager, 'is_mock') and websocket_manager.is_mock):
            logger.warning("No global data feeder (Upstox/Dhan) is connected. Real-time data will be missing.")

        return rest_client, websocket_manager
