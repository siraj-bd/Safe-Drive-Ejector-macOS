"""
Configuration and persistent state manager for macOS (~/.config/safe-eject).
"""

import json
import os
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List


def get_config_dir() -> Path:
    """Returns the directory for SafeEject configuration with graceful fallback."""
    custom_dir = os.environ.get("SAFEEJECT_CONFIG_DIR")
    if custom_dir:
        path = Path(custom_dir)
        path.mkdir(parents=True, exist_ok=True)
        return path

    path = Path.home() / ".config" / "safe-eject"

    try:
        path.mkdir(parents=True, exist_ok=True)
        test_file = path / ".write_test"
        with open(test_file, "w") as f:
            f.write("1")
        test_file.unlink(missing_ok=True)
        return path
    except Exception:
        # In sandboxed environments or restricted users, fallback to workspace .config
        fallback_path = Path.cwd() / ".safe_eject_config"
        fallback_path.mkdir(parents=True, exist_ok=True)
        return fallback_path


SLEEP_TIMER_PRESETS = {
    "2m": 120,
    "5m": 300,
    "10m": 600,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "2h": 7200,
    "never": 0,
}

MAX_MANAGED_DRIVES = 6


@dataclass
class SafeEjectConfig:
    eject_on_sleep: bool = True
    remount_on_wake: bool = True
    show_notifications: bool = True
    check_blocking_processes: bool = True
    auto_kill_blocking: bool = False
    excluded_volumes: List[str] = field(default_factory=list)
    excluded_drives: List[str] = field(default_factory=list)
    dev_github_url: str = "https://github.com/siraj-bd"
    repo_github_url: str = "https://github.com/siraj-bd/Safe-Drive-Ejector-macOS"
    github_stars: int = 11
    retry_count: int = 2
    retry_delay_seconds: float = 1.0
    log_level: str = "INFO"

    # System & Automation Settings
    start_at_login: bool = True
    show_menubar_icon: bool = True
    show_disks_count_badge: bool = True
    eject_after_display_off: bool = False
    eject_before_logout: bool = False

    # What to Eject
    eject_hard_disks_ssds: bool = True
    eject_dvds_cds: bool = False
    eject_disk_images: bool = False
    eject_network_drives: bool = False
    eject_sd_cards: bool = False
    unmount_instead_of_eject: bool = True

    # Notification & Sounds
    sound_on_success: bool = True
    sound_on_failure: bool = True
    success_sound: str = "Bubble"
    failure_sound: str = "Gong"

    # Options
    remount_delay_seconds: int = 5

    # Custom Functional Features (Individual SSD Selection & Mac Touch Wake)
    sleep_timer_seconds: int = 120       # Default: 2 minutes
    deep_sleep_mode: bool = True         # True = Hardware Deep Sleep (LED off, manual mount only); False = Normal Sleep (touch wakeable)
    managed_drive_uuids: List[str] = field(default_factory=list) # Up to 6 managed drives
    selected_sleep_drive_uuids: List[str] = field(default_factory=list) # Individually selected SSDs to sleep
    wake_mode: str = "touch"             # "touch" = auto active on Mac touch; "manual" = stay asleep until manual mount
    play_sounds: bool = True             # Audio feedback

    # Backwards compatibility property aliases for unified flags
    @property
    def eject_before_sleep(self) -> bool:
        return self.eject_on_sleep

    @eject_before_sleep.setter
    def eject_before_sleep(self, val: bool) -> None:
        self.eject_on_sleep = bool(val)

    @property
    def auto_awake(self) -> bool:
        return self.remount_on_wake

    @auto_awake.setter
    def auto_awake(self, val: bool) -> None:
        self.remount_on_wake = bool(val)

    @property
    def notify_after_eject_remount(self) -> bool:
        return self.show_notifications

    @notify_after_eject_remount.setter
    def notify_after_eject_remount(self, val: bool) -> None:
        self.show_notifications = bool(val)

    @property
    def sleep_timer_label(self) -> str:
        sec = self.sleep_timer_seconds
        if sec <= 0:
            return "Never"
        elif sec < 60:
            return f"{sec} Seconds"
        elif sec < 3600:
            return f"{sec // 60} Minute{'s' if sec // 60 > 1 else ''}"
        else:
            hours = sec // 3600
            return f"{hours} Hour{'s' if hours > 1 else ''}"

    def is_drive_sleep_selected(self, uuid: str, drive_id: str = "") -> bool:
        """Check if an individual SSD/partition is selected for sleep."""
        if not self.selected_sleep_drive_uuids:
            # If none explicitly selected, select all managed drives
            return self.is_drive_managed(uuid, drive_id)
        if self.selected_sleep_drive_uuids == ["__none__"]:
            return False
        clean_uuid = (uuid or "").strip().lower()
        clean_drive_id = (drive_id or "").replace("/dev/", "").strip().lower()
        for s in self.selected_sleep_drive_uuids:
            s_clean = s.replace("/dev/", "").strip().lower()
            if (clean_uuid and s_clean == clean_uuid) or (clean_drive_id and s_clean == clean_drive_id):
                return True
        return False

    def set_drive_sleep_selected(self, uuid: str, selected: bool) -> bool:
        """Explicitly set an individual drive or volume sleep selection."""
        clean_uuid = uuid.strip()
        if not clean_uuid:
            return False
        if self.selected_sleep_drive_uuids == ["__none__"]:
            self.selected_sleep_drive_uuids = []

        found_idx = -1
        for idx, s in enumerate(self.selected_sleep_drive_uuids):
            if s.lower() == clean_uuid.lower():
                found_idx = idx
                break

        if selected:
            if found_idx == -1:
                self.selected_sleep_drive_uuids.append(clean_uuid)
                self.save()
            return True
        else:
            if found_idx != -1:
                self.selected_sleep_drive_uuids.pop(found_idx)
                if not self.selected_sleep_drive_uuids:
                    self.selected_sleep_drive_uuids = ["__none__"]
                self.save()
            return False

    def set_selected_sleep_drives(self, targets: List[str]) -> None:
        """Set exact list of targets selected for sleep."""
        cleaned = [t.strip() for t in targets if t and t.strip() and t.strip() != "__none__"]
        self.selected_sleep_drive_uuids = cleaned if cleaned else ["__none__"]
        self.save()

    def toggle_sleep_drive_selection(self, uuid: str) -> bool:
        """Toggle individual SSD selection for auto-sleep."""
        clean_uuid = uuid.strip()
        if not clean_uuid:
            return False
        if self.selected_sleep_drive_uuids == ["__none__"]:
            self.selected_sleep_drive_uuids = []
        for idx, s in enumerate(self.selected_sleep_drive_uuids):
            if s.lower() == clean_uuid.lower():
                self.selected_sleep_drive_uuids.pop(idx)
                if not self.selected_sleep_drive_uuids:
                    self.selected_sleep_drive_uuids = ["__none__"]
                self.save()
                return False
        self.selected_sleep_drive_uuids.append(clean_uuid)
        self.save()
        return True

    def set_timer_preset(self, preset_key: str) -> bool:
        clean_key = preset_key.lower().strip()
        if clean_key in SLEEP_TIMER_PRESETS:
            self.sleep_timer_seconds = SLEEP_TIMER_PRESETS[clean_key]
            self.save()
            return True
        return False

    def is_drive_managed(self, uuid: str, drive_id: str = "") -> bool:
        """If no managed drives are explicitly defined, manage all connected external drives (up to 6)."""
        if not self.managed_drive_uuids:
            return True
        for m in self.managed_drive_uuids:
            if m.lower() == uuid.lower() or (drive_id and m.lower() == drive_id.lower()):
                return True
        return False

    def toggle_managed_drive(self, uuid: str) -> bool:
        """Add or remove drive from up-to-6 managed drives list. Returns True if now managed."""
        clean_uuid = uuid.strip()
        if not clean_uuid:
            return False

        # If present, remove
        for idx, m in enumerate(self.managed_drive_uuids):
            if m.lower() == clean_uuid.lower():
                self.managed_drive_uuids.pop(idx)
                self.save()
                return False

        # If not present, add up to limit
        if len(self.managed_drive_uuids) < MAX_MANAGED_DRIVES:
            self.managed_drive_uuids.append(clean_uuid)
            self.save()
            return True
        return False

    def to_dict(self) -> Dict[str, Any]:
        """Convert configuration to dictionary including compatibility aliases."""
        data = asdict(self)
        data["eject_before_sleep"] = self.eject_on_sleep
        data["auto_awake"] = self.remount_on_wake
        data["notify_after_eject_remount"] = self.show_notifications
        return data

    def update_settings(self, updates: Dict[str, Any]) -> None:
        """Update multiple configuration settings at once and save."""
        alias_map = {
            "eject_before_sleep": "eject_on_sleep",
            "auto_awake": "remount_on_wake",
            "notify_after_eject_remount": "show_notifications",
        }
        for k, v in updates.items():
            target_key = alias_map.get(k, k)
            if hasattr(self, target_key):
                field_val = getattr(self, target_key)
                if isinstance(field_val, bool) and isinstance(v, str):
                    v = v.lower() in ("true", "1", "yes")
                elif isinstance(field_val, int) and isinstance(v, (str, float)):
                    v = int(v)
                setattr(self, target_key, v)
        self.save()

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SafeEjectConfig":
        """Construct SafeEjectConfig from a dictionary with aliases mapped."""
        d = dict(data)
        if "eject_on_sleep" not in d and "eject_before_sleep" in d:
            d["eject_on_sleep"] = d["eject_before_sleep"]
        if "remount_on_wake" not in d and "auto_awake" in d:
            d["remount_on_wake"] = d["auto_awake"]
        if "show_notifications" not in d and "notify_after_eject_remount" in d:
            d["show_notifications"] = d["notify_after_eject_remount"]
        if "deep_sleep_mode" not in d and "remount_on_wake" in d:
            d["deep_sleep_mode"] = not d["remount_on_wake"]
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})

    @classmethod
    def load(cls) -> "SafeEjectConfig":
        config_path = get_config_dir() / "config.json"
        if not config_path.exists():
            default_config = cls()
            default_config.save()
            return default_config

        try:
            with open(config_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return cls.from_dict(data)
        except Exception:
            return cls()

    def save(self) -> None:
        config_path = get_config_dir() / "config.json"
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(asdict(self), f, indent=4)

    def is_excluded(self, volume_name: str, volume_uuid: str = "", drive_id: str = "") -> bool:
        """Check if a volume or drive is excluded from unmounting/ejecting."""
        for excl in self.excluded_volumes:
            if excl and (excl.lower() == volume_name.lower() or (volume_uuid and excl.lower() == volume_uuid.lower())):
                return True
        for excl in self.excluded_drives:
            if excl and excl.lower() == drive_id.lower():
                return True
        return False


class StateManager:
    """Tracks state such as drives ejected prior to sleep, for remounting on wake."""

    @staticmethod
    def get_state_file() -> Path:
        return get_config_dir() / "ejected_state.json"

    @classmethod
    def save_ejected_drives(
        cls,
        drive_ids: List[str],
        volume_identifiers: List[str],
        append: bool = True,
        is_explicit_deep_sleep: bool = False,
    ) -> None:
        """
        Saves ejected/sleeping drives and volumes with strict separation between
        Explicit Deep Sleep (manual/UI) and Idle Sleep (timer/touch-wake).
        A drive can NEVER be in both explicit and idle sleep sets simultaneously.
        """
        try:
            clean_drives = [d.strip().replace("/dev/", "") for d in drive_ids if d.strip()]
            clean_vols = [v.strip().replace("/dev/", "") for v in volume_identifiers if v.strip()]

            if append:
                existing = cls.load_ejected_drives()
                combined_drives = list(dict.fromkeys(existing.get("drive_ids", []) + clean_drives))
                combined_vols = list(dict.fromkeys(existing.get("volume_identifiers", []) + clean_vols))
                existing_explicit = existing.get("explicit_sleep_parent_ids", [])
                existing_idle = existing.get("idle_sleep_parent_ids", [])

                if is_explicit_deep_sleep:
                    # Enforce disjoint: add to explicit, purge completely from idle!
                    explicit_parents = list(dict.fromkeys(existing_explicit + clean_drives))
                    idle_parents = [p for p in existing_idle if p not in explicit_parents]
                else:
                    # Idle sleep: only add if not already in explicit deep sleep!
                    explicit_parents = existing_explicit
                    idle_parents = list(dict.fromkeys([
                        p for p in (existing_idle + clean_drives) if p not in explicit_parents
                    ]))
            else:
                combined_drives = list(dict.fromkeys(clean_drives))
                combined_vols = list(dict.fromkeys(clean_vols))
                if is_explicit_deep_sleep:
                    explicit_parents = list(dict.fromkeys(clean_drives))
                    idle_parents = []
                else:
                    explicit_parents = []
                    idle_parents = list(dict.fromkeys(clean_drives))

            state_path = cls.get_state_file()
            data = {
                "drive_ids": combined_drives,
                "volume_identifiers": combined_vols,
                "explicit_sleep_parent_ids": explicit_parents,
                "idle_sleep_parent_ids": idle_parents,
            }
            with open(state_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=4)
        except Exception:
            pass

    @classmethod
    def load_ejected_drives(cls) -> Dict[str, Any]:
        state_path = cls.get_state_file()
        if not state_path.exists():
            return {
                "drive_ids": [],
                "volume_identifiers": [],
                "explicit_sleep_parent_ids": [],
                "idle_sleep_parent_ids": [],
            }
        try:
            with open(state_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                drive_ids = [d.strip().replace("/dev/", "") for d in data.get("drive_ids", []) if d.strip()]
                vol_ids = [v.strip().replace("/dev/", "") for v in data.get("volume_identifiers", []) if v.strip()]
                explicit_parents = [p.strip().replace("/dev/", "") for p in data.get("explicit_sleep_parent_ids", []) if p.strip()]
                idle_parents = [p.strip().replace("/dev/", "") for p in data.get("idle_sleep_parent_ids", []) if p.strip() and p not in explicit_parents]
                return {
                    "drive_ids": drive_ids,
                    "volume_identifiers": vol_ids,
                    "explicit_sleep_parent_ids": explicit_parents,
                    "idle_sleep_parent_ids": idle_parents,
                }
        except Exception:
            return {
                "drive_ids": [],
                "volume_identifiers": [],
                "explicit_sleep_parent_ids": [],
                "idle_sleep_parent_ids": [],
            }

    @classmethod
    def clear_ejected_drives(cls) -> None:
        state_path = cls.get_state_file()
        if state_path.exists():
            try:
                state_path.unlink()
            except OSError:
                pass

    @classmethod
    def get_sleeping_parent_ids(cls) -> List[str]:
        """Return normalized list of parent drive identifiers currently recorded as sleeping."""
        state = cls.load_ejected_drives()
        return [d.strip().replace("/dev/", "") for d in state.get("drive_ids", []) if d.strip()]

    @classmethod
    def get_explicit_sleep_parent_ids(cls) -> List[str]:
        """Return parent drives put into explicit Deep Sleep (manual/UI)."""
        state = cls.load_ejected_drives()
        return [d.strip().replace("/dev/", "") for d in state.get("explicit_sleep_parent_ids", []) if d.strip()]

    @classmethod
    def get_idle_sleep_parent_ids(cls) -> List[str]:
        """Return parent drives put into sleep purely via idle timer (eligible for touch wake)."""
        state = cls.load_ejected_drives()
        return [d.strip().replace("/dev/", "") for d in state.get("idle_sleep_parent_ids", []) if d.strip()]

    @classmethod
    def get_auto_wakeable_parent_ids(cls) -> List[str]:
        """Return only parent drives that can be auto-awakened (idle sleep only, never explicit deep sleep)."""
        explicit = set(p.lower() for p in cls.get_explicit_sleep_parent_ids())
        return [p for p in cls.get_idle_sleep_parent_ids() if p.lower() not in explicit]

    @classmethod
    def is_parent_sleeping(cls, target: str) -> bool:
        """Check whether a drive or its parent is registered as sleeping."""
        clean = target.strip().replace("/dev/", "").lower()
        sleeping = [p.lower() for p in cls.get_sleeping_parent_ids()]
        return clean in sleeping

    @classmethod
    def is_explicit_deep_sleeping(cls, target: str) -> bool:
        """Check whether a target parent drive is in explicit Deep Sleep (immune to auto-wake)."""
        clean = target.strip().replace("/dev/", "").lower()
        explicit = [p.lower() for p in cls.get_explicit_sleep_parent_ids()]
        return clean in explicit

    @classmethod
    def remove_ejected_target(cls, target: str, parent_id: str = "") -> None:
        """Remove a remounted volume or drive (and its parent if specified) from ejected_state.json across all categories."""
        try:
            state = cls.load_ejected_drives()
            clean_t = target.strip().replace("/dev/", "").lower()
            clean_p = parent_id.strip().replace("/dev/", "").lower() if parent_id else ""

            drives = [
                d for d in state.get("drive_ids", [])
                if d.replace("/dev/", "").lower() not in (clean_t, clean_p)
            ]
            vols = [
                v for v in state.get("volume_identifiers", [])
                if v.replace("/dev/", "").lower() != clean_t
            ]
            explicit_parents = [
                p for p in state.get("explicit_sleep_parent_ids", [])
                if p.replace("/dev/", "").lower() not in (clean_t, clean_p)
            ]
            idle_parents = [
                p for p in state.get("idle_sleep_parent_ids", [])
                if p.replace("/dev/", "").lower() not in (clean_t, clean_p)
            ]

            if not drives and not vols:
                cls.clear_ejected_drives()
            else:
                state_path = cls.get_state_file()
                data = {
                    "drive_ids": drives,
                    "volume_identifiers": vols,
                    "explicit_sleep_parent_ids": explicit_parents,
                    "idle_sleep_parent_ids": idle_parents,
                }
                with open(state_path, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=4)
        except Exception:
            pass

    @classmethod
    def remove_disconnected_parents(cls, parents: List[str]) -> None:
        """Remove physically disconnected parent drives and their associated entries from ejected_state.json."""
        if not parents:
            return
        try:
            state = cls.load_ejected_drives()
            targets = set(p.strip().replace("/dev/", "").lower() for p in parents if p.strip())
            drives = [d for d in state.get("drive_ids", []) if d.replace("/dev/", "").lower() not in targets]
            explicit_parents = [p for p in state.get("explicit_sleep_parent_ids", []) if p.replace("/dev/", "").lower() not in targets]
            idle_parents = [p for p in state.get("idle_sleep_parent_ids", []) if p.replace("/dev/", "").lower() not in targets]
            vols = state.get("volume_identifiers", [])

            if not drives and not vols:
                cls.clear_ejected_drives()
            else:
                data = {
                    "drive_ids": drives,
                    "volume_identifiers": vols,
                    "explicit_sleep_parent_ids": explicit_parents,
                    "idle_sleep_parent_ids": idle_parents,
                }
                with open(cls.get_state_file(), "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=4)
        except Exception:
            pass
