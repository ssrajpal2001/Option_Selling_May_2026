#!/bin/bash
# Install all bot dependencies into the correct Python 3.11 environment.
# Run manually with:  cd bot && bash setup_env.sh
#
# Production mode (default): installs from requirements.lock for reproducible builds.
# Development mode:           installs from requirements.txt (unpinned source of truth).
#   Usage: bash setup_env.sh --dev
#
# This script uses the pyenv-managed Python 3.11.15 pip directly so packages
# never land in the system Python 3.9 by accident.

set -e

PYENV_PIP="$HOME/.pyenv/versions/3.11.15/bin/pip"
PYENV_PYTHON="$HOME/.pyenv/versions/3.11.15/bin/python"

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
REQUIREMENTS="$SCRIPT_DIR/requirements.txt"
LOCK_FILE="$SCRIPT_DIR/requirements.lock"

echo "========================================"
echo "  AlgoSoft — Python 3.11 env setup"
echo "========================================"

if [ ! -f "$PYENV_PIP" ]; then
    echo "ERROR: pyenv pip not found at $PYENV_PIP"
    echo "Make sure pyenv is installed and Python 3.11.15 is built:"
    echo "  pyenv install 3.11.15"
    exit 1
fi

echo "Python : $($PYENV_PYTHON --version)"
echo "pip    : $($PYENV_PIP --version)"
echo ""

DEV_MODE=false
if [ "${1:-}" = "--dev" ]; then
    DEV_MODE=true
fi

if [ "$DEV_MODE" = true ]; then
    echo "Mode   : development (requirements.txt)"
    echo "Reqs   : $REQUIREMENTS"
    echo ""
    echo "Installing packages..."
    $PYENV_PIP install --upgrade pip --quiet
    $PYENV_PIP install -r "$REQUIREMENTS"
else
    if [ ! -f "$LOCK_FILE" ]; then
        echo "ERROR: requirements.lock not found at $LOCK_FILE"
        echo "Generate it with: pip install -r requirements.txt && pip freeze > requirements.lock"
        exit 1
    fi
    echo "Mode   : production (requirements.lock)"
    echo "Lock   : $LOCK_FILE"
    echo ""
    echo "Installing packages..."
    $PYENV_PIP install --upgrade pip --quiet
    $PYENV_PIP install -r "$LOCK_FILE"
fi

echo ""
echo "========================================"
echo "  Setup complete — all packages installed"
echo "========================================"
