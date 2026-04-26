"""
ReconnectManager — background broker auto-reconnect scheduler.

When a broker session token expires or a live connection drops, the manager
schedules an asyncio task that calls the broker's headless login handler every
60 seconds, up to MAX_RETRIES (5) attempts.  State is persisted in-memory per
(user_id, broker) pair and exposed to the dashboard via the client API.
"""

import asyncio
from datetime import datetime, timezone, timedelta
from utils.logger import logger

IST = timezone(timedelta(hours=5, minutes=30))
MAX_RETRIES = 5
INTERVAL_SECONDS = 60


class _BrokerReconnectSession:
    """Holds mutable state for one (user_id, broker) reconnect loop."""

    def __init__(self, user_id: int, broker: str):
        self.user_id = user_id
        self.broker = broker
        self.attempts = 0
        self.last_attempt: str | None = None
        self.last_status = "pending"  # pending | success | failed | exhausted
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

    def get_status(self, user_id: int, broker: str) -> dict:
        session = self._sessions.get((user_id, broker))
        if session is None:
            return {"active": False, "attempts": 0, "max_attempts": MAX_RETRIES, "last_status": None}
        return session.as_dict()

    def is_active(self, user_id: int, broker: str) -> bool:
        session = self._sessions.get((user_id, broker))
        return session is not None and session.task is not None and not session.task.done()

    def schedule(self, user_id: int, broker: str, headless_login_fn) -> bool:
        """
        Start a background reconnect loop (asyncio task) for the given
        (user_id, broker) pair.  Idempotent — returns False if already running.

        headless_login_fn must be a *synchronous* callable that returns the new
        access-token string on success or None/falsy on failure.
        """
        if self.is_active(user_id, broker):
            return False

        session = _BrokerReconnectSession(user_id, broker)
        self._sessions[(user_id, broker)] = session

        async def _loop():
            for attempt in range(1, MAX_RETRIES + 1):
                await asyncio.sleep(INTERVAL_SECONDS)
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

            session.last_status = "exhausted"
            logger.warning(
                f"[ReconnectManager] {broker} exhausted {MAX_RETRIES} retries for user {user_id}"
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
