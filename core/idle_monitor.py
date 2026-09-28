"""
Idle Monitor Component.
Tracks Mac user idle time (keyboard, trackpad, mouse activity) and disk I/O.
Supports:
- Auto-Sleep: When Mac is idle for the preset duration (default 2 mins), puts individually selected SSDs to sleep.
- Auto-Active: When Mac is touched (user activity detected), automatically wakes/remounts the SSDs (if in 'touch' mode).
- Manual Mode: In 'manual' mode, SSDs stay asleep until the user explicitly mounts them via menu/CLI.
"""

import logging
import re
import subprocess
import sys
import threading
import time
from typing import Callable, List, Optional, Set

from core.config import SafeEjectConfig
from core.models import DriveInfo

logger = logging.getLogger("SafeEject.IdleMonitor")


class IdleMonitor:
    """Monitors user activity and disk idle times, handling touch wake and manual sleep modes."""

    def __init__(
        self,
        config: SafeEjectConfig,
        on_idle_sleep: Callable[[List[str]], None],
        on_touch_wake: Callable[[List[str]], None],
        check_interval_seconds: float = 2.0,
    ):
        self.config = config
        self.on_idle_sleep = on_idle_sleep
        self.on_touch_wake = on_touch_wake
        self.check_interval = check_interval_seconds

        # Set of drive IDs currently in idle sleep
        self.sleeping_drive_ids: Set[str] = set()

        # Last detected user idle time in seconds
        self.last_user_idle_seconds: float = 0.0
        self.is_user_currently_idle: bool = False

        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

        # Status message for bottom area display
        self.status_message: str = "Ready"

    def start(self):
        """Starts the background idle monitor loop."""
        with self._lock:
            if self._running:
                return
            self._running = True
            self._thread = threading.Thread(target=self._monitor_loop, daemon=True)
            self._thread.start()
            logger.info("IdleMonitor started (Mac touch-detection & idle sleep active).")

    def stop(self):
        """Stops the idle monitor loop."""
        with self._lock:
            self._running = False

    def mark_drive_awake(self, drive_id: str):
        """Called when a drive is manually or automatically mounted."""
        self.sleeping_drive_ids.discard(drive_id)

    def mark_drive_asleep(self, drive_id: str):
        """Called when a drive is put to sleep."""
        self.sleeping_drive_ids.add(drive_id)

    def get_mac_user_idle_seconds(self) -> float:
        """
        Returns seconds since the user last interacted with the Mac
        (mouse movement, keyboard stroke, trackpad touch).
        Uses IOHIDSystem HIDIdleTime on macOS.
        """
        if sys.platform == "darwin":
            try:
                proc = subprocess.run(
                    ["ioreg", "-c", "IOHIDSystem"],
                    capture_output=True,
                    text=True,
                    timeout=2,
                )
                match = re.search(r'\"HIDIdleTime\"\s*=\s*(\d+)', proc.stdout)
                if match:
                    nanos = int(match.group(1))
                    return nanos / 1_000_000_000.0
            except Exception:
                pass
        return 0.0

    def get_status_summary(self, managed_drives: List[DriveInfo]) -> str:
        """Generates a real-time status string for the bottom area."""
        timer_label = self.config.sleep_timer_label
        mode_desc = "Auto-Wake on Touch" if self.config.wake_mode == "touch" else "Manual Wake Only"

        if self.config.sleep_timer_seconds <= 0:
            return f"Timer: Never • Mode: {mode_desc}"

        if self.sleeping_drive_ids:
            return f"💤 {len(self.sleeping_drive_ids)} SSD(s) in Sleep • {mode_desc}"

        remaining = max(0.0, self.config.sleep_timer_seconds - self.last_user_idle_seconds)
        mins = int(remaining // 60)
        secs = int(remaining % 60)

        return f"⏱ Idle Sleep in {mins:02d}:{secs:02d} (Limit: {timer_label}) • {mode_desc}"

    def _monitor_loop(self):
        """Checks Mac user idle time and triggers sleep or touch-wake."""
        while self._running:
            try:
                self._check_state()
            except Exception as e:
                logger.debug(f"IdleMonitor loop error: {e}")

            time.sleep(self.check_interval)

    def _check_state(self):
        timeout_limit = self.config.sleep_timer_seconds
        idle_secs = self.get_mac_user_idle_seconds()
        self.last_user_idle_seconds = idle_secs

        # Case 1: Mac was idle, and user just touched the Mac!
        # (idle_secs < 2.0 and we had sleeping drives)
        if idle_secs < 2.5:
            if self.is_user_currently_idle:
                self.is_user_currently_idle = False
                logger.debug("User touch detected on Mac.")

                # If wake_mode is "touch", wake up the sleeping SSDs
                if self.config.wake_mode == "touch" and self.sleeping_drive_ids:
                    logger.info(
                        f"Mac touched! Auto-activating {len(self.sleeping_drive_ids)} sleeping SSD(s)..."
                    )
                    to_wake = list(self.sleeping_drive_ids)
                    self.sleeping_drive_ids.clear()
                    self.on_touch_wake(to_wake)
                elif self.config.wake_mode == "manual" and self.sleeping_drive_ids:
                    logger.debug(
                        "Mac touched, but wake_mode is 'manual'. SSDs remain asleep until manual mount."
                    )

        # Case 2: Mac has been idle beyond timeout limit
        if timeout_limit > 0 and idle_secs >= timeout_limit:
            if not self.is_user_currently_idle:
                self.is_user_currently_idle = True
                logger.info(
                    f"Mac has been idle for {int(idle_secs)}s (>= {timeout_limit}s). Triggering auto-sleep for selected SSDs..."
                )
                self.on_idle_sleep([])
