import os
import sys
import subprocess
import signal
from datetime import datetime, timezone
from utils.logger import logger


class InstanceManager:
    """
    Manages per-client bot subprocess instances.
    Each client gets an isolated Python process running main.py
    with client-specific config injected via environment variables.
    """

    def __init__(self):
        self._processes: dict[int, subprocess.Popen] = {}

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
        })

        # Ensure logs directory exists using absolute path to prevent startup failures
        log_dir = os.path.join(os.getcwd(), "logs")
        os.makedirs(log_dir, exist_ok=True)

        log_file = os.path.join(log_dir, f"client_{client_id}_{broker}.log")
        env["CLIENT_LOG_FILE"] = log_file

        try:
            # We allow the subprocess to inherit stdout/stderr so that logs appear in the EC2 terminal.
            # The bot's internal logger (configured in main.py) will still write to the log_file
            # using a FileHandler, ensuring the UI's log tail functionality continues to work.
            proc = subprocess.Popen(
                [sys.executable, "main.py", "--client_mode"],
                env=env,
                stdout=None,
                stderr=None,
            )
            self._processes[instance_id] = proc
            logger.info(f"[InstanceManager] Started instance {instance_id} (PID {proc.pid}) for client {username}")
            return True, f"Bot started (PID {proc.pid})", proc.pid
        except Exception as e:
            logger.error(f"[InstanceManager] Failed to start instance {instance_id}: {e}")
            return False, f"Failed to start bot: {e}", None

    def stop_instance(self, instance_id: int) -> tuple[bool, str]:
        stopped = False
        proc = self._processes.get(instance_id)
        if proc:
            if proc.poll() is None:
                try:
                    proc.send_signal(signal.SIGTERM)
                    try:
                        proc.wait(timeout=5)
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

        if stopped:
            logger.info(f"[InstanceManager] Stopped instance {instance_id}")
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
        return result


instance_manager = InstanceManager()
