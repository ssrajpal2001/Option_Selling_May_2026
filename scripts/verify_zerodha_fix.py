import asyncio
import os
import sys
from unittest.mock import MagicMock, AsyncMock, patch

# Add current dir to path
sys.path.append(os.getcwd())

async def test_provider_factory_client_mode():
    from hub.provider_factory import ProviderFactory
    from utils.config_manager import ConfigManager

    config = ConfigManager(config_file='config/config_trader.ini')
    config.set_override('data_providers', 'provider_list', 'none') # Force fallback

    mock_broker = MagicMock()
    mock_broker.instance_name = "client_1_zerodha"
    mock_broker.broker_name = "zerodha"
    mock_broker.is_mock = False

    broker_manager = MagicMock()
    broker_manager.brokers = {"client_1_zerodha": mock_broker}

    print("Testing ProviderFactory fallback in client mode...")
    rest, ws = await ProviderFactory.create_data_provider(
        api_client_manager=None,
        config_manager=config,
        is_backtest=False,
        broker_manager=broker_manager
    )

    assert rest is not None
    assert ws == mock_broker
    print("SUCCESS: ProviderFactory fell back to client broker.")

async def test_zerodha_start_and_subscribe():
    from brokers.zerodha_client import ZerodhaClient
    from utils.config_manager import ConfigManager

    config = ConfigManager(config_file='config/config_trader.ini')

    # Mock KiteTicker
    with patch('brokers.zerodha_client.KiteTicker') as MockTicker:
        client = ZerodhaClient("test_z", config, login_required=False)
        client.kite = MagicMock()
        client.kite.api_key = "test_key"
        client.kite.access_token = "test_token"

        print("Testing Zerodha data feed start...")
        client.start_data_feed()

        assert client.ticker is not None
        MockTicker.assert_called_once()
        print("SUCCESS: Zerodha Ticker initialized.")

        print("Testing Zerodha instrument subscription...")
        client.subscribe_instruments({"NSE_INDEX|Nifty 50": 256265})

        # Since ticker is mocked, it won't be "connected" unless we mock ws
        client.ticker.ws = MagicMock()
        client.subscribe_instruments({"NSE_FO|12345": 12345})

        assert client.token_to_key[256265] == "NSE_INDEX|Nifty 50"
        assert 12345 in client.subscribed_tokens
        print("SUCCESS: Instruments mapped and subscribed.")

async def test_trading_toggle_logic():
    from hub.tick_processor import TickProcessor
    from unittest.mock import AsyncMock

    print("Testing Trading Toggle OFF logic...")
    orch = MagicMock()
    orch.is_backtest = False
    orch.instrument_name = "NIFTY"
    orch.user_sessions = {"test_user": MagicMock()}
    session = orch.user_sessions["test_user"]
    session.is_in_trade.return_value = True
    session.manage_active_trades = AsyncMock()
    session.position_manager = AsyncMock()
    session.signal_monitor = MagicMock()
    session.signal_monitor.is_monitoring.return_value = True
    orch.state_manager = MagicMock()
    orch.state_manager.last_exchange_time = None
    orch.state_manager.spot_price = 25000.0
    orch.state_manager.index_price = 25000.0
    import datetime
    orch._get_timestamp.return_value = datetime.datetime.now()
    orch.get_exchange_open_time.return_value = datetime.datetime.now()
    orch.get_market_open_time.return_value = datetime.datetime.now()
    orch.data_recorder = None
    orch.sell_manager = MagicMock()
    orch.sell_manager.strangle_closed = True
    orch.oi_exit_monitor = MagicMock()
    orch.oi_exit_monitor.check = AsyncMock()
    orch.status_writer = MagicMock()
    orch.status_writer.maybe_write = MagicMock()

    tp = TickProcessor(orch)

    # Create toggle file
    user_id = "test_user"
    os.environ['CLIENT_ID'] = user_id
    toggle_file = f"config/trading_enabled_{user_id}.json"
    with open(toggle_file, 'w') as f:
        import json
        json.dump({"enabled": False}, f)

    await tp.process_tick_v2()

    session.position_manager.close_all_positions.assert_called_once()
    session.signal_monitor.stop_monitoring.assert_called_once()
    print("SUCCESS: Trading Toggle OFF correctly triggered square-off.")

    if os.path.exists(toggle_file): os.remove(toggle_file)

if __name__ == "__main__":
    asyncio.run(test_provider_factory_client_mode())
    asyncio.run(test_zerodha_start_and_subscribe())
    asyncio.run(test_trading_toggle_logic())
