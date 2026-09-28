#!/usr/bin/env python3
"""
Safe Drive Ejector Tool - Native macOS External Disk Safe Ejector & Auto-Remounter
"""

import json
import os
import sys
from pathlib import Path

# Ensure project root is in sys.path
BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from core.config import StateManager
from core.engine import SafeEjectEngine


def handle_json_status():
    """Output machine-readable JSON status of connected drives and configuration."""
    engine = SafeEjectEngine()
    ext_drives = engine.get_external_drives()
    managed_drives = [d for d in ext_drives if d.is_managed]

    # Calculate actual real-time ejected/unmounted count
    ejected_count = 0
    currently_mounted_ids = set()
    for d in ext_drives:
        has_real_volumes = False
        for v in d.volumes:
            if v.name == "EFI" or v.fs_type == "Apple_APFS":
                continue
            has_real_volumes = True
            if v.is_mounted:
                currently_mounted_ids.add(v.device_id)
            else:
                ejected_count += 1
        if not has_real_volumes and d.name:
            if getattr(d, "is_mounted", True):
                currently_mounted_ids.add(d.id)
            else:
                ejected_count += 1

    state = StateManager.load_ejected_drives()
    # Clean up state entries ONLY for drives/volumes that are actually currently mounted
    active_ejected_vols = [vid for vid in state.get("volume_identifiers", []) if vid not in currently_mounted_ids]
    active_ejected_drives = [did for did in state.get("drive_ids", []) if did not in currently_mounted_ids]
    if (len(active_ejected_vols) != len(state.get("volume_identifiers", [])) or
        len(active_ejected_drives) != len(state.get("drive_ids", []))):
        if not active_ejected_vols and not active_ejected_drives:
            StateManager.clear_ejected_drives()
            state = {
                "drive_ids": [],
                "volume_identifiers": [],
                "explicit_sleep_parent_ids": [],
                "idle_sleep_parent_ids": [],
            }
        else:
            state["volume_identifiers"] = active_ejected_vols
            state["drive_ids"] = active_ejected_drives
            state["explicit_sleep_parent_ids"] = [p for p in state.get("explicit_sleep_parent_ids", []) if p in active_ejected_drives]
            state["idle_sleep_parent_ids"] = [p for p in state.get("idle_sleep_parent_ids", []) if p in active_ejected_drives]
            try:
                with open(StateManager.get_state_file(), "w", encoding="utf-8") as f:
                    json.dump(state, f, indent=4)
            except Exception:
                pass

    bottom_status = engine.idle_monitor.get_status_summary(managed_drives)
    data = {
        "platform": sys.platform,
        "config": engine.config.to_dict(),
        "sleep_timer_label": engine.config.sleep_timer_label,
        "bottom_status": bottom_status,
        "ejected_state": state,
        "ejected_count": ejected_count,
        "external_drives": [d.to_dict() for d in ext_drives],
        "managed_drives": [d.to_dict() for d in managed_drives],
    }
    print(json.dumps(data, indent=2))


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "json-status":
        handle_json_status()
    else:
        # Default to CLI
        from ui.cli import main as cli_main
        cli_main()


if __name__ == "__main__":
    main()
