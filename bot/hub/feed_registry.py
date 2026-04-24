"""
Global registry that tracks the runtime state of active data feed instances.
DualFeedManager and individual feed managers register here so the admin health
endpoint can report authoritative websocket state without coupling to instance_manager.
"""
import time
from typing import Dict, Any, Optional

_registry: Dict[str, Any] = {}


def register_feed(provider: str, feed_obj) -> None:
    """Register a live feed object for health tracking."""
    _registry[provider] = feed_obj


def unregister_feed(provider: str) -> None:
    _registry.pop(provider, None)


def get_ws_state(provider: str) -> dict:
    """
    Return live websocket state for a provider.
    Keys: ws_connected (bool), last_tick_time (float|None)
    """
    feed = _registry.get(provider)
    if feed is None:
        return {"ws_connected": False, "last_tick_time": None}

    connected = getattr(feed, "is_connected", False)
    last_tick = getattr(feed, "_last_message_time", None)

    return {
        "ws_connected": bool(connected),
        "last_tick_time": last_tick,
    }


def get_all_ws_state() -> Dict[str, dict]:
    return {provider: get_ws_state(provider) for provider in ("upstox", "dhan")}
