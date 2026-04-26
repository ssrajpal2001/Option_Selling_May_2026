"""
Log file cleanup utilities.

Keeps disk usage from the logs/ directory bounded by:
  - Deleting rotated backup files (*.log.1 … *.log.5) older than MAX_BACKUP_AGE_DAYS
  - Deleting ALL log files (including the primary .log) for clients that are no
    longer active *and* whose files have not been touched in MAX_INACTIVE_AGE_DAYS
"""

import os
import time
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "logs")

MAX_BACKUP_AGE_DAYS = 7
MAX_INACTIVE_AGE_DAYS = 14


def _log_dir() -> Path:
    return Path(LOG_DIR).resolve()


def _active_client_ids() -> set[int]:
    """Return the set of client IDs that have at least one active broker instance."""
    try:
        from web.db import db_fetchall
        rows = db_fetchall(
            "SELECT DISTINCT client_id FROM client_broker_instances WHERE status='running'"
        )
        return {int(r["client_id"]) for r in rows}
    except Exception as e:
        logger.warning(f"[LogCleanup] Could not query active clients: {e}")
        return set()


def _mtime_age_days(path: Path) -> float:
    """Return how many days ago the file was last modified."""
    try:
        return (time.time() - path.stat().st_mtime) / 86400
    except OSError:
        return 0.0


def get_log_disk_usage() -> dict:
    """
    Return a summary of disk usage in the logs/ directory.

    Returns:
        {
            "total_bytes": int,
            "total_files": int,
            "files": [{"name": str, "size_bytes": int, "age_days": float}, ...]
        }
    """
    log_dir = _log_dir()
    if not log_dir.exists():
        return {"total_bytes": 0, "total_files": 0, "files": []}

    files = []
    total_bytes = 0
    for p in sorted(log_dir.iterdir()):
        if p.is_file():
            try:
                size = p.stat().st_size
                age = _mtime_age_days(p)
                total_bytes += size
                files.append({
                    "name": p.name,
                    "size_bytes": size,
                    "age_days": round(age, 1),
                })
            except OSError:
                pass

    return {
        "total_bytes": total_bytes,
        "total_files": len(files),
        "files": files,
    }


def cleanup_old_logs(
    max_backup_age_days: int = MAX_BACKUP_AGE_DAYS,
    max_inactive_age_days: int = MAX_INACTIVE_AGE_DAYS,
    dry_run: bool = False,
) -> dict:
    """
    Delete stale log files.

    Rules:
      1. Rotated backup files (*.log.1 … *.log.9) older than max_backup_age_days
         are deleted regardless of client status.
      2. Primary log files matching client_<id>_<broker>.log for clients that are
         no longer running *and* that have not been touched in max_inactive_age_days
         are deleted.
      3. Any other file (e.g. github-push.log, sr_details.log) is left untouched.

    Args:
        max_backup_age_days: Age threshold for rotated backup files.
        max_inactive_age_days: Age threshold for primary logs of inactive clients.
        dry_run: If True, only report what would be deleted without deleting.

    Returns:
        {
            "deleted": [str, ...],   # file names that were (or would be) deleted
            "kept": [str, ...],      # file names kept
            "errors": [str, ...],    # any errors encountered
            "dry_run": bool,
        }
    """
    log_dir = _log_dir()
    deleted: list[str] = []
    kept: list[str] = []
    errors: list[str] = []

    if not log_dir.exists():
        logger.info("[LogCleanup] logs/ directory does not exist, nothing to clean.")
        return {"deleted": deleted, "kept": kept, "errors": errors, "dry_run": dry_run}

    active_ids = _active_client_ids()

    for p in sorted(log_dir.iterdir()):
        if not p.is_file():
            continue

        name = p.name
        age = _mtime_age_days(p)

        # ── Rotated backup files  *.log.1 … *.log.9 ──────────────────────
        parts = name.rsplit(".", 1)
        if len(parts) == 2 and parts[1].isdigit():
            if age >= max_backup_age_days:
                _delete(p, name, dry_run, deleted, errors, reason="old backup")
            else:
                kept.append(name)
            continue

        # ── Primary log files  client_<id>_<broker>.log ──────────────────
        if name.startswith("client_") and name.endswith(".log"):
            try:
                stem = name[len("client_"):-len(".log")]  # e.g. "42_zerodha"
                client_id_str = stem.split("_")[0]
                client_id = int(client_id_str)
            except (ValueError, IndexError):
                client_id = None

            if client_id is not None and client_id in active_ids:
                kept.append(name)
                continue

            if age >= max_inactive_age_days:
                _delete(p, name, dry_run, deleted, errors,
                        reason="inactive client, stale log")
            else:
                kept.append(name)
            continue

        # ── Everything else (e.g. github-push.log, sr_details.log) ───────
        kept.append(name)

    logger.info(
        f"[LogCleanup] {'DRY RUN — ' if dry_run else ''}deleted={len(deleted)} "
        f"kept={len(kept)} errors={len(errors)}"
    )
    return {"deleted": deleted, "kept": kept, "errors": errors, "dry_run": dry_run}


def _delete(path: Path, name: str, dry_run: bool,
            deleted: list, errors: list, reason: str = ""):
    if dry_run:
        deleted.append(name)
        logger.info(f"[LogCleanup] DRY RUN would delete: {name} ({reason})")
        return
    try:
        path.unlink()
        deleted.append(name)
        logger.info(f"[LogCleanup] Deleted: {name} ({reason})")
    except OSError as e:
        errors.append(f"{name}: {e}")
        logger.warning(f"[LogCleanup] Failed to delete {name}: {e}")
