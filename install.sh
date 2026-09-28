#!/bin/bash
# Safe Drive Ejector Tool - Universal Installer
# Automatically detects macOS or Linux and sets up Menu Bar / System Tray integration.

set -e

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

OS="$(uname -s)"

echo "======================================================="
echo "   Safe Drive Ejector Tool - Native System Installer"
echo "======================================================="
echo "Detected Operating System: $OS"
echo ""

if [ "$OS" != "Darwin" ]; then
    echo "ERROR: Safe Drive Ejector Tool is designed natively for macOS."
    echo "Current operating system ($OS) is not supported."
    exit 1
fi

echo "--- Installing on macOS ---"

# 1. Compile native Swift Menu Bar binary
echo "[1/3] Compiling native Menu Bar app with Swift..."
mkdir -p bin .build/cache
swiftc -module-cache-path .build/cache -O ui/SafeEjectMenuBar.swift -o bin/SafeEjectMenuBar
chmod +x bin/SafeEjectMenuBar main.py run_menubar_mac.sh

# 2. Register LaunchAgent for auto-start at login
echo "[2/3] Configuring macOS LaunchAgent for auto-start..."
PLIST_SRC="install/macos/com.user.safeeject.plist"
PLIST_DEST="$HOME/Library/LaunchAgents/com.user.safeeject.plist"
mkdir -p "$HOME/Library/LaunchAgents"

# Inject current directory path into plist
sed -E "s|__SAFEEJECT_BIN_PATH__|$DIR/bin/SafeEjectMenuBar|g; s|<string>.*bin/SafeEjectMenuBar</string>|<string>$DIR/bin/SafeEjectMenuBar</string>|g" "$PLIST_SRC" > "$PLIST_DEST"

# 3. Load LaunchAgent & Launch app
echo "[3/3] Starting Safe Drive Ejector Tool in Menu Bar..."
killall SafeEjectMenuBar 2>/dev/null || true
sleep 0.5
launchctl unload "$PLIST_DEST" 2>/dev/null || true
launchctl load "$PLIST_DEST" 2>/dev/null || true

echo ""
echo "======================================================="
echo "SUCCESS! Safe Drive Ejector Tool is now running natively!"
echo "Check your top macOS Menu Bar for the Red Diamond icon."
echo "Clicking the icon will open the Safe Drive Ejector Card."
echo "======================================================="
