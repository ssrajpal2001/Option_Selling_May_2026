#!/bin/bash
set -e
pnpm install --frozen-lockfile
pnpm --filter db push

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HOOK_FILE="$REPO_ROOT/.git/hooks/post-commit"

if [ ! -f "$HOOK_FILE" ] || ! grep -q "github-push.sh" "$HOOK_FILE" 2>/dev/null; then
  cat > "$HOOK_FILE" << 'HOOK_EOF'
#!/usr/bin/env bash
# Post-commit hook: auto-push to GitHub after every commit.
# Runs in the background so it never slows down or blocks the commit.
REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null)"
if [ -n "$REPO_ROOT" ] && [ -f "$REPO_ROOT/scripts/github-push.sh" ]; then
  "$REPO_ROOT/scripts/github-push.sh" &
fi
exit 0
HOOK_EOF
  chmod +x "$HOOK_FILE"
fi

# Rebuild the Rust acceleration module if cargo and the source are available.
RUST_CORE_SH="$REPO_ROOT/bot/scripts/install_rust_core.sh"
if command -v cargo &> /dev/null && [ -f "$RUST_CORE_SH" ]; then
  echo "Rebuilding Rust core acceleration module..."
  bash "$RUST_CORE_SH" || echo "WARNING: Rust core build failed — Python fallback remains active."
fi
