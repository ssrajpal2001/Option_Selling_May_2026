import asyncio
from .base_broker import BaseBroker
from utils.logger import logger
from utils.trade_logger import TradeLogger
from hub.event_bus import event_bus

class PaperTradeClient(BaseBroker):
    def __init__(self, broker_instance_name, config_manager, login_required=True, user_id=None, db_config=None):
        super().__init__(broker_instance_name, config_manager, user_id=user_id, db_config=db_config)
        logger.info(f"{self.instance_name} initialized for instruments: {list(self.instruments)}")

    def connect(self):
        self.is_connected = True
        logger.info(f"PaperTradeClient '{self.instance_name}' is ready.")

    def start_data_feed(self):
        """Not required for paper trade, uses historical or simulated feed."""
        pass

    def stop_data_feed(self):
        pass

    async def handle_entry_signal(self, **kwargs):
        instrument_name = kwargs.get('instrument_name')
        instrument_symbol = kwargs.get('instrument_symbol')
        direction = kwargs.get('direction')
        price = kwargs.get('ltp', 0)
        entry_type = kwargs.get('entry_type', 'BUY')

        logger.info(f"--- [{self.instance_name}] PAPER TRADE ENTRY ({direction} - {entry_type}) for {instrument_name} ---")
        logger.info(f"Instrument: {instrument_symbol} | Entry Price: {price}")

        # Use the new instrument-aware logger
        self.trade_logger.log_entry(
            broker=self.instance_name,
            instrument_name=instrument_name,
            instrument_symbol=instrument_symbol,
            trade_type=direction,
            price=price,
            strategy_log=kwargs.get('strategy_log', ""),
            user_id=self.user_id
        )

        # Feedback to StateManager (Paper trade confirms immediately)
        await event_bus.publish('TRADE_CONFIRMED', {
            'user_id': self.user_id,
            'instrument_name': instrument_name,
            'direction': direction,
            'trade_contract': kwargs.get('contract'),
            'ltp': price,
            'entry_type': entry_type
        })

    async def handle_close_signal(self, **kwargs):
        instrument_name = kwargs.get('instrument_name')
        direction = kwargs.get('direction')
        price = kwargs.get('ltp')
        reason = kwargs.get('reason', 'UNKNOWN')

        if not self.state_manager.is_in_trade(direction):
            logger.warning(f"[{self.instance_name}] Received close signal for {direction} but StateManager reports no active trade. Ignoring.")
            return

        position = self.state_manager.get_position(direction)
        instrument_symbol = position.get('instrument_symbol')
        entry_price = position.get('entry_price', 0)
        entry_type = position.get('entry_type', 'BUY')

        if entry_type == 'SELL':
            pnl = (entry_price - price) if entry_price > 0 else 0
        else:
            pnl = (price - entry_price) if entry_price > 0 else 0

        logger.info(f"--- [{self.instance_name}] PAPER TRADE EXIT ({direction} - {entry_type}) for {instrument_name} ---")
        logger.info(f"Instrument: {instrument_symbol} | Exit Price: {price} | PNL: {pnl:.2f} | Reason: {reason}")

        # Use the new instrument-aware logger
        self.trade_logger.log_exit(
            broker=self.instance_name,
            instrument_name=instrument_name,
            instrument_symbol=instrument_symbol,
            trade_type=f"EXIT_{direction}",
            price=price,
            pnl=pnl,
            reason=reason,
            strategy_log=kwargs.get('strategy_log', ""),
            user_id=self.user_id
        )

        # Feedback to StateManager
        await event_bus.publish('TRADE_CLOSED', {
            'user_id': self.user_id,
            'instrument_name': instrument_name,
            'direction': direction
        })

    async def get_funds(self):
        return 0.0

    async def get_positions(self):
        return []

    # --- Abstract Method Implementations ---

    def place_order(self, contract, transaction_type, quantity, expiry, product_type='NRML', market_protection=None):
        return "PAPER_ORDER_ID"

    async def close_all_positions(self):
        """
        Industrial Standard: Closes all open positions.
        """
        logger.info(f"[{self.instance_name}] End-of-day closure: Closing all open paper trade positions.")
        if self.state_manager:
            try:
                if self.state_manager.is_in_trade('CALL'):
                    await self.handle_close_signal(direction='CALL', reason="EOD")
                if self.state_manager.is_in_trade('PUT'):
                    await self.handle_close_signal(direction='PUT', reason="EOD")
            except Exception as e:
                logger.error(f"Error during EOD closure for {self.instance_name}: {e}")

    def construct_zerodha_symbol(self, contract, signal_expiry_date=None):
        """
        Constructs a Zerodha-compatible trading symbol for a given contract.
        - Monthly format: NIFTY<YY><MON><STRIKE>CE (e.g., NIFTY26MAR23350PE)
        - Weekly format:  NIFTY<YY><M><DD><STRIKE>CE (e.g., NIFTY2633023350PE)
        """
        import datetime

        # Dynamic prefix detection
        name_map = {
            "NIFTY 50": "NIFTY",
            "NIFTY BANK": "BANKNIFTY",
            "NIFTY FINANCIAL SERVICES": "FINNIFTY",
            "NIFTY MIDCAP SELECT": "MIDCPNIFTY",
            "MIDCAP": "MIDCPNIFTY",
            "NIFTY MID SELECT": "MIDCPNIFTY",
            "BANKNIFTY": "BANKNIFTY",
            "FINNIFTY": "FINNIFTY",
            "SENSEX": "SENSEX",
            "BANKEX": "BANKEX"
        }

        raw_name = str(getattr(contract, 'name', 'NIFTY') or 'NIFTY').upper()
        instrument_name = name_map.get(raw_name, raw_name)

        expiry = contract.expiry
        strike = int(contract.strike_price)
        option_type = str(getattr(contract, 'instrument_type', 'CE') or 'CE').upper()
        if option_type == "PUT": option_type = "PE"
        if option_type == "CALL": option_type = "CE"

        year_str = expiry.strftime('%y')

        # Normalize to date object
        expiry_date = expiry.date() if isinstance(expiry, datetime.datetime) else expiry

        # Robust Monthly Detection: Compare against the confirmed monthly_expiries list populated by ContractManager.
        # This accurately handles holiday shifts (like March 26, 2026) by looking at actual contract availability.
        is_monthly_expiry = False
        if self.state_manager and self.state_manager.monthly_expiries:
            is_monthly_expiry = (expiry_date in self.state_manager.monthly_expiries)
        else:
            # Fallback only if state_manager list is empty/missing
            next_week = expiry_date + datetime.timedelta(days=7)
            is_monthly_expiry = (next_week.month != expiry_date.month)

        logger.debug(f"construct_zerodha_symbol: name={instrument_name}, expiry={expiry_date}, is_monthly={is_monthly_expiry}")

        if is_monthly_expiry:
            # Monthly format: NIFTY26MAR23350PE
            month_names = {1:"JAN", 2:"FEB", 3:"MAR", 4:"APR", 5:"MAY", 6:"JUN",
                          7:"JUL", 8:"AUG", 9:"SEP", 10:"OCT", 11:"NOV", 12:"DEC"}
            month_str = month_names[expiry_date.month]
            return f"{instrument_name}{year_str}{month_str}{strike}{option_type}"
        else:
            # Weekly format: NIFTY2633023350PE
            month_val = expiry_date.month
            if month_val == 10: month_char = 'O'
            elif month_val == 11: month_char = 'N'
            elif month_val == 12: month_char = 'D'
            else: month_char = str(month_val)

            day_str = expiry_date.strftime('%d')
            return f"{instrument_name}{year_str}{month_char}{day_str}{strike}{option_type}"

    def translate_symbol(self, standard_symbol):
        # Paper trading uses the trading symbol directly.
        return standard_symbol

    def execute_trade(self, trade_type, instrument_symbol, stop_loss_price):
        # Placeholder to satisfy the abstract base class.
        pass

    def _place_live_order(self, broker_symbol):
        # Not applicable for a paper trading client.
        pass

    def _log_paper_trade(self, broker_symbol):
        # This is the core function of this class, but the logic is handled
        # in handle_entry_signal and handle_close_signal for clarity.
        pass
