#!/data/data/com.termux/files/usr/bin/bash
set -euo pipefail

# Install Primal Space CLI inside Termux (ARM64).
# Termux:API companion app must also be installed from F-Droid for battery and
# Wi-Fi readings. Without it, the CLI still works with /proc and /sys fallbacks.

pkg install -y python termux-api
python -m pip install --upgrade pip setuptools wheel
python -m pip install --no-build-isolation .

primal --version
echo "Installed. Next: primal init --server <monitor-url> --enrollment-key <key>"
