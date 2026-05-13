import os
import sys
import subprocess
import signal
import logging
from datetime import datetime, timezone

# Avoid importing utils.logger while this module is being initialized.  The web
# app imports hub modules during startup, and importing utils.logger here can
# participate in circular imports that leave hub.instance_manager partially
# initialized without the singleton below.
logger = logging.getLogger('UpstoxApp')


class InstanceManager:
    """
    Manages per-client bot subprocess instances.
    Each client gets an isolated Python process running main.py
    with client-specific config injected via environment variables.
    """

    def __init__(self):
        self._processes: dict[int, subprocess.Popen] = {}
        self._log_fds: dict[int, object] = {}

    def start_instance(
        self,
        instance_id: int,
        client_id: int,
        username: str,
        broker: str,
        instrument: str,
        quantity: int,
        strategy_version: str,
        trading_mode: str,
        api_key: str,
        access_token: str,
    ) -> tuple[bool, str, int]:
        if instance_id in self._processes:
            proc = self._processes[instance_id]
            if proc.poll() is None:
                return False, "Instance is already running.", proc.pid
            del self._processes[instance_id]

        env = os.environ.copy()
        from web.db import db_fetchone
        from web.auth import decrypt_secret
        instance_row = db_fetchone("SELECT password_encrypted, totp_encrypted, broker_user_id_encrypted, api_secret_encrypted FROM client_broker_instances WHERE id=?", (instance_id,))
        password = decrypt_secret(instance_row["password_encrypted"]) if instance_row and instance_row["password_encrypted"] else ""
        totp = decrypt_secret(instance_row["totp_encrypted"]) if instance_row and instance_row["totp_encrypted"] else ""
        broker_user_id = decrypt_secret(instance_row["broker_user_id_encrypted"]) if instance_row and instance_row["broker_user_id_encrypted"] else ""
        api_secret = decrypt_secret(instance_row["api_secret_encrypted"]) if instance_row and instance_row["api_secret_encrypted"] else ""

        # Propagate client's assigned Elastic IP into the subprocess so the broker
        # layer can bind all outbound connections to that source address.
        user_row = db_fetchone("SELECT static_ip FROM users WHERE id=?", (client_id,))
        static_ip = (user_row.get("static_ip") or "") if user_row else ""

        env.update({
            "CLIENT_ID": str(client_id),
            "CLIENT_USERNAME": username,
            "CLIENT_BROKER": broker,
            "CLIENT_INSTRUMENT": instrument,
            "CLIENT_QUANTITY": str(quantity),
            "CLIENT_STRATEGY_VERSION": strategy_version,
            "CLIENT_TRADING_MODE": trading_mode,
            "CLIENT_API_KEY": api_key,
            "CLIENT_API_SECRET": api_secret,
            "CLIENT_ACCESS_TOKEN": access_token,
            "CLIENT_PASSWORD": password,
            "CLIENT_TOTP": totp,
            "CLIENT_BROKER_USER_ID": broker_user_id,
            "CLIENT_INSTANCE_ID": str(instance_id),
            "CLIENT_STATIC_IP": static_ip,
        })

        # Ensure logs directory exists using absolute path to prevent startup failures
        log_dir = os.path.join(os.getcwd(), "logs")
        os.makedirs(log_dir, exist_ok=True)

        log_file = os.path.join(log_dir, f"client_{client_id}_{broker}.log")
        env["CLIENT_LOG_FILE"] = log_file

        log_fd = None
        try:
            # Redirect both stdout and stderr to the log file so that unhandled exceptions
            # and tracebacks appear in the admin Logs tab rather than disappearing into
            # the uvicorn terminal.  Open in append mode so existing log lines are kept.
            log_fd = open(log_file, 'a')
            proc = subprocess.Popen(
                [sys.executable, "main.py", "--client_mode"],
                env=env,
                stdout=log_fd,
                stderr=log_fd,
                start_new_session=True,  # isolate from uvicorn console group — Ctrl+C won't auto-square-off
            )
            self._processes[instance_id] = proc
            self._log_fds[instance_id] = log_fd
            logger.info(f"[InstanceManager] Started instance {instance_id} (PID {proc.pid}) for client {username}")
            try:
                from utils.notifier import notify_admin_instance_event
                notify_admin_instance_event(username, "started", trading_mode, instrument)
            except Exception as _tge:
                logger.debug(f"[InstanceManager] Admin Telegram start alert failed: {_tge}")
            return True, f"Bot started (PID {proc.pid})", proc.pid
        except Exception as e:
            if log_fd:
                try:
                    log_fd.close()
                except Exception:
                    pass
            logger.error(f"[InstanceManager] Failed to start instance {instance_id}: {e}")
            return False, f"Failed to start bot: {e}", None

    def stop_instance(self, instance_id: int, reason: str = "") -> tuple[bool, str]:
        stopped = False
        proc = self._processes.get(instance_id)
        if proc:
            if proc.poll() is None:
                try:
                    if sys.platform == 'win32':
                        # CTRL_BREAK_EVENT works in new process group and lets the bot do graceful square-off
                        try:
                            os.kill(proc.pid, signal.CTRL_BREAK_EVENT)
                        except Exception:
                            proc.terminate()
                    else:
                        proc.send_signal(signal.SIGTERM)
                    try:
                        proc.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                    stopped = True
                except: pass
            del self._processes[instance_id]

        # Fallback: Check DB for PID and kill it (handles server restarts)
        try:
            from web.db import db_fetchone
            row = db_fetchone("SELECT bot_pid FROM client_broker_instances WHERE id=?", (instance_id,))
            if row and row["bot_pid"]:
                pid = int(row["bot_pid"])
                try:
                    os.kill(pid, signal.SIGTERM)
                    # Small wait to see if it exits
                    import time
                    time.sleep(1)
                    try:
                        os.kill(pid, 0)
                        # Still alive? Force kill
                        os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    stopped = True
                except ProcessLookupError:
                    stopped = True # Already dead
        except Exception as e:
            logger.error(f"[InstanceManager] Error killing PID for instance {instance_id}: {e}")

        # Close the log file descriptor opened during start_instance
        log_fd = self._log_fds.pop(instance_id, None)
        if log_fd:
            try:
                log_fd.flush()
                log_fd.close()
            except Exception:
                pass

        if stopped:
            logger.info(f"[InstanceManager] Stopped instance {instance_id}")
            try:
                from web.db import db_fetchone
                inst_row = db_fetchone(
                    "SELECT u.username, cbi.trading_mode, cbi.instrument "
                    "FROM client_broker_instances cbi "
                    "JOIN users u ON u.id = cbi.client_id "
                    "WHERE cbi.id=?",
                    (instance_id,)
                )
                if inst_row:
                    from utils.notifier import notify_admin_instance_event
                    notify_admin_instance_event(
                        inst_row["username"], "stopped",
                        inst_row.get("trading_mode", ""),
                        inst_row.get("instrument", ""),
                        reason=reason,
                    )
            except Exception as _tge:
                logger.debug(f"[InstanceManager] Admin Telegram stop alert failed: {_tge}")
            return True, "Bot stopped."
        return False, "No running instance found."

    def stop_all_for_client(self, client_id: int):
        from web.db import db_fetchall, db_execute
        instances = db_fetchall(
            "SELECT id FROM client_broker_instances WHERE client_id=?", (client_id,)
        )
        for row in instances:
            iid = row["id"]
            self.stop_instance(iid)
            db_execute("UPDATE client_broker_instances SET status='idle', bot_pid=NULL WHERE id=?", (iid,))

    def get_instance_status(self, instance_id: int) -> dict:
        proc = self._processes.get(instance_id)
        if not proc:
            return {"running": False, "pid": None}
        alive = proc.poll() is None
        return {
            "running": alive,
            "pid": proc.pid if alive else None,
        }

    def list_running(self) -> list:
        result = []
        for iid, proc in list(self._processes.items()):
            if proc.poll() is None:
                result.append({"instance_id": iid, "pid": proc.pid})
            else:
                del self._processes[iid]
                # Close the log fd for naturally-exited processes to avoid FD leaks
                log_fd = self._log_fds.pop(iid, None)
                if log_fd:
                    try:
                        log_fd.flush()
                        log_fd.close()
                    except Exception:
                        pass
        return result


instance_manager = InstanceManager()
