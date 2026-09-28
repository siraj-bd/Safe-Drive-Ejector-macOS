"""
SSD Volume Manager & Drive Controller.
Coordinates volume mounting/unmounting, up-to-6 managed drives selection,
Eject Now logic, and drive state refreshing.
"""

import logging
import os
import re
import subprocess
import sys
import threading
from typing import List, Optional, Tuple

from core.config import MAX_MANAGED_DRIVES, SafeEjectConfig, StateManager
from core.models import DriveInfo, EjectResult, RemountResult
from platform_adapters.base import PlatformAdapter

logger = logging.getLogger("SafeEject.VolumeManager")


class SSDVolumeManager:
    """Manages volume-level operations and tracks up to 6 managed external SSDs/HDDs."""

    def __init__(self, adapter: PlatformAdapter, config: SafeEjectConfig):
        self.adapter = adapter
        self.config = config

    def get_all_external_drives(self) -> List[DriveInfo]:
        """Fetch all physical external drives, annotating whether they are managed and sleep-selected."""
        all_drives = self.adapter.get_drives()
        external_drives: List[DriveInfo] = []

        for d in all_drives:
            if d.is_external and not d.is_virtual:
                # Check if managed
                d.is_managed = self.config.is_drive_managed(d.primary_uuid, drive_id=d.id)
                # Check if individually selected for sleep
                d.is_sleep_selected = self.config.is_drive_sleep_selected(d.primary_uuid, drive_id=d.id)

                for v in d.volumes:
                    v.is_managed = self.config.is_drive_managed(v.uuid or v.device_id, drive_id=d.id)
                    v.is_sleep_selected = self.config.is_drive_sleep_selected(v.uuid or v.device_id, drive_id=d.id)

                external_drives.append(d)

        # Enforce maximum of 6 managed drives
        managed_count = 0
        for d in external_drives:
            if d.is_managed:
                managed_count += 1
                if managed_count > MAX_MANAGED_DRIVES:
                    d.is_managed = False

        return external_drives

    def play_sound(self, sound_name: str):
        """Play a macOS system sound if sounds are enabled without leaking subprocesses."""
        if not self.config.play_sounds:
            return
        sound_file = f"/System/Library/Sounds/{sound_name}.aiff"
        if not os.path.exists(sound_file):
            return

        def _play():
            try:
                subprocess.run(
                    ["afplay", sound_file],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=5,
                    check=False,
                )
            except Exception:
                pass

        threading.Thread(target=_play, daemon=True).start()

    def get_managed_drives(self) -> List[DriveInfo]:
        """Return only the managed external drives (up to 6)."""
        drives = self.get_all_external_drives()
        return [d for d in drives if d.is_managed]

    def eject_now(self, targets: Optional[List[str]] = None) -> List[EjectResult]:
        """
        'Eject Now' Logic:
        If specific targets are provided, immediately safely unmounts/ejects ONLY those targets.
        If no targets are provided, ejects all managed external drives.
        """
        results: List[EjectResult] = []
        ejected_ids: List[str] = []
        ejected_vols: List[str] = []

        if targets:
            logger.info(f"Executing 'Eject Now' for {len(targets)} selected target(s): {targets}")
            clean_targets = [t.strip().replace("/dev/", "") for t in targets if t and t.strip()]
            all_drives = self.get_all_external_drives()
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
                            f"Parent Precedence in Eject Now: Deep Sleep mode ON. "
                            f"Executing Hardware Deep Sleep for physical parent drive {d.id}..."
                        )
                        res = self.deep_sleep_drive(d.id, is_explicit=True)
                    else:
                        logger.info(
                            f"Parent Precedence in Eject Now: Deep Sleep mode OFF. "
                            f"Executing normal device eject for physical parent drive {d.id}..."
                        )
                        res = self.adapter.eject_drive(d.id)

                    results.append(res)
                    if res.success:
                        ejected_ids.append(d.id)
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
                res = self.unmount_target(t)
                results.append(res)
                if res.success:
                    if "s" in t.lower():
                        ejected_vols.append(t)
                    else:
                        ejected_ids.append(t)
                handled_targets.add(t)

            if ejected_ids or ejected_vols:
                StateManager.save_ejected_drives(ejected_ids, ejected_vols)
            if self.config.show_notifications:
                self.adapter.show_notification(
                    "SafeEject",
                    f"Safely unmounted/ejected {len(results)} target(s).",
                )
            return results

        logger.info("Executing 'Eject Now' for all managed drives...")
        managed = self.get_managed_drives()

        for drive in managed:
            # Check exclusions (volume name, volume UUID, or drive ID)
            is_excluded = False
            for v in drive.volumes:
                if self.config.is_excluded(volume_name=v.name, volume_uuid=v.uuid, drive_id=drive.id):
                    is_excluded = True
                    break
            if is_excluded or self.config.is_excluded("", drive_id=drive.id):
                continue

            if self.config.deep_sleep_mode:
                logger.info(
                    f"Eject Now: Deep Sleep mode ON. "
                    f"Executing Hardware Deep Sleep for physical parent drive {drive.id}..."
                )
                res = self.deep_sleep_drive(drive.id, is_explicit=True)
            else:
                logger.info(
                    f"Eject Now: Deep Sleep mode OFF. "
                    f"Executing normal device eject for physical parent drive {drive.id}..."
                )
                res = self.adapter.eject_drive(drive.id)

            results.append(res)
            if res.success:
                ejected_ids.append(drive.id)

        if ejected_ids or ejected_vols:
            StateManager.save_ejected_drives(ejected_ids, ejected_vols)
        if self.config.show_notifications:
            self.adapter.show_notification(
                "SafeEject",
                f"Safely ejected {len(results)} drive(s) successfully.",
            )

        return results

    def mount_target(self, target_id: str) -> RemountResult:
        """
        Mount a specific target (volume or parent drive) following safe wake order:
        1. Explicit wake/mount operation requested.
        2. Resolve physical parent for target.
        3. Execute mount on target.
        4. VERIFY mount success.
        5. ONLY on verified success: clear sleeping parent state in StateManager so discovery resumes.
        """
        logger.info(f"Manual Mount requested for: {target_id}")
        clean_target = target_id.strip()
        clean_id = clean_target.replace("/dev/", "")

        # Resolve physical parent using adapter logic / metadata
        parent_id = ""
        if hasattr(self.adapter, "resolve_parent_for_target"):
            parent_id = self.adapter.resolve_parent_for_target(clean_id)

        drives = self.get_all_external_drives()

        # 1. Prioritize volume match so mounting disk8s1 targets only that volume
        for d in drives:
            for v in d.volumes:
                if (
                    v.device_id.lower() == clean_target.lower()
                    or (v.device_id.replace("/dev/", "").lower() == clean_id.lower())
                    or (v.uuid and v.uuid.lower() == clean_target.lower())
                    or (v.name and v.name.lower() == clean_target.lower())
                ):
                    res = self.adapter.mount_volume(v.device_id)
                    # Safe sequence: verify success before clearing sleep state!
                    if res.success:
                        resolved_parent = parent_id or d.id
                        StateManager.remove_ejected_target(v.device_id, parent_id=resolved_parent)
                        logger.info(f"Mount verified for volume {v.device_id}; cleared sleeping state for parent {resolved_parent}.")
                    return res

        # 2. Check if target matches drive ID or primary UUID
        for d in drives:
            if (
                d.id.lower() == clean_target.lower()
                or (d.id.replace("/dev/", "").lower() == clean_id.lower())
                or (d.primary_uuid and d.primary_uuid.lower() == clean_target.lower())
            ):
                res = self.adapter.mount_drive(d.id)
                # Safe sequence: verify success before clearing sleep state!
                if res.success:
                    StateManager.remove_ejected_target(d.id)
                    for v in d.volumes:
                        StateManager.remove_ejected_target(v.device_id)
                    logger.info(f"Mount verified for drive {d.id}; cleared sleeping state.")
                return res

        # 3. Direct fallback
        if re.search(r"disk\d+s\d+", clean_id.lower()):
            res = self.adapter.mount_volume(clean_id)
        else:
            res = self.adapter.mount_drive(clean_id)

        if res.success:
            StateManager.remove_ejected_target(clean_id, parent_id=parent_id)

        return res

    def unmount_target(self, target_id: str) -> EjectResult:
        """Unmount a specific volume or drive by ID, UUID, or name."""
        logger.info(f"Manual Unmount requested for: {target_id}")
        clean_target = target_id.strip()
        drives = self.get_all_external_drives()

        # 1. Prioritize volume match so unmounting disk8s1 unmounts ONLY that volume partition
        for d in drives:
            for v in d.volumes:
                if (
                    v.device_id.lower() == clean_target.lower()
                    or (v.device_id.replace("/dev/", "").lower() == clean_target.replace("/dev/", "").lower())
                    or (v.uuid and v.uuid.lower() == clean_target.lower())
                    or (v.name and v.name.lower() == clean_target.lower())
                ):
                    if self.config.deep_sleep_mode:
                        logger.info(
                            f"Matched volume {v.device_id} ({v.name}). "
                            f"Deep Sleep mode ON: putting physical parent {d.id} into Hardware Deep Sleep."
                        )
                        return self.deep_sleep_drive(d.id, is_explicit=True)

                    logger.info(
                        f"Matched volume {v.device_id} ({v.name}). "
                        f"Deep Sleep mode OFF: unmounting volume only; parent remains awake."
                    )
                    if v.mount_point and hasattr(self.adapter, "graceful_close_blocking_processes"):
                        self.adapter.graceful_close_blocking_processes(v.mount_point)
                    res = self.adapter.unmount_volume(v.device_id)
                    if res.success:
                        StateManager.save_ejected_drives(
                            [], [v.device_id], append=True, is_explicit_deep_sleep=False
                        )
                    return res

        # 2. Match parent physical drive
        for d in drives:
            if (
                d.id.lower() == clean_target.lower()
                or (d.id.replace("/dev/", "").lower() == clean_target.replace("/dev/", "").lower())
                or (d.primary_uuid and d.primary_uuid.lower() == clean_target.lower())
            ):
                if self.config.deep_sleep_mode:
                    return self.deep_sleep_drive(d.id, is_explicit=True)
                return self.normal_sleep_drive(d.id)

        # 3. Direct fallback: if target looks like a volume partition (e.g. disk8s1)
        if re.search(r"disk\d+s\d+", clean_target.lower()):
            return self.adapter.unmount_volume(clean_target)

        return self.adapter.eject_drive(clean_target)

    def deep_sleep_drive(self, drive_id: str, is_explicit: bool = True) -> EjectResult:
        """
        Deep Sleep / Hardware Silence workflow:
        1. Flush filesystem buffers (sync).
        2. Detect & cleanly unmount active Time Machine / APFS snapshot dependencies via adapter.
           If ANY snapshot unmount fails, ABORT immediately (NEVER force-eject, NEVER save sleeping state).
        3. Safely unmount all mounted child volumes of target physical drive.
           If ANY required unmount fails, ABORT immediately.
        4. On macOS, cleanly teardown synthesized APFS container dependencies (diskutil unmountDisk).
           If ANY container teardown fails, ABORT immediately.
        5. Only after all unmounts and teardowns succeed, eject physical parent disk (diskutil eject <parentId>).
        6. Record sleeping parent disk and child volumes with strict separation between explicit and idle sleep.
        """
        clean_id = drive_id.strip().replace("/dev/", "")
        drives = self.get_all_external_drives()

        target_drive = None
        for d in drives:
            d_id = d.id.replace("/dev/", "")
            if d_id.lower() == clean_id.lower():
                target_drive = d
                break
            for v in d.volumes:
                v_id = v.device_id.replace("/dev/", "")
                if v_id.lower() == clean_id.lower():
                    target_drive = d
                    break
            if target_drive:
                break

        if not target_drive:
            return self.adapter.eject_drive(clean_id)

        # 1. Flush filesystem buffers
        try:
            if sys.platform == "darwin" and type(self.adapter).__name__ == "MacOSAdapter":
                subprocess.run(["sync"], check=False)
        except Exception:
            pass

        # 2. Detect & cleanly unmount active Time Machine / APFS snapshots
        if hasattr(self.adapter, "teardown_snapshots_for_drive"):
            snap_abort = self.adapter.teardown_snapshots_for_drive(target_drive)
            if snap_abort:
                return snap_abort

        # 3. Safely unmount all mounted child volumes
        # Verify true mount status from OS mount table to avoid stale cache issues
        real_mounted_devs = set()
        if sys.platform == "darwin" and type(self.adapter).__name__ == "MacOSAdapter":
            try:
                proc = subprocess.run(["mount"], capture_output=True, text=True, check=False)
                for line in proc.stdout.splitlines():
                    m = re.match(r"^/dev/(\S+)\s+on\s+", line)
                    if m:
                        real_mounted_devs.add(m.group(1).lower())
            except Exception:
                pass

        vols_to_unmount = [
            v for v in target_drive.volumes
            if v.is_mounted or v.device_id.replace("/dev/", "").lower() in real_mounted_devs
        ]
        unmounted_vols: List[str] = []

        # Smart Graceful App Shutdown: close apps/processes holding files on volumes before unmount
        if hasattr(self.adapter, "graceful_close_blocking_processes"):
            for v in vols_to_unmount:
                if v.mount_point:
                    closed = self.adapter.graceful_close_blocking_processes(v.mount_point)
                    if closed:
                        logger.info(f"Deep Sleep: Gracefully closed {len(closed)} app(s) on {v.device_id} ({v.name}): {closed}")

        for v in vols_to_unmount:
            res = self.adapter.unmount_volume(v.device_id)
            if not res.success:
                logger.error(f"Deep Sleep aborted: volume {v.device_id} ({v.name}) failed to unmount: {res.message}")
                return EjectResult(
                    target=target_drive.id,
                    success=False,
                    message=f"Deep Sleep aborted: {v.name or v.device_id} in use ({res.message})",
                    blocking_processes=res.blocking_processes,
                )
            unmounted_vols.append(v.device_id)

        # 4. Teardown associated synthesized APFS container disks (e.g. disk8, disk9)
        if hasattr(self.adapter, "get_apfs_containers_for_disk"):
            containers = self.adapter.get_apfs_containers_for_disk(target_drive.id)
            for c_id in containers:
                c_res = self.adapter.unmount_disk(c_id)
                if not c_res.success:
                    logger.error(f"Deep Sleep aborted: APFS container {c_id} teardown failed: {c_res.message}")
                    return EjectResult(
                        target=target_drive.id,
                        success=False,
                        message=f"Deep Sleep aborted: Container {c_id} in use ({c_res.message})",
                    )

        # 5. Only after all successful unmounts & container teardowns, eject physical parent disk
        eject_res = self.adapter.eject_drive(target_drive.id)
        if eject_res.success:
            if hasattr(self.adapter, "cache_drive_info"):
                self.adapter.cache_drive_info(target_drive)
            StateManager.save_ejected_drives(
                [target_drive.id],
                unmounted_vols,
                append=True,
                is_explicit_deep_sleep=is_explicit,
            )
            return EjectResult(
                target=target_drive.id,
                success=True,
                message=f"Drive {target_drive.id} in Deep Sleep (Hardware Silence). {len(unmounted_vols)} volume(s) safely unmounted.",
            )
        else:
            logger.warning(f"Parent eject for {target_drive.id} failed after unmounting volumes: {eject_res.message}")
            return eject_res

    def normal_sleep_drive(self, drive_id: str) -> EjectResult:
        """
        Normal Sleep workflow:
        1. Resolve the physical parent drive.
        2. Safely unmount all mounted child volumes.
        3. Keep the physical parent drive mounted/awake (no parent eject).
        4. Record child volumes as idle-sleep state so touch wake can remount them.
        """
        clean_id = drive_id.strip().replace("/dev/", "")
        drives = self.get_all_external_drives()

        target_drive = None
        for d in drives:
            d_id = d.id.replace("/dev/", "")
            if d_id.lower() == clean_id.lower():
                target_drive = d
                break
            for v in d.volumes:
                v_id = v.device_id.replace("/dev/", "")
                if v_id.lower() == clean_id.lower():
                    target_drive = d
                    break
            if target_drive:
                break

        if not target_drive:
            return EjectResult(
                target=clean_id,
                success=False,
                message=f"External drive {clean_id} not found.",
            )

        unmounted_vols: List[str] = []

        # Smart Graceful App Shutdown: close apps/processes holding files on volumes before unmount
        if hasattr(self.adapter, "graceful_close_blocking_processes"):
            for v in target_drive.volumes:
                if v.is_mounted and v.mount_point:
                    closed = self.adapter.graceful_close_blocking_processes(v.mount_point)
                    if closed:
                        logger.info(f"Normal Sleep: Gracefully closed {len(closed)} app(s) on {v.device_id} ({v.name}): {closed}")

        for v in target_drive.volumes:
            if not v.is_mounted:
                continue

            res = self.adapter.unmount_volume(v.device_id)
            if not res.success:
                logger.error(
                    f"Normal Sleep aborted: volume {v.device_id} ({v.name}) "
                    f"failed to unmount: {res.message}"
                )
                return EjectResult(
                    target=target_drive.id,
                    success=False,
                    message=f"Normal Sleep aborted: {v.name or v.device_id} in use ({res.message})",
                    blocking_processes=res.blocking_processes,
                )

            unmounted_vols.append(v.device_id)

        if unmounted_vols:
            StateManager.save_ejected_drives(
                [],
                unmounted_vols,
                append=True,
                is_explicit_deep_sleep=False,
            )

        return EjectResult(
            target=target_drive.id,
            success=True,
            message=(
                f"Drive {target_drive.id} in Normal Sleep. "
                f"{len(unmounted_vols)} volume(s) unmounted; parent remains awake."
            ),
        )

    def toggle_managed(self, target_id: str) -> Tuple[bool, str]:
        """
        Toggle managed state for a drive or volume UUID.
        Returns (is_now_managed, message).
        """
        drives = self.get_all_external_drives()
        target_uuid = target_id

        for d in drives:
            if d.id.lower() == target_id.lower():
                target_uuid = d.primary_uuid
                break
            for v in d.volumes:
                if v.device_id.lower() == target_id.lower() or v.name.lower() == target_id.lower():
                    target_uuid = v.uuid or v.device_id
                    break

        is_managed = self.config.toggle_managed_drive(target_uuid)
        status_msg = f"Drive '{target_id}' is now {'Managed' if is_managed else 'Unmanaged'}."
        logger.info(status_msg)
        return is_managed, status_msg
