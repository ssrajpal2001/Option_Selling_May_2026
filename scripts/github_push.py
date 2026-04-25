#!/usr/bin/env python3
"""
GitHub Auto-Push via Dulwich
Pushes the local 'main' branch to GitHub after each commit.
Reads GITHUB_TOKEN and GITHUB_REPO from environment variables.
Failures are logged but never raise an exception (always exits 0).
"""

import io
import os
import sys
import logging
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = REPO_ROOT / "logs"
LOG_FILE = LOG_DIR / "github-push.log"

LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler(LOG_FILE),
    ],
)
log = logging.getLogger("github-push")


def main() -> None:
    github_token = os.environ.get("GITHUB_TOKEN", "").strip()
    github_repo = os.environ.get("GITHUB_REPO", "").strip()

    if not github_token:
        log.error("GITHUB_TOKEN is not set — skipping push")
        return

    if not github_repo:
        log.error(
            "GITHUB_REPO is not set — skipping push "
            "(set it to owner/repo, e.g. ssrajpal2001/MyRepo)"
        )
        return

    try:
        from dulwich.repo import Repo
        from dulwich import porcelain
        from dulwich.errors import SendPackError, GitProtocolError, HangupException
    except ImportError:
        log.error("dulwich is not installed — skipping push (pip install dulwich)")
        return

    try:
        repo = Repo(str(REPO_ROOT))
    except Exception as exc:
        log.error("Could not open git repo at %s: %s", REPO_ROOT, exc)
        return

    try:
        head_ref = repo.refs.get_symrefs().get(b"HEAD")
        if head_ref is None:
            log.error("Could not resolve HEAD — skipping push")
            return
        current_branch = head_ref.decode("utf-8").removeprefix("refs/heads/")
    except Exception as exc:
        log.error("Could not determine current branch: %s", exc)
        return

    if current_branch != "main":
        log.info(
            "Current branch is '%s' (not 'main') — skipping push", current_branch
        )
        return

    remote_url = f"https://x-access-token:{github_token}@github.com/{github_repo}.git"
    refspec = b"refs/heads/main:refs/heads/main"

    def _sanitize(text: str) -> str:
        return text.replace(github_token, "***")

    log.info("Pushing 'main' to github.com/%s via dulwich ...", github_repo)
    try:
        _null = io.BytesIO()
        porcelain.push(
            str(REPO_ROOT),
            remote_url,
            refspecs=[refspec],
            outstream=_null,
            errstream=_null,
        )
        log.info("SUCCESS: Pushed 'main' to github.com/%s", github_repo)
    except (SendPackError, GitProtocolError, HangupException) as exc:
        log.error(
            "Push rejected by remote for github.com/%s: %s — "
            "remote may have diverged; no force-push will be attempted",
            github_repo,
            _sanitize(str(exc)),
        )
    except Exception as exc:
        log.error("Push failed for github.com/%s: %s", github_repo, _sanitize(str(exc)))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        try:
            log.error("Unhandled error in github_push.py: %s", exc)
        except Exception:
            pass
    sys.exit(0)
