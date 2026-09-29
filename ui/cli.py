"""
Command Line Interface (CLI) for SafeEject.
Supports rich terminal formatting, command actions, and configuration management.
"""

import argparse
import json
import logging
import os
import sys
from typing import List

# Ensure project root is in sys.path when invoked directly as a script
_project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from core.config import StateManager
from core.engine import SafeEjectEngine


def _status_icon(success: bool, stream=None) -> str:
    """Return '✓' / '✗' if encodable on the output stream, else ASCII fallback '[OK]' / '[FAIL]'."""
    target_stream = stream or sys.stdout
    encoding = getattr(target_stream, "encoding", None) or "utf-8"
    icon = "✓" if success else "✗"
    try:
        icon.encode(encoding)
        return icon
    except (UnicodeEncodeError, LookupError):
        return "[OK]" if success else "[FAIL]"


def setup_logger(level_name: str = "INFO"):
    level = getattr(logging, level_name.upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )


def print_table(headers: List[str], rows: List[List[str]]):
    """Simple clean ASCII table printer without external dependencies."""
    col_widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            if i < len(col_widths):
                col_widths[i] = max(col_widths[i], len(str(cell)))

    header_line = " | ".join(h.ljust(col_widths[i]) for i, h in enumerate(headers))
    separator = "-+-".join("-" * col_widths[i] for i in range(len(headers)))

    print(header_line)
    print(separator)
    for row in rows:
        line = " | ".join(str(cell).ljust(col_widths[i]) for i, cell in enumerate(row))
        print(line)


def cmd_list(engine: SafeEjectEngine, args):
    """List connected drives."""
    all_drives = engine.adapter.get_drives()
    if args.all:
        drives = all_drives
    else:
        drives = [d for d in all_drives if d.is_external and not d.is_virtual]

    if not drives:
        print("\nNo external physical drives detected.")
        if not args.all:
            print("Tip: Use 'safe-eject list --all' to inspect internal and virtual disks.\n")
        return

    print(f"\nConnected Disks ({'All Disks' if args.all else 'External Physical Only'}):")
    print("=" * 80)

    rows = []
    for d in drives:
        status = "EXTERNAL" if d.is_external else "INTERNAL"
        if d.is_virtual:
            status += " (Virtual)"

        mounted_vols = [v for v in d.volumes if v.is_mounted]
        vol_desc = ", ".join(f"'{v.name}' ({v.mount_point})" for v in mounted_vols) if mounted_vols else "(None mounted)"

        rows.append([
            d.id,
            d.name,
            d.human_size,
            d.bus_protocol,
            status,
            vol_desc,
        ])

    print_table(
        ["Disk ID", "Model/Name", "Size", "Bus", "Type", "Mounted Volumes"],
        rows,
    )
    print()

    # If verbose, check for blocking processes on mounted volumes
    if args.check_locks:
        print("Checking for active file locks on mounted external volumes...")
        found_any = False
        for d in drives:
            for v in d.volumes:
                if v.mount_point:
                    locks = engine.adapter.get_blocking_processes(v.mount_point)
                    if locks:
                        found_any = True
                        print(f" -> Volume '{v.name}' ({v.mount_point}) is held open by {len(locks)} process(es):")
                        for lock in locks:
                            print(f"      PID {lock.pid} ({lock.process_name}): {lock.file_path}")
        if not found_any:
            print(" -> All mounted volumes are clean (no active file locks detected).\n")
        else:
            print()


def cmd_check_locks(engine: SafeEjectEngine, args):
    """Scan mounted external volumes for active process locks and open file handles."""
    ext_drives = engine.get_external_drives()
    if not ext_drives:
        print("No external drives are currently connected.")
        return

    found_locks = {}
    total_mounted_vols = 0
    for d in ext_drives:
        for v in d.volumes:
            if v.mount_point and os.path.exists(v.mount_point):
                total_mounted_vols += 1
                locks = engine.adapter.get_blocking_processes(v.mount_point)
                if locks:
                    vol_key = v.name or v.device_id
                    found_locks[vol_key] = locks

    if total_mounted_vols == 0:
        print("All external volumes are currently unmounted or asleep. No active locks.")
        return

    if not found_locks:
        print("All External Drives are Clean!\n\nNo apps or background processes are holding active file locks on your drives.")
    else:
        print("Active File Locks Detected:\n")
        for vol_name, locks in found_locks.items():
            print(f"• Volume '{vol_name}':")
            proc_map = {}
            for l in locks:
                proc_map.setdefault((l.pid, l.process_name), []).append(l.file_path)
            for (pid, pname), paths in proc_map.items():
                short_paths = paths[:3]
                path_str = ", ".join(os.path.basename(p) or p for p in short_paths)
                if len(paths) > 3:
                    path_str += f" (+{len(paths) - 3} more)"
                print(f"  - {pname} (PID {pid}): {path_str}")
        print("\nTip: Close these applications or processes before ejecting.")


def cmd_eject_all(engine: SafeEjectEngine, args):
    """Eject all external physical drives."""
    ext_drives = engine.get_external_drives()
    if not ext_drives:
        print("\nNo external physical drives are currently connected.\n")
        return

    print(f"\nFound {len(ext_drives)} external drive(s) to eject:")
    for d in ext_drives:
        vols = [v.name for v in d.volumes if v.is_mounted]
        vol_str = f" [Volumes: {', '.join(vols)}]" if vols else ""
        print(f"  • {d.id} - {d.name} ({d.human_size}){vol_str}")

    if args.dry_run:
        print("\n[Dry Run] No drives were ejected.\n")
        return

    print("\nSafely unmounting and ejecting...")
    results = engine.eject_all_external(manual=True)

    success_all = True
    for res in results:
        icon = _status_icon(res.success)
        print(f" {icon} {res.target}: {res.message}")
        if not res.success:
            success_all = False
            if res.blocking_processes:
                print(f"    Blocking processes ({len(res.blocking_processes)}):")
                for p in res.blocking_processes:
                    print(f"      - {p}")

    print()
    if success_all:
        print("All external disks ejected safely! You may now safely unplug cables.\n")
    else:
        print("Some drives could not be ejected. Check open files or apps locking the drive.\n")


def cmd_eject_single(engine: SafeEjectEngine, args):
    """Eject specific drive(s) or volume(s) with Parent Precedence."""
    raw_targets = getattr(args, "targets", None) or [getattr(args, "target", "")]
    targets = [t for t in (raw_targets if isinstance(raw_targets, list) else [raw_targets]) if t]
    if not targets:
        return
    results = engine.eject_targets(targets)
    all_success = True
    failed_messages = []
    for res in results:
        icon = _status_icon(res.success)
        print(f" {icon} {res.message}")
        if not res.success:
            all_success = False
            failed_messages.append(res.message)
        if res.blocking_processes:
            print(f"Blocking processes ({len(res.blocking_processes)}):")
            for p in res.blocking_processes:
                print(f"  - {p}")
            print()
    if not all_success:
        import sys
        print("\n".join(failed_messages), file=sys.stderr)
        sys.exit(1)


def cmd_deep_sleep(engine: SafeEjectEngine, args):
    """Explicit Deep Sleep (Hardware Silence) for physical parent target(s)."""
    raw_targets = getattr(args, "targets", None) or [getattr(args, "target", "")]
    targets = [t for t in (raw_targets if isinstance(raw_targets, list) else [raw_targets]) if t]
    if not targets:
        print("\nTriggering Deep Sleep for selected drives...")
        results = engine._on_idle_sleep([])
        all_success = all(r.success for r in results) if results else True
        if not all_success:
            import sys
            failed_msgs = [r.message for r in results if not r.success]
            print("\n".join(failed_msgs), file=sys.stderr)
            sys.exit(1)
        return
    all_success = True
    failed_messages = []
    for t in targets:
        print(f"\nPutting '{t}' into Deep Sleep (Hardware Silence)...")
        res = engine.deep_sleep_target(t)
        icon = _status_icon(res.success)
        print(f" {icon} {res.message}\n")
        if not res.success:
            all_success = False
            failed_messages.append(res.message)
        if res.blocking_processes:
            print(f"Blocking processes ({len(res.blocking_processes)}):")
            for p in res.blocking_processes:
                print(f"  - {p}")
            print()
    if not all_success:
        import sys
        print("\n".join(failed_messages), file=sys.stderr)
        sys.exit(1)


def cmd_remount_all(engine: SafeEjectEngine, args):
    """Remount previously ejected drives."""
    only_recorded = getattr(args, "only_recorded", False)
    state = StateManager.load_ejected_drives()
    drive_ids = state.get("drive_ids", [])
    volume_ids = state.get("volume_identifiers", [])

    if only_recorded and not drive_ids and not volume_ids:
        print("\nNo recorded sleeping drives/volumes found in state. Skipping remount.\n")
        return

    item_count = len(drive_ids) + len(volume_ids)
    if item_count > 0:
        print(f"\nRemounting {item_count} previously sleeping drive/volume item(s)...")
    else:
        print("\nScanning and mounting all connected external unmounted drives/volumes...")
    results = engine.remount_all_ejected(only_if_recorded=only_recorded)
    for res in results:
        icon = _status_icon(res.success)
        print(f" {icon} {res.target}: {res.message}")
    print()


def cmd_remount_single(engine: SafeEjectEngine, args):
    """Remount single or multiple drives/volumes."""
    raw_targets = getattr(args, "targets", None) or [getattr(args, "target", "")]
    targets = raw_targets if isinstance(raw_targets, list) else [raw_targets]
    for target in targets:
        if not target:
            continue
        print(f"\nRemounting '{target}'...")
        res = engine.remount_single(target)
        icon = _status_icon(res.success)
        print(f" {icon} {res.message}\n")


def cmd_daemon(engine: SafeEjectEngine, args):
    """Run the sleep/wake watcher daemon in foreground."""
    print("\n" + "=" * 60)
    print(" SafeEject Power Monitoring Daemon Running")
    print("=" * 60)
    print(f" - Eject on sleep:       {engine.config.eject_on_sleep}")
    print(f" - Remount on wake:      {engine.config.remount_on_wake}")
    print(f" - Show notifications:   {engine.config.show_notifications}")
    print(f" - Excluded volumes:     {engine.config.excluded_volumes or 'None'}")
    print(" Press Ctrl+C to stop.\n")

    try:
        engine.run_daemon()
    except KeyboardInterrupt:
        print("\nSafeEject daemon stopped by user.")


def cmd_eject_now(engine: SafeEjectEngine, args):
    """Eject Now Logic: immediately safely unmounts and ejects all managed drives or specific selected targets."""
    targets = getattr(args, "targets", None)
    if targets:
        print(f"\n[Eject Now] Ejecting/unmounting {len(targets)} selected target(s): {', '.join(targets)}")
        results = engine.eject_now(targets=targets)
        success_count = sum(1 for r in results if r.success)
        for res in results:
            icon = _status_icon(res.success)
            print(f" {icon} {res.target}: {res.message}")
        print(f"\nCompleted: {success_count}/{len(results)} targets safely processed.\n")
        return

    managed = engine.get_managed_drives()
    if not managed:
        print("\nNo managed external drives are currently connected.\n")
        return

    print(f"\n[Eject Now] Ejecting {len(managed)} managed drive(s):")
    for d in managed:
        vols = [v.name for v in d.volumes if v.is_mounted]
        vol_str = f" [Volumes: {', '.join(vols)}]" if vols else ""
        print(f"  • {d.id} - {d.name} ({d.human_size}){vol_str}")

    results = engine.eject_now()
    print("\nResults:")
    for res in results:
        icon = _status_icon(res.success)
        print(f" {icon} {res.target}: {res.message}")
    print()


def cmd_timer(engine: SafeEjectEngine, args):
    """Configure sleep timer preset."""
    if not args.preset:
        print(f"\nCurrent Sleep Timer: {engine.config.sleep_timer_label} ({engine.config.sleep_timer_seconds}s)")
        print("Available presets: 2m, 5m, 10m, 15m, 30m, 1h, 2h, never\n")
        return

    ok = engine.set_timer_preset(args.preset)
    if ok:
        print(f"Sleep Timer updated to: {engine.config.sleep_timer_label}\n")
    else:
        print(f"Invalid preset '{args.preset}'. Choose from: 2m, 5m, 10m, 15m, 30m, 1h, 2h, never\n")


def cmd_manage(engine: SafeEjectEngine, args):
    """Toggle persistent management of an external drive (up to 6 max)."""
    if not args.target:
        managed = engine.get_managed_drives()
        print(f"\nManaged Drives ({len(managed)}/6):")
        for d in managed:
            print(f"  • [{d.id}] {d.name} (UUID: {d.primary_uuid})")
        print("\nTo toggle a drive: ./main.py manage <disk_id_or_uuid>\n")
        return

    is_managed, msg = engine.toggle_manage_drive(args.target)
    print(f"\n{msg}\n")


def cmd_wake_mode(engine: SafeEjectEngine, args):
    """Set or inspect Wake Mode (touch: auto-active on Mac touch, manual: stay asleep until manual mount)."""
    if not args.mode:
        mode_desc = "Auto-Active on Mac Touch" if engine.config.wake_mode == "touch" else "Manual (Stay Asleep until Manual Mount)"
        print(f"\nCurrent Wake Mode: {mode_desc} ('{engine.config.wake_mode}')")
        print("Options: ./main.py wake-mode touch  OR  ./main.py wake-mode manual\n")
        return

    ok = engine.set_wake_mode(args.mode)
    if ok:
        mode_desc = "Auto-Active on Mac Touch" if engine.config.wake_mode == "touch" else "Stay Asleep until Manual Mount"
        print(f"\nWake Mode updated to: {mode_desc}\n")
    else:
        print(f"Invalid mode '{args.mode}'. Choose from: touch, manual\n")


def cmd_select_sleep(engine: SafeEjectEngine, args):
    """Choose which individual SSD/partition goes to sleep when Mac is idle."""
    if getattr(args, "set_targets", None) is not None:
        targets = args.set_targets
        is_sel, msg = engine.set_all_sleep_selections(targets)
        print(f"\n{msg}\n")
        return

    target = getattr(args, "target", None)
    extra = getattr(args, "extra", []) or []

    if not target:
        selected = engine.get_sleep_selected_drives()
        print(f"\nSSDs Chosen for Auto-Sleep ({len(selected)}):")
        for d in selected:
            print(f"  • [{d.id}] {d.name} (UUID: {d.primary_uuid})")
        print("\nTo toggle an SSD: ./main.py select-sleep <disk_id_or_uuid>\n")
        return

    if extra and extra[0].lower() in ("true", "false", "1", "0", "yes", "no"):
        is_enabled = extra[0].lower() in ("true", "1", "yes")
        is_sel, msg = engine.set_sleep_selection(target, is_enabled)
        print(f"\n{msg}\n")
        return

    is_sel, msg = engine.toggle_sleep_selection(target)
    print(f"\n{msg}\n")


def cmd_eject_and_sleep(engine: SafeEjectEngine, args):
    """Eject external drives and sleep system immediately."""
    print("\nEjecting external drives and putting system to sleep...")
    engine.eject_and_sleep()


def cmd_sleep_system(engine: SafeEjectEngine, args):
    """Put system to sleep."""
    print("\nPutting system to sleep...")
    engine.sleep_system()


def cmd_idle_sleep(engine: SafeEjectEngine, args):
    """Trigger idle sleep on selected SSDs or all external drives."""
    print("\nTriggering idle sleep for drives...")
    results = engine._on_idle_sleep([])
    if results:
        for r in results:
            icon = _status_icon(r.success)
            print(f" {icon} {r.target}: {r.message}")
        all_success = all(r.success for r in results)
        if not all_success:
            import sys
            failed_msgs = [r.message for r in results if not r.success]
            print("\n".join(failed_msgs), file=sys.stderr)
            sys.exit(1)


def cmd_open(engine: SafeEjectEngine, args):
    """Open a mounted volume in Finder or Explorer."""
    ok = engine.open_volume(args.target)
    if ok:
        print(f"Opened '{args.target}' in file manager.")
    else:
        print(f"Could not open '{args.target}'. Is it mounted?")


def cmd_mount_and_open(engine: SafeEjectEngine, args):
    """Mount volume and open in Finder or Explorer."""
    print(f"Mounting and opening '{args.target}'...")
    ok = engine.mount_and_open(args.target)
    if ok:
        print(f"Mounted and opened '{args.target}'.")
    else:
        print(f"Failed to mount or open '{args.target}'.")


def cmd_mount_and_open_all(engine: SafeEjectEngine, args):
    """Mount all external drives and open in Finder or Explorer."""
    print("Mounting and opening all external drives...")
    opened = engine.mount_and_open_all()
    print(f"Mounted and opened {opened} volume(s).")


def cmd_update_config_json(engine: SafeEjectEngine, args):
    """Update configuration from JSON string."""
    try:
        data = json.loads(args.json_data)
        engine.config.update_settings(data)
        print("Config updated successfully.")
    except Exception as e:
        print(f"Failed to update config: {e}")


def cmd_config(engine: SafeEjectEngine, args):
    """Manage configuration settings."""
    if args.action == "show":
        print("\nCurrent Configuration:")
        print(json.dumps(engine.config.to_dict(), indent=4))
        print()
    elif args.action == "set":
        if not args.key or args.value is None:
            print("Error: Specify both key and value to set (e.g. safe-eject config set eject_on_sleep true)")
            return
        key = args.key
        val = args.value
        if val.lower() in ("true", "1", "yes"):
            val_parsed = True
        elif val.lower() in ("false", "0", "no"):
            val_parsed = False
        elif val.isdigit():
            val_parsed = int(val)
        else:
            val_parsed = val

        if hasattr(engine.config, key):
            setattr(engine.config, key, val_parsed)
            engine.config.save()
            print(f"Config updated: {key} = {val_parsed}")
        else:
            print(f"Unknown config key '{key}'. Available keys: {list(engine.config.__dict__.keys())}")
    elif args.action == "exclude-add":
        if not args.key:
            print("Error: Specify volume name to exclude (e.g. safe-eject config exclude-add MyDrive)")
            return
        if args.key not in engine.config.excluded_volumes:
            engine.config.excluded_volumes.append(args.key)
            engine.config.save()
            print(f"Volume '{args.key}' added to exclusion list.")
        else:
            print(f"Volume '{args.key}' is already in exclusion list.")
    elif args.action == "exclude-remove":
        if args.key in engine.config.excluded_volumes:
            engine.config.excluded_volumes.remove(args.key)
            engine.config.save()
            print(f"Volume '{args.key}' removed from exclusion list.")
        else:
            print(f"Volume '{args.key}' not found in exclusion list.")


def cmd_test_notify(engine: SafeEjectEngine, args):
    """Send a test desktop notification."""
    print("Sending test desktop notification...")
    engine.adapter.show_notification("SafeEject", "This is a test notification from SafeEject!")
    print("Notification sent successfully.\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="safe-eject",
        description="SafeEject: Native macOS External Disk Safe Ejector & Auto-Remounter",
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable verbose debug logging")

    subparsers = parser.add_subparsers(dest="command", help="Available subcommands")

    # list
    p_list = subparsers.add_parser("list", aliases=["ls"], help="List connected drives and volumes")
    p_list.add_argument("--all", "-a", action="store_true", help="Show all drives (including internal & virtual)")
    p_list.add_argument("--check-locks", "-l", action="store_true", help="Scan for active file locks on mounted volumes")

    # check-locks
    subparsers.add_parser("check-locks", help="Scan mounted external volumes for active process file locks")

    # eject-now
    p_eject_now = subparsers.add_parser("eject-now", help="Immediately safely eject all managed external drives or specific targets")
    p_eject_now.add_argument("targets", nargs="*", help="Optional specific drive or volume identifiers to eject")

    # eject-all
    p_eject_all = subparsers.add_parser("eject-all", help="Safely eject all external physical drives")
    p_eject_all.add_argument("--dry-run", action="store_true", help="Simulate without actually unmounting")

    # eject <targets>
    p_eject = subparsers.add_parser("eject", help="Eject specific disk(s) or volume(s)")
    p_eject.add_argument("targets", nargs="+", help="Disk identifier(s) (e.g. disk8s1) or volume name/mountpoint")

    # deep-sleep <targets>
    p_deep_sleep = subparsers.add_parser("deep-sleep", help="Put physical parent drive(s) into Deep Sleep (Hardware Silence)")
    p_deep_sleep.add_argument("targets", nargs="*", help="Parent disk identifier(s) (e.g. disk7)")

    # remount-all
    p_remount_all = subparsers.add_parser("remount-all", help="Remount previously ejected drives")
    p_remount_all.add_argument("--only-recorded", action="store_true", help="Only remount drives recorded in sleep state")

    # remount <targets>
    p_remount = subparsers.add_parser("remount", help="Remount specific drive(s) or volume(s)")
    p_remount.add_argument("targets", nargs="+", help="Disk identifier(s) or volume name")

    # timer <preset>
    p_timer = subparsers.add_parser("timer", help="Set idle sleep timer preset (2m, 5m, 10m, 15m, 30m, 1h, 2h, never)")
    p_timer.add_argument("preset", nargs="?", help="Preset value: 2m, 5m, 10m, 15m, 30m, 1h, 2h, never")

    # manage <target>
    p_manage = subparsers.add_parser("manage", help="Toggle persistent drive management (up to 6 max)")
    p_manage.add_argument("target", nargs="?", help="Disk identifier or UUID to toggle")

    # wake-mode <touch|manual>
    p_wake_mode = subparsers.add_parser("wake-mode", help="Set Wake Mode: touch (auto active on Mac touch) or manual (stay asleep)")
    p_wake_mode.add_argument("mode", nargs="?", choices=["touch", "manual"], help="touch or manual")

    # select-sleep <target>
    p_select_sleep = subparsers.add_parser("select-sleep", help="Individually choose which SSD sleeps on idle")
    p_select_sleep.add_argument("--set", dest="set_targets", nargs="*", help="Set exact list of selected sleep targets")
    p_select_sleep.add_argument("target", nargs="?", help="Disk identifier or UUID to toggle or set")
    p_select_sleep.add_argument("extra", nargs="*", help="Additional targets or boolean state")

    # daemon
    subparsers.add_parser("daemon", help="Run the sleep/wake listener daemon in foreground")

    # status
    subparsers.add_parser("status", help="Show current daemon configuration and drive status")

    # json-status
    subparsers.add_parser("json-status", help="Output live status in JSON format")

    # test-notify
    subparsers.add_parser("test-notify", help="Send a test desktop notification")

    # eject-and-sleep
    subparsers.add_parser("eject-and-sleep", help="Eject external drives and sleep system immediately")

    # sleep-system
    subparsers.add_parser("sleep-system", help="Put the system to sleep")

    # idle-sleep
    subparsers.add_parser("idle-sleep", help="Trigger idle sleep on selected/all drives")

    # open <target>
    p_open = subparsers.add_parser("open", help="Open a mounted volume in Finder")
    p_open.add_argument("target", help="Volume name or mount point")

    # mount-and-open <target>
    p_m_open = subparsers.add_parser("mount-and-open", help="Mount volume and open in Finder")
    p_m_open.add_argument("target", help="Volume name or device identifier")

    # mount-and-open-all
    subparsers.add_parser("mount-and-open-all", help="Mount all external drives and open them in Finder")

    # update-config-json <json_data>
    p_update_json = subparsers.add_parser("update-config-json", help="Update config with JSON string")
    p_update_json.add_argument("json_data", help="JSON string with key-value settings")

    # config
    p_config = subparsers.add_parser("config", help="View or modify SafeEject settings")
    p_config.add_argument("action", choices=["show", "set", "exclude-add", "exclude-remove"], help="Config action")
    p_config.add_argument("key", nargs="?", help="Setting key or volume name")
    p_config.add_argument("value", nargs="?", help="Setting value")

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(0)

    log_level = "DEBUG" if args.verbose else "INFO"
    setup_logger(log_level)

    engine = SafeEjectEngine()

    if args.command in ("list", "ls"):
        cmd_list(engine, args)
    elif args.command == "check-locks":
        cmd_check_locks(engine, args)
    elif args.command == "eject-now":
        cmd_eject_now(engine, args)
    elif args.command == "eject-all":
        cmd_eject_all(engine, args)
    elif args.command == "eject":
        cmd_eject_single(engine, args)
    elif args.command == "deep-sleep":
        cmd_deep_sleep(engine, args)
    elif args.command == "remount-all":
        cmd_remount_all(engine, args)
    elif args.command == "remount":
        cmd_remount_single(engine, args)
    elif args.command == "timer":
        cmd_timer(engine, args)
    elif args.command == "manage":
        cmd_manage(engine, args)
    elif args.command == "wake-mode":
        cmd_wake_mode(engine, args)
    elif args.command == "select-sleep":
        cmd_select_sleep(engine, args)
    elif args.command == "eject-and-sleep":
        cmd_eject_and_sleep(engine, args)
    elif args.command == "sleep-system":
        cmd_sleep_system(engine, args)
    elif args.command == "idle-sleep":
        cmd_idle_sleep(engine, args)
    elif args.command == "open":
        cmd_open(engine, args)
    elif args.command == "mount-and-open":
        cmd_mount_and_open(engine, args)
    elif args.command == "mount-and-open-all":
        cmd_mount_and_open_all(engine, args)
    elif args.command == "update-config-json":
        cmd_update_config_json(engine, args)
    elif args.command == "daemon":
        cmd_daemon(engine, args)
    elif args.command == "status":
        print(f"\n{engine.get_bottom_status()}\n")
    elif args.command == "json-status":
        import main as root_main
        root_main.handle_json_status()
    elif args.command == "config":
        cmd_config(engine, args)
    elif args.command == "test-notify":
        cmd_test_notify(engine, args)


if __name__ == "__main__":
    main()
