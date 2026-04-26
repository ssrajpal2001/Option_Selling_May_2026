#!/bin/bash
# Install all bot dependencies into the correct Python 3.11 environment.
# Run manually with:  cd bot && bash setup_env.sh
#
# This script uses the pyenv-managed Python 3.11.15 pip directly so packages
# never land in the system Python 3.9 by accident.

set -e

PYENV_PIP="$HOME/.pyenv/versions/3.11.15/bin/pip"
PYENV_PYTHON="$HOME/.pyenv/versions/3.11.15/bin/python"

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
REQUIREMENTS="$SCRIPT_DIR/requirements.txt"

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
echo "Reqs   : $REQUIREMENTS"
echo ""
echo "Installing packages..."
$PYENV_PIP install --upgrade pip --quiet
$PYENV_PIP install -r "$REQUIREMENTS"

echo ""
echo "========================================"
echo "  Setup complete — all packages installed"
echo "========================================"
