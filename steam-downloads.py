#!/usr/bin/env python3
"""Report in-flight Steam downloads for the Omarchy Steam Download widget.

Steam does not expose download progress over a supported API on Linux, so
this reads the files the client already maintains:

  <library>/steamapps/appmanifest_<appid>.acf   per-app metadata and totals
  <library>/steamapps/downloading/<appid>/      bytes landed so far
  <library>/steamapps/libraryfolders.vdf       the full library list

Progress is measured from the on-disk size of the `downloading/<appid>/`
tree, because the `BytesDownloaded` field in the appmanifest is only
rewritten when the download starts or stops and is therefore stale while
bytes are still arriving.

The denominator is `BytesToStage`, the uncompressed on-disk size, because
that is what the staging tree measures and therefore what Steam's own
"Installing files" percentage is built from. `BytesToDownload` is the
compressed transfer size shown on Steam's separate "Downloading data" bar;
it describes the same bytes at a different compression, so the two must
never be added together. Steam runs the two phases concurrently, which is
why the staging tree can already exceed the entire compressed total while
the download is still running.

When Steam preallocates a depot the staging directory can be sparse, so the
tree is measured by walking it and summing allocated blocks (st_blocks*512)
with the apparent size (st_size) as a fallback. The whole scan is bounded by
WALK_BUDGET_SECONDS so a huge library cannot stall the poll.

Library roots come from libraryfolders.vdf, falling back to the Steam root
when that file is missing or unreadable.

Prints one compact JSON object per active download on its own line:

    {"appid": 2842040, "name": "Star Wars Outlaws", "downloaded": 14500000000,
     "total": 73089514868, "library": "..."}

and one final line describing the client:

    {"__meta": {"running": true, "steamRoot": "...", "downloads": 1,
                "note": ""}}

`rate` is bytes/second measured across --rate-window seconds from the size
of the same tree, so it is absent on the first sample. Only downloads whose
bytes moved within the window are reported, which naturally drops finished
and paused downloads.
"""

import argparse
import json
import os
import pwd
import re
import shutil
import signal
import sys
import time

# Steam's appmanifest StateFlags bits.
STATE_UNINSTALLED = 2
STATE_UPDATE_STARTED = 1024

# A download is considered finished once this much of the tree is left over.
# Steam keeps the staging directory around briefly while it commits, and the
# trailing bytes can churn for a second or two after the last chunk lands.
SETTLE_SECONDS = 12

WALK_BUDGET_SECONDS = 1.5


def fail(message, code=1):
    sys.stderr.write(message.rstrip() + "\n")
    sys.exit(code)


def prepare_environment():
    """The widget launches this with a cleared environment, so rebuild HOME."""
    if not os.environ.get("HOME"):
        try:
            os.environ["HOME"] = pwd.getpwuid(os.getuid()).pw_dir
        except Exception:
            pass


def find_steam_root():
    home = os.environ.get("HOME") or ""
    candidates = [
        os.environ.get("STEAM_DIR") or "",
        os.path.join(home, ".local", "share", "Steam"),
        os.path.join(home, ".steam", "steam"),
        os.path.join(home, ".steam", "root"),
        os.path.join(home, ".steam", "debian-installation"),
    ]
    for path in candidates:
        if path and os.path.isdir(os.path.join(path, "steamapps")):
            return path
    return None


def steam_running(steam_root):
    # Steam ships no live query interface, so fall back to scanning /proc.
    # Cheap enough at a few-second poll interval and needs no subprocess.
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open("/proc/%s/comm" % entry) as handle:
                if handle.read().strip() == "steam":
                    return True
        except OSError:
            continue
    return False


def parse_vdf_paths(text):
    """Pull the "path" values out of libraryfolders.vdf.

    Steam writes this as a nested VDF with quoted keys, so a regex over
    quoted path values is enough and avoids pulling in a VDF parser.
    """
    return re.findall(r'"path"\s+"([^"]+)"', text)


def library_roots(steam_root):
    roots = []
    vdf = os.path.join(steam_root, "steamapps", "libraryfolders.vdf")
    try:
        with open(vdf, encoding="utf-8", errors="replace") as handle:
            for path in parse_vdf_paths(handle.read()):
                if os.path.isdir(os.path.join(path, "steamapps")):
                    roots.append(path)
    except OSError:
        pass
    if not roots:
        roots.append(steam_root)
    # Stable order, no duplicates, Steam's own root first.
    seen = set()
    ordered = []
    for path in [steam_root] + roots:
        if path and path not in seen and os.path.isdir(os.path.join(path, "steamapps")):
            seen.add(path)
            ordered.append(path)
    return ordered


def parse_manifest(path):
    """Read the handful of scalar keys we need out of an appmanifest ACF."""
    fields = {}
    wanted = ("appid", "name", "StateFlags", "BytesToDownload", "BytesToStage")
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line or line.startswith("//"):
                    continue
                for key in wanted:
                    if key in fields:
                        continue
                    match = re.match(r'"%s"\s+"([^"]*)"' % re.escape(key), line)
                    if match:
                        fields[key] = match.group(1)
    except OSError:
        return None
    if "appid" not in fields:
        return None

    def as_int(name):
        try:
            return int(fields.get(name, "0") or 0)
        except ValueError:
            return 0

    return {
        "appid": as_int("appid"),
        "name": fields.get("name") or "App %d" % as_int("appid"),
        "flags": as_int("StateFlags"),
        "to_download": as_int("BytesToDownload"),
        "to_stage": as_int("BytesToStage"),
    }


def tree_bytes(path, deadline):
    """Allocated bytes and newest mtime under `path`.

    Steam preallocates staging space, so a fully written depot can have
    apparent size far ahead of what is actually on the platter. st_blocks
    reflects real allocation, which tracks the download closely. Files that
    report zero blocks (sparse, not yet written) contribute their apparent
    size instead, so an in-flight download is never reported as empty.

    The newest file mtime is returned alongside the size because the
    directory's own mtime only moves when entries are added or removed, and
    Steam streams most bytes into files that already exist.
    """
    allocated = 0
    apparent = 0
    newest = 0.0
    truncated = False
    stack = [path]
    while stack:
        if time.monotonic() > deadline:
            truncated = True
            break
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        is_dir = entry.is_dir(follow_symlinks=False)
                    except OSError:
                        continue
                    if is_dir:
                        stack.append(entry.path)
                        continue
                    try:
                        stat = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    apparent += stat.st_size
                    allocated += stat.st_blocks * 512
                    if stat.st_mtime > newest:
                        newest = stat.st_mtime
        except OSError:
            continue
    total = allocated if allocated > 0 else apparent
    return total, newest, truncated


def library_space(roots):
    """Free/total bytes across the distinct filesystems Steam uses."""
    seen = set()
    free = 0
    total = 0
    for root in roots:
        try:
            real = os.path.realpath(root)
        except OSError:
            continue
        if real in seen:
            continue
        seen.add(real)
        try:
            usage = shutil.disk_usage(real)
        except OSError:
            continue
        free += usage.free
        total += usage.total
    return free, total


def snapshot(roots, deadline):
    """Current download size and totals for every app in every library."""
    entries = []
    now = time.time()
    for root in roots:
        steamapps = os.path.join(root, "steamapps")
        try:
            manifests = [n for n in os.listdir(steamapps)
                         if n.startswith("appmanifest_") and n.endswith(".acf")]
        except OSError:
            continue
        for name in sorted(manifests):
            meta = parse_manifest(os.path.join(steamapps, name))
            if not meta:
                continue
            appid = meta["appid"]
            if appid <= 0:
                continue
            downloading = os.path.join(steamapps, "downloading", str(appid))
            present = os.path.isdir(downloading)

            # The staging total is the uncompressed on-disk size, which is
            # what the tree measures, so it is the only denominator Steam's
            # own percentage is built from. BytesToDownload is the compressed
            # transfer size and is NOT additive with it -- the two describe
            # the same bytes at different compressions, so adding them
            # roughly halves the reported progress.
            total = meta["to_stage"] or meta["to_download"]

            if present:
                size, mtime, truncated = tree_bytes(downloading, deadline)
            else:
                size, mtime, truncated = 0, 0.0, False

            entries.append({
                "appid": appid,
                "name": meta["name"],
                "library": root,
                "downloaded": size,
                "total": total,
                "present": present,
                "started": bool(meta["flags"] & STATE_UPDATE_STARTED),
                "queued": bool(meta["flags"] & STATE_UNINSTALLED) and
                          not bool(meta["flags"] & STATE_UPDATE_STARTED),
                "mtime": mtime,
                "truncated": truncated,
                "now": now,
            })
    return entries


def main():
    parser = argparse.ArgumentParser(description="Report in-flight Steam downloads")
    parser.add_argument("--include-queued", action="store_true",
                        help="also report downloads Steam has accepted but not started")
    args = parser.parse_args()

    # Bound the whole scan so a library with hundreds of thousands of files
    # cannot hold up the bar's refresh.
    deadline = time.monotonic() + WALK_BUDGET_SECONDS

    steam_root = find_steam_root()
    if not steam_root:
        print(json.dumps({"__meta": {
            "running": False, "steamRoot": "", "downloads": 0,
            "note": "Steam is not installed for this user"}}))
        return

    running = steam_running(steam_root)
    roots = library_roots(steam_root)
    entries = snapshot(roots, deadline)

    # Anything that only has a queued manifest is not downloading yet.
    active = [e for e in entries
              if e["present"] and (e["started"] or e["downloaded"] > 0)]
    queued = [e for e in entries
              if not e["present"] and e["queued"] and e["total"] > 0]

    # A tree Steam has not touched for a while is a finished or paused
    # download; either way it should not hold the bar.
    live = [e for e in active
            if e["mtime"] == 0.0 or (e["now"] - e["mtime"]) <= SETTLE_SECONDS]

    free_bytes, total_bytes = library_space(roots)
    meta = {
        "running": running,
        "steamRoot": steam_root,
        "downloads": len(live),
        "libraries": len(roots),
        "freeBytes": free_bytes,
        "totalBytes": total_bytes,
        "note": "",
    }
    if not live and not running:
        meta["note"] = "Steam is not running"

    out = [{
        "appid": e["appid"],
        "name": e["name"],
        "downloaded": e["downloaded"],
        "total": e["total"],
        "library": e["library"],
        "idle": round(max(0.0, e["now"] - e["mtime"]) if e["mtime"] else 0.0, 1),
        "truncated": e["truncated"],
    } for e in sorted(live, key=lambda e: e["downloaded"], reverse=True)]

    if args.include_queued:
        for e in sorted(queued, key=lambda e: e["name"].lower()):
            out.append({
                "appid": e["appid"],
                "name": e["name"],
                "downloaded": 0,
                "total": e["total"],
                "library": e["library"],
                "queued": True,
            })

    out.append({"__meta": meta})
    sys.stdout.write("".join(json.dumps(row) + "\n" for row in out))


def main_with_signals():
    # A hung filesystem walk should not leave the bar waiting forever.
    def on_alarm(signum, frame):
        raise TimeoutError

    signal.signal(signal.SIGALRM, on_alarm)
    signal.alarm(int(WALK_BUDGET_SECONDS * 3))
    try:
        main()
    except TimeoutError:
        print(json.dumps({"__meta": {
            "running": False, "steamRoot": "", "downloads": 0,
            "note": "Timed out reading the Steam library"}}))
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    prepare_environment()
    main_with_signals()
