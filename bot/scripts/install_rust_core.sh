#!/bin/bash
set -e

# AlgoSoft V3: Rust Acceleration Installer
# Builds the high-performance Rust core and installs it into the local Python environment.
# Works on EC2 (system Python) and Replit (.pythonlibs virtual environment).

echo "--- AlgoSoft V3: Initializing Rust Acceleration ---"

# 1. Check for Rust/Cargo
if ! command -v cargo &> /dev/null; then
    echo "ERROR: Rust/Cargo not found. Install from https://rustup.rs/ then re-run."
    exit 1
fi
echo "✓ Rust: $(rustc --version)"

# 2. Install/Upgrade maturin
echo "Step 1: Installing maturin build tool..."
python3 -m pip install --upgrade maturin --quiet

# 3. Locate rust_core source relative to this script
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
RUST_CORE_DIR="$SCRIPT_DIR/../hub/sell_v3/rust_core"

if [ ! -d "$RUST_CORE_DIR" ]; then
    echo "ERROR: Cannot find Rust source at $RUST_CORE_DIR"
    exit 1
fi

cd "$RUST_CORE_DIR"
echo "Step 2: Building Rust core in $(pwd) ..."

# Detect Python virtual environment (Replit uses .pythonlibs)
PYTHON_PREFIX="$(python3 -c 'import sys; print(sys.prefix)')"
if [ -f "$PYTHON_PREFIX/pyvenv.cfg" ]; then
    echo "  Using virtualenv: $PYTHON_PREFIX"
    VIRTUAL_ENV="$PYTHON_PREFIX" python3 -m maturin develop --release
else
    # EC2 / system Python: build a wheel and install it
    echo "  No virtualenv detected — building wheel for system Python..."
    WHEEL_OUT="$(mktemp -d)"
    python3 -m maturin build --release --out "$WHEEL_OUT"
    WHEEL="$(ls "$WHEEL_OUT"/rust_core-*.whl | head -1)"
    echo "Step 3: Installing wheel: $WHEEL"
    python3 -m pip install "$WHEEL" --force-reinstall --quiet
    rm -rf "$WHEEL_OUT"
fi

echo ""
echo "--- SUCCESS: Rust Acceleration is now ACTIVE ---"
python3 -c "import rust_core; print('  Module path:', rust_core.__file__)"
