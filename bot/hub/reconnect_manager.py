"""
ReconnectManager — background broker auto-reconnect scheduler.

When a broker session token expires or a live connection drops, the manager
schedules an asyncio task that calls the broker's headless login handler every
60 seconds, up to MAX_RETRIES (5) attempts.

Retry-cap safety: after a loop exhausts MAX_RETRIES, the session is recorded in
_exhausted with a timestamp.  A new schedule() call for the same (user_id, broker)
pair is blocked until EXHAUSTED_COOLDOWN_HOURS have elapsed (or until the user
explicitly calls clear_exhausted()).  This prevents unbounded restart loops.

Hub-driven: broker_manager calls schedule() on startup when a loaded instance has
a stale token, so reconnect runs even when the client dashboard is closed.
"""

import asyncio
from datetime import datetime, timezone, timedelta
from utils.logger import logger

IST = timezone(timedelta(hours=5, minutes=30))
MAX_RETRIES = 5
INTERVAL_SECONDS = 60
URGENT_INTERVAL_SECONDS = 30  # Used when open positions exist during disconnect
EXHAUSTED_COOLDOWN_HOURS = 1


class _BrokerReconnectSession:
    """Holds mutable state for one (user_id, broker) reconnect loop."""

    def __init__(self, user_id: int, broker: str):
        self.user_id = user_id
        self.broker = broker
        self.attempts = 0
        self.last_attempt: str | None = None
        self.last_status = "pending"  # pending | success | failed | exhausted | cancelled
        self.task: asyncio.Task | None = None

    def as_dict(self) -> dict:
        return {
            "active": self.task is not None and not self.task.done(),
            "attempts": self.attempts,
            "max_attempts": MAX_RETRIES,
            "last_status": self.last_status,
            "last_attempt": self.last_attempt,
        }


class ReconnectManager:
    """Singleton that manages background reconnect loops for all active users."""

    def __init__(self):
        self._sessions: dict[tuple[int, str], _BrokerReconnectSession] = {}
        # Maps (user_id, broker) → IST datetime when the loop was exhausted
        self._exhausted: dict[tuple[int, str], datetime] = {}

    # ── public query helpers ──────────────────────────────────────────────────

    def get_status(self, user_id: int, broker: str) -> dict:
        session = self._sessions.get((user_id, broker))
        if session is None:
            result = {
                "active": False, "attempts": 0, "max_attempts": MAX_RETRIES,
                "last_status": None, "last_attempt": None,
            }
        else:
            result = session.as_dict()
        # Append cooldown info when exhausted
        key = (user_id, broker)
        if key in self._exhausted:
            exhausted_at = self._exhausted[key]
            cooldown_until = exhausted_at + timedelta(hours=EXHAUSTED_COOLDOWN_HOURS)
            now = datetime.now(IST)
            if now < cooldown_until:
                result["exhausted_until"] = cooldown_until.isoformat()
                result["last_status"] = "exhausted"
            else:
                # Cooldown elapsed — clean up record so schedule() is unblocked
                del self._exhausted[key]
        return result

    def is_active(self, user_id: int, broker: str) -> bool:
        session = self._sessions.get((user_id, broker))
        return session is not None and session.task is not None and not session.task.done()

    def is_cooldown(self, user_id: int, broker: str) -> bool:
        """Returns True when the loop is in post-exhaustion cooldown."""
        key = (user_id, broker)
        if key not in self._exhausted:
            return False
        cooldown_until = self._exhausted[key] + timedelta(hours=EXHAUSTED_COOLDOWN_HOURS)
        if datetime.now(IST) >= cooldown_until:
            del self._exhausted[key]
            return False
        return True

    # ── public mutation helpers ───────────────────────────────────────────────

    def clear_exhausted(self, user_id: int, broker: str):
        """Allow immediate restart by discarding the cooldown record."""
        self._exhausted.pop((user_id, broker), None)

    def schedule(self, user_id: int, broker: str, headless_login_fn,
                 force: bool = False,
                 has_open_positions: bool = False,
                 on_exhausted_fn=None) -> bool:
        """
        Start a background reconnect loop (asyncio task) for the given
        (user_id, broker) pair.  Idempotent — returns False if already running.

        headless_login_fn must be a *synchronous* callable that returns the new
        access-token string on success or None/falsy on failure.

        Set force=True to override the exhaustion cooldown (e.g. manual retry).

        has_open_positions=True activates urgent mode:
          - First retry fires after URGENT_INTERVAL_SECONDS (30s) instead of the normal 60s.
          - A WARNING is logged immediately so operators act fast.

        on_exhausted_fn: optional async callable invoked when all retries are exhausted.
          Typical use: lambda: eod_squareoff() to close positions if reconnect fails.
        """
        if self.is_active(user_id, broker):
            return False

        # Enforce cooldown after exhaustion unless the caller explicitly forces
        if not force and self.is_cooldown(user_id, broker):
            key = (user_id, broker)
            cooldown_until = self._exhausted[key] + timedelta(hours=EXHAUSTED_COOLDOWN_HOURS)
            logger.warning(
                f"[ReconnectManager] {broker} for user {user_id} is in cooldown until "
                f"{cooldown_until.isoformat()} — schedule() blocked"
            )
            return False

        # Clear any stale exhaustion record when rescheduling
        self._exhausted.pop((user_id, broker), None)

        session = _BrokerReconnectSession(user_id, broker)
        self._sessions[(user_id, broker)] = session

        # Urgent mode: shorter retry interval and an immediate alert when positions are open.
        retry_interval = URGENT_INTERVAL_SECONDS if has_open_positions else INTERVAL_SECONDS
        if has_open_positions:
            logger.warning(
                f"[ReconnectManager] URGENT: {broker} disconnected for user {user_id} "
                f"with OPEN POSITIONS. Attempting reconnect every {retry_interval}s. "
                f"If {MAX_RETRIES} attempts fail, emergency square-off will trigger."
            )

        async def _loop():
            for attempt in range(1, MAX_RETRIES + 1):
                await asyncio.sleep(retry_interval)
                session.attempts = attempt
                session.last_attempt = datetime.now(IST).isoformat()
                logger.info(
                    f"[ReconnectManager] {broker} attempt {attempt}/{MAX_RETRIES} for user {user_id}"
                )
                try:
                    token = await asyncio.to_thread(headless_login_fn)
                    if token:
                        session.last_status = "success"
                        logger.info(
                            f"[ReconnectManager] {broker} reconnected for user {user_id} "
                            f"(attempt {attempt})"
                        )
                        return token
                    else:
                        session.last_status = "failed"
                        logger.warning(
                            f"[ReconnectManager] {broker} attempt {attempt} returned no token "
                            f"for user {user_id}"
                        )
                except asyncio.CancelledError:
                    session.last_status = "cancelled"
                    raise
                except Exception as exc:
                    session.last_status = "failed"
                    logger.warning(
                        f"[ReconnectManager] {broker} attempt {attempt} error "
                        f"for user {user_id}: {exc}"
                    )

            # All attempts exhausted — record timestamp and start cooldown
            session.last_status = "exhausted"
            self._exhausted[(user_id, broker)] = datetime.now(IST)
            logger.warning(
                f"[ReconnectManager] {broker} exhausted {MAX_RETRIES} retries for user {user_id}; "
                f"cooldown active for {EXHAUSTED_COOLDOWN_HOURS}h"
            )

            # When positions are open and we still can't reconnect, fire the emergency callback.
            if has_open_positions and on_exhausted_fn is not None:
                logger.critical(
                    f"[ReconnectManager] Reconnect FAILED with open positions for user {user_id}. "
                    f"Triggering emergency square-off."
                )
                try:
                    if asyncio.iscoroutinefunction(on_exhausted_fn):
                        await on_exhausted_fn()
                    else:
                        on_exhausted_fn()
                except Exception as cb_exc:
                    logger.error(
                        f"[ReconnectManager] Emergency square-off callback raised: {cb_exc}",
                        exc_info=True,
                    )

            return None

        task = asyncio.create_task(_loop())
        session.task = task
        return True

    def cancel(self, user_id: int, broker: str) -> bool:
        session = self._sessions.pop((user_id, broker), None)
        if session and session.task and not session.task.done():
            session.task.cancel()
            return True
        return False

    def cancel_all_for_user(self, user_id: int) -> int:
        keys = [k for k in self._sessions if k[0] == user_id]
        count = 0
        for k in keys:
            if self.cancel(*k):
                count += 1
        return count


reconnect_manager = ReconnectManager()
