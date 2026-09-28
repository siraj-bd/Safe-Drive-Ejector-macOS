"""
SafeEject Engine.
Orchestrates:
- Individual SSD selection for sleep
- Mac user inactivity detection (2m default)
- Auto-Active on Mac touch vs. Manual Stay Asleep modes
- Sleep Timer Presets (2m, 5m, 10m, 15m, 30m, 1h, 2h, Never)
- Audio feedback (Pop, Basso, Bubble, etc.)
- System sleep/wake and state persistence.
"""

import logging
import os
import subprocess
import sys
import time
from typing import List, Optional, Tuple

from core.config import SafeEjectConfig, StateManager
from core.idle_monitor import IdleMonitor
from core.models import DriveInfo, EjectResult, RemountResult
from core.volume_manager import SSDVolumeManager
from platform_adapters import get_platform_adapter
from platform_adapters.base import PlatformAdapter

logger = logging.getLogger("SafeEject.Engine")


class SafeEjectEngine:
    """Core controller coordinating volume management, idle monitoring, and power events."""

    def __init__(self, config: SafeEjectConfig = None, adapter: PlatformAdapter = None):
        self.config = config or SafeEjectConfig.load()
        self.adapter = adapter or get_platform_adapter()

        self.volume_manager = SSDVolumeManager(self.adapter, self.config)
        self.idle_monitor = IdleMonitor(
            self.config,
            on_idle_sleep=self._on_idle_sleep,
            on_touch_wake=self._on_touch_wake,
        )

        self.status_message: str = "Ready"

    def get_external_drives(self) -> List[DriveInfo]:
        """Returns physical external drives annotated with managed state, sleep selection, and idle seconds."""
        drives = self.volume_manager.get_all_external_drives()
        for d in drives:
            d.idle_seconds = self.idle_monitor.last_user_idle_seconds
        return drives

    def get_managed_drives(self) -> List[DriveInfo]:
        """Returns up to 6 managed external drives."""
        return [d for d in self.get_external_drives() if d.is_managed]

    def get_sleep_selected_drives(self) -> List[DriveInfo]:
        """Returns only the drives individually selected to sleep."""
        return [d for d in self.get_external_drives() if d.is_sleep_selected]

    def get_bottom_status(self) -> str:
        """Returns real-time status message formatted for the bottom area."""
        managed = self.get_managed_drives()
        return self.idle_monitor.get_status_summary(managed)

    def eject_now(self, targets: Optional[List[str]] = None) -> List[EjectResult]:
        """Immediate 1-click safe ejection of managed drives or selected targets."""
        results = self.volume_manager.eject_now(targets=targets)
        success = any(r.success for r in results)
        if success:
            if not self.config.deep_sleep_mode:
                StateManager.clear_ejected_drives()
            if targets:
                for t in targets:
                    self.idle_monitor.mark_drive_awake(t)
            else:
                self.idle_monitor.sleeping_drive_ids.clear()
            self.volume_manager.play_sound(self.config.success_sound)
        else:
            self.volume_manager.play_sound(self.config.failure_sound)
        self.status_message = f"Ejected {len(results)} drive(s)"
        return results

    def eject_all_external(self, manual: bool = False) -> List[EjectResult]:
        """Eject all managed external drives and record in state for remounting."""
        results = self.volume_manager.eject_now()
        success = any(r.success for r in results)
        if success:
            self.volume_manager.play_sound(self.config.success_sound)
        else:
            self.volume_manager.play_sound(self.config.failure_sound)
        self.status_message = f"Ejected {len(results)} drive(s)"
        return results

    def remount_all_ejected(self, only_if_recorded: bool = False) -> List[RemountResult]:
        """Remount drives and volumes that were previously safely ejected by SafeEject."""
        state = StateManager.load_ejected_drives()
        drive_ids = list(state.get("drive_ids", []))
        volume_ids = list(state.get("volume_identifiers", []))
        explicit_parents = set(p.lower() for p in state.get("explicit_sleep_parent_ids", []))
        results: List[RemountResult] = []

        if not drive_ids and not volume_ids:
            logger.info("No recorded sleeping drives/volumes found in state. Skipping remount.")
            self.status_message = "No sleeping drives to restore"
            return []

        # If only_if_recorded is True (auto-wake, touch-wake, system-wake):
        # EXCLUDE any drive or volume belonging to an explicit deep-sleep parent!
        if only_if_recorded:
            auto_wake_parents = set(p.lower() for p in StateManager.get_auto_wakeable_parent_ids())
            drive_ids = [d for d in drive_ids if d.lower() in auto_wake_parents]
            if hasattr(self.adapter, "resolve_parent_for_target"):
                volume_ids = [
                    v for v in volume_ids
                    if self.adapter.resolve_parent_for_target(v).lower() not in explicit_parents
                ]
            if not drive_ids and not volume_ids:
                logger.info("Auto-Wake: All sleeping drives are in explicit Deep Sleep. Skipping auto-remount.")
                self.status_message = "No auto-wakeable drives to restore"
                return []

        logger.info(f"Remounting {len(volume_ids)} volume(s) and {len(drive_ids)} drive(s)...")
        success_count = 0

        # 1. Remount recorded volumes (preserves selective wake)
        if volume_ids:
            for vid in volume_ids:
                res = self.volume_manager.mount_target(vid)
                results.append(res)
                if res.success:
                    success_count += 1
                    self.idle_monitor.mark_drive_awake(vid)
                else:
                    logger.warning(f"Could not remount volume {vid}: {res.message}")
        elif drive_ids:
            # 2. Remount full physical drives only if no specific volumes were recorded
            for drive_id in drive_ids:
                res = self.volume_manager.mount_target(drive_id)
                results.append(res)
                if res.success:
                    success_count += 1
                    self.idle_monitor.mark_drive_awake(drive_id)
                else:
                    logger.warning(f"Could not remount drive {drive_id}: {res.message}")

        if success_count > 0:
            self.volume_manager.play_sound(self.config.success_sound)
            if self.config.show_notifications:
                self.adapter.show_notification(
                    "SafeEject",
                    f"{success_count} external drive/volume item(s) remounted successfully.",
                )
        else:
            self.volume_manager.play_sound(self.config.failure_sound)

        self.status_message = f"Remounted {success_count} item(s)"
        return results

    def eject_single(self, target: str) -> EjectResult:
        """Manual unmount/eject of a single drive or volume."""
        res = self.volume_manager.unmount_target(target)
        if res.success:
            self.idle_monitor.mark_drive_asleep(target)
            self.volume_manager.play_sound(self.config.success_sound)
        else:
            self.volume_manager.play_sound(self.config.failure_sound)
        return res

    def remount_single(self, target: str) -> RemountResult:
        """Manual mount of a single drive or volume."""
        res = self.volume_manager.mount_target(target)
        if res.success:
            self.idle_monitor.mark_drive_awake(target)
            StateManager.remove_ejected_target(target)
            self.volume_manager.play_sound(self.config.success_sound)
        else:
            self.volume_manager.play_sound(self.config.failure_sound)
        return res

    def sleep_system(self) -> bool:
        """Puts the host computer to sleep."""
        logger.info("Triggering system sleep...")
        try:
            subprocess.run(["osascript", "-e", 'tell application "System Events" to sleep'], check=False)
            return True
        except Exception as e:
            logger.error(f"Failed to sleep system: {e}")
        return False

    def eject_and_sleep(self) -> bool:
        """Ejects all selected external SSDs and immediately sleeps the system."""
        self.eject_now()
        time.sleep(1.0)
        return self.sleep_system()

    def open_volume(self, target: str) -> bool:
        """Opens a mounted volume in macOS Finder."""
        mount_point = ""
        drives = self.get_external_drives()
        for d in drives:
            for v in d.volumes:
                if (
                    v.name.lower() == target.lower()
                    or v.device_id.lower() == target.lower()
                    or (v.mount_point and v.mount_point.lower() == target.lower())
                ):
                    if v.is_mounted and v.mount_point:
                        mount_point = v.mount_point
                        break
            if mount_point:
                break

        if not mount_point and os.path.exists(target):
            mount_point = target

        if mount_point and os.path.exists(mount_point):
            subprocess.run(["open", mount_point])
            return True
        return False

    def mount_and_open(self, target: str) -> bool:
        """Mounts an unmounted volume and opens it in Finder."""
        res = self.remount_single(target)
        time.sleep(0.8)
        return self.open_volume(target)

    def mount_and_open_all(self) -> int:
        """Mounts all external drives and opens each mounted volume in Finder."""
        self.remount_all_ejected()
        time.sleep(1.0)
        opened = 0
        drives = self.get_external_drives()
        for d in drives:
            for v in d.volumes:
                if v.is_mounted and v.mount_point:
                    subprocess.run(["open", v.mount_point])
                    opened += 1
        return opened

    def set_timer_preset(self, preset_key: str) -> bool:
        """Sets sleep timer preset (2m, 5m, 10m, 15m, 30m, 1h, 2h, never)."""
        ok = self.config.set_timer_preset(preset_key)
        if ok:
            self.status_message = f"Timer set to {self.config.sleep_timer_label}"
        return ok

    def set_wake_mode(self, mode: str) -> bool:
        """Sets wake mode: 'touch' (auto-active on Mac touch) or 'manual' (stay asleep until manual mount)."""
        clean_mode = mode.lower().strip()
        if clean_mode in ("touch", "manual"):
            self.config.wake_mode = clean_mode
            self.config.save()
            self.status_message = f"Wake mode: {'Auto-Active on Touch' if clean_mode == 'touch' else 'Stay Asleep until Manual Mount'}"
            return True
        return False

    def set_sleep_selection(self, target: str, selected: bool) -> Tuple[bool, str]:
        """Explicitly enable or disable individual SSD/partition selection for auto-sleep."""
        drives = self.get_external_drives()
        clean_target = target.strip().replace("/dev/", "").lower()
        target_uuid = target
        found = False

        # 1. Match parent drive ID or primary UUID
        for d in drives:
            d_clean = d.id.replace("/dev/", "").lower()
            if d_clean == clean_target or (d.primary_uuid and d.primary_uuid.lower() == clean_target):
                target_uuid = d.primary_uuid or d_clean
                found = True
                break

        # 2. If not matched, match child volume
        if not found:
            for d in drives:
                for v in d.volumes:
                    v_clean = v.device_id.replace("/dev/", "").lower()
                    if (
                        v_clean == clean_target
                        or (v.name and v.name.lower() == clean_target)
                        or (v.uuid and v.uuid.lower() == clean_target)
                    ):
                        target_uuid = v.uuid or v_clean
                        found = True
                        break
                if found:
                    break

        is_sel = self.config.set_drive_sleep_selected(target_uuid, selected)
        msg = f"Drive '{target}' auto-sleep: {'Enabled' if is_sel else 'Disabled'}."
        return is_sel, msg

    def set_all_sleep_selections(self, targets: List[str]) -> Tuple[bool, str]:
        """Sets the exact list of targets (parent drives and/or child partitions) for auto-sleep."""
        clean_targets = []
        drives = self.get_external_drives()
        for t in targets:
            if not t or t == "__none__":
                continue
            t_clean = t.strip().replace("/dev/", "").lower()
            resolved = False
            for d in drives:
                d_clean = d.id.replace("/dev/", "").lower()
                if d_clean == t_clean or (d.primary_uuid and d.primary_uuid.lower() == t_clean):
                    clean_targets.append(d.primary_uuid or d.id.replace("/dev/", ""))
                    resolved = True
                    break
                for v in d.volumes:
                    v_clean = v.device_id.replace("/dev/", "").lower()
                    if (
                        v_clean == t_clean
                        or (v.name and v.name.lower() == t_clean)
                        or (v.uuid and v.uuid.lower() == t_clean)
                    ):
                        clean_targets.append(v.uuid or v.device_id.replace("/dev/", ""))
                        resolved = True
                        break
                if resolved:
                    break
            if not resolved:
                clean_targets.append(t.strip().replace("/dev/", ""))

        self.config.set_selected_sleep_drives(clean_targets if clean_targets or targets else ["__none__"])
        msg = f"Auto-sleep targets updated: {clean_targets if clean_targets else 'None'}"
        return True, msg

    def toggle_sleep_selection(self, target: str) -> Tuple[bool, str]:
        """Toggle individual SSD selection for auto-sleep."""
        drives = self.get_external_drives()
        clean_target = target.strip().replace("/dev/", "").lower()
        target_uuid = target
        found = False

        # 1. Match parent drive ID or primary UUID
        for d in drives:
            d_clean = d.id.replace("/dev/", "").lower()
            if d_clean == clean_target or (d.primary_uuid and d.primary_uuid.lower() == clean_target):
                target_uuid = d.primary_uuid or d_clean
                found = True
                break

        # 2. If not matched, match child volume
        if not found:
            for d in drives:
                for v in d.volumes:
                    v_clean = v.device_id.replace("/dev/", "").lower()
                    if (
                        v_clean == clean_target
                        or (v.name and v.name.lower() == clean_target)
                        or (v.uuid and v.uuid.lower() == clean_target)
                    ):
                        target_uuid = v.uuid or v_clean
                        found = True
                        break
                if found:
                    break

        is_sel = self.config.toggle_sleep_drive_selection(target_uuid)
        msg = f"Drive '{target}' auto-sleep: {'Enabled' if is_sel else 'Disabled'}."
        return is_sel, msg

    def toggle_manage_drive(self, target: str) -> Tuple[bool, str]:
        """Toggle managed state for a drive (up to 6 max)."""
        return self.volume_manager.toggle_managed(target)

    def deep_sleep_target(self, target: str) -> EjectResult:
        """Dedicated Deep Sleep (Hardware Silence) for a physical parent drive or target."""
        res = self.volume_manager.deep_sleep_drive(target, is_explicit=True)
        if res.success:
            # Explicit Deep Sleep: Ensure target is purged from idle_monitor touch-wake set!
            self.idle_monitor.mark_drive_awake(target)
            self.volume_manager.play_sound(self.config.success_sound)
        else:
            self.volume_manager.play_sound(self.config.failure_sound)
        return res

    def eject_targets(self, targets: List[str]) -> List[EjectResult]:
        """
        Eject or unmount specific targets enforcing Parent Precedence:
        - If parent physical drive (e.g. disk7) is among targets, OR if all mounted child volumes are targeted:
          Execute Deep Sleep once for the entire SSD (eject parent, Hardware Silence).
          Any child partitions of this parent in targets are subsumed.
        - If only a subset of child partition(s) are targeted (e.g. disk8s1):
          Unmount partition only; never eject parent disk or turn LED off.
        """
        if not targets:
            return []

        all_drives = self.get_external_drives()
        clean_targets = [t.strip().replace("/dev/", "") for t in targets if t and t.strip()]
        results: List[EjectResult] = []
        handled_targets = set()

        # 1. Identify parent drives present in targets or whose all child volumes are targeted (Parent Precedence)
        for d in all_drives:
            d_id = d.id.replace("/dev/", "")
            valid_vols = [v for v in d.volumes if v.name != "EFI" and v.fs_type != "Apple_APFS"]
            valid_vids = [v.device_id.replace("/dev/", "") for v in valid_vols]
            mounted_vids = [v.device_id.replace("/dev/", "") for v in valid_vols if v.is_mounted]

            parent_targeted = d_id in clean_targets or (d.primary_uuid and d.primary_uuid in clean_targets)

            all_vols_targeted = False
            if valid_vids and all(
                vid in clean_targets or any(v.uuid and v.uuid in clean_targets for v in d.volumes if v.device_id.replace("/dev/", "") == vid)
                for vid in valid_vids
            ):
                all_vols_targeted = True
            elif mounted_vids and all(
                vid in clean_targets or any(v.uuid and v.uuid in clean_targets for v in d.volumes if v.device_id.replace("/dev/", "") == vid)
                for vid in mounted_vids
            ):
                if len(clean_targets) >= len(mounted_vids) and (len(clean_targets) > 1 or len(valid_vids) <= 1):
                    all_vols_targeted = True

            if parent_targeted or all_vols_targeted:
                if self.config.deep_sleep_mode:
                    logger.info(
                        f"Parent Precedence: Deep Sleep mode ON. "
                        f"Executing Hardware Deep Sleep for physical parent drive {d.id}."
                    )
                    res = self.volume_manager.deep_sleep_drive(
                        d.id,
                        is_explicit=True,
                    )
                else:
                    logger.info(
                        f"Parent Precedence: Deep Sleep mode OFF. "
                        f"Executing normal device eject for physical parent drive {d.id}."
                    )
                    res = self.adapter.eject_drive(d.id)

                results.append(res)
                if res.success:
                    # Explicit Deep Sleep must not enter the touch-wake queue.
                    self.idle_monitor.mark_drive_awake(d.id)
                    for v in d.volumes:
                        self.idle_monitor.mark_drive_awake(v.device_id)
                handled_targets.add(d_id)
                if d.primary_uuid:
                    handled_targets.add(d.primary_uuid)
                for v in d.volumes:
                    v_id = v.device_id.replace("/dev/", "")
                    handled_targets.add(v_id)
                    if v.uuid:
                        handled_targets.add(v.uuid)
                    if v.name:
                        handled_targets.add(v.name)

        # 2. Process remaining targets (child partitions whose parent was NOT targeted)
        for t in clean_targets:
            if t in handled_targets:
                continue
            logger.info(f"Partition unmount for: {t} (Parent remains awake)...")
            res = self.volume_manager.unmount_target(t)
            results.append(res)
            if res.success:
                self.idle_monitor.mark_drive_awake(t)
            handled_targets.add(t)

        success = any(r.success for r in results)
        if success:
            self.volume_manager.play_sound(self.config.success_sound)
        else:
            self.volume_manager.play_sound(self.config.failure_sound)
        return results

    def _on_idle_sleep(self, _targets: List[str] = None) -> List[EjectResult]:
        """
        Triggered when Mac is idle beyond the sleep timer (e.g. 2m/5m/10m/15m/30m/1h/2h)
        or before system sleep / logout:
        - If parent SSD (disk7) is selected: Deep Sleep (flush -> clean unmount children -> APFS container teardown -> parent eject -> Hardware Silence).
          Does not skip if children already unmounted.
        - If only child partition selected: Unmount partition only (parent stays awake, LED stays ON).
        - If both selected: Parent precedence applies (single Deep Sleep run).
        """
        all_drives = self.get_external_drives()
        if not all_drives:
            return []

        has_any_selection = any(d.is_sleep_selected or any(v.is_sleep_selected for v in d.volumes) for d in all_drives)
        has_managed = any(d.is_managed for d in all_drives)

        logger.info(f"Triggering Idle Sleep workflow (has_explicit_selection: {has_any_selection})...")
        sleep_count = 0
        results: List[EjectResult] = []

        for d in all_drives:
            # If no explicit selection, fallback to managed drives or all drives
            should_sleep_parent = d.is_sleep_selected or (not has_any_selection and (d.is_managed if has_managed else True))

            if should_sleep_parent:
                if self.config.deep_sleep_mode:
                    logger.info(
                        f"Idle Sleep (Deep Sleep Mode ON): Parent precedence for {d.id}. "
                        f"Executing Full Hardware Deep Sleep (LED OFF, manual mount only)..."
                    )
                    r = self.volume_manager.deep_sleep_drive(d.id, is_explicit=True)
                else:
                    logger.info(
                        f"Idle Sleep (Deep Sleep Mode OFF): Executing Normal Sleep for {d.id} "
                        f"(parent remains awake, touch wake enabled)..."
                    )
                    r = self.volume_manager.normal_sleep_drive(d.id)

                results.append(r)
                if r.success:
                    sleep_count += 1
                    self.idle_monitor.mark_drive_asleep(d.id)
                    for v in d.volumes:
                        self.idle_monitor.mark_drive_asleep(v.device_id)
                else:
                    logger.warning(f"Could not put drive {d.id} into sleep: {r.message}")
            else:
                # Parent NOT selected; check child partitions.
                for v in d.volumes:
                    if v.is_sleep_selected and v.is_mounted:
                        if self.config.deep_sleep_mode:
                            logger.info(
                                f"Idle Sleep (Deep Sleep Mode ON): Child {v.device_id} selected; "
                                f"executing Hardware Deep Sleep for physical parent {d.id}."
                            )
                            res = self.volume_manager.deep_sleep_drive(d.id, is_explicit=True)
                            results.append(res)
                            if res.success:
                                sleep_count += 1
                                self.idle_monitor.mark_drive_asleep(d.id)
                                for child in d.volumes:
                                    self.idle_monitor.mark_drive_asleep(child.device_id)
                            else:
                                logger.warning(
                                    f"Could not put drive {d.id} into Deep Sleep: {res.message}"
                                )
                            break

                        logger.info(
                            f"Idle Sleep (Deep Sleep Mode OFF): Unmounting child partition "
                            f"{v.device_id} ({v.name}); parent {d.id} remains awake."
                        )
                        res = self.adapter.unmount_volume(v.device_id)
                        results.append(res)
                        if res.success:
                            sleep_count += 1
                            StateManager.save_ejected_drives(
                                [],
                                [v.device_id],
                                append=True,
                                is_explicit_deep_sleep=False,
                            )
                            self.idle_monitor.mark_drive_asleep(v.device_id)
                        else:
                            logger.warning(
                                f"Could not unmount partition {v.device_id}: {res.message}"
                            )

        if sleep_count > 0:
            self.volume_manager.play_sound(self.config.success_sound)
            if self.config.show_notifications:
                self.adapter.show_notification(
                    "SafeEject - Deep Sleep",
                    f"{sleep_count} external drive item(s) safely put to sleep.",
                )
        return results

    def _on_touch_wake(self, sleeping_ids: List[str]):
        """Triggered when user touches the Mac and wake_mode == 'touch'."""
        if self.config.wake_mode != "touch":
            logger.debug("Mac touched, but wake_mode is manual. Keeping SSDs asleep.")
            return

        # CRITICAL RULE: Explicit Deep Sleep drives are NEVER awakened by touch wake!
        explicit_parents = set(p.lower() for p in StateManager.get_explicit_sleep_parent_ids())
        eligible_targets = []
        for target_id in sleeping_ids:
            parent_id = target_id
            if hasattr(self.adapter, "resolve_parent_for_target"):
                parent_id = self.adapter.resolve_parent_for_target(target_id)
            if parent_id.lower() in explicit_parents:
                logger.info(f"Touch-wake: Skipping explicit Deep Sleep target '{target_id}' (parent '{parent_id}' immune to touch wake).")
                continue
            eligible_targets.append(target_id)

        if not eligible_targets:
            logger.info("Touch-wake: No eligible idle-sleep targets to wake.")
            return

        logger.info(f"User touched Mac. Auto-activating {len(eligible_targets)} sleeping target(s)...")
        wake_count = 0
        for target_id in eligible_targets:
            res = self.volume_manager.mount_target(target_id)
            if res.success:
                wake_count += 1
                self.idle_monitor.mark_drive_awake(target_id)
            else:
                self.idle_monitor.mark_drive_asleep(target_id)

        if wake_count > 0:
            self.volume_manager.play_sound(self.config.success_sound)
            if self.config.show_notifications:
                self.adapter.show_notification(
                    "SafeEject - Auto-Active",
                    f"Mac active! {wake_count} SSD(s) remounted automatically.",
                )

    def run_daemon(self) -> None:
        """Run the sleep/wake watcher daemon and idle monitor."""
        logger.info("Starting SafeEject power listener and idle monitor...")

        self.idle_monitor.start()

        def on_sleep():
            logger.info("Power event: PRE-SLEEP triggered.")
            if self.config.eject_on_sleep:
                self._on_idle_sleep([])

        def on_wake():
            logger.info("Power event: WAKE triggered (Auto Awake).")
            if self.config.remount_on_wake:
                time.sleep(1.5)
                self.remount_all_ejected(only_if_recorded=True)

        try:
            self.adapter.start_power_listener(on_sleep, on_wake)
        finally:
            self.idle_monitor.stop()
