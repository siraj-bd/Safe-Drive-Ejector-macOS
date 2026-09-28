"""
Regression tests for Deep Sleep Auto-Wake Isolation.
Verifies strict separation of Explicit Deep Sleep from Idle Sleep,
prevention of touch/system auto-wakes, elimination of mount-all fallback,
clean snapshot dependency aborts, and selective child partition wake.
"""

import unittest

from core.config import SafeEjectConfig, StateManager
from core.engine import SafeEjectEngine
from core.models import DriveInfo, EjectResult, RemountResult, VolumeInfo
from platform_adapters.base import PlatformAdapter


class IsolationMockAdapter(PlatformAdapter):
    def __init__(self, drives=None):
        self.drives = drives or []
        self.ejected = []
        self.mounted = []
        self.mounted_volumes = []
        self.unmounted_volumes = []
        self.unmounted_disks = []
        self.snapshot_fail = False

    def get_drives(self):
        return self.drives

    def eject_drive(self, drive_id: str):
        self.ejected.append(drive_id)
        return EjectResult(target=drive_id, success=True, message=f"Drive {drive_id} ejected successfully.")

    def unmount_volume(self, volume_id: str):
        self.unmounted_volumes.append(volume_id)
        return EjectResult(target=volume_id, success=True, message=f"Volume {volume_id} unmounted.")

    def mount_drive(self, drive_id: str):
        self.mounted.append(drive_id)
        return RemountResult(target=drive_id, success=True, message=f"Drive {drive_id} mounted.")

    def mount_volume(self, volume_id: str):
        self.mounted_volumes.append(volume_id)
        return RemountResult(target=volume_id, success=True, message=f"Volume {volume_id} mounted.")

    def unmount_disk(self, disk_id: str):
        self.unmounted_disks.append(disk_id)
        return EjectResult(target=disk_id, success=True, message=f"Container {disk_id} unmounted.")

    def teardown_snapshots_for_drive(self, target_drive):
        if self.snapshot_fail:
            return EjectResult(
                target=target_drive.id,
                success=False,
                message="Deep Sleep aborted: Snapshot /Volumes/.timemachine/backup in use (Resource busy)",
            )
        return None

    def get_apfs_containers_for_disk(self, disk_id: str):
        return ["disk8", "disk9"] if disk_id == "disk7" else []

    def resolve_parent_for_target(self, target_id: str):
        clean = target_id.replace("/dev/", "").lower()
        if "disk8" in clean or "disk9" in clean or "disk7" in clean:
            return "disk7"
        return target_id

    def get_blocking_processes(self, mount_point: str):
        return []

    def kill_process(self, pid: int):
        return True

    def show_notification(self, title: str, message: str):
        pass

    def start_power_listener(self, on_sleep, on_wake):
        pass


class TestDeepSleepAutoWakeIsolation(unittest.TestCase):

    def setUp(self):
        StateManager.clear_ejected_drives()

    def tearDown(self):
        StateManager.clear_ejected_drives()

    def _create_mock_engine(self, snapshot_fail=False):
        v1 = VolumeInfo(device_id="disk8s1", name="support-external-drive", mount_point="/Volumes/support-external-drive", is_mounted=True)
        v2 = VolumeInfo(device_id="disk9s1", name="Macbook Backup", mount_point="/Volumes/Macbook Backup", is_mounted=True)
        parent = DriveInfo(id="disk7", name="StoreJet Transcend", is_external=True, is_managed=True, volumes=[v1, v2])
        adapter = IsolationMockAdapter(drives=[parent])
        adapter.snapshot_fail = snapshot_fail
        config = SafeEjectConfig(show_notifications=False, remount_on_wake=True, wake_mode="touch")
        engine = SafeEjectEngine(config=config, adapter=adapter)
        return engine, adapter, parent, v1, v2

    def test_explicit_sleep_records_disjoint_state(self):
        """Rule 1, 2, 3: Explicit Deep Sleep parent can NEVER be in idle_sleep_parent_ids."""
        engine, adapter, parent, v1, v2 = self._create_mock_engine()

        res = engine.deep_sleep_target("disk7")
        self.assertTrue(res.success)
        self.assertIn("disk7", adapter.ejected)

        # Verify state
        state = StateManager.load_ejected_drives()
        self.assertIn("disk7", state.get("explicit_sleep_parent_ids", []))
        self.assertNotIn("disk7", state.get("idle_sleep_parent_ids", []))
        self.assertTrue(StateManager.is_explicit_deep_sleeping("disk7"))
        self.assertNotIn("disk7", StateManager.get_auto_wakeable_parent_ids())

    def test_touch_wake_isolation_skips_explicit_deep_sleep_drives(self):
        """Rule 4: mouse/keyboard activity must NEVER wake an explicit Deep Sleep drive."""
        engine, adapter, parent, v1, v2 = self._create_mock_engine()

        # Put parent into explicit Deep Sleep
        engine.deep_sleep_target("disk7")
        self.assertIn("disk7", adapter.ejected)

        # Simulate user touching the Mac (touch-wake trigger)
        engine._on_touch_wake(["disk7"])

        # Drive and volumes must remain completely UNMOUNTED
        self.assertEqual(len(adapter.mounted), 0)
        self.assertEqual(len(adapter.mounted_volumes), 0)
        self.assertTrue(StateManager.is_explicit_deep_sleeping("disk7"))

    def test_system_wake_isolation_skips_explicit_deep_sleep_drives(self):
        """Rule 5: Mac system wake must NOT remount explicit Deep Sleep drives."""
        engine, adapter, parent, v1, v2 = self._create_mock_engine()

        # Explicit Deep Sleep
        engine.deep_sleep_target("disk7")

        # Simulate system wake (remount_all_ejected with only_if_recorded=True)
        results = engine.remount_all_ejected(only_if_recorded=True)

        # Must skip explicit sleep drive and return no results
        self.assertEqual(len(results), 0)
        self.assertEqual(len(adapter.mounted), 0)
        self.assertEqual(len(adapter.mounted_volumes), 0)
        self.assertTrue(StateManager.is_explicit_deep_sleeping("disk7"))

    def test_no_fallback_mount_all_when_state_is_empty(self):
        """Rule 6: remount_all_ejected with empty state must NEVER mount managed drives."""
        engine, adapter, parent, v1, v2 = self._create_mock_engine()

        # Ensure state is 100% empty
        StateManager.clear_ejected_drives()

        # Call remount_all_ejected (even without only_if_recorded flag)
        results = engine.remount_all_ejected(only_if_recorded=False)

        # Must NOT call mount_drive or mount_volume
        self.assertEqual(len(results), 0)
        self.assertEqual(len(adapter.mounted), 0)
        self.assertEqual(len(adapter.mounted_volumes), 0)

    def test_selective_child_mount_wakes_only_target(self):
        """Rule 8: Mounting disk8s1 wakes parent disk7 and mounts ONLY disk8s1 while disk9s1 stays unmounted."""
        engine, adapter, parent, v1, v2 = self._create_mock_engine()

        # Explicit Deep Sleep
        engine.deep_sleep_target("disk7")

        # Mount ONLY disk8s1
        res = engine.remount_single("disk8s1")
        self.assertTrue(res.success)

        # Verify only disk8s1 was mounted
        self.assertIn("disk8s1", adapter.mounted_volumes)
        self.assertNotIn("disk9s1", adapter.mounted_volumes)

        # Verified success clears parent sleeping state
        self.assertFalse(StateManager.is_parent_sleeping("disk7"))
        self.assertFalse(StateManager.is_explicit_deep_sleeping("disk7"))

    def test_busy_snapshot_dependency_aborts_deep_sleep_safely(self):
        """Rule 9: Busy Time Machine snapshot dependency aborts Deep Sleep without force ejecting."""
        engine, adapter, parent, v1, v2 = self._create_mock_engine(snapshot_fail=True)

        res = engine.deep_sleep_target("disk7")

        # Must abort cleanly with error
        self.assertFalse(res.success)
        self.assertIn("Snapshot", res.message)
        self.assertIn("in use", res.message)

        # Must NOT eject parent drive
        self.assertNotIn("disk7", adapter.ejected)

        # Must NOT save sleeping state
        self.assertFalse(StateManager.is_parent_sleeping("disk7"))
        self.assertFalse(StateManager.is_explicit_deep_sleeping("disk7"))


if __name__ == "__main__":
    unittest.main()
