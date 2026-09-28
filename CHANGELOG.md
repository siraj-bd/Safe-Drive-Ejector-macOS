# Changelog

All notable changes to **Safe Drive Ejector Tool** will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [1.0.0] - 2026-09-28

### Initial Release — Native macOS Universal 2 (Apple Silicon & Intel)

- **Smart Graceful App Shutdown (Auto System)**:
  - Deep inspection of locking processes via `lsof` and `fuser` before unmount, sleep, deep sleep, or emergency eject.
  - Automatic graceful quit protocol via AppleScript with a 1.5-second buffer period allowing documents and state to save cleanly.
  - Automated SIGTERM/SIGKILL fallback for stubborn headless processes.
  - Completely prevents apps installed on or reading from external disks from freezing or crashing during drive sleep or unmount events.
- **Native macOS Menu Bar Experience**:
  - High-performance Swift menu bar application (`SafeEjectMenuBar`) with a sleek Red/Green Diamond status item.
  - Floating, borderless, zero-shadow glassmorphic control card rendered natively via WebKit.
  - Real-time two-way IPC bridge between Swift status bar daemon and front-end interface.
- **10-Drive Grid Matrix with `{ALL}` Master Toggle**:
  - Symmetrical 2-row layout hosting slots `1st` to `10th` with custom high-contrast red checkboxes.
  - Real-time status dots (Green = Mounted, Yellow/Amber = Sleeping/Unmounted, Gray = Empty slot).
  - Dedicated `{ALL}` master slot for instantaneous one-click selection of all connected external drives.
- **Dedicated Batch Action Center**:
  - Color-coded batch actions: `Mount` (green), `Unmount` (red), and `Finder` (blue).
  - Multi-drive batch unmount and mount execution in a single atomic sweep.
  - Reveal multiple mounted drives simultaneously in macOS Finder.
- **Strict Drive Selection Guard**:
  - Action guard preventing empty selection clicks on `Mount`, `Unmount`, and `Finder`.
  - Visual shake animation on the drive grid container, audio error chime, and descriptive notification warning.
- **Micro Audio and Notification Toggles**:
  - Compact `Sound` toggle switch with indicator for real-time mute/unmute of UI audio cues.
  - Compact `Notify` toggle switch with indicator for enabling/disabling desktop banners.
- **Hardware Deep Sleep & Silence (LED Off)**:
  - Cleanly flushes write buffers, tears down APFS/HFS+ containers (`diskutil unmountDisk`), and puts external disks and USB bridges into true hardware silence with **LED lights turned OFF** and zero background polling.
- **Normal Sleep vs Deep Sleep vs Eject Now**:
  - *Normal Sleep*: Unmounts target partitions while keeping hardware standby ready for quick touch-wake.
  - *Deep Sleep*: Complete container teardown and parent drive detachment (`diskutil eject`).
  - *Eject Now*: Instant one-click safe detachment sweep for all connected external storage.
- **Active Lock Inspector (`check-locks`)**:
  - Live inspection tool to list active PID, command name, and user holding open file locks on target external volumes.
- **Dynamic Scale Selector (1X, 1.25X, 1.5X, 2X)**:
  - Responsive zoom scaling tailored for any display resolution or viewing distance.
- **Universal 2 Architecture (`arm64` + `x86_64`)**:
  - Single standalone Drag-and-Drop DMG (`SafeDriveEjector-macOS-Universal.dmg`) running natively on Apple Silicon (M1/M2/M3/M4) and Intel Macs without Rosetta 2.
- **Zero External Python Dependencies**:
  - Pure Python standard library core engine integrating natively with macOS `diskutil`, `IOKit`, and `osascript`.
- **Comprehensive Test Suite**:
  - 83 unit and isolation tests covering disk discovery, parent precedence, graceful process shutdown, deep sleep isolation, and configuration persistence.
