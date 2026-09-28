#!/bin/bash
# SafeEject Menu Bar App Launcher for macOS
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

if [ ! -f "bin/SafeEjectMenuBar" ]; then
    echo "Building SafeEjectMenuBar binary..."
    mkdir -p bin .build/cache
    swiftc -module-cache-path .build/cache -O ui/SafeEjectMenuBar.swift -o bin/SafeEjectMenuBar
fi

# Kill any previously running instance
killall SafeEjectMenuBar 2>/dev/null || true

echo "Starting SafeEject Menu Bar App..."
nohup "$DIR/bin/SafeEjectMenuBar" > /tmp/safeeject_app.log 2>&1 &
echo "SafeEject is now running in your macOS Menu Bar!"
echo "Look for the Red Diamond icon in your top Menu Bar."
