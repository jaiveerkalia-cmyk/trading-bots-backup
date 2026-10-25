"""
backup_manager.py
─────────────────
Automated file backup and versioning system for trading system bots and scripts.
All backups are stored in a dedicated 'backups/' directory with versioning and metadata.

Usage CLI:
    python backup_manager.py backup 7--Live_Websockets_buy_sell_with_hedge_websockets.py "Initial clean state"
    python backup_manager.py list
    python backup_manager.py list 7--Live_Websockets_buy_sell_with_hedge_websockets.py
    python backup_manager.py restore 7--Live_Websockets_buy_sell_with_hedge_websockets.py 1
"""

import os
import sys
import json
import shutil
import hashlib
import re
from datetime import datetime

BACKUP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backups")
MANIFEST_PATH = os.path.join(BACKUP_DIR, "manifest.json")


def _ensure_backup_dir():
    os.makedirs(BACKUP_DIR, exist_ok=True)
    if not os.path.exists(MANIFEST_PATH):
        with open(MANIFEST_PATH, "w", encoding="utf-8") as f:
            json.dump([], f, indent=2)


def _load_manifest():
    _ensure_backup_dir()
    try:
        with open(MANIFEST_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def _save_manifest(manifest):
    _ensure_backup_dir()
    with open(MANIFEST_PATH, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)


def _compute_sha256(filepath):
    hasher = hashlib.sha256()
    with open(filepath, "rb") as f:
        while chunk := f.read(65536):
            hasher.update(chunk)
    return hasher.hexdigest()[:12]


def get_next_version(original_filename):
    """Finds next version number (v1, v2, ...) for a given filename."""
    manifest = _load_manifest()
    versions = [
        item["version"]
        for item in manifest
        if item.get("original_file") == original_filename
    ]
    if versions:
        return max(versions) + 1

    base_name, _ = os.path.splitext(original_filename)
    pattern = re.compile(rf"^{re.escape(base_name)}\.v(\d+)_")
    existing_nums = []
    if os.path.exists(BACKUP_DIR):
        for f in os.listdir(BACKUP_DIR):
            m = pattern.match(f)
            if m:
                existing_nums.append(int(m.group(1)))
    return max(existing_nums, default=0) + 1


def create_backup(filepath, note=""):
    """
    Creates a versioned backup of the specified file in the backups directory.
    Returns the path to the created backup file.
    """
    _ensure_backup_dir()
    if not os.path.exists(filepath):
        raise FileNotFoundError(f"Source file not found: {filepath}")

    filename = os.path.basename(filepath)
    base_name, ext = os.path.splitext(filename)
    ver = get_next_version(filename)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    backup_filename = f"{base_name}.v{ver}_{timestamp}.bak"
    backup_path = os.path.join(BACKUP_DIR, backup_filename)

    shutil.copy2(filepath, backup_path)
    file_size = os.path.getsize(backup_path)
    checksum = _compute_sha256(backup_path)

    manifest = _load_manifest()
    entry = {
        "original_file": filename,
        "backup_file": backup_filename,
        "version": ver,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "size_bytes": file_size,
        "sha256": checksum,
        "note": note
    }
    manifest.append(entry)
    _save_manifest(manifest)

    print(f"[BACKUP] Created: backups/{backup_filename} (Version: v{ver} | Size: {file_size:,} bytes | Note: '{note}')")
    return backup_path


def list_backups(target_filename=None):
    """Lists all tracked backups, optionally filtered by file."""
    manifest = _load_manifest()
    if not manifest:
        print("[BACKUP] No backups recorded yet in manifest.")
        return

    filtered = [
        item for item in manifest
        if target_filename is None or item["original_file"] == os.path.basename(target_filename)
    ]

    if not filtered:
        print(f"[BACKUP] No backups found for {target_filename}")
        return

    print("\n" + "=" * 95)
    print(f"{'Version':<10} | {'Original File':<35} | {'Timestamp':<19} | {'Size':<10} | {'Note'}")
    print("-" * 95)
    for entry in sorted(filtered, key=lambda x: (x['original_file'], x['version'])):
        print(f"v{entry['version']:<9} | {entry['original_file'][:35]:<35} | {entry['timestamp']:<19} | {entry['size_bytes']:<10} | {entry.get('note', '')}")
    print("=" * 95 + "\n")


def restore_backup(original_filepath, version):
    """
    Restores a specific backup version to the target file.
    Safely takes a backup of the current state before overwriting.
    """
    manifest = _load_manifest()
    filename = os.path.basename(original_filepath)
    matching = [
        item for item in manifest
        if item["original_file"] == filename and item["version"] == int(version)
    ]

    if not matching:
        raise ValueError(f"No backup version v{version} found for {filename}")

    target_backup_file = os.path.join(BACKUP_DIR, matching[0]["backup_file"])
    if not os.path.exists(target_backup_file):
        raise FileNotFoundError(f"Backup file missing on disk: {target_backup_file}")

    if os.path.exists(original_filepath):
        print("[RESTORE SAFETY] Creating backup of current file before restoring...")
        create_backup(original_filepath, note=f"Auto-backup before restoring v{version}")

    shutil.copy2(target_backup_file, original_filepath)
    print(f"[RESTORE SUCCESS] Restored {filename} from version v{version} ({matching[0]['backup_file']})")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(0)

    cmd = sys.argv[1].lower()
    if cmd == "backup":
        if len(sys.argv) < 3:
            print("Usage: python backup_manager.py backup <filepath> [note]")
            sys.exit(1)
        fpath = sys.argv[2]
        note_str = sys.argv[3] if len(sys.argv) > 3 else "Manual backup"
        create_backup(fpath, note=note_str)

    elif cmd == "list":
        filt = sys.argv[2] if len(sys.argv) > 2 else None
        list_backups(filt)

    elif cmd == "restore":
        if len(sys.argv) < 4:
            print("Usage: python backup_manager.py restore <filepath> <version_number>")
            sys.exit(1)
        fpath = sys.argv[2]
        ver_num = int(sys.argv[3])
        restore_backup(fpath, ver_num)

    else:
        print(f"Unknown command: {cmd}")
        print(__doc__)
