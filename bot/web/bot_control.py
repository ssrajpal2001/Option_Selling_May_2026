import asyncio
import logging
import os
import signal
import subprocess
import sys
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path
from fastapi import APIRouter
from fastapi.responses import JSONResponse

router = APIRouter()
PROJECT_ROOT = Path(__file__).parent.parent
BOT_PID_FILE = PROJECT_ROOT / "config" / "bot.pid"

_APP_LOG_MAX_BYTES = 10 * 1024 * 1024
_APP_LOG_BACKUP_COUNT = 3


def _stream_to_rotating_log(stream, log_path: Path) -> None:
    """Read lines from a binary stream and write them to a rotating log file.

    Runs in a daemon thread so the FastAPI process lifetime does not depend on
    it, and the subprocess's stdout pipe is continuously drained to prevent
    the bot from blocking on a full pipe buffer.
    """
    handler = RotatingFileHandler(
        str(log_path),
        maxBytes=_APP_LOG_MAX_BYTES,
        backupCount=_APP_LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(message)s"))
    try:
        for raw_line in stream:
            line = raw_line.decode("utf-8", errors="replace").rstrip("\n")
            record = logging.LogRecord(
                name="app", level=logging.INFO, pathname="",
                lineno=0, msg=line, args=(), exc_info=None,
            )
            handler.emit(record)
    except Exception as exc:
        sys.stderr.write(f"[bot_control] app.log stream error: {exc}\n")
    finally:
        handler.close()


def _find_bot_pid():
    if BOT_PID_FILE.exists():
        try:
            pid = int(BOT_PID_FILE.read_text().strip())
            os.kill(pid, 0)
            return pid
        except (ProcessLookupError, ValueError):
            BOT_PID_FILE.unlink(missing_ok=True)
    try:
        result = subprocess.run(
            ["pgrep", "-f", "python3 main.py"],
            capture_output=True, text=True
        )
        pids = [int(p) for p in result.stdout.strip().split() if p]
        return pids[0] if pids else None
    except Exception:
        return None


@router.get("/bot/status")
async def bot_status():
    pid = _find_bot_pid()
    return JSONResponse({"running": pid is not None, "pid": pid})


@router.post("/bot/restart")
async def restart_bot():
    pid = _find_bot_pid()
    if pid:
        try:
            os.kill(pid, signal.SIGTERM)
            await asyncio.sleep(2)
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        except ProcessLookupError:
            pass

    await asyncio.sleep(1)
    app_log = PROJECT_ROOT / "app.log"
    proc = subprocess.Popen(
        ["python3", "main.py"],
        cwd=str(PROJECT_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    BOT_PID_FILE.write_text(str(proc.pid))

    log_thread = threading.Thread(
        target=_stream_to_rotating_log,
        args=(proc.stdout, app_log),
        daemon=True,
    )
    log_thread.start()

    return JSONResponse({"success": True, "pid": proc.pid, "message": "Bot restarted successfully."})


@router.post("/bot/stop")
async def stop_bot():
    pid = _find_bot_pid()
    if not pid:
        return JSONResponse({"success": False, "message": "Bot is not running."})
    try:
        os.kill(pid, signal.SIGTERM)
        await asyncio.sleep(2)
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        BOT_PID_FILE.unlink(missing_ok=True)
        return JSONResponse({"success": True, "message": "Bot stopped."})
    except Exception as e:
        return JSONResponse({"success": False, "message": str(e)})
