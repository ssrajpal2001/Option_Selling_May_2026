#!/usr/bin/env bash
# =============================================================================
# GitHub Auto-Push — Shell Wrapper
# Calls scripts/github_push.py (dulwich-based).
# Always exits 0 so it never blocks the commit workflow.
# =============================================================================

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

PYTHON=""
for candidate in \
  "$SCRIPT_DIR/../.pythonlibs/bin/python3" \
  "$(command -v python3 2>/dev/null)" \
  "$(command -v python 2>/dev/null)"; do
  if [ -x "$candidate" ] && "$candidate" -c "import dulwich" 2>/dev/null; then
    PYTHON="$candidate"
    break
  fi
done

if [ -z "$PYTHON" ]; then
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] ERROR: No Python with dulwich found — skipping push" \
    >> "$SCRIPT_DIR/../logs/github-push.log"
  exit 0
fi

exec "$PYTHON" "$SCRIPT_DIR/github_push.py"
