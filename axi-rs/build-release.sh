#!/bin/bash
# Build Axi Rust binaries in release mode and copy to target locations.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

echo "Building release binaries..."
cargo build --release

echo ""
echo "Release binaries:"
ls -lh target/release/axi-bot target/release/procmux target/release/discordquery
echo ""
echo "To install systemd services:"
echo "  sudo cp systemd/axi-procmux.service /etc/systemd/system/"
echo "  sudo cp systemd/axi-bot.service /etc/systemd/system/"
echo "  sudo systemctl daemon-reload"
echo "  sudo systemctl enable axi-procmux axi-bot"
echo "  sudo systemctl start axi-procmux axi-bot"
