"""
Abstract Base Class for OS Platform Adapters.
"""

from abc import ABC, abstractmethod
from typing import Callable, List
from core.models import DriveInfo, ProcessLockInfo, EjectResult, RemountResult


class PlatformAdapter(ABC):
    """Abstract interface for platform-specific disk and power operations."""

    @abstractmethod
    def get_drives(self) -> List[DriveInfo]:
        """Enumerate all connected drives and volumes."""
        pass

    @abstractmethod
    def eject_drive(self, drive_id: str) -> EjectResult:
        """Safely eject / dismount a whole physical or virtual drive."""
        pass

    @abstractmethod
    def unmount_volume(self, volume_id: str) -> EjectResult:
        """Safely unmount a single volume / partition."""
        pass

    def unmount_disk(self, disk_id: str) -> EjectResult:
        """Safely unmount an entire disk or container."""
        return EjectResult(target=disk_id, success=True, message=f"Disk {disk_id} unmounted.")

    def get_apfs_containers_for_disk(self, disk_id: str) -> List[str]:
        """Return synthesized container identifiers belonging to the disk if supported by platform."""
        return []

    @abstractmethod
    def mount_drive(self, drive_id: str) -> RemountResult:
        """Remount all volumes belonging to a drive."""
        pass

    @abstractmethod
    def mount_volume(self, volume_id: str) -> RemountResult:
        """Remount a single volume."""
        pass

    @abstractmethod
    def get_blocking_processes(self, mount_point: str) -> List[ProcessLockInfo]:
        """Find processes that currently hold open file handles on a volume."""
        pass

    @abstractmethod
    def kill_process(self, pid: int) -> bool:
        """Terminate a process holding a lock."""
        pass

    def graceful_close_blocking_processes(self, mount_point: str) -> List[str]:
        """Gracefully close applications and processes locking files on a volume before unmount/sleep."""
        return []

    @abstractmethod
    def show_notification(self, title: str, message: str) -> None:
        """Display a native desktop notification."""
        pass

    @abstractmethod
    def start_power_listener(
        self,
        on_sleep: Callable[[], None],
        on_wake: Callable[[], None],
    ) -> None:
        """
        Register for system sleep and wake power events.
        Blocks the calling thread or runs an OS event loop.
        """
        pass
