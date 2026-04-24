#!/bin/bash
set -e

# AlgoSoft V3: Rust Acceleration Installer
# This script builds the high-performance Rust core and installs it into the local Python environment.

echo "--- AlgoSoft V3: Initializing Rust Acceleration ---"

# 1. Check for Rust/Cargo
if ! command -v cargo &> /dev/null; then
    echo "ERROR: Rust/Cargo not found. Please install Rust first: https://rustup.rs/"
    exit 1
fi

# 2. Install/Upgrade build tools
echo "Step 1: Installing build tools (maturin)..."
pip3 install --upgrade maturin

# 3. Build and Install the Rust Module
echo "Step 2: Building and installing Rust core module..."
cd hub/sell_v3/rust_core

# maturin build --release installs the module directly into the current environment
# If not in a venv, it will build a wheel and we'll install it manually.
maturin build --release --out ..

# Install the generated wheel
echo "Step 3: Installing the optimized binary..."
cd ..
pip3 install rust_core-*.whl --force-reinstall

# Cleanup
rm rust_core-*.whl

echo "--- SUCCESS: Rust Acceleration is now ACTIVE ---"
echo "Verification: Run 'python3 -c \"import rust_core; print(rust_core.__file__)\"'"
