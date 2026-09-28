#!/bin/bash
# Safe Drive Ejector Tool - Universal Uninstaller
# Completely stops services, unregisters LaunchAgent, removes configs, and cleans build caches.

set -e

echo "======================================================="
echo "   Safe Drive Ejector Tool - Uninstaller"
echo "======================================================="

# 1. Stop running processes
echo "[1/4] Stopping running SafeEject processes..."
killall SafeEjectMenuBar 2>/dev/null || true
pkill -f "main.py run" 2>/dev/null || true
pkill -f "main.py daemon" 2>/dev/null || true

# 2. Unload and delete LaunchAgent (macOS)
if [ "$(uname -s)" = "Darwin" ]; then
    echo "[2/4] Unregistering macOS LaunchAgent..."
    PLIST="$HOME/Library/LaunchAgents/com.user.safeeject.plist"
    if [ -f "$PLIST" ]; then
        launchctl unload "$PLIST" 2>/dev/null || true
        rm -f "$PLIST"
        echo "      Removed: $PLIST"
    fi
fi

# 3. Remove user configurations and state
echo "[3/4] Removing configuration and cache data..."
rm -rf "$HOME/.config/safe-eject" "$HOME/.config/safely-disk-ejector-tool"
rm -f /tmp/com.user.safeeject.lock /tmp/safeeject_app.log /tmp/safeeject_err.log
echo "      Cleaned: ~/.config/safe-eject"

# 4. Clean local build artifacts in workspace
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
echo "[4/4] Cleaning build artifacts..."
rm -rf "$DIR/bin/SafeEjectMenuBar" "$DIR/.build"
echo "      Cleaned: bin/SafeEjectMenuBar, .build/"

echo ""
echo "======================================================="
echo "SUCCESS: Safe Drive Ejector Tool has been completely uninstalled!"
echo "You can now run './install.sh' for a completely fresh installation."
echo "======================================================="
