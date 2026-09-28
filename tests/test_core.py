"""
Unit tests for SafeEject core engine, models, and configuration.
"""

import unittest

from core.config import SafeEjectConfig, StateManager
from core.engine import SafeEjectEngine
from core.models import DriveInfo, EjectResult, RemountResult, VolumeInfo, format_size
from platform_adapters.base import PlatformAdapter


class MockAdapter(PlatformAdapter):
    def __init__(self, drives=None):
        self.drives = drives or []
        self.ejected = []
        self.mounted = []
        self.notifications = []

    def get_drives(self):
        return self.drives

    def eject_drive(self, drive_id: str):
        self.ejected.append(drive_id)
        return EjectResult(target=drive_id, success=True, message="Mock ejected")

    def unmount_volume(self, volume_id: str):
        return EjectResult(target=volume_id, success=True, message="Mock unmounted")

    def mount_drive(self, drive_id: str):
        self.mounted.append(drive_id)
        return RemountResult(target=drive_id, success=True, message="Mock mounted")

    def mount_volume(self, volume_id: str):
        self.mounted.append(volume_id)
        return RemountResult(target=volume_id, success=True, message="Mock mounted")

    def get_blocking_processes(self, mount_point: str):
        return []

    def kill_process(self, pid: int):
        return True

    def show_notification(self, title: str, message: str):
        self.notifications.append((title, message))

    def start_power_listener(self, on_sleep, on_wake):
        pass


class TestSafeEjectCore(unittest.TestCase):
    def setUp(self):
        StateManager.clear_ejected_drives()

    def tearDown(self):
        StateManager.clear_ejected_drives()

    def test_format_size(self):
        self.assertEqual(format_size(0), "0 B")
        self.assertEqual(format_size(1024), "1.0 KB")
        self.assertEqual(format_size(1024 * 1024 * 500), "500.0 MB")
        self.assertEqual(format_size(1024 * 1024 * 1024 * 250), "250.0 GB")

    def test_drive_and_volume_models(self):
        vol = VolumeInfo(
            device_id="disk7s1",
            name="BackupDisk",
            mount_point="/Volumes/BackupDisk",
            size_bytes=1000000000,
            is_mounted=True,
        )
        drive = DriveInfo(
            id="disk7",
            name="External USB Drive",
            size_bytes=1000000000,
            bus_protocol="USB",
            is_external=True,
            volumes=[vol],
        )

        self.assertTrue(drive.is_external)
        self.assertFalse(drive.is_virtual)
        self.assertTrue(drive.has_mounted_volumes)
        self.assertEqual(len(drive.volumes), 1)
        self.assertEqual(drive.volumes[0].name, "BackupDisk")

    def test_config_exclusions(self):
        config = SafeEjectConfig(
            excluded_volumes=["SecretData", "WorkDrive"],
            excluded_drives=["disk99"],
        )

        self.assertTrue(config.is_excluded("SecretData"))
        self.assertTrue(config.is_excluded("workdrive"))  # Case-insensitive
        self.assertTrue(config.is_excluded("", drive_id="disk99"))
        self.assertFalse(config.is_excluded("NormalDrive"))

    def test_engine_eject_and_remount_flow(self):
        vol1 = VolumeInfo(device_id="disk7s1", name="ExtVol1", mount_point="/Volumes/ExtVol1", is_mounted=True)
        drive1 = DriveInfo(id="disk7", name="USB Drive", is_external=True, volumes=[vol1])

        vol_int = VolumeInfo(device_id="disk0s1", name="MacHD", mount_point="/", is_mounted=True)
        drive_int = DriveInfo(id="disk0", name="Internal SSD", is_external=False, volumes=[vol_int])

        mock_adapter = MockAdapter(drives=[drive1, drive_int])
        config = SafeEjectConfig(show_notifications=False)
        engine = SafeEjectEngine(config=config, adapter=mock_adapter)

        # 1. External drives filter
        exts = engine.get_external_drives()
        self.assertEqual(len(exts), 1)
        self.assertEqual(exts[0].id, "disk7")

        # 2. Eject all external
        eject_results = engine.eject_all_external()
        self.assertEqual(len(eject_results), 1)
        self.assertTrue(eject_results[0].success)
        self.assertIn("disk7", mock_adapter.ejected)
        self.assertNotIn("disk0", mock_adapter.ejected)

        # 3. Remount all previously ejected
        remount_results = engine.remount_all_ejected()
        self.assertEqual(len(remount_results), 1)
        self.assertTrue(remount_results[0].success)
        self.assertIn("disk7s1", mock_adapter.mounted)

    def test_engine_skips_excluded_volumes(self):
        vol1 = VolumeInfo(device_id="disk7s1", name="KeepMounted", mount_point="/Volumes/KeepMounted", is_mounted=True)
        drive1 = DriveInfo(id="disk7", name="USB Drive", is_external=True, volumes=[vol1])

        mock_adapter = MockAdapter(drives=[drive1])
        config = SafeEjectConfig(excluded_volumes=["KeepMounted"], show_notifications=False)
        engine = SafeEjectEngine(config=config, adapter=mock_adapter)

        eject_results = engine.eject_all_external()
        self.assertEqual(len(eject_results), 0)
        self.assertEqual(len(mock_adapter.ejected), 0)


if __name__ == "__main__":
    unittest.main()
