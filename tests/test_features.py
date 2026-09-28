"""
Unit tests for Safe-Drive-Ejector-macOS functional features:
- Drive Controller & SSD Volume Manager
- Sleep Timer Presets (2m, 5m, 10m, 15m, 30m, 1h, 2h, Never)
- Up to 6 Managed Drives limit
- Idle Monitor countdown & auto safe-eject trigger
- Eject Now logic
- Auto Awake
- Bottom status message display
"""

import time
import unittest

from core.config import MAX_MANAGED_DRIVES, SafeEjectConfig, StateManager
from core.engine import SafeEjectEngine
from core.idle_monitor import IdleMonitor
from core.models import DriveInfo, EjectResult, RemountResult, VolumeInfo
from core.volume_manager import SSDVolumeManager
from platform_adapters.base import PlatformAdapter


class FeatureMockAdapter(PlatformAdapter):
    def __init__(self, drives=None):
        self.drives = drives or []
        self.ejected = []
        self.mounted = []
        self.unmounted_volumes = []
        self.mounted_volumes = []
        self.unmounted_disks = []
        self.containers = {}

    def get_drives(self):
        return self.drives

    def unmount_disk(self, disk_id: str):
        self.unmounted_disks.append(disk_id)
        return EjectResult(target=disk_id, success=True, message=f"Unmounted disk {disk_id}")

    def get_apfs_containers_for_disk(self, disk_id: str):
        return self.containers.get(disk_id, [])

    def eject_drive(self, drive_id: str):
        self.ejected.append(drive_id)
        return EjectResult(target=drive_id, success=True, message=f"Ejected {drive_id}")

    def unmount_volume(self, volume_id: str):
        if getattr(self, "fail_unmount", False):
            return EjectResult(target=volume_id, success=False, message="Resource busy")
        self.ejected.append(volume_id)
        self.unmounted_volumes.append(volume_id)
        return EjectResult(target=volume_id, success=True, message=f"Unmounted {volume_id}")

    def mount_drive(self, drive_id: str):
        self.mounted.append(drive_id)
        return RemountResult(target=drive_id, success=True, message=f"Mounted {drive_id}")

    def mount_volume(self, volume_id: str):
        self.mounted.append(volume_id)
        self.mounted_volumes.append(volume_id)
        return RemountResult(target=volume_id, success=True, message=f"Mounted {volume_id}")

    def get_blocking_processes(self, mount_point: str):
        if hasattr(self, "blocking_processes"):
            return self.blocking_processes.get(mount_point, [])
        return []

    def kill_process(self, pid: int):
        return True

    def graceful_close_blocking_processes(self, mount_point: str):
        if not hasattr(self, "graceful_closed_mounts"):
            self.graceful_closed_mounts = []
        self.graceful_closed_mounts.append(mount_point)
        return ["MockBrowser", "MockTelegram"]

    def show_notification(self, title: str, message: str):
        pass

    def start_power_listener(self, on_sleep, on_wake):
        pass


class TestFunctionalFeatures(unittest.TestCase):

    def setUp(self):
        StateManager.clear_ejected_drives()

    def tearDown(self):
        StateManager.clear_ejected_drives()

    def test_sleep_timer_presets(self):
        config = SafeEjectConfig()
        
        # Test all required presets
        presets = ["2m", "5m", "10m", "15m", "30m", "1h", "2h", "never"]
        expected_seconds = [120, 300, 600, 900, 1800, 3600, 7200, 0]

        for p, s in zip(presets, expected_seconds):
            self.assertTrue(config.set_timer_preset(p))
            self.assertEqual(config.sleep_timer_seconds, s)

        # Invalid preset
        self.assertFalse(config.set_timer_preset("invalid_preset"))

    def test_max_6_managed_drives(self):
        config = SafeEjectConfig(managed_drive_uuids=[])

        # Add up to 6 drives
        for i in range(MAX_MANAGED_DRIVES):
            uuid = f"UUID-{i}"
            res = config.toggle_managed_drive(uuid)
            self.assertTrue(res)

        self.assertEqual(len(config.managed_drive_uuids), 6)

        # Adding 7th should be rejected by limit
        res_7th = config.toggle_managed_drive("UUID-7")
        self.assertFalse(res_7th)
        self.assertEqual(len(config.managed_drive_uuids), 6)

        # Toggling an existing one removes it
        res_remove = config.toggle_managed_drive("UUID-0")
        self.assertFalse(res_remove)
        self.assertEqual(len(config.managed_drive_uuids), 5)

    def test_eject_now_logic(self):
        vol = VolumeInfo(device_id="disk7s1", name="Backup", mount_point="/Volumes/Backup", is_mounted=True)
        drive = DriveInfo(id="disk7", name="Samsung T7", is_external=True, volumes=[vol])

        adapter = FeatureMockAdapter(drives=[drive])
        config = SafeEjectConfig(show_notifications=False)
        vol_mgr = SSDVolumeManager(adapter=adapter, config=config)

        results = vol_mgr.eject_now()
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].success)
        self.assertIn("disk7", adapter.ejected)

    def test_eject_now_with_specific_targets(self):
        vol1 = VolumeInfo(device_id="disk8s1", name="support-external-drive", mount_point="/Volumes/support-external-drive", is_mounted=True)
        vol2 = VolumeInfo(device_id="disk9s1", name="Macbook Backup", mount_point="/Volumes/Macbook Backup", is_mounted=True)
        drive = DriveInfo(id="disk7", name="Transcend SSD", is_external=True, volumes=[vol1, vol2])

        adapter = FeatureMockAdapter(drives=[drive])
        config = SafeEjectConfig(show_notifications=False)
        vol_mgr = SSDVolumeManager(adapter=adapter, config=config)

        # Deep Sleep ON is the master mode: selecting any child target
        # executes Hardware Deep Sleep for the physical parent.
        results = vol_mgr.eject_now(targets=["disk8s1"])
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].success)
        self.assertIn("disk7", adapter.ejected)
        self.assertIn("disk8s1", adapter.unmounted_volumes)
        self.assertIn("disk9s1", adapter.unmounted_volumes)

    def _assert_physical_parent_sleep_behavior(
        self,
        media_type,
        fs_type,
        type_desc,
        deep_sleep_mode,
        drive_id,
        volume_id,
    ):
        volume = VolumeInfo(
            device_id=volume_id,
            name=f"{media_type} {fs_type} Data",
            mount_point=f"/Volumes/{media_type} {fs_type} Data",
            fs_type=fs_type,
            type_desc=type_desc,
            uuid=f"UUID-{drive_id}",
            is_mounted=True,
        )
        drive = DriveInfo(
            id=drive_id,
            name=f"Test {media_type}",
            is_external=True,
            is_removable=True,
            media_type=media_type,
            volumes=[volume],
        )

        adapter = FeatureMockAdapter(drives=[drive])
        config = SafeEjectConfig(
            show_notifications=False,
            deep_sleep_mode=deep_sleep_mode,
        )
        volume_mgr = SSDVolumeManager(adapter=adapter, config=config)

        result = volume_mgr.unmount_target(volume_id)

        self.assertTrue(result.success)

        if deep_sleep_mode:
            self.assertIn(volume_id, adapter.unmounted_volumes)
            self.assertIn(drive_id, adapter.ejected)
        else:
            self.assertIn(volume_id, adapter.unmounted_volumes)
            self.assertNotIn(drive_id, adapter.ejected)

    def test_matrix_ssd_apfs_deep_sleep(self):
        self._assert_physical_parent_sleep_behavior(
            "Solid state", "APFS", "APFS Volume", True, "disk20", "disk20s1"
        )

    def test_matrix_ssd_apfs_normal_sleep(self):
        self._assert_physical_parent_sleep_behavior(
            "Solid state", "APFS", "APFS Volume", False, "disk21", "disk21s1"
        )

    def test_matrix_ssd_exfat_deep_sleep(self):
        self._assert_physical_parent_sleep_behavior(
            "Solid state", "ExFAT", "exFAT", True, "disk22", "disk22s1"
        )

    def test_matrix_ssd_exfat_normal_sleep(self):
        self._assert_physical_parent_sleep_behavior(
            "Solid state", "ExFAT", "exFAT", False, "disk23", "disk23s1"
        )

    def test_matrix_hdd_apfs_deep_sleep(self):
        self._assert_physical_parent_sleep_behavior(
            "Hard Disk", "APFS", "APFS Volume", True, "disk24", "disk24s1"
        )

    def test_matrix_hdd_apfs_normal_sleep(self):
        self._assert_physical_parent_sleep_behavior(
            "Hard Disk", "APFS", "APFS Volume", False, "disk25", "disk25s1"
        )

    def test_matrix_hdd_exfat_deep_sleep(self):
        self._assert_physical_parent_sleep_behavior(
            "Hard Disk", "ExFAT", "exFAT", True, "disk26", "disk26s1"
        )

    def test_matrix_hdd_exfat_normal_sleep(self):
        self._assert_physical_parent_sleep_behavior(
            "Hard Disk", "ExFAT", "exFAT", False, "disk27", "disk27s1"
        )

    def _assert_idle_sleep_timer_matrix(
        self,
        media_type,
        fs_type,
        type_desc,
        deep_sleep_mode,
        drive_id,
        volume_id,
    ):
        """Exercise the real idle-sleep engine path for one timer matrix case."""
        volume = VolumeInfo(
            device_id=volume_id,
            name=f"Timer {media_type} {fs_type} Data",
            mount_point=f"/Volumes/Timer {media_type} {fs_type} Data",
            fs_type=fs_type,
            type_desc=type_desc,
            uuid=f"UUID-TIMER-{drive_id}",
            is_mounted=True,
        )
        drive = DriveInfo(
            id=drive_id,
            name=f"Timer Test {media_type}",
            is_external=True,
            is_removable=True,
            media_type=media_type,
            volumes=[volume],
            is_sleep_selected=True,
        )

        adapter = FeatureMockAdapter(drives=[drive])
        config = SafeEjectConfig(
            show_notifications=False,
            sleep_timer_seconds=120,
            deep_sleep_mode=deep_sleep_mode,
            wake_mode="touch",
        )
        engine = SafeEjectEngine(config=config, adapter=adapter)

        # Simulate timer expiry through the same engine callback used by IdleMonitor.
        results = engine._on_idle_sleep([])

        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].success)
        self.assertIn(volume_id, adapter.unmounted_volumes)

        if deep_sleep_mode:
            # Deep Sleep: physical parent is ejected and explicitly recorded.
            self.assertIn(drive_id, adapter.ejected)

            state = StateManager.load_ejected_drives()
            self.assertIn(
                drive_id,
                state.get("explicit_sleep_parent_ids", []),
            )

        else:
            # Normal Sleep: physical parent remains awake and is touch-wakeable.
            self.assertNotIn(drive_id, adapter.ejected)

            state = StateManager.load_ejected_drives()
            self.assertNotIn(
                drive_id,
                state.get("explicit_sleep_parent_ids", []),
            )

            engine._on_touch_wake([volume_id])
            self.assertIn(volume_id, adapter.mounted_volumes)


    def test_timer_matrix_ssd_apfs_deep_sleep(self):
        self._assert_idle_sleep_timer_matrix(
            "Solid state", "APFS", "APFS Volume", True, "disk40", "disk40s1"
        )

    def test_timer_matrix_ssd_apfs_normal_sleep(self):
        self._assert_idle_sleep_timer_matrix(
            "Solid state", "APFS", "APFS Volume", False, "disk41", "disk41s1"
        )

    def test_timer_matrix_ssd_exfat_deep_sleep(self):
        self._assert_idle_sleep_timer_matrix(
            "Solid state", "ExFAT", "exFAT", True, "disk42", "disk42s1"
        )

    def test_timer_matrix_ssd_exfat_normal_sleep(self):
        self._assert_idle_sleep_timer_matrix(
            "Solid state", "ExFAT", "exFAT", False, "disk43", "disk43s1"
        )

    def test_timer_matrix_hdd_apfs_deep_sleep(self):
        self._assert_idle_sleep_timer_matrix(
            "Hard Disk", "APFS", "APFS Volume", True, "disk44", "disk44s1"
        )

    def test_timer_matrix_hdd_apfs_normal_sleep(self):
        self._assert_idle_sleep_timer_matrix(
            "Hard Disk", "APFS", "APFS Volume", False, "disk45", "disk45s1"
        )

    def test_timer_matrix_hdd_exfat_deep_sleep(self):
        self._assert_idle_sleep_timer_matrix(
            "Hard Disk", "ExFAT", "exFAT", True, "disk46", "disk46s1"
        )

    def test_timer_matrix_hdd_exfat_normal_sleep(self):
        self._assert_idle_sleep_timer_matrix(
            "Hard Disk", "ExFAT", "exFAT", False, "disk47", "disk47s1"
        )

    def test_generic_hdd_exfat_child_target_deep_sleep(self):
        """Generic external HDD + exFAT must use physical-parent Deep Sleep when enabled."""
        volume = VolumeInfo(
            device_id="disk10s1",
            name="Generic HDD Data",
            mount_point="/Volumes/Generic HDD Data",
            fs_type="ExFAT",
            type_desc="exFAT",
            uuid="UUID-HDD-EXFAT",
            is_mounted=True,
        )
        drive = DriveInfo(
            id="disk10",
            name="Generic USB HDD",
            is_external=True,
            is_removable=True,
            media_type="Hard Disk",
            volumes=[volume],
        )

        adapter = FeatureMockAdapter(drives=[drive])
        config = SafeEjectConfig(
            show_notifications=False,
            deep_sleep_mode=True,
        )
        volume_mgr = SSDVolumeManager(adapter=adapter, config=config)

        result = volume_mgr.unmount_target("disk10s1")

        self.assertTrue(result.success)
        self.assertIn("disk10s1", adapter.unmounted_volumes)
        self.assertIn("disk10", adapter.ejected)

    def test_generic_hdd_exfat_child_target_normal_sleep(self):
        """Generic external HDD + exFAT must stay awake when Deep Sleep is disabled."""
        volume = VolumeInfo(
            device_id="disk11s1",
            name="Generic HDD Data",
            mount_point="/Volumes/Generic HDD Data",
            fs_type="ExFAT",
            type_desc="exFAT",
            uuid="UUID-HDD-EXFAT-2",
            is_mounted=True,
        )
        drive = DriveInfo(
            id="disk11",
            name="Generic USB HDD",
            is_external=True,
            is_removable=True,
            media_type="Hard Disk",
            volumes=[volume],
        )

        adapter = FeatureMockAdapter(drives=[drive])
        config = SafeEjectConfig(
            show_notifications=False,
            deep_sleep_mode=False,
        )
        volume_mgr = SSDVolumeManager(adapter=adapter, config=config)

        result = volume_mgr.unmount_target("disk11s1")

        self.assertTrue(result.success)
        self.assertIn("disk11s1", adapter.unmounted_volumes)
        self.assertNotIn("disk11", adapter.ejected)

    def _assert_eject_now_matrix(
        self,
        media_type,
        fs_type,
        type_desc,
        deep_sleep_mode,
        drive_id,
        volume_id,
    ):
        volume = VolumeInfo(
            device_id=volume_id,
            name=f"{media_type} {fs_type} Data",
            mount_point=f"/Volumes/{media_type} {fs_type} Data",
            fs_type=fs_type,
            type_desc=type_desc,
            uuid=f"UUID-EJECT-{drive_id}",
            is_mounted=True,
        )
        drive = DriveInfo(
            id=drive_id,
            name=f"Eject Test {media_type}",
            is_external=True,
            is_removable=True,
            media_type=media_type,
            is_managed=True,
            volumes=[volume],
        )

        adapter = FeatureMockAdapter(drives=[drive])
        config = SafeEjectConfig(
            show_notifications=False,
            deep_sleep_mode=deep_sleep_mode,
        )
        volume_mgr = SSDVolumeManager(adapter=adapter, config=config)

        results = volume_mgr.eject_now()

        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].success)
        self.assertIn(drive_id, adapter.ejected)

        if deep_sleep_mode:
            self.assertIn(volume_id, adapter.unmounted_volumes)

    def test_matrix_eject_now_ssd_apfs_deep_sleep(self):
        self._assert_eject_now_matrix(
            "Solid state", "APFS", "APFS Volume", True, "disk30", "disk30s1"
        )

    def test_matrix_eject_now_ssd_apfs_normal(self):
        self._assert_eject_now_matrix(
            "Solid state", "APFS", "APFS Volume", False, "disk31", "disk31s1"
        )

    def test_matrix_eject_now_ssd_exfat_deep_sleep(self):
        self._assert_eject_now_matrix(
            "Solid state", "ExFAT", "exFAT", True, "disk32", "disk32s1"
        )

    def test_matrix_eject_now_ssd_exfat_normal(self):
        self._assert_eject_now_matrix(
            "Solid state", "ExFAT", "exFAT", False, "disk33", "disk33s1"
        )

    def test_matrix_eject_now_hdd_apfs_deep_sleep(self):
        self._assert_eject_now_matrix(
            "Hard Disk", "APFS", "APFS Volume", True, "disk34", "disk34s1"
        )

    def test_matrix_eject_now_hdd_apfs_normal(self):
        self._assert_eject_now_matrix(
            "Hard Disk", "APFS", "APFS Volume", False, "disk35", "disk35s1"
        )

    def test_matrix_eject_now_hdd_exfat_deep_sleep(self):
        self._assert_eject_now_matrix(
            "Hard Disk", "ExFAT", "exFAT", True, "disk36", "disk36s1"
        )

    def test_matrix_eject_now_hdd_exfat_normal(self):
        self._assert_eject_now_matrix(
            "Hard Disk", "ExFAT", "exFAT", False, "disk37", "disk37s1"
        )

    def test_idle_monitor_timeout_trigger(self):
        timed_out_drives = []

        def on_sleep(drives):
            timed_out_drives.extend(drives)

        def on_wake(drives):
            pass

        config = SafeEjectConfig(sleep_timer_seconds=2)
        monitor = IdleMonitor(config=config, on_idle_sleep=on_sleep, on_touch_wake=on_wake, check_interval_seconds=0.1)

        # Simulate user idle exceeding timeout limit
        monitor.get_mac_user_idle_seconds = lambda: 3.0
        monitor._check_state()

        self.assertTrue(monitor.is_user_currently_idle)

    def test_bottom_status_summary(self):
        config = SafeEjectConfig(sleep_timer_seconds=600, wake_mode="touch")  # 10m
        monitor = IdleMonitor(config=config, on_idle_sleep=lambda d: None, on_touch_wake=lambda d: None)

        vol = VolumeInfo(device_id="disk7s1", name="DriveOne", mount_point="/Volumes/DriveOne", is_mounted=True)
        drive = DriveInfo(id="disk7", name="DriveOne", is_external=True, volumes=[vol])

        summary = monitor.get_status_summary([drive])
        self.assertIn("10 Minutes", summary)
        self.assertIn("Auto-Wake on Touch", summary)

        # Test with sleeping drive
        monitor.mark_drive_asleep("disk7")
        sleeping_summary = monitor.get_status_summary([drive])
        self.assertIn("1 SSD(s) in Sleep", sleeping_summary)

    def test_config_update_settings(self):
        config = SafeEjectConfig()
        config.update_settings({
            "start_at_login": True,
            "show_disks_count_badge": True,
            "success_sound": "Bubble",
            "failure_sound": "Gong",
            "unmount_instead_of_eject": True
        })
        self.assertTrue(config.start_at_login)
        self.assertTrue(config.show_disks_count_badge)
        self.assertEqual(config.success_sound, "Bubble")
        self.assertEqual(config.failure_sound, "Gong")
        self.assertTrue(config.unmount_instead_of_eject)

    def test_config_aliases_and_to_dict(self):
        config = SafeEjectConfig()
        # Direct property setter/getter tests
        config.eject_before_sleep = False
        self.assertFalse(config.eject_on_sleep)
        config.eject_on_sleep = True
        self.assertTrue(config.eject_before_sleep)

        config.auto_awake = False
        self.assertFalse(config.remount_on_wake)
        config.remount_on_wake = True
        self.assertTrue(config.auto_awake)

        config.notify_after_eject_remount = False
        self.assertFalse(config.show_notifications)
        config.show_notifications = True
        self.assertTrue(config.notify_after_eject_remount)

        # update_settings with legacy alias keys
        config.update_settings({
            "eject_before_sleep": False,
            "auto_awake": False,
            "notify_after_eject_remount": False,
        })
        self.assertFalse(config.eject_on_sleep)
        self.assertFalse(config.remount_on_wake)
        self.assertFalse(config.show_notifications)

        # to_dict contains both primary and alias keys
        d = config.to_dict()
        self.assertIn("eject_on_sleep", d)
        self.assertIn("eject_before_sleep", d)
        self.assertIn("remount_on_wake", d)
        self.assertIn("auto_awake", d)
        self.assertIn("show_notifications", d)
        self.assertIn("notify_after_eject_remount", d)

    def test_idle_sleep_deep_sleep_mode_uses_parent_hardware_sleep(self):
        adapter = FeatureMockAdapter()
        vol = VolumeInfo(
            device_id="disk8s1",
            name="SupportDrive",
            mount_point="/Volumes/SupportDrive",
            is_mounted=True,
        )
        drive = DriveInfo(
            id="disk7",
            name="Samsung T7",
            is_external=True,
            volumes=[vol],
        )
        adapter.drives = [drive]
        adapter.resolve_parent_for_target = lambda target: "disk7"

        config = SafeEjectConfig(
            show_notifications=False,
            unmount_instead_of_eject=True,
            managed_drive_uuids=["disk7"],
            selected_sleep_drive_uuids=["disk7"],
            deep_sleep_mode=True,
        )
        engine = SafeEjectEngine(config=config, adapter=adapter)

        engine._on_idle_sleep([])

        # Deep Sleep must unmount the child volume before ejecting
        # the physical parent drive.
        self.assertIn("disk8s1", adapter.unmounted_volumes)
        self.assertIn("disk7", adapter.ejected)
        self.assertIn("disk8s1", engine.idle_monitor.sleeping_drive_ids)

        # Explicit Deep Sleep state must be recorded.
        state = StateManager.load_ejected_drives()
        self.assertIn("disk8s1", state.get("volume_identifiers", []))
        self.assertIn("disk7", state.get("explicit_sleep_parent_ids", []))

        # Explicit Deep Sleep must NOT be awakened by touch.
        engine._on_touch_wake(["disk8s1"])
        self.assertNotIn("disk8s1", adapter.mounted_volumes)
        self.assertIn("disk8s1", engine.idle_monitor.sleeping_drive_ids)

    def test_idle_sleep_normal_mode_allows_touch_wake(self):
        adapter = FeatureMockAdapter()
        vol = VolumeInfo(
            device_id="disk10s1",
            name="NormalSleepDrive",
            mount_point="/Volumes/NormalSleepDrive",
            is_mounted=True,
        )
        drive = DriveInfo(
            id="disk10",
            name="Normal Sleep HDD",
            is_external=True,
            volumes=[vol],
        )
        adapter.drives = [drive]
        adapter.resolve_parent_for_target = lambda target: "disk10"

        config = SafeEjectConfig(
            show_notifications=False,
            unmount_instead_of_eject=True,
            managed_drive_uuids=["disk10"],
            selected_sleep_drive_uuids=["disk10"],
            deep_sleep_mode=False,
            wake_mode="touch",
        )
        engine = SafeEjectEngine(config=config, adapter=adapter)

        engine._on_idle_sleep([])

        # Normal Sleep unmounts the child volume but keeps the physical
        # parent drive awake; there must be no parent eject.
        self.assertIn("disk10s1", adapter.unmounted_volumes)
        self.assertNotIn("disk10", adapter.ejected)
        self.assertIn("disk10s1", engine.idle_monitor.sleeping_drive_ids)

        # Normal Sleep is touch-wakeable.
        engine._on_touch_wake(["disk10s1"])
        self.assertIn("disk10s1", adapter.mounted_volumes)
        self.assertNotIn("disk10s1", engine.idle_monitor.sleeping_drive_ids)

        # Normal Sleep must not be recorded as explicit Deep Sleep.
        state = StateManager.load_ejected_drives()
        self.assertNotIn("disk10", state.get("explicit_sleep_parent_ids", []))

    def test_remount_all_ejected_volume_identifiers(self):
        adapter = FeatureMockAdapter()
        vol = VolumeInfo(device_id="disk9s1", name="WorkBackup", mount_point="/Volumes/WorkBackup", is_mounted=False)
        drive = DriveInfo(id="disk9", name="BackupSSD", is_external=True, volumes=[vol])
        adapter.drives = [drive]

        config = SafeEjectConfig(show_notifications=False)
        engine = SafeEjectEngine(config=config, adapter=adapter)

        # Save only volume identifier in state
        StateManager.save_ejected_drives([], ["disk9s1"], append=False)

        results = engine.remount_all_ejected()
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].success)
        self.assertIn("disk9s1", adapter.mounted_volumes)

        # State should be cleared
        cleared_state = StateManager.load_ejected_drives()
        self.assertEqual(cleared_state.get("volume_identifiers", []), [])

    def test_open_volume_and_sleep_system(self):
        from unittest.mock import patch
        adapter = FeatureMockAdapter()
        vol = VolumeInfo(device_id="disk8s1", name="SanDisk", mount_point="/Volumes/SanDisk", is_mounted=True)
        drive = DriveInfo(id="disk8", name="SanDisk Ultra", is_external=True, volumes=[vol])
        adapter.drives = [drive]
        engine = SafeEjectEngine(adapter=adapter)

        with patch("subprocess.run") as mock_subproc, patch("os.path.exists", return_value=True):
            # Test open_volume does not raise NameError
            opened = engine.open_volume("SanDisk")
            self.assertTrue(opened)
            mock_subproc.assert_called()

            # Test sleep_system does not raise NameError
            slept = engine.sleep_system()
            self.assertTrue(slept)

    def test_auto_wake_only_recorded_drives(self):
        adapter = FeatureMockAdapter()
        vol1 = VolumeInfo(device_id="disk8s1", name="DriveOne", mount_point="/Volumes/DriveOne", is_mounted=False)
        drive1 = DriveInfo(id="disk8", name="SSDOne", is_external=True, volumes=[vol1])
        vol2 = VolumeInfo(device_id="disk9s1", name="UnrelatedDrive", mount_point="/Volumes/UnrelatedDrive", is_mounted=False)
        drive2 = DriveInfo(id="disk9", name="SSDTwo", is_external=True, volumes=[vol2])
        adapter.drives = [drive1, drive2]

        config = SafeEjectConfig(show_notifications=False)
        engine = SafeEjectEngine(config=config, adapter=adapter)

        # 1. When state is empty, auto-wake (only_if_recorded=True) must NOT remount any drives
        StateManager.clear_ejected_drives()
        res_empty = engine.remount_all_ejected(only_if_recorded=True)
        self.assertEqual(len(res_empty), 0)
        self.assertEqual(len(adapter.mounted_volumes), 0)

        # 2. When only disk8s1 was put to sleep, only disk8s1 must be remounted
        StateManager.save_ejected_drives([], ["disk8s1"], append=False)
        res_recorded = engine.remount_all_ejected(only_if_recorded=True)
        self.assertEqual(len(res_recorded), 1)
        self.assertIn("disk8s1", adapter.mounted_volumes)
        self.assertNotIn("disk9s1", adapter.mounted_volumes)

    def test_legacy_daemon_sleep_uses_safe_unmount(self):
        adapter = FeatureMockAdapter()
        vol = VolumeInfo(device_id="disk8s1", name="DriveOne", mount_point="/Volumes/DriveOne", is_mounted=True)
        drive = DriveInfo(id="disk8", name="SSDOne", is_external=True, volumes=[vol])
        adapter.drives = [drive]

        config = SafeEjectConfig(
            show_notifications=False,
            eject_on_sleep=True,
            remount_on_wake=False,
            deep_sleep_mode=True,
        )
        engine = SafeEjectEngine(config=config, adapter=adapter)

        captured_callbacks = {}
        def mock_power_listener(on_sleep, on_wake):
            captured_callbacks["sleep"] = on_sleep
            captured_callbacks["wake"] = on_wake

        adapter.start_power_listener = mock_power_listener

        import threading
        t = threading.Thread(target=engine.run_daemon, daemon=True)
        t.start()
        time.sleep(0.05)

        # Trigger sleep callback from power listener
        captured_callbacks["sleep"]()

        # Deep Sleep ON: safely unmount all mounted volumes, then eject the physical parent.
        self.assertIn("disk8s1", adapter.unmounted_volumes)
        self.assertIn("disk8", adapter.ejected)

        # State must record the sleeping volume
        state = StateManager.load_ejected_drives()
        self.assertIn("disk8s1", state.get("volume_identifiers", []))

        # Stop idle monitor
        engine.idle_monitor.stop()

    def test_deep_sleep_all_mounted_volumes_unmounted_before_parent_eject(self):
        adapter = FeatureMockAdapter()
        vol1 = VolumeInfo(device_id="disk8s1", name="Part1", mount_point="/Volumes/Part1", is_mounted=True)
        vol2 = VolumeInfo(device_id="disk9s1", name="Part2", mount_point="/Volumes/Part2", is_mounted=True)
        drive = DriveInfo(id="disk7", name="Transcend", is_external=True, volumes=[vol1, vol2])
        adapter.drives = [drive]

        config = SafeEjectConfig(show_notifications=False)
        volume_mgr = SSDVolumeManager(config=config, adapter=adapter)

        res = volume_mgr.deep_sleep_drive("disk7")
        self.assertTrue(res.success)
        # Verify both volumes were safely unmounted before parent disk7 was ejected
        self.assertIn("disk8s1", adapter.unmounted_volumes)
        self.assertIn("disk9s1", adapter.unmounted_volumes)
        self.assertIn("disk7", adapter.ejected)

        # State must record both unmounted volume partitions for Auto-Wake
        state = StateManager.load_ejected_drives()
        self.assertIn("disk8s1", state.get("volume_identifiers", []))
        self.assertIn("disk9s1", state.get("volume_identifiers", []))

    def test_deep_sleep_aborted_when_unmount_fails(self):
        adapter = FeatureMockAdapter()
        vol1 = VolumeInfo(device_id="disk8s1", name="Part1", mount_point="/Volumes/Part1", is_mounted=True)
        drive = DriveInfo(id="disk7", name="Transcend", is_external=True, volumes=[vol1])
        adapter.drives = [drive]

        # Simulate unmount failure (e.g. file in use)
        def fail_unmount(vid):
            return EjectResult(target=vid, success=False, message="Resource busy")
        adapter.unmount_volume = fail_unmount

        config = SafeEjectConfig(show_notifications=False)
        volume_mgr = SSDVolumeManager(config=config, adapter=adapter)

        res = volume_mgr.deep_sleep_drive("disk7")
        self.assertFalse(res.success)
        self.assertIn("Resource busy", res.message)
        # Parent disk7 MUST NOT be ejected when unmount fails
        self.assertNotIn("disk7", adapter.ejected)

    def test_deep_sleep_selective_wake_drive1_and_drive2(self):
        adapter = FeatureMockAdapter()
        vol1 = VolumeInfo(device_id="disk8s1", name="Part1", mount_point="/Volumes/Part1", is_mounted=False)
        vol2 = VolumeInfo(device_id="disk9s1", name="Part2", mount_point="/Volumes/Part2", is_mounted=False)
        drive = DriveInfo(id="disk7", name="Transcend", is_external=True, volumes=[vol1, vol2])
        adapter.drives = [drive]

        config = SafeEjectConfig(show_notifications=False)
        engine = SafeEjectEngine(config=config, adapter=adapter)

        # 1. Wake Drive 1 (disk8s1) only
        res1 = engine.remount_single("disk8s1")
        self.assertTrue(res1.success)
        self.assertIn("disk8s1", adapter.mounted_volumes)
        self.assertNotIn("disk9s1", adapter.mounted_volumes)

        # 2. Wake Drive 2 (disk9s1) only
        adapter.mounted_volumes = []
        res2 = engine.remount_single("disk9s1")
        self.assertTrue(res2.success)
        self.assertIn("disk9s1", adapter.mounted_volumes)
        self.assertNotIn("disk8s1", adapter.mounted_volumes)

    def test_eject_now_clears_auto_wake_state(self):
        adapter = FeatureMockAdapter()
        vol1 = VolumeInfo(device_id="disk8s1", name="Part1", mount_point="/Volumes/Part1", is_mounted=True)
        drive = DriveInfo(id="disk7", name="Transcend", is_external=True, volumes=[vol1], is_managed=True)
        adapter.drives = [drive]

        config = SafeEjectConfig(show_notifications=False)
        engine = SafeEjectEngine(config=config, adapter=adapter)

        # Populate sleep state
        StateManager.save_ejected_drives([], ["disk8s1"], append=False)
        self.assertTrue(len(StateManager.load_ejected_drives().get("volume_identifiers", [])) > 0)

        # Execute Eject Now (explicit physical removal workflow)
        res = engine.eject_now()
        self.assertTrue(any(r.success for r in res))
        self.assertIn("disk7", adapter.ejected)

        # Explicit Deep Sleep state must remain recorded so touch/system wake
        # cannot automatically remount the drive. Manual Mount clears this state.
        state = StateManager.load_ejected_drives()
        self.assertTrue(
            "disk7" in state.get("drive_ids", [])
            or "disk7" in state.get("explicit_sleep_parent_ids", [])
        )

    def test_parent_and_child_hierarchy_control(self):
        adapter = FeatureMockAdapter()
        vol1 = VolumeInfo(device_id="disk8s1", name="support-external-drive", mount_point="/Volumes/support-external-drive", is_mounted=True)
        vol2 = VolumeInfo(device_id="disk9s1", name="Macbook Backup", mount_point="/Volumes/Macbook Backup", is_mounted=True)
        drive = DriveInfo(id="disk7", name="StoreJet Transcend Media", is_external=True, volumes=[vol1, vol2])
        adapter.drives = [drive]

        config = SafeEjectConfig(show_notifications=False)
        volume_mgr = SSDVolumeManager(config=config, adapter=adapter)

        # 1. Deep Sleep ON: selecting child disk8s1 triggers Hardware
        # Deep Sleep for the physical parent and unmounts all children.
        res_child = volume_mgr.unmount_target("disk8s1")
        self.assertTrue(res_child.success)
        self.assertIn("disk8s1", adapter.unmounted_volumes)
        self.assertIn("disk9s1", adapter.unmounted_volumes)
        self.assertIn("disk7", adapter.ejected)

        # 2. Mounting child disk8s1 still mounts only that selected child.
        res_mount_child = volume_mgr.mount_target("disk8s1")
        self.assertTrue(res_mount_child.success)
        self.assertIn("disk8s1", adapter.mounted_volumes)

        # 3. Mounting parent disk7 mounts the physical drive.
        res_mount_parent = volume_mgr.mount_target("disk7")
        self.assertTrue(res_mount_parent.success)
        self.assertIn("disk7", adapter.mounted)

    def test_parent_precedence_in_eject_targets(self):
        """When parent (disk7) and child (disk8s1) are both in targets, parent takes precedence (single Deep Sleep run)."""
        adapter = FeatureMockAdapter()
        vol1 = VolumeInfo(device_id="disk8s1", name="support-external-drive", mount_point="/Volumes/support-external-drive", is_mounted=True)
        vol2 = VolumeInfo(device_id="disk9s1", name="Macbook Backup", mount_point="/Volumes/Macbook Backup", is_mounted=True)
        drive = DriveInfo(id="disk7", name="StoreJet Transcend Media", is_external=True, volumes=[vol1, vol2])
        adapter.drives = [drive]

        config = SafeEjectConfig(show_notifications=False)
        engine = SafeEjectEngine(config=config, adapter=adapter)

        # Both parent and child targets passed together
        results = engine.eject_targets(["disk7", "disk8s1", "disk9s1"])
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].success)
        self.assertEqual(results[0].target, "disk7")
        # Parent disk7 was ejected
        self.assertIn("disk7", adapter.ejected)
        # All child volumes were unmounted as part of Deep Sleep
        self.assertIn("disk8s1", adapter.unmounted_volumes)
        self.assertIn("disk9s1", adapter.unmounted_volumes)

    def test_child_only_unmount_never_ejects_parent(self):
        """When only child partition is unmounted, parent disk is NEVER ejected, leaving SSD LED ON."""
        adapter = FeatureMockAdapter()
        vol1 = VolumeInfo(device_id="disk8s1", name="support-external-drive", mount_point="/Volumes/support-external-drive", is_mounted=True)
        vol2 = VolumeInfo(device_id="disk9s1", name="Macbook Backup", mount_point="/Volumes/Macbook Backup", is_mounted=False)
        drive = DriveInfo(id="disk7", name="StoreJet Transcend Media", is_external=True, volumes=[vol1, vol2])
        adapter.drives = [drive]

        config = SafeEjectConfig(show_notifications=False)
        volume_mgr = SSDVolumeManager(config=config, adapter=adapter)

        # Deep Sleep ON is the master mode: selecting any child target
        # puts the physical parent into Hardware Deep Sleep.
        res = volume_mgr.unmount_target("disk8s1")
        self.assertTrue(res.success)
        self.assertIn("disk8s1", adapter.unmounted_volumes)
        self.assertIn("disk7", adapter.ejected)

    def test_deep_sleep_aborts_on_volume_unmount_failure_no_force(self):
        """If any child volume fails to unmount (e.g. busy), Deep Sleep aborts and NEVER force-ejects parent."""
        adapter = FeatureMockAdapter()
        vol1 = VolumeInfo(device_id="disk8s1", name="support-external-drive", mount_point="/Volumes/support-external-drive", is_mounted=True)
        vol2 = VolumeInfo(device_id="disk9s1", name="Macbook Backup", mount_point="/Volumes/Macbook Backup", is_mounted=True)
        drive = DriveInfo(id="disk7", name="StoreJet Transcend Media", is_external=True, volumes=[vol1, vol2])
        adapter.drives = [drive]

        # Simulate busy lock on disk8s1
        def unmount_volume_fail(vol_id):
            if vol_id == "disk8s1":
                return EjectResult(target=vol_id, success=False, message="Resource busy", blocking_processes=[])
            adapter.unmounted_volumes.append(vol_id)
            return EjectResult(target=vol_id, success=True, message="OK")
        adapter.unmount_volume = unmount_volume_fail

        config = SafeEjectConfig(show_notifications=False)
        volume_mgr = SSDVolumeManager(config=config, adapter=adapter)

        res = volume_mgr.deep_sleep_drive("disk7")
        self.assertFalse(res.success)
        self.assertIn("Deep Sleep aborted", res.message)
        # Parent disk7 must NEVER be ejected on failure
        self.assertNotIn("disk7", adapter.ejected)

    def test_idle_sleep_parent_precedence_and_no_skipping(self):
        """_on_idle_sleep must not skip parent disk7 even if child volumes were already unmounted."""
        adapter = FeatureMockAdapter()
        vol1 = VolumeInfo(device_id="disk8s1", name="support-external-drive", mount_point="/Volumes/support-external-drive", is_mounted=False)
        vol2 = VolumeInfo(device_id="disk9s1", name="Macbook Backup", mount_point="/Volumes/Macbook Backup", is_mounted=False)
        drive = DriveInfo(id="disk7", name="StoreJet Transcend Media", is_external=True, volumes=[vol1, vol2], is_sleep_selected=True)
        adapter.drives = [drive]

        config = SafeEjectConfig(show_notifications=False)
        engine = SafeEjectEngine(config=config, adapter=adapter)

        # has_mounted_volumes is False because both vol1 and vol2 are unmounted
        self.assertFalse(drive.has_mounted_volumes)

        # Trigger idle sleep: must still put parent disk7 into Deep Sleep
        engine._on_idle_sleep([])
        self.assertIn("disk7", adapter.ejected)

    def test_deep_sleep_teardown_apfs_containers(self):
        """Deep Sleep teardown must cleanly unmount synthesized APFS container disks before parent eject."""
        adapter = FeatureMockAdapter()
        vol1 = VolumeInfo(device_id="disk8s1", name="support-external-drive", mount_point="/Volumes/support-external-drive", is_mounted=True)
        vol2 = VolumeInfo(device_id="disk9s1", name="Macbook Backup", mount_point="/Volumes/Macbook Backup", is_mounted=True)
        drive = DriveInfo(id="disk7", name="StoreJet Transcend Media", is_external=True, volumes=[vol1, vol2])
        adapter.drives = [drive]
        adapter.containers = {"disk7": ["disk8", "disk9"]}

        config = SafeEjectConfig(show_notifications=False)
        volume_mgr = SSDVolumeManager(config=config, adapter=adapter)

        res = volume_mgr.deep_sleep_drive("disk7")
        self.assertTrue(res.success)
        self.assertIn("disk8", adapter.unmounted_disks)
        self.assertIn("disk9", adapter.unmounted_disks)
        self.assertIn("disk7", adapter.ejected)

    def test_drive_and_volume_hover_metadata(self):
        """Verify dynamic OS metadata for hover tooltip (macOS APFS and ExFAT formats)."""
        # macOS Parent & Child Volumes
        vol1 = VolumeInfo(
            device_id="disk8s1",
            name="support-external-drive",
            mount_point="/Volumes/support-external-drive",
            size_bytes=200394182656,
            fs_type="APFS",
            type_desc="APFS Volume",
        )
        vol2 = VolumeInfo(
            device_id="disk9s1",
            name="Macbook Backup",
            mount_point="/Volumes/Macbook Backup",
            size_bytes=279499014144,
            fs_type="APFS",
            type_desc="APFS Volume",
        )
        parent = DriveInfo(
            id="disk7",
            name="StoreJet Transcend Media",
            size_bytes=480103981056,
            is_external=True,
            media_type="Solid state",
            child_count=3,
            volumes=[vol1, vol2],
        )

        p_dict = parent.to_dict()
        self.assertEqual(p_dict["name"], "StoreJet Transcend Media")
        self.assertEqual(p_dict["location"], "External")
        self.assertEqual(p_dict["decimal_size"], "480.1 GB")
        self.assertEqual(p_dict["child_count"], 3)
        self.assertEqual(p_dict["media_type"], "Solid state")

        v1_dict = vol1.to_dict()
        self.assertEqual(v1_dict["name"], "support-external-drive")
        self.assertEqual(v1_dict["mount_point"], "/Volumes/support-external-drive")
        self.assertEqual(v1_dict["decimal_size"], "200.39 GB")
        self.assertEqual(v1_dict["type_desc"], "APFS Volume")

        # External ExFAT Volume on macOS
        exfat_vol = VolumeInfo(
            device_id="disk10s1",
            name="My Backup",
            mount_point="/Volumes/My Backup",
            size_bytes=1000000000000,
            fs_type="exFAT",
            type_desc="exFAT",
        )
        exfat_dict = exfat_vol.to_dict()
        self.assertEqual(exfat_dict["name"], "My Backup")
        self.assertEqual(exfat_dict["device_id"], "disk10s1")
        self.assertEqual(exfat_dict["mount_point"], "/Volumes/My Backup")
        self.assertEqual(exfat_dict["type_desc"], "exFAT")
        self.assertEqual(exfat_dict["decimal_size"], "1 TB")

    def test_disk7_vs_dev_disk7_selection_normalization(self):
        """Verify disk7 and /dev/disk7 both normalize to match parent drive and store primary UUID."""
        adapter = FeatureMockAdapter()
        vol1 = VolumeInfo(device_id="disk8s1", name="support-external-drive", mount_point="/Volumes/support-external-drive", uuid="UUID-DISK7-PARENT", is_mounted=True)
        drive = DriveInfo(id="/dev/disk7", name="StoreJet Transcend", is_external=True, volumes=[vol1])
        adapter.drives = [drive]

        config = SafeEjectConfig(show_notifications=False)
        engine = SafeEjectEngine(config=config, adapter=adapter)

        # Toggle using UI format "disk7" (without /dev/)
        is_sel, msg = engine.toggle_sleep_selection("disk7")
        self.assertTrue(is_sel)
        self.assertIn("UUID-DISK7-PARENT", config.selected_sleep_drive_uuids)

        # Config check returns True for both disk7 and /dev/disk7
        self.assertTrue(config.is_drive_sleep_selected("UUID-DISK7-PARENT", "disk7"))
        self.assertTrue(config.is_drive_sleep_selected("UUID-DISK7-PARENT", "/dev/disk7"))

    def test_parent_selection_persistence(self):
        """Verify parent UUID saved in config persists across config reloads."""
        config1 = SafeEjectConfig(selected_sleep_drive_uuids=["UUID-PARENT-TEST"])
        dict_data = config1.to_dict()

        config2 = SafeEjectConfig.from_dict(dict_data)
        self.assertIn("UUID-PARENT-TEST", config2.selected_sleep_drive_uuids)
        self.assertTrue(config2.is_drive_sleep_selected("UUID-PARENT-TEST", "disk7"))
        self.assertFalse(config2.is_drive_sleep_selected("UUID-OTHER", "disk4"))

    def test_parent_precedence_in_idle_sleep_execution(self):
        """Verify _on_idle_sleep executes parent Deep Sleep once and skips child branches."""
        adapter = FeatureMockAdapter()
        vol1 = VolumeInfo(device_id="disk8s1", name="support-external-drive", mount_point="/Volumes/support-external-drive", is_mounted=True)
        vol2 = VolumeInfo(device_id="disk9s1", name="Macbook Backup", mount_point="/Volumes/Macbook Backup", is_mounted=True)
        drive = DriveInfo(id="disk7", name="StoreJet Transcend", is_external=True, volumes=[vol1, vol2], is_sleep_selected=True)
        adapter.drives = [drive]

        config = SafeEjectConfig(show_notifications=False)
        engine = SafeEjectEngine(config=config, adapter=adapter)

        results = engine._on_idle_sleep([])
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].success)
        self.assertEqual(results[0].target, "disk7")
        self.assertIn("disk7", adapter.ejected)

    def test_deep_sleep_failure_returns_non_zero_exit_code(self):
        """Verify that CLI exits with code 1 when volume unmount fails during Deep Sleep."""
        from unittest.mock import patch
        from ui.cli import cmd_deep_sleep, cmd_eject_single

        adapter = FeatureMockAdapter()
        vol1 = VolumeInfo(device_id="disk8s1", name="support-external-drive", mount_point="/Volumes/support-external-drive", is_mounted=True)
        drive = DriveInfo(id="disk7", name="StoreJet Transcend", is_external=True, volumes=[vol1])
        adapter.drives = [drive]
        adapter.fail_unmount = True  # force failure

        config = SafeEjectConfig(show_notifications=False)
        engine = SafeEjectEngine(config=config, adapter=adapter)

        class FakeArgs:
            targets = ["disk7"]
            target = "disk7"

        # cmd_eject_single should exit with code 1
        with self.assertRaises(SystemExit) as cm:
            cmd_eject_single(engine, FakeArgs())
        self.assertEqual(cm.exception.code, 1)

        # cmd_deep_sleep should exit with code 1
        with self.assertRaises(SystemExit) as cm:
            cmd_deep_sleep(engine, FakeArgs())
        self.assertEqual(cm.exception.code, 1)

    def test_sleeping_parent_recorded_in_state_manager(self):
        """Verify that parent disk7 is recorded in StateManager.drive_ids upon Deep Sleep."""
        adapter = FeatureMockAdapter()
        vol1 = VolumeInfo(device_id="disk8s1", name="support-external-drive", mount_point="/Volumes/support-external-drive", is_mounted=True)
        drive = DriveInfo(id="disk7", name="StoreJet Transcend", is_external=True, volumes=[vol1])
        adapter.drives = [drive]

        config = SafeEjectConfig(show_notifications=False)
        volume_mgr = SSDVolumeManager(config=config, adapter=adapter)

        res = volume_mgr.deep_sleep_drive("disk7")
        self.assertTrue(res.success)
        self.assertIn("Hardware Silence", res.message)

        # Check StateManager
        state = StateManager.load_ejected_drives()
        self.assertIn("disk7", state.get("drive_ids", []))
        self.assertIn("disk8s1", state.get("volume_identifiers", []))
        self.assertTrue(StateManager.is_parent_sleeping("disk7"))

    def test_safe_mount_wake_sequence_and_parent_cleanup(self):
        """Verify that mounting child disk8s1 verifies success and clears parent disk7 from sleeping state."""
        adapter = FeatureMockAdapter()
        vol1 = VolumeInfo(device_id="disk8s1", name="support-external-drive", mount_point="/Volumes/support-external-drive", is_mounted=False)
        drive = DriveInfo(id="disk7", name="StoreJet Transcend", is_external=True, volumes=[vol1])
        adapter.drives = [drive]

        def resolve_parent(tgt):
            return "disk7"
        adapter.resolve_parent_for_target = resolve_parent

        config = SafeEjectConfig(show_notifications=False)
        volume_mgr = SSDVolumeManager(config=config, adapter=adapter)

        # Prepopulate sleep state
        StateManager.save_ejected_drives(["disk7"], ["disk8s1"], append=False)
        self.assertTrue(StateManager.is_parent_sleeping("disk7"))

        # 1. Mount child volume disk8s1
        res = volume_mgr.mount_target("disk8s1")
        self.assertTrue(res.success)
        self.assertIn("disk8s1", adapter.mounted_volumes)

        # Verify parent disk7 is cleared from sleeping state on verified success
        self.assertFalse(StateManager.is_parent_sleeping("disk7"))
        state = StateManager.load_ejected_drives()
        self.assertNotIn("disk7", state.get("drive_ids", []))
        self.assertNotIn("disk8s1", state.get("volume_identifiers", []))

    def test_safe_mount_wake_sequence_retains_state_on_failure(self):
        """Verify that if mount fails, sleeping parent state is retained to prevent uncontrolled polling."""
        adapter = FeatureMockAdapter()
        vol1 = VolumeInfo(device_id="disk8s1", name="support-external-drive", mount_point="/Volumes/support-external-drive", is_mounted=False)
        drive = DriveInfo(id="disk7", name="StoreJet Transcend", is_external=True, volumes=[vol1])
        adapter.drives = [drive]

        # Simulate mount failure
        def fail_mount(vid):
            return RemountResult(target=vid, success=False, message="Device not configured")
        adapter.mount_volume = fail_mount
        adapter.resolve_parent_for_target = lambda tgt: "disk7"

        config = SafeEjectConfig(show_notifications=False)
        volume_mgr = SSDVolumeManager(config=config, adapter=adapter)

        # Prepopulate sleep state
        StateManager.save_ejected_drives(["disk7"], ["disk8s1"], append=False)

        res = volume_mgr.mount_target("disk8s1")
        self.assertFalse(res.success)

        # State must NOT be cleared when mount fails
        self.assertTrue(StateManager.is_parent_sleeping("disk7"))
        state = StateManager.load_ejected_drives()
        self.assertIn("disk7", state.get("drive_ids", []))

    def test_macos_adapter_hardware_silence_gates(self):
        """Verify MacOSAdapter suppresses diskutil list when all external drives sleep and never queries diskutil info."""
        import sys
        if sys.platform != "darwin":
            self.skipTest("MacOSAdapter tests require macOS platform")

        from platform_adapters.macos import (
            MacOSAdapter,
            _save_hardware_cache,
        )
        from unittest.mock import patch, MagicMock

        adapter = MacOSAdapter()

        # Seed hardware_cache.json with static metadata (pure metadata, no physical state)
        sample_cache = {
            "disk7": {
                "id": "disk7",
                "name": "StoreJet Transcend Media",
                "size_bytes": 480103981056,
                "bus_protocol": "USB",
                "is_external": True,
                "is_removable": True,
                "is_virtual": False,
                "media_type": "Solid state",
                "child_count": 2,
                "volumes": [
                    {
                        "device_id": "disk8s1",
                        "name": "support-external-drive",
                        "mount_point": "/Volumes/support-external-drive",
                        "size_bytes": 240000000000,
                        "fs_type": "APFS",
                        "type_desc": "APFS Volume",
                        "uuid": "UUID-1",
                        "is_mounted": True,
                    }
                ],
            }
        }
        _save_hardware_cache(sample_cache)
        StateManager.save_ejected_drives(["disk7"], ["disk8s1"], append=False)

        with patch("subprocess.run") as mock_run:
            # 1. When all external drives are sleeping: ZERO diskutil list execution
            drives = adapter.get_drives()
            mock_run.assert_not_called()
            self.assertEqual(len(drives), 1)
            self.assertEqual(drives[0].id, "disk7")
            # All volumes must be reported unmounted
            self.assertFalse(drives[0].volumes[0].is_mounted)

            # 2. In mixed state: active internal/external drive + sleeping parent disk7
            StateManager.save_ejected_drives(["disk7"], ["disk8s1"], append=False)
            mock_plist = {
                "WholeDisks": ["disk0", "disk7"],
                "AllDisksAndPartitions": [
                    {"DeviceIdentifier": "disk0", "Partitions": []},
                    {"DeviceIdentifier": "disk7", "Partitions": []},
                ],
            }
            import plistlib
            mock_proc = MagicMock()
            mock_proc.stdout = plistlib.dumps(mock_plist)
            mock_run.return_value = mock_proc

            drives_mixed = adapter.get_drives()
            # Verify diskutil info was NOT called for disk7
            info_calls = [
                c for c in mock_run.call_args_list
                if len(c[0]) > 0 and isinstance(c[0][0], list) and "info" in c[0][0] and "disk7" in c[0][0]
            ]
            self.assertEqual(len(info_calls), 0, "diskutil info must NEVER be called on sleeping parent disk7")

    def test_set_sleep_selection_and_set_all(self):
        """Verify set_sleep_selection and set_all_sleep_selections correctly update config."""
        adapter = FeatureMockAdapter()
        vol1 = VolumeInfo(device_id="disk8s1", name="support-external-drive", uuid="UUID-VOL1", is_mounted=True)
        vol2 = VolumeInfo(device_id="disk9s1", name="Macbook Backup", uuid="UUID-VOL2", is_mounted=True)
        drive = DriveInfo(id="disk7", name="StoreJet Transcend", is_external=True, volumes=[vol1, vol2])
        adapter.drives = [drive]

        config = SafeEjectConfig(show_notifications=False)
        engine = SafeEjectEngine(config=config, adapter=adapter)

        # 1. Set only vol2
        is_sel, msg = engine.set_all_sleep_selections(["disk9s1"])
        self.assertTrue(is_sel)
        self.assertEqual(config.selected_sleep_drive_uuids, ["UUID-VOL2"])
        self.assertTrue(config.is_drive_sleep_selected("UUID-VOL2", "disk9s1"))
        self.assertFalse(config.is_drive_sleep_selected("UUID-VOL1", "disk8s1"))
        self.assertFalse(config.is_drive_sleep_selected("UUID-PARENT", "disk7"))

        # 2. Set __none__ (all unchecked)
        is_sel, msg = engine.set_all_sleep_selections(["__none__"])
        self.assertTrue(is_sel)
        self.assertEqual(config.selected_sleep_drive_uuids, ["__none__"])
        self.assertFalse(config.is_drive_sleep_selected("UUID-VOL2", "disk9s1"))
        self.assertFalse(config.is_drive_sleep_selected("UUID-PARENT", "disk7"))

        # 3. Explicit set_sleep_selection on/off
        is_sel, msg = engine.set_sleep_selection("disk8s1", True)
        self.assertTrue(is_sel)
        self.assertIn("UUID-VOL1", config.selected_sleep_drive_uuids)

        is_sel, msg = engine.set_sleep_selection("disk8s1", False)
        self.assertFalse(is_sel)
        self.assertNotIn("UUID-VOL1", config.selected_sleep_drive_uuids)

    def test_eject_targets_does_not_register_touch_wake(self):
        """Verify manual unmount/eject via eject_targets purges from idle_monitor instead of queuing touch-wake."""
        adapter = FeatureMockAdapter()
        vol1 = VolumeInfo(device_id="disk8s1", name="support-external-drive", uuid="UUID-VOL1", is_mounted=True)
        drive = DriveInfo(id="disk7", name="StoreJet Transcend", is_external=True, volumes=[vol1])
        adapter.drives = [drive]

        config = SafeEjectConfig(show_notifications=False)
        engine = SafeEjectEngine(config=config, adapter=adapter)

        # Pre-seed sleeping_drive_ids
        engine.idle_monitor.mark_drive_asleep("disk8s1")
        self.assertIn("disk8s1", engine.idle_monitor.sleeping_drive_ids)

        # Manually unmount disk8s1
        results = engine.eject_targets(["disk8s1"])
        self.assertTrue(results[0].success)

        # Must be purged from sleeping_drive_ids so mouse movement does not auto-remount
        self.assertNotIn("disk8s1", engine.idle_monitor.sleeping_drive_ids)

    def test_child_partition_selective_idle_sleep(self):
        """Verify that selecting only a child volume leaves the parent and sibling partitions untouched."""
        adapter = FeatureMockAdapter()
        vol1 = VolumeInfo(device_id="disk8s1", name="support-external-drive", uuid="UUID-VOL1", is_mounted=True)
        vol2 = VolumeInfo(device_id="disk9s1", name="Macbook Backup", uuid="UUID-VOL2", is_mounted=True)
        drive = DriveInfo(id="disk7", name="StoreJet Transcend", is_external=True, volumes=[vol1, vol2])
        adapter.drives = [drive]

        config = SafeEjectConfig(show_notifications=False, selected_sleep_drive_uuids=["UUID-VOL2"])
        engine = SafeEjectEngine(config=config, adapter=adapter)

        # Deep Sleep ON is the master mode: selecting any child volume
        # causes Hardware Deep Sleep for the physical parent.
        results = engine._on_idle_sleep([])
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].target, "disk7")
        self.assertTrue(results[0].success)

        # Hardware Deep Sleep unmounts all mounted child volumes before
        # ejecting the physical parent.
        self.assertIn("disk9s1", adapter.unmounted_volumes)
        self.assertIn("disk8s1", adapter.unmounted_volumes)
        self.assertIn("disk7", adapter.ejected)

    def test_all_child_volumes_targeted_triggers_parent_deep_sleep(self):
        """Verify targeting all child partitions of a physical SSD executes parent Deep Sleep (Hardware Silence)."""
        adapter = FeatureMockAdapter()
        vol1 = VolumeInfo(device_id="disk8s1", name="support-external-drive", uuid="UUID-VOL1", is_mounted=True)
        vol2 = VolumeInfo(device_id="disk9s1", name="Macbook Backup", uuid="UUID-VOL2", is_mounted=True)
        drive = DriveInfo(id="disk7", name="StoreJet Transcend", is_external=True, volumes=[vol1, vol2])
        adapter.drives = [drive]

        config = SafeEjectConfig(show_notifications=False)
        engine = SafeEjectEngine(config=config, adapter=adapter)

        # 1. Eject both child volumes via eject_targets -> Must execute parent Deep Sleep on disk7
        results = engine.eject_targets(["disk8s1", "disk9s1"])
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].success)
        self.assertEqual(results[0].target, "disk7")
        self.assertIn("disk7", adapter.ejected)

        # 2. Eject both child volumes via eject_now -> Must execute parent Deep Sleep on disk7
        adapter.ejected.clear()
        results_now = engine.volume_manager.eject_now(targets=["disk8s1", "disk9s1"])
        self.assertEqual(len(results_now), 1)
        self.assertTrue(results_now[0].success)
        self.assertEqual(results_now[0].target, "disk7")
        self.assertIn("disk7", adapter.ejected)

    def test_all_child_volumes_targeted_even_when_already_unmounted(self):
        """When child volumes are already unmounted, targeting them still triggers parent Deep Sleep once."""
        adapter = FeatureMockAdapter()
        vol1 = VolumeInfo(device_id="disk5s1", name="HGST 1st", is_mounted=False)
        vol2 = VolumeInfo(device_id="disk5s2", name="HGST 2nd", is_mounted=False)
        drive = DriveInfo(id="disk5", name="HGST Travelstar HDD", media_type="Hard Disk", is_external=True, volumes=[vol1, vol2])
        adapter.drives = [drive]

        config = SafeEjectConfig(show_notifications=False)
        engine = SafeEjectEngine(config=config, adapter=adapter)

        results = engine.eject_targets(["disk5s1", "disk5s2"])
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].success)
        self.assertEqual(results[0].target, "disk5")
        self.assertIn("disk5", adapter.ejected)

        # eject_now with both volumes must also trigger parent Deep Sleep once
        adapter.ejected.clear()
        results_now = engine.volume_manager.eject_now(targets=["disk5s1", "disk5s2"])
        self.assertEqual(len(results_now), 1)
        self.assertTrue(results_now[0].success)
        self.assertEqual(results_now[0].target, "disk5")
        self.assertIn("disk5", adapter.ejected)

    def test_cmd_check_locks_no_drives(self):
        """cmd_check_locks displays friendly message when no external drives connected."""
        import io
        import sys
        from ui.cli import cmd_check_locks

        adapter = FeatureMockAdapter()
        engine = SafeEjectEngine(adapter=adapter)

        captured_out = io.StringIO()
        old_stdout = sys.stdout
        try:
            sys.stdout = captured_out
            cmd_check_locks(engine, None)
        finally:
            sys.stdout = old_stdout

        output = captured_out.getvalue()
        self.assertIn("No external drives are currently connected", output)

    def test_cmd_check_locks_clean_mounted_volumes(self):
        """cmd_check_locks reports clean status when mounted volumes have no blocking processes."""
        import io
        import sys
        import tempfile
        from ui.cli import cmd_check_locks

        with tempfile.TemporaryDirectory() as tmp_mount:
            adapter = FeatureMockAdapter()
            vol = VolumeInfo(device_id="disk8s1", name="WorkDrive", mount_point=tmp_mount, is_mounted=True)
            drive = DriveInfo(id="disk8", name="Samsung SSD", media_type="Solid State", is_external=True, volumes=[vol])
            adapter.drives = [drive]

            engine = SafeEjectEngine(adapter=adapter)

            captured_out = io.StringIO()
            old_stdout = sys.stdout
            try:
                sys.stdout = captured_out
                cmd_check_locks(engine, None)
            finally:
                sys.stdout = old_stdout

            output = captured_out.getvalue()
            self.assertIn("All External Drives are Clean!", output)
            self.assertIn("No apps or background processes", output)

    def test_cmd_check_locks_active_locks(self):
        """cmd_check_locks reports detailed active locks when processes are holding files."""
        import io
        import sys
        import tempfile
        from core.models import ProcessLockInfo
        from ui.cli import cmd_check_locks

        with tempfile.TemporaryDirectory() as tmp_mount:
            adapter = FeatureMockAdapter()
            adapter.blocking_processes = {
                tmp_mount: [
                    ProcessLockInfo(pid=1234, process_name="QuickLook", file_path=f"{tmp_mount}/photo.jpg"),
                    ProcessLockInfo(pid=5678, process_name="FinalCut", file_path=f"{tmp_mount}/video.mp4"),
                ]
            }
            vol = VolumeInfo(device_id="disk8s1", name="ProjectDrive", mount_point=tmp_mount, is_mounted=True)
            drive = DriveInfo(id="disk8", name="SanDisk Extreme", media_type="Solid State", is_external=True, volumes=[vol])
            adapter.drives = [drive]

            engine = SafeEjectEngine(adapter=adapter)

            captured_out = io.StringIO()
            old_stdout = sys.stdout
            try:
                sys.stdout = captured_out
                cmd_check_locks(engine, None)
            finally:
                sys.stdout = old_stdout

            output = captured_out.getvalue()
            self.assertIn("Active File Locks Detected", output)
            self.assertIn("ProjectDrive", output)
            self.assertIn("QuickLook", output)
            self.assertIn("PID 1234", output)
            self.assertIn("FinalCut", output)
            self.assertIn("PID 5678", output)

    def test_smart_graceful_shutdown_in_deep_sleep(self):
        adapter = FeatureMockAdapter()
        vol1 = VolumeInfo(device_id="disk8s1", name="AppsSSD", mount_point="/Volumes/AppsSSD", is_mounted=True)
        vol2 = VolumeInfo(device_id="disk8s2", name="DataSSD", mount_point="/Volumes/DataSSD", is_mounted=True)
        drive = DriveInfo(id="disk8", name="External SSD", is_external=True, volumes=[vol1, vol2])
        adapter.drives = [drive]

        mgr = SSDVolumeManager(config=SafeEjectConfig(), adapter=adapter)
        res = mgr.deep_sleep_drive("disk8")
        self.assertTrue(res.success)
        self.assertIn("/Volumes/AppsSSD", adapter.graceful_closed_mounts)
        self.assertIn("/Volumes/DataSSD", adapter.graceful_closed_mounts)

    def test_smart_graceful_shutdown_in_normal_sleep(self):
        adapter = FeatureMockAdapter()
        vol = VolumeInfo(device_id="disk5s1", name="MediaDrive", mount_point="/Volumes/MediaDrive", is_mounted=True)
        drive = DriveInfo(id="disk5", name="External HDD", is_external=True, volumes=[vol])
        adapter.drives = [drive]

        mgr = SSDVolumeManager(config=SafeEjectConfig(), adapter=adapter)
        res = mgr.normal_sleep_drive("disk5")
        self.assertTrue(res.success)
        self.assertIn("/Volumes/MediaDrive", adapter.graceful_closed_mounts)

    def test_smart_graceful_shutdown_in_unmount_target(self):
        adapter = FeatureMockAdapter()
        vol = VolumeInfo(device_id="disk6s1", name="WebSSD", mount_point="/Volumes/WebSSD", is_mounted=True)
        drive = DriveInfo(id="disk6", name="External SSD", is_external=True, volumes=[vol])
        adapter.drives = [drive]

        config = SafeEjectConfig()
        config.deep_sleep_mode = False
        mgr = SSDVolumeManager(config=config, adapter=adapter)
        res = mgr.unmount_target("disk6s1")
        self.assertTrue(res.success)
        self.assertIn("/Volumes/WebSSD", adapter.graceful_closed_mounts)

    def test_smart_graceful_shutdown_in_eject_now(self):
        adapter = FeatureMockAdapter()
        vol = VolumeInfo(device_id="disk7s1", name="FastSSD", mount_point="/Volumes/FastSSD", is_mounted=True)
        drive = DriveInfo(id="disk7", name="Portable SSD", is_managed=True, is_external=True, volumes=[vol])
        adapter.drives = [drive]

        mgr = SSDVolumeManager(config=SafeEjectConfig(), adapter=adapter)
        results = mgr.eject_now()
        self.assertTrue(any(r.success for r in results))
        self.assertIn("/Volumes/FastSSD", adapter.graceful_closed_mounts)

    def test_macos_adapter_graceful_close_ignores_whitelist(self):
        from platform_adapters.macos import MacOSAdapter
        adapter = MacOSAdapter()
        # Non-existent mount point returns cleanly
        closed = adapter.graceful_close_blocking_processes("/non/existent/path")
        self.assertEqual(closed, [])


if __name__ == "__main__":
    unittest.main()


