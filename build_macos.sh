#!/bin/bash
# ==============================================================================
# Safe Drive Ejector Tool - macOS Build & Packaging Script
# Supports:
#   1. Slice Build:      ./build_macos.sh --slice <arm64|x86_64>
#   2. Universal Build:  ./build_macos.sh --universal (merges arm64 + x86_64)
#   3. Native Fallback:  ./build_macos.sh (builds host arch or auto-merges if slices exist)
# ==============================================================================

set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

BUILD_DIR="$DIR/.build"
DIST_DIR="$DIR/dist"
APP_NAME="Safe Drive Ejector"
APP_BUNDLE="$DIST_DIR/$APP_NAME.app"
SLICES_DIR="$BUILD_DIR/slices"

MODE="auto"
ARCH_OVERRIDE=""

for arg in "$@"; do
    case "$arg" in
        --slice)
            MODE="slice"
            ;;
        --universal)
            MODE="universal"
            ;;
        arm64|x86_64)
            ARCH_OVERRIDE="$arg"
            ;;
        *)
            ;;
    esac
done

if [ "$MODE" = "auto" ]; then
    if [ "${ARCH:-}" = "universal2" ] || [ "${ARCH:-}" = "universal" ] || [ "${BUILD_UNIVERSAL:-}" = "true" ]; then
        MODE="universal"
    elif [ -f "$SLICES_DIR/arm64/SafeEjectMenuBar" ] && [ -f "$SLICES_DIR/x86_64/SafeEjectMenuBar" ]; then
        MODE="universal"
    fi
fi

HOST_ARCH="$(uname -m)"
TARGET_ARCH="${ARCH_OVERRIDE:-${ARCH:-$HOST_ARCH}}"

# ==============================================================================
# Helper Functions
# ==============================================================================
find_pyinstaller() {
    if command -v pyinstaller >/dev/null 2>&1; then
        echo "pyinstaller"
    elif [ -f ".venv/bin/pyinstaller" ]; then
        echo ".venv/bin/pyinstaller"
    else
        echo "ERROR: PyInstaller not found in PATH or .venv/bin/pyinstaller" >&2
        exit 1
    fi
}

compile_host_slice() {
    local arch="$1"
    echo "-------------------------------------------------------"
    echo "   Compiling macOS Slice: $arch (Host: $HOST_ARCH)"
    echo "-------------------------------------------------------"
    mkdir -p "$SLICES_DIR/$arch" "$BUILD_DIR/cache" "$BUILD_DIR/pyinstaller_config"

    local pyinstaller_bin
    pyinstaller_bin="$(find_pyinstaller)"

    echo "[1/2] Freezing Python core engine with PyInstaller ($arch)..."
    PYINSTALLER_CONFIG_DIR="$BUILD_DIR/pyinstaller_config" \
    "$pyinstaller_bin" \
        --clean \
        --noconfirm \
        --onefile \
        --name "safeeject_core_$arch" \
        --workpath "$BUILD_DIR/work_$arch" \
        --distpath "$SLICES_DIR/$arch" \
        --paths . \
        --collect-submodules core \
        --collect-submodules platform_adapters \
        --collect-submodules ui \
        main.py

    mv "$SLICES_DIR/$arch/safeeject_core_$arch" "$SLICES_DIR/$arch/safeeject_core"
    chmod +x "$SLICES_DIR/$arch/safeeject_core"

    echo "[2/2] Compiling native Swift Menu Bar binary ($arch)..."
    swiftc -module-cache-path "$BUILD_DIR/cache" -O ui/SafeEjectMenuBar.swift -o "$SLICES_DIR/$arch/SafeEjectMenuBar"
    chmod +x "$SLICES_DIR/$arch/SafeEjectMenuBar"

    echo "Slice for $arch successfully compiled into $SLICES_DIR/$arch/"
}

# ==============================================================================
# MODE: SLICE BUILD ONLY
# ==============================================================================
if [ "$MODE" = "slice" ]; then
    compile_host_slice "$TARGET_ARCH"
    exit 0
fi

# ==============================================================================
# MODE: UNIVERSAL BUILD (Apple Silicon + Intel)
# ==============================================================================
if [ "$MODE" = "universal" ]; then
    DMG_NAME="${DMG_NAME:-SafeDriveEjector-1.2.0-macOS-Universal.dmg}"
    DMG_OUTPUT="$DIST_DIR/$DMG_NAME"

    echo "======================================================="
    echo "   Building $APP_NAME for macOS (Universal 2: arm64 + x86_64)"
    echo "======================================================="

    # If arm64 slice is missing on Apple Silicon host, compile it
    if [ ! -f "$SLICES_DIR/arm64/SafeEjectMenuBar" ] || [ ! -f "$SLICES_DIR/arm64/safeeject_core" ]; then
        if [ "$HOST_ARCH" = "arm64" ]; then
            echo "ARM64 slice missing. Compiling natively on host..."
            compile_host_slice "arm64"
        else
            echo "ERROR: Missing ARM64 slice at $SLICES_DIR/arm64. Cannot build Universal package." >&2
            exit 1
        fi
    fi

    # Verify x86_64 slice presence
    if [ ! -f "$SLICES_DIR/x86_64/SafeEjectMenuBar" ] || [ ! -f "$SLICES_DIR/x86_64/safeeject_core" ]; then
        if [ "$HOST_ARCH" = "x86_64" ]; then
            echo "x86_64 slice missing. Compiling natively on host..."
            compile_host_slice "x86_64"
        else
            echo "ERROR: Missing x86_64 slice at $SLICES_DIR/x86_64. Cannot build Universal package." >&2
            exit 1
        fi
    fi

    echo "[1/6] Cleaning staging directory..."
    rm -rf "$APP_BUNDLE" "$DMG_OUTPUT" "$BUILD_DIR/universal_stage"
    mkdir -p "$APP_BUNDLE/Contents/MacOS" "$APP_BUNDLE/Contents/Resources" "$DIST_DIR"

    echo "[2/6] Merging native Swift Menu Bar binary with lipo..."
    lipo -create \
        "$SLICES_DIR/arm64/SafeEjectMenuBar" \
        "$SLICES_DIR/x86_64/SafeEjectMenuBar" \
        -output "$APP_BUNDLE/Contents/MacOS/SafeEjectMenuBar"
    chmod +x "$APP_BUNDLE/Contents/MacOS/SafeEjectMenuBar"

    echo "[3/6] Compiling Universal Python engine dispatcher..."
    cat << 'EOF' > "$BUILD_DIR/launcher.c"
#include <unistd.h>
#include <mach-o/dyld.h>
#include <limits.h>
#include <string.h>
#include <libgen.h>
#include <stdio.h>

int main(int argc, char *argv[]) {
    (void)argc;
    char exec_path[PATH_MAX];
    uint32_t size = sizeof(exec_path);
    if (_NSGetExecutablePath(exec_path, &size) != 0) {
        fprintf(stderr, "SafeEject: Error resolving executable path\n");
        return 1;
    }
    char *dir = dirname(exec_path);
    char target[PATH_MAX];
#if defined(__arm64__) || defined(__aarch64__)
    snprintf(target, sizeof(target), "%s/safeeject_core_arm64", dir);
#elif defined(__x86_64__)
    snprintf(target, sizeof(target), "%s/safeeject_core_x86_64", dir);
#else
    snprintf(target, sizeof(target), "%s/safeeject_core_arm64", dir);
#endif

    argv[0] = target;
    return execv(target, argv);
}
EOF
    clang -arch arm64 -arch x86_64 -O3 -Wall -Wextra "$BUILD_DIR/launcher.c" -o "$APP_BUNDLE/Contents/MacOS/safeeject_core"
    rm -f "$BUILD_DIR/launcher.c"
    chmod +x "$APP_BUNDLE/Contents/MacOS/safeeject_core"

    # Copy architecture-specific engine payloads
    cp "$SLICES_DIR/arm64/safeeject_core" "$APP_BUNDLE/Contents/MacOS/safeeject_core_arm64"
    cp "$SLICES_DIR/x86_64/safeeject_core" "$APP_BUNDLE/Contents/MacOS/safeeject_core_x86_64"
    chmod +x "$APP_BUNDLE/Contents/MacOS/safeeject_core_arm64"
    chmod +x "$APP_BUNDLE/Contents/MacOS/safeeject_core_x86_64"

    # Verify Universal 2 slices with lipo
    echo "Verifying Universal 2 slices:"
    lipo -info "$APP_BUNDLE/Contents/MacOS/SafeEjectMenuBar"
    lipo -info "$APP_BUNDLE/Contents/MacOS/safeeject_core"

    echo "[4/6] Copying resources and generating Info.plist..."
    cp "ui/components/SafeDriveEjectorCard.html" "$APP_BUNDLE/Contents/Resources/"
    if [ -f "assets/AppIcon.icns" ]; then
        cp "assets/AppIcon.icns" "$APP_BUNDLE/Contents/Resources/AppIcon.icns"
    fi

    cat << 'EOF' > "$APP_BUNDLE/Contents/Info.plist"
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleExecutable</key>
    <string>SafeEjectMenuBar</string>
    <key>CFBundleIdentifier</key>
    <string>com.user.safedriveejector</string>
    <key>CFBundleName</key>
    <string>Safe Drive Ejector</string>
    <key>CFBundleDisplayName</key>
    <string>Safe Drive Ejector</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>CFBundleShortVersionString</key>
    <string>1.2.0</string>
    <key>CFBundleVersion</key>
    <string>2</string>
    <key>LSMinimumSystemVersion</key>
    <string>12.0</string>
    <key>LSUIElement</key>
    <true/>
    <key>NSHighResolutionCapable</key>
    <true/>
    <key>NSPrincipalClass</key>
    <string>NSApplication</string>
    <key>CFBundleIconFile</key>
    <string>AppIcon</string>
</dict>
</plist>
EOF

    echo "[5/6] Inside-Out Code Signing & Strict Verification..."
    SIGNING_IDENTITY="${APPLE_SIGNING_IDENTITY:-${CODESIGN_IDENTITY:--}}"
    if [ "$SIGNING_IDENTITY" = "-" ]; then
        DETECTED_ID=$(security find-identity -v -p codesigning 2>/dev/null | { grep "Developer ID Application:" || true; } | head -1 | awk -F '"' '{print $2}')
        if [ -n "$DETECTED_ID" ]; then
            echo "Auto-detected Developer ID Application identity: $DETECTED_ID"
            SIGNING_IDENTITY="$DETECTED_ID"
        fi
    fi
    ENTITLEMENTS_FILE="$DIR/entitlements.plist"

    if [ "$SIGNING_IDENTITY" != "-" ]; then
        echo "Signing with Developer ID ($SIGNING_IDENTITY) and Hardened Runtime..."
        codesign --force --options runtime --timestamp --sign "$SIGNING_IDENTITY" --entitlements "$ENTITLEMENTS_FILE" "$APP_BUNDLE/Contents/MacOS/safeeject_core_arm64"
        codesign --force --options runtime --timestamp --sign "$SIGNING_IDENTITY" --entitlements "$ENTITLEMENTS_FILE" "$APP_BUNDLE/Contents/MacOS/safeeject_core_x86_64"
        codesign --force --options runtime --timestamp --sign "$SIGNING_IDENTITY" --entitlements "$ENTITLEMENTS_FILE" "$APP_BUNDLE/Contents/MacOS/safeeject_core"
        codesign --force --options runtime --timestamp --sign "$SIGNING_IDENTITY" --entitlements "$ENTITLEMENTS_FILE" "$APP_BUNDLE/Contents/MacOS/SafeEjectMenuBar"
        codesign --force --options runtime --timestamp --sign "$SIGNING_IDENTITY" --entitlements "$ENTITLEMENTS_FILE" "$APP_BUNDLE"
    else
        echo "Signing ad-hoc with valid sealed resources (inside-out)..."
        codesign --force --sign - --timestamp=none "$APP_BUNDLE/Contents/MacOS/safeeject_core_arm64"
        codesign --force --sign - --timestamp=none "$APP_BUNDLE/Contents/MacOS/safeeject_core_x86_64"
        codesign --force --sign - --timestamp=none "$APP_BUNDLE/Contents/MacOS/safeeject_core"
        codesign --force --sign - --timestamp=none "$APP_BUNDLE/Contents/MacOS/SafeEjectMenuBar"
        codesign --force --sign - --timestamp=none --entitlements "$ENTITLEMENTS_FILE" "$APP_BUNDLE"
    fi

    # Verify complete bundle signature
    codesign --verify --deep --strict "$APP_BUNDLE"
    echo "Code signature valid for Universal $APP_BUNDLE."

    echo "[6/6] Packaging Drag-and-Drop Universal DMG..."
    DMG_STAGE="$BUILD_DIR/dmg_stage"
    rm -rf "$DMG_STAGE"
    mkdir -p "$DMG_STAGE"

    if [ -d "/Volumes/$APP_NAME" ]; then
        hdiutil detach "/Volumes/$APP_NAME" -force 2>/dev/null || true
    fi

    cp -R "$APP_BUNDLE" "$DMG_STAGE/"
    ln -s /Applications "$DMG_STAGE/Applications"
    if [ -f "assets/AppIcon.icns" ]; then
        cp "assets/AppIcon.icns" "$DMG_STAGE/.VolumeIcon.icns"
    fi
    if [ -f "FIRST_LAUNCH_INSTRUCTIONS.txt" ]; then
        cp "FIRST_LAUNCH_INSTRUCTIONS.txt" "$DMG_STAGE/FIRST_LAUNCH_INSTRUCTIONS.txt"
    fi

    rm -f "$DMG_OUTPUT"

    hdiutil create \
        -volname "$APP_NAME" \
        -srcfolder "$DMG_STAGE" \
        -ov \
        -format UDZO \
        "$DMG_OUTPUT"

    rm -rf "$DMG_STAGE"

    if [ "$SIGNING_IDENTITY" != "-" ]; then
        echo "Signing DMG with $SIGNING_IDENTITY..."
        codesign --force --timestamp --sign "$SIGNING_IDENTITY" "$DMG_OUTPUT"
        codesign --verify --strict "$DMG_OUTPUT"
    fi

    echo ""
    echo "======================================================="
    echo "UNIVERSAL 2 BUILD & PACKAGING SUCCESSFUL!"
    echo "App Bundle: $APP_BUNDLE"
    echo "DMG Image:  $DMG_OUTPUT"
    echo "======================================================="
    exit 0
fi

# ==============================================================================
# MODE: SINGLE ARCHITECTURE STANDALONE BUILD (Fallback)
# ==============================================================================
DMG_NAME="${DMG_NAME:-SafeDriveEjector-1.2.0-macOS-${TARGET_ARCH}.dmg}"
DMG_OUTPUT="$DIST_DIR/$DMG_NAME"

echo "======================================================="
echo "   Building $APP_NAME for macOS ($TARGET_ARCH Single Arch)"
echo "======================================================="

rm -rf "$BUILD_DIR/work" "$BUILD_DIR/dist" "$APP_BUNDLE" "$DMG_OUTPUT"
mkdir -p "$BUILD_DIR/cache" "$BUILD_DIR/pyinstaller_config" "$DIST_DIR"

PYINSTALLER_BIN="$(find_pyinstaller)"

PYINSTALLER_CONFIG_DIR="$BUILD_DIR/pyinstaller_config" \
"$PYINSTALLER_BIN" \
    --clean \
    --noconfirm \
    --onefile \
    --name safeeject_core \
    --workpath "$BUILD_DIR/work" \
    --distpath "$BUILD_DIR/dist" \
    --paths . \
    --collect-submodules core \
    --collect-submodules platform_adapters \
    --collect-submodules ui \
    main.py

chmod +x "$BUILD_DIR/dist/safeeject_core"

swiftc -module-cache-path "$BUILD_DIR/cache" -O ui/SafeEjectMenuBar.swift -o "$BUILD_DIR/SafeEjectMenuBar"
chmod +x "$BUILD_DIR/SafeEjectMenuBar"

mkdir -p "$APP_BUNDLE/Contents/MacOS" "$APP_BUNDLE/Contents/Resources"
cp "$BUILD_DIR/SafeEjectMenuBar" "$APP_BUNDLE/Contents/MacOS/"
cp "$BUILD_DIR/dist/safeeject_core" "$APP_BUNDLE/Contents/MacOS/"
cp "ui/components/SafeDriveEjectorCard.html" "$APP_BUNDLE/Contents/Resources/"
if [ -f "assets/AppIcon.icns" ]; then
    cp "assets/AppIcon.icns" "$APP_BUNDLE/Contents/Resources/AppIcon.icns"
fi

cat << 'EOF' > "$APP_BUNDLE/Contents/Info.plist"
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleExecutable</key>
    <string>SafeEjectMenuBar</string>
    <key>CFBundleIdentifier</key>
    <string>com.user.safedriveejector</string>
    <key>CFBundleName</key>
    <string>Safe Drive Ejector</string>
    <key>CFBundleDisplayName</key>
    <string>Safe Drive Ejector</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>CFBundleShortVersionString</key>
    <string>1.2.0</string>
    <key>CFBundleVersion</key>
    <string>2</string>
    <key>LSMinimumSystemVersion</key>
    <string>12.0</string>
    <key>LSUIElement</key>
    <true/>
    <key>NSHighResolutionCapable</key>
    <true/>
    <key>NSPrincipalClass</key>
    <string>NSApplication</string>
    <key>CFBundleIconFile</key>
    <string>AppIcon</string>
</dict>
</plist>
EOF

SIGNING_IDENTITY="${APPLE_SIGNING_IDENTITY:-${CODESIGN_IDENTITY:--}}"
ENTITLEMENTS_FILE="$DIR/entitlements.plist"

codesign --force --sign - --timestamp=none "$APP_BUNDLE/Contents/MacOS/safeeject_core"
codesign --force --sign - --timestamp=none "$APP_BUNDLE/Contents/MacOS/SafeEjectMenuBar"
codesign --force --sign - --timestamp=none --entitlements "$ENTITLEMENTS_FILE" "$APP_BUNDLE"

codesign --verify --deep --strict "$APP_BUNDLE"

DMG_STAGE="$BUILD_DIR/dmg_stage"
rm -rf "$DMG_STAGE"
mkdir -p "$DMG_STAGE"

if [ -d "/Volumes/$APP_NAME" ]; then
    hdiutil detach "/Volumes/$APP_NAME" -force 2>/dev/null || true
fi

cp -R "$APP_BUNDLE" "$DMG_STAGE/"
ln -s /Applications "$DMG_STAGE/Applications"
if [ -f "assets/AppIcon.icns" ]; then
    cp "assets/AppIcon.icns" "$DMG_STAGE/.VolumeIcon.icns"
fi
if [ -f "FIRST_LAUNCH_INSTRUCTIONS.txt" ]; then
    cp "FIRST_LAUNCH_INSTRUCTIONS.txt" "$DMG_STAGE/FIRST_LAUNCH_INSTRUCTIONS.txt"
fi

rm -f "$DMG_OUTPUT"

hdiutil create \
    -volname "$APP_NAME" \
    -srcfolder "$DMG_STAGE" \
    -ov \
    -format UDZO \
    "$DMG_OUTPUT"

rm -rf "$DMG_STAGE"

echo "Build successful: $DMG_OUTPUT"
