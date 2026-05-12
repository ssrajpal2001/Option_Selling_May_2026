"""
Typed event models for the AlgoSoft event bus (v2).

Uses stdlib dataclasses — no Pydantic dependency required.
Old event_bus.py continues to work unchanged; these models are used
exclusively by event_bus_v2.py and new code that imports them.

IST timestamps: always pass timezone-aware datetimes (pytz Asia/Kolkata).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


# ---------------------------------------------------------------------------
# Event type registry
# ---------------------------------------------------------------------------

class EventType(str, Enum):
    TICK_UPDATE             = "TICK_UPDATE"
    EXECUTE_TRADE_REQUEST   = "EXECUTE_TRADE_REQUEST"
    EXIT_TRADE_REQUEST      = "EXIT_TRADE_REQUEST"
    POSITION_UPDATED        = "POSITION_UPDATED"
    TRADE_CONFIRMED         = "TRADE_CONFIRMED"
    BROKER_CONNECTED        = "BROKER_CONNECTED"
    BROKER_DISCONNECTED     = "BROKER_DISCONNECTED"
    BROKER_ERROR            = "BROKER_ERROR"
    EOD_SQUAREOFF           = "EOD_SQUAREOFF"
    RECONNECT_REQUESTED     = "RECONNECT_REQUESTED"
    SESSION_STARTED         = "SESSION_STARTED"
    SESSION_STOPPED         = "SESSION_STOPPED"


def _new_id() -> str:
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Base event
# ---------------------------------------------------------------------------

@dataclass
class BaseEvent:
    correlation_id: str = field(default_factory=_new_id)
    timestamp: Optional[datetime] = field(default=None)

    def __post_init__(self):
        if self.timestamp is None:
            import pytz
            self.timestamp = datetime.now(tz=pytz.timezone('Asia/Kolkata'))


# ---------------------------------------------------------------------------
# Market data events
# ---------------------------------------------------------------------------

@dataclass
class TickEvent(BaseEvent):
    event_type: str = EventType.TICK_UPDATE
    instrument: str = ""
    ltp: float = 0.0
    bid: float = 0.0
    ask: float = 0.0
    volume: int = 0
    instrument_key: str = ""


# ---------------------------------------------------------------------------
# Trade lifecycle events
# ---------------------------------------------------------------------------

@dataclass
class TradeRequestEvent(BaseEvent):
    event_type: str = EventType.EXECUTE_TRADE_REQUEST
    user_id: Optional[str] = None
    instrument_name: str = ""
    ce_symbol: str = ""
    pe_symbol: str = ""
    ce_strike: int = 0
    pe_strike: int = 0
    lots: int = 1
    entry_price_ce: float = 0.0
    entry_price_pe: float = 0.0


@dataclass
class ExitTradeRequestEvent(BaseEvent):
    event_type: str = EventType.EXIT_TRADE_REQUEST
    user_id: Optional[str] = None
    instrument_name: str = ""
    ce_symbol: str = ""
    pe_symbol: str = ""
    exit_reason: str = ""
    exit_price_ce: float = 0.0
    exit_price_pe: float = 0.0


@dataclass
class TradeConfirmedEvent(BaseEvent):
    event_type: str = EventType.TRADE_CONFIRMED
    user_id: Optional[str] = None
    order_id: str = ""
    instrument_name: str = ""
    symbol: str = ""
    side: str = ""
    qty: int = 0
    fill_price: float = 0.0
    is_paper: bool = True


@dataclass
class PositionUpdatedEvent(BaseEvent):
    event_type: str = EventType.POSITION_UPDATED
    user_id: Optional[str] = None
    instrument_name: str = ""
    pnl: float = 0.0
    open_positions: int = 0


# ---------------------------------------------------------------------------
# Broker events
# ---------------------------------------------------------------------------

@dataclass
class BrokerConnectedEvent(BaseEvent):
    event_type: str = EventType.BROKER_CONNECTED
    broker_instance: str = ""
    user_id: Optional[str] = None


@dataclass
class BrokerDisconnectedEvent(BaseEvent):
    event_type: str = EventType.BROKER_DISCONNECTED
    broker_instance: str = ""
    user_id: Optional[str] = None
    reason: str = ""


@dataclass
class BrokerErrorEvent(BaseEvent):
    event_type: str = EventType.BROKER_ERROR
    broker_instance: str = ""
    user_id: Optional[str] = None
    error_message: str = ""
    recoverable: bool = True


# ---------------------------------------------------------------------------
# Session events
# ---------------------------------------------------------------------------

@dataclass
class EodSquareoffEvent(BaseEvent):
    event_type: str = EventType.EOD_SQUAREOFF
    instrument_name: str = ""
    reason: str = "EOD"


@dataclass
class ReconnectRequestedEvent(BaseEvent):
    event_type: str = EventType.RECONNECT_REQUESTED
    broker_instance: str = ""
    has_open_positions: bool = False
    attempt: int = 1
