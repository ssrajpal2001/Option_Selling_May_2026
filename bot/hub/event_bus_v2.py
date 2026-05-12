"""
TypedEventBus — improved event bus with:
  - Typed event models (hub.events)
  - Error isolation: one bad subscriber can't kill others
  - Dead-letter queue for failed events
  - Per-event-type publish/error metrics
  - Legacy bridge: publishes raw-dict events from old event_bus.py as typed events

MIGRATION PATH (additive, zero breaking changes):
  1. New code imports TypedEventBus and subscribes using EventType enums.
  2. Old managers continue using hub.event_bus unchanged.
  3. Wire the bridge in main.py or orchestrator to forward old events here.
"""

from __future__ import annotations

import asyncio
import inspect
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from utils.logger import logger
from hub.events import BaseEvent, EventType


# ---------------------------------------------------------------------------
# Dead-letter queue entry
# ---------------------------------------------------------------------------

@dataclass
class DeadLetterEntry:
    event_type: str
    event: Any
    handler_name: str
    error: Exception
    timestamp: datetime = field(default_factory=datetime.utcnow)


# ---------------------------------------------------------------------------
# TypedEventBus
# ---------------------------------------------------------------------------

class TypedEventBus:
    """
    Typed, error-isolated async event bus.

    Unlike the singleton event_bus.py, this class is instantiable so it
    can be injected into components and replaced in tests.
    """

    def __init__(self, max_dlq_size: int = 100):
        self._listeners: Dict[str, List[Callable]] = defaultdict(list)
        self._metrics: Dict[str, Dict[str, int]] = defaultdict(
            lambda: {"published": 0, "errors": 0}
        )
        self._dlq: List[DeadLetterEntry] = []
        self._max_dlq_size = max_dlq_size

    # ── Subscription ──────────────────────────────────────────────────────

    def subscribe(self, event_type: str, handler: Callable) -> None:
        """Subscribe handler to an event type (EventType enum or raw string)."""
        key = str(event_type)
        self._listeners[key].append(handler)
        logger.debug(
            f"[EventBusV2] {getattr(handler, '__name__', repr(handler))} "
            f"subscribed to {key}"
        )

    def unsubscribe(self, event_type: str, handler: Callable) -> None:
        key = str(event_type)
        try:
            self._listeners[key].remove(handler)
        except ValueError:
            pass

    # ── Publishing ────────────────────────────────────────────────────────

    async def publish(self, event: BaseEvent) -> None:
        """
        Publish a typed event. Each subscriber runs independently;
        an exception in one handler is caught, logged to DLQ, and
        does NOT prevent other handlers from running.
        """
        key = str(event.event_type)
        self._metrics[key]["published"] += 1

        handlers = self._listeners.get(key, [])[:]
        if not handlers:
            return

        tasks = []
        for handler in handlers:
            tasks.append(self._call_handler(handler, event, key))

        await asyncio.gather(*tasks, return_exceptions=False)

    async def _call_handler(self, handler: Callable, event: BaseEvent, key: str) -> None:
        """Run a single handler, catching and recording any exception."""
        try:
            if inspect.iscoroutinefunction(handler):
                await handler(event)
            else:
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, handler, event)
        except Exception as exc:
            self._metrics[key]["errors"] += 1
            name = getattr(handler, '__name__', repr(handler))
            logger.error(
                f"[EventBusV2] Handler '{name}' raised on {key}: {exc}",
                exc_info=True,
            )
            entry = DeadLetterEntry(
                event_type=key,
                event=event,
                handler_name=name,
                error=exc,
            )
            if len(self._dlq) < self._max_dlq_size:
                self._dlq.append(entry)

    # ── Metrics ───────────────────────────────────────────────────────────

    def get_metrics(self) -> Dict[str, Dict[str, int]]:
        """Returns per-event-type publish + error counts. Used by /api/admin/bus-metrics."""
        return {k: dict(v) for k, v in self._metrics.items()}

    def get_dlq(self) -> List[DeadLetterEntry]:
        """Returns the dead-letter queue (failed handler entries)."""
        return list(self._dlq)

    def clear_dlq(self) -> None:
        self._dlq.clear()


# ---------------------------------------------------------------------------
# Module-level singleton (mirrors event_bus.event_bus pattern)
# ---------------------------------------------------------------------------

event_bus_v2 = TypedEventBus()


# ---------------------------------------------------------------------------
# Legacy bridge
# ---------------------------------------------------------------------------

async def bridge_legacy_event(event_type: str, *args, **kwargs) -> None:
    """
    Forward a raw old-style event to event_bus_v2 as a BaseEvent.

    Call this from the old event_bus subscribers to make old events
    visible to new typed handlers during the migration period.

    Example wiring in main.py:
        from hub.event_bus import event_bus
        from hub.event_bus_v2 import bridge_legacy_event
        event_bus.subscribe('EXECUTE_TRADE_REQUEST',
            lambda *a, **kw: asyncio.create_task(
                bridge_legacy_event('EXECUTE_TRADE_REQUEST', *a, **kw)))
    """
    from hub.events import BaseEvent
    # Wrap raw dict payload in a BaseEvent for type-safe routing
    payload = kwargs if kwargs else (args[0] if args else {})
    event = BaseEvent()
    event.event_type = event_type  # type: ignore[assignment]
    # Attach raw payload as attribute for backward compat
    event._legacy_payload = payload  # type: ignore[attr-defined]
    await event_bus_v2.publish(event)
