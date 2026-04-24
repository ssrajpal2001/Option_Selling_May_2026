from utils.logger import logger
import pandas as pd

class BreachGateManager:
    def __init__(self, orchestrator):
        self.orchestrator = orchestrator
        self.state_manager = orchestrator.state_manager
        self.indicator_manager = orchestrator.indicator_manager

    async def check_market_open_range_breach(self, timestamp):
        """Checks if the underlying index has breached its market open range."""
        index_key = self.orchestrator.index_instrument_key
        idx_high, idx_low = await self.indicator_manager.get_index_open_range(index_key, timestamp)

        is_mcx = any(x in self.orchestrator.instrument_name.upper() for x in ['CRUDE', 'NATURAL', 'GOLD', 'SILVER'])
        idx_ltp = (self.state_manager.spot_price if is_mcx else self.state_manager.index_price) or 0.0

        if not idx_high or not idx_low: return

        # 1. Breach UP (CE)
        if idx_ltp > idx_high:
            if not getattr(self.state_manager, 'range_915_breached_up', False):
                self.state_manager.range_915_breached_up = True
                self.state_manager.range_915_breached_down = False
                label = "Futures" if any(x in self.orchestrator.instrument_name.upper() for x in ['CRUDE', 'NATURAL', 'GOLD', 'SILVER']) else "Index"
                logger.info(f"V2: Market Open Range Breach UP! {label}: {idx_ltp:.2f} > High: {idx_high:.2f}. CE Enabled.")

        # 2. Breach DOWN (PE)
        elif idx_ltp < idx_low:
            if not getattr(self.state_manager, 'range_915_breached_down', False):
                self.state_manager.range_915_breached_down = True
                self.state_manager.range_915_breached_up = False
                label = "Futures" if any(x in self.orchestrator.instrument_name.upper() for x in ['CRUDE', 'NATURAL', 'GOLD', 'SILVER']) else "Index"
                logger.info(f"V2: Market Open Range Breach DOWN! {label}: {idx_ltp:.2f} < Low: {idx_low:.2f}. PE Enabled.")

        # 3. Handle boundary crossing (Sticky Logic)
        if getattr(self.state_manager, 'range_915_breached_up', False) and idx_ltp < idx_low:
             self.state_manager.range_915_breached_up = False
             self.state_manager.range_915_breached_down = True
             logger.info(f"V2: Index {idx_ltp:.2f} fell below Market Open Low ({idx_low:.2f}). Switching to PE side.")
        elif getattr(self.state_manager, 'range_915_breached_down', False) and idx_ltp > idx_high:
             self.state_manager.range_915_breached_down = False
             self.state_manager.range_915_breached_up = True
             logger.info(f"V2: Index {idx_ltp:.2f} rose above Market Open High ({idx_high:.2f}). Switching to CE side.")

    def is_side_permitted(self, side, idx_ltp, idx_high, idx_low):
        """Checks if the side is permitted based on the current market open breach status and price position."""
        if side == 'CE':
            breached = getattr(self.state_manager, 'range_915_breached_up', False)
            return breached and (idx_ltp > idx_low if idx_low else False)
        else: # PE
            breached = getattr(self.state_manager, 'range_915_breached_down', False)
            return breached and (idx_ltp < idx_high if idx_high else False)
