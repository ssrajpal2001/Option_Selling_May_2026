"""
Global registry that tracks the runtime state of active data feed instances.
DualFeedManager and individual feed managers register here so the admin health
endpoint can report authoritative websocket state without coupling to instance_manager.
"""
import time
from typing import Any, Dict, Optional

_registry: Dict[str, Any] = {}


def register_feed(provider: str, feed_obj) -> None:
    """Register a live feed object for health tracking."""
    _registry[provider] = feed_obj


def unregister_feed(provider: str) -> None:
    _registry.pop(provider, None)


def get_ws_state(provider: str) -> dict:
    """
    Return live websocket state for a provider.
    Keys: ws_connected (bool), last_tick_time (float|None — Unix epoch seconds)

    Both WebSocketManager (Upstox) and DhanWebSocketManager expose:
      - is_connected   (bool)
      - _last_tick_epoch (float, 0.0 if no tick yet) — real Unix epoch, not monotonic
    """
    feed = _registry.get(provider)
    if feed is None:
        return {"ws_connected": False, "last_tick_time": None}

    connected = getattr(feed, "is_connected", False)
    epoch = getattr(feed, "_last_tick_epoch", 0.0)
    last_tick = epoch if epoch > 0 else None

    return {
        "ws_connected": bool(connected),
        "last_tick_time": last_tick,
    }


def get_all_ws_state() -> Dict[str, dict]:
    return {provider: get_ws_state(provider) for provider in ("upstox", "dhan")}


def refresh_feed_credentials(provider: str, access_token: str, api_key: Optional[str] = None) -> bool:
    """
    Signal a registered live feed to adopt a new access token without a full restart.
    For Upstox: updates the auth object in place so the next reconnect uses the new token.
    For Dhan: updates the stored token and closes the current WS to trigger reconnection.
    Returns True if a running feed was found and signaled, False if no feed registered.
    """
    feed = _registry.get(provider)
    if feed is None:
        return False
    if hasattr(feed, "refresh_credentials"):
        feed.refresh_credentials(access_token, api_key=api_key)
    return True
