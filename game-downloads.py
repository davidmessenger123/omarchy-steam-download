#!/usr/bin/env python3
"""Report in-flight Steam, Heroic and Epic Games Launcher downloads.

Each launcher keeps its own private state rather than exposing a supported
progress API.  This helper reads what they already write and normalises the
results into one row per download:

  Steam
    <library>/steamapps/appmanifest_<appid>.acf   totals and title
    <library>/steamapps/downloading/<appid>/       bytes landed
    <library>/steamapps/libraryfolders.vdf        library list

  Heroic
    ~/.config/heroic/store/download-manager.json  queue and finished items
    ~/.local/state/Heroic/logs/heroic.log          progress lines by runner

  Epic Games Launcher (native EGLv2)
    ~/.local/share/com.epicgames.eglv2/queue.json  operation journal
    <install_path>/.egstore/chunk_cache            downloaded chunks

The output is one compact JSON object per download followed by one __meta
object.  Rows carry a stable `key` (source plus app id) so the QML model can
derive speed from the growth between two polls.  Nothing is written to the
launchers' files and no privileged API is used.

The helper deliberately does not rely on Steam's appmanifest BytesDownloaded
field.  Steam only rewrites it when a download starts or stops, so it is stale
for the whole download; the staging tree is the live value.  For Steam the
denominator is BytesToStage (the uncompressed on-disk size), never the sum of
BytesToStage and BytesToDownload: those describe the same bytes at different
compressions and adding them roughly halves the apparent progress.
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
LOG_TAIL_BYTES = 512 * 1024
LOG_FRESH_SECONDS = 3600

# Internal test hook: the unit tests need to model a running launcher without
# spawning one.  It is never set by the plugin.
PROC_OVERRIDE = {
    name.strip().lower()
    for name in (os.environ.get("GAME_DOWNLOADS_PROC_OVERRIDE") or "").split(",")
    if name.strip()
}
DISABLE_PROC = bool(os.environ.get("GAME_DOWNLOADS_DISABLE_PROC"))

RUNNER_NAMES = {
    "legendary": "Epic",
    "gog": "GOG",
    "nile": "Amazon",
    "zoom": "Zoom",
    "sideload": "Sideload",
}

SOURCE_NAMES = {
    "steam": "Steam",
    "heroic": "Heroic",
    "epic": "Epic Games",
}


def fail(message, code=1):
    sys.stderr.write(message.rstrip() + "\n")
    sys.exit(code)


def prepare_environment():
    """The widget clears the environment, so rebuild HOME from the passwd db."""
    if not os.environ.get("HOME"):
        try:
            os.environ["HOME"] = pwd.getpwuid(os.getuid()).pw_dir
        except Exception:
            pass


def home_path(*parts):
    home = os.environ.get("HOME") or ""
    return os.path.join(home, *parts)


def config_home():
    return os.environ.get("XDG_CONFIG_HOME") or home_path(".config")


def data_home():
    return os.environ.get("XDG_DATA_HOME") or home_path(".local", "share")


def state_home():
    return os.environ.get("XDG_STATE_HOME") or home_path(".local", "state")


def read_json(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def first(mapping, *keys, default=""):
    if not isinstance(mapping, dict):
        return default
    for key in keys:
        value = mapping.get(key)
        if value not in (None, ""):
            return value
    return default


def as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return 0


def parse_size(value):
    """Parse Heroic's human sizes such as "12.3 GB" or "?? MB"."""
    if not isinstance(value, str):
        return 0
    match = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*([kmgtp]?i?b)\b", value, re.I)
    if not match:
        return 0
    try:
        number = float(match.group(1))
    except ValueError:
        return 0
    unit = match.group(2).lower()
    factors = {
        "b": 1,
        "kb": 1024,
        "kib": 1024,
        "mb": 1024 ** 2,
        "mib": 1024 ** 2,
        "gb": 1024 ** 3,
        "gib": 1024 ** 3,
        "tb": 1024 ** 4,
        "tib": 1024 ** 4,
        "pb": 1024 ** 5,
        "pib": 1024 ** 5,
    }
    return int(number * factors.get(unit, 1))


def _process_comm(pid):
    try:
        with open("/proc/%s/comm" % pid, encoding="utf-8", errors="replace") as handle:
            return handle.read().strip()
    except OSError:
        return ""


def _process_exe(pid):
    try:
        return os.path.basename(os.readlink("/proc/%s/exe" % pid)).lower()
    except OSError:
        return ""


def _process_cmdline(pid):
    try:
        with open("/proc/%s/cmdline" % pid, "rb") as handle:
            return handle.read().replace(b"\0", b" ").decode("utf-8", "replace").lower()
    except OSError:
        return ""


def source_running(source):
    """Is the launcher process alive?  A tiny /proc scan, no subprocess."""
    if source.lower() in PROC_OVERRIDE:
        return True
    if DISABLE_PROC:
        return False
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        comm = _process_comm(entry).lower()
        if source == "steam":
            if comm == "steam":
                return True
            continue
        if source == "heroic":
            if comm.startswith("heroic") or _process_exe(entry).startswith("heroic"):
                return True
            continue
        if source == "epic":
            if comm.startswith("epicgameslaunch") or comm.startswith("epic-games-launch"):
                return True
            exe = _process_exe(entry)
            if exe.startswith("epicgameslaunch") or exe.startswith("epic-games-launcher"):
                return True
            cmdline = _process_cmdline(entry)
            if "com.epicgames.eglv2" in cmdline or "epic-games-launcher" in cmdline:
                return True
    return False


# ---------------------------------------------------------------------------
# Steam
# ---------------------------------------------------------------------------


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


def parse_vdf_paths(text):
    """Pull the "path" values out of libraryfolders.vdf."""
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

    def as_int_field(name):
        try:
            return int(fields.get(name, "0") or 0)
        except ValueError:
            return 0

    return {
        "appid": as_int_field("appid"),
        "name": fields.get("name") or "App %d" % as_int_field("appid"),
        "flags": as_int_field("StateFlags"),
        "to_download": as_int_field("BytesToDownload"),
        "to_stage": as_int_field("BytesToStage"),
    }


def tree_bytes(path, deadline):
    """Allocated bytes and newest mtime under `path`.

    Steam preallocates staging space, so a fully written depot can have
    apparent size far ahead of what is actually on the platter.  st_blocks
    reflects real allocation and tracks the download closely.  Files that
    report zero blocks (sparse, not yet written) contribute their apparent
    size instead.
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

            # BytesToStage is the uncompressed on-disk size, which is what
            # the tree measures and therefore what Steam's own percentage is
            # built from.  BytesToDownload is the compressed transfer size
            # and must not be added to it.
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


def steam_rows(include_queued, deadline):
    steam_root = find_steam_root()
    if not steam_root:
        return [], {
            "installed": False,
            "running": False,
            "active": 0,
            "note": "Steam is not installed for this user",
            "paths": [],
        }

    running = source_running("steam")
    roots = library_roots(steam_root)
    entries = snapshot(roots, deadline)

    active = [e for e in entries
              if e["present"] and (e["started"] or e["downloaded"] > 0)]
    queued = [e for e in entries
              if not e["present"] and e["queued"] and e["total"] > 0]
    live = [e for e in active
            if e["mtime"] == 0.0 or (e["now"] - e["mtime"]) <= SETTLE_SECONDS]

    rows = []
    for entry in sorted(live, key=lambda e: e["downloaded"], reverse=True):
        total = entry["total"]
        rows.append({
            "key": "steam:%d" % entry["appid"],
            "source": "steam",
            "sourceName": SOURCE_NAMES["steam"],
            "appid": entry["appid"],
            "name": entry["name"],
            "downloaded": entry["downloaded"],
            "total": total,
            "library": entry["library"],
            "queued": False,
            "status": "downloading",
            "indeterminate": total <= 0,
        })
    if include_queued:
        for entry in sorted(queued, key=lambda e: e["name"].lower()):
            rows.append({
                "key": "steam:%d" % entry["appid"],
                "source": "steam",
                "sourceName": SOURCE_NAMES["steam"],
                "appid": entry["appid"],
                "name": entry["name"],
                "downloaded": 0,
                "total": entry["total"],
                "library": entry["library"],
                "queued": True,
                "status": "queued",
                "indeterminate": False,
            })

    note = "Steam is not running" if not running else ""
    return rows, {
        "installed": True,
        "running": running,
        "active": len(live),
        "note": note,
        "paths": roots,
        "steamRoot": steam_root,
    }


# ---------------------------------------------------------------------------
# Heroic
# ---------------------------------------------------------------------------


def heroic_config_dir():
    return os.path.join(config_home(), "heroic")


def heroic_queue_path():
    return os.path.join(heroic_config_dir(), "store", "download-manager.json")


def heroic_log_path():
    return os.path.join(state_home(), "Heroic", "logs", "heroic.log")


def heroic_default_install_path():
    for relative in (os.path.join("store", "config.json"), "config.json"):
        data = read_json(os.path.join(heroic_config_dir(), relative))
        if not isinstance(data, dict):
            continue
        settings = data.get("settings") if isinstance(data.get("settings"), dict) else data
        path = first(settings, "defaultInstallPath", "default_install_path")
        if isinstance(path, str) and path:
            return path
    return ""


def heroic_titles():
    """Map Epic AppName -> display title from Heroic's local library cache."""
    titles = {}
    cache = os.path.join(heroic_config_dir(), "store_cache", "legendary_library.json")
    data = read_json(cache)
    library = data.get("library") if isinstance(data, dict) else data
    if isinstance(library, list):
        for record in library:
            if not isinstance(record, dict):
                continue
            app_name = first(record, "app_name", "appName")
            title = first(record, "title", "name")
            if isinstance(app_name, str) and isinstance(title, str):
                titles[app_name.lower()] = title

    install_info = read_json(os.path.join(
        heroic_config_dir(), "store_cache", "legendary_install_info.json"))
    if isinstance(install_info, dict):
        for key, value in install_info.items():
            if not isinstance(value, dict):
                continue
            game = value.get("game") or {}
            app_name = first(game, "app_name", "appName", default=key)
            title = first(game, "title", "name")
            if isinstance(app_name, str) and isinstance(title, str):
                titles.setdefault(app_name.lower(), title)
    return titles


def _parse_eta(value):
    if not isinstance(value, str):
        return 0
    parts = value.strip().split(":")
    try:
        numbers = [int(part) for part in parts]
    except ValueError:
        return 0
    if len(numbers) == 3:
        return numbers[0] * 3600 + numbers[1] * 60 + numbers[2]
    if len(numbers) == 2:
        return numbers[0] * 60 + numbers[1]
    return 0


def heroic_progress():
    """Latest progress line per title from Heroic's general log.

    Heroic writes these lines only when all four fields are available:
      (12:34:56) [INFO]: [Legendary]: Progress for Game: 42.3%/1.2GB/00:05:00 Down: 12.3MB/s / Disk: 45.6MB/s
    The file is bounded so a large log cannot stall a poll.  The current log
    is used, not the rotated .old file, to avoid reporting a previous session.
    """
    path = heroic_log_path()
    try:
        stat = os.stat(path)
        if time.time() - stat.st_mtime > LOG_FRESH_SECONDS:
            return {}
        with open(path, "rb") as handle:
            handle.seek(max(0, stat.st_size - LOG_TAIL_BYTES))
            raw = handle.read()
    except OSError:
        return {}

    text = raw.decode("utf-8", errors="replace")
    pattern = re.compile(
        r"Progress for (.+?):\s+([0-9]+(?:\.[0-9]+)?)%/([^/\s]+)/([0-9:]+)"
        r"\s+Down:\s*([0-9]+(?:\.[0-9]+)?)MB/s"
        r"\s*/\s*Disk:\s*([0-9]+(?:\.[0-9]+)?)MB/s",
        re.I,
    )
    progress = {}
    for line in text.splitlines():
        match = pattern.search(line)
        if not match:
            continue
        title = match.group(1).strip()
        if not title:
            continue
        percent = float(match.group(2))
        downloaded = parse_size(match.group(3))
        eta = _parse_eta(match.group(4))
        rate = float(match.group(5)) * 1024 * 1024
        total = downloaded / (percent / 100.0) if percent > 0 else 0
        progress[title.lower()] = {
            "title": title,
            "percent": max(0.0, min(100.0, percent)),
            "downloaded": max(0, downloaded),
            "total": max(0, int(total)),
            "eta": max(0, eta),
            "rate": max(0.0, rate),
            "log_mtime": stat.st_mtime,
        }
    return progress


def _heroic_progress_for(progress, title, started_at):
    entry = progress.get((title or "").lower())
    if not entry:
        return None
    if started_at and entry.get("log_mtime", 0) < started_at - 5:
        return None
    return entry


def heroic_rows(include_queued):
    config = heroic_config_dir()
    installed = os.path.isdir(config) or os.path.isfile(heroic_queue_path())
    if not installed:
        return [], {
            "installed": False,
            "running": False,
            "active": 0,
            "note": "Heroic is not installed for this user",
            "paths": [],
        }

    running = source_running("heroic")
    data = read_json(heroic_queue_path())
    queue = []
    if isinstance(data, dict):
        queue = data.get("queue") or []
    elif isinstance(data, list):
        queue = data
    if not isinstance(queue, list):
        queue = []

    titles = heroic_titles()
    progress = heroic_progress()
    rows = []
    active_count = 0

    for index, item in enumerate(queue):
        if not isinstance(item, dict):
            continue
        params = item.get("params") if isinstance(item.get("params"), dict) else item
        game = params.get("gameInfo") if isinstance(params.get("gameInfo"), dict) else {}
        app_name = first(params, "appName", "app_name", "id")
        if str(app_name).lower() == "gog-redist":
            # Heroic installs this small Galaxy dependency alongside GOG games;
            # it is not a user-facing game download.
            continue
        runner = first(params, "runner", default="legendary")
        title = first(game, "title", "name")
        if not title and isinstance(app_name, str):
            title = titles.get(app_name.lower(), "")
        if not title:
            title = app_name if isinstance(app_name, str) and app_name else "Heroic game"

        started_at = as_int(item.get("startTime") or params.get("startTime")) / 1000.0
        entry_progress = _heroic_progress_for(progress, title, started_at)
        is_active = running and index == 0
        if is_active:
            active_count += 1

        downloaded = entry_progress["downloaded"] if entry_progress else 0
        total = entry_progress["total"] if entry_progress else 0
        percent = entry_progress["percent"] if entry_progress else None
        rate = entry_progress["rate"] if entry_progress else 0
        eta = entry_progress["eta"] if entry_progress else 0
        if not total:
            total = parse_size(first(params, "size", "downloadSize", "download_size"))

        row = {
            "key": "heroic:%s:%s" % (runner, app_name or index),
            "source": "heroic",
            "sourceName": SOURCE_NAMES["heroic"],
            "runner": runner,
            "runnerName": RUNNER_NAMES.get(str(runner).lower(), str(runner).title()),
            "appid": app_name or "",
            "name": title,
            "downloaded": downloaded,
            "total": total,
            "library": first(params, "path", "installPath", "install_path"),
            "queued": not is_active,
            "status": "downloading" if is_active else "queued",
            "indeterminate": bool(is_active and not entry_progress and downloaded <= 0),
            "rate": rate,
            "eta": eta,
        }
        if percent is not None:
            row["percent"] = percent
        if is_active and not entry_progress:
            row["status"] = "starting"
        if not is_active:
            row["downloaded"] = 0
            row["total"] = total
        if is_active or include_queued:
            rows.append(row)

    note = "Heroic is not running" if not running else ""
    paths = [row["library"] for row in rows if row.get("library")]
    default_path = heroic_default_install_path()
    if default_path:
        paths.append(default_path)
    return rows, {
        "installed": True,
        "running": running,
        "active": active_count,
        "note": note,
        "paths": paths,
    }


# ---------------------------------------------------------------------------
# Epic Games Launcher (native EGLv2)
# ---------------------------------------------------------------------------


def epic_data_dir():
    return os.path.join(data_home(), "com.epicgames.eglv2")


def epic_queue_path():
    return os.path.join(epic_data_dir(), "queue.json")


def epic_settings():
    return read_json(os.path.join(epic_data_dir(), "settings", "app-settings.json")) or {}


def epic_title_maps():
    """Resolve EGLv2 AppNames and folder names from its catalog cache.

    The launcher caches the library catalog under cache/prod/catalog-cache.json.
    Each item carries a display title, its release appId(s), and the install
    folder name from customAttributes.  This is the same local data the launcher
    uses to paint the library and needs no account access.
    """
    app_titles = {}
    folder_titles = {}
    data = read_json(os.path.join(epic_data_dir(), "cache", "prod", "catalog-cache.json"))
    entries = data.get("items") if isinstance(data, dict) else None
    if not isinstance(entries, dict):
        return app_titles, folder_titles
    for entry in entries.values():
        if not isinstance(entry, dict):
            continue
        item = entry.get("item") if isinstance(entry.get("item"), dict) else entry
        title = first(item, "title", "name")
        if not isinstance(title, str) or not title:
            continue
        releases = item.get("releaseInfo") or []
        if isinstance(releases, list):
            for release in releases:
                if not isinstance(release, dict):
                    continue
                app_id = first(release, "appId", "app_id")
                if isinstance(app_id, str) and app_id:
                    app_titles[app_id.lower()] = title
        item_id = first(item, "id", "itemId", "item_id")
        if isinstance(item_id, str) and item_id:
            app_titles.setdefault(item_id.lower(), title)
        custom = item.get("customAttributes") or {}
        folder = ""
        if isinstance(custom, dict):
            folder_value = custom.get("FolderName") or custom.get("folderName")
            if isinstance(folder_value, dict):
                folder = first(folder_value, "value")
            elif isinstance(folder_value, str):
                folder = folder_value
        if isinstance(folder, str) and folder:
            folder_titles[folder.lower()] = title
    return app_titles, folder_titles


def _epic_app_identity(value):
    """Return (app_name, item_id, namespace_id) from the journal's AppId."""
    if isinstance(value, str):
        return value, "", ""
    if isinstance(value, (list, tuple)):
        # AppId is declared as namespaceId, itemId, appName.  A tuple
        # serialisation therefore puts the useful name last.
        fields = [field if isinstance(field, str) else "" for field in value[:3]]
        fields += [""] * (3 - len(fields))
        return fields[2], fields[1], fields[0]
    if not isinstance(value, dict):
        return "", "", ""
    app_name = first(value, "appName", "app_name", "appId", "app_id")
    item_id = first(value, "itemId", "item_id")
    namespace_id = first(value, "namespaceId", "namespace_id")
    if not isinstance(app_name, str):
        app_name = ""
    if not isinstance(item_id, str):
        item_id = ""
    if not isinstance(namespace_id, str):
        namespace_id = ""
    return app_name, item_id, namespace_id


def _sum_size_fields(value, needle):
    total = 0
    if isinstance(value, dict):
        for key, child in value.items():
            lower = str(key).lower()
            if needle in lower and isinstance(child, (int, float, str)):
                try:
                    total += max(0, int(float(child)))
                except (TypeError, ValueError):
                    pass
            elif isinstance(child, (dict, list)):
                total += _sum_size_fields(child, needle)
    elif isinstance(value, list):
        for child in value:
            total += _sum_size_fields(child, needle)
    return total


def epic_total_bytes(operation):
    sizes = operation.get("last_action_sizes") or operation.get("lastActionSizes")
    download = _sum_size_fields(sizes, "download")
    if download > 0:
        return download
    install = _sum_size_fields(sizes, "install")
    if install > 0:
        return install
    direct = as_int(first(operation, "totalDownloadSize", "total_download_size",
                          "downloadSize", "download_size"))
    if direct > 0:
        return direct
    return as_int(first(operation, "installSize", "install_size"))


def _epic_chunk_bytes(install_path, deadline):
    if not install_path:
        return 0
    candidates = [
        os.path.join(install_path, ".egstore", "chunk_cache"),
        os.path.join(install_path, ".egstore", "chunk_store"),
        os.path.join(install_path, ".egstore", "chunks"),
    ]
    for path in candidates:
        if os.path.isdir(path):
            size, _, _ = tree_bytes(path, deadline)
            if size > 0:
                return size
    return 0


def _enum_text(value):
    """Accept both a plain Rust enum string and an externally tagged object."""
    if isinstance(value, dict):
        for key in ("state", "status", "kind", "type", "value", "name"):
            child = value.get(key)
            if isinstance(child, str) and child:
                return child
        for key in value:
            if isinstance(key, str) and key:
                return key
        return ""
    return str(value or "")


def _epic_state_kind(state):
    value = re.sub(r"[^a-z]", "", str(state or "").lower())
    if not value:
        return "unknown"
    if "paused" in value or value in ("queued", "preloaded", "caninstall"):
        return "queued"
    if any(word in value for word in ("canlaunch", "completed", "failed", "cancel", "done")):
        return "terminal"
    if any(word in value for word in (
            "download", "install", "verif", "repair", "running", "update",
            "preload", "prereq", "extract")):
        return "active"
    return "unknown"


def _epic_status(state):
    value = re.sub(r"[^a-z]", "", str(state or "").lower())
    if "paused" in value:
        return "paused"
    if "verif" in value:
        return "verifying"
    if "repair" in value:
        return "repairing"
    if "preload" in value and "preloaded" not in value:
        return "preloading"
    if "prereq" in value:
        return "installing"
    if "install" in value:
        return "installing"
    if "update" in value:
        return "updating"
    if "download" in value or "running" in value:
        return "downloading"
    if "queued" in value:
        return "queued"
    return "downloading" if value else "queued"


def epic_rows(include_queued, deadline):
    data_dir = epic_data_dir()
    installed = os.path.isdir(data_dir)
    if not installed:
        return [], {
            "installed": False,
            "running": False,
            "active": 0,
            "note": "Epic Games Launcher is not installed for this user",
            "paths": [],
        }

    running = source_running("epic")
    data = read_json(epic_queue_path())
    operations = []
    if isinstance(data, dict):
        operations = data.get("operations") or data.get("queue") or []
    elif isinstance(data, list):
        operations = data
    if isinstance(operations, dict):
        operations = list(operations.values())
    if not isinstance(operations, list):
        operations = []

    settings = epic_settings()
    default_dir = first(settings, "defaultInstallDir", "default_install_dir")
    note = "Epic Games Launcher is not running" if not running else ""
    if not operations:
        return [], {
            "installed": True,
            "running": running,
            "active": 0,
            "note": note,
            "paths": [default_dir] if default_dir else [],
        }

    catalog_titles, folder_titles = epic_title_maps()
    titles = heroic_titles()
    titles.update(catalog_titles)
    rows = []
    active_count = 0

    for index, operation in enumerate(operations):
        if not isinstance(operation, dict):
            continue
        kind = _enum_text(first(operation, "kind", "type", default="install")).lower()
        if kind in ("uninstall", "remove", "delete"):
            continue

        install = operation.get("install") if isinstance(operation.get("install"), dict) else {}
        app_name, item_id, namespace_id = _epic_app_identity(
            operation.get("app_id") or operation.get("appId") or operation.get("app"))
        operation_id = first(operation, "op_id", "operation_id", "operationId", "id")
        identity = app_name or item_id or namespace_id or operation_id or str(index)
        install_path = first(install, "install_path", "installPath")
        if not install_path:
            install_path = first(operation, "dir", "installDir", "install_dir")
        if not install_path:
            install_path = default_dir

        state = _enum_text(first(operation, "state", "status"))
        state_kind = _epic_state_kind(state)
        if state_kind == "terminal":
            continue
        if state_kind == "unknown":
            state_kind = "active" if running and index == 0 else "queued"
        is_active = running and state_kind == "active"

        total = epic_total_bytes(operation)
        downloaded = _epic_chunk_bytes(install_path, deadline) if is_active else 0
        status = _epic_status(state)
        if not is_active and status == "downloading":
            status = "queued"

        folder_name = first(install, "folder_name", "folderName")
        title = titles.get(app_name.lower()) if isinstance(app_name, str) and app_name else ""
        if not title and isinstance(folder_name, str) and folder_name:
            title = folder_titles.get(folder_name.lower()) or folder_name
        if not title and isinstance(app_name, str) and app_name and not re.fullmatch(
                r"[0-9a-f]{16,}", app_name, re.I):
            title = app_name
        if not title:
            title = "Epic Games download"

        row = {
            "key": "epic:%s" % identity,
            "source": "epic",
            "sourceName": SOURCE_NAMES["epic"],
            "appid": identity,
            "name": title,
            "downloaded": downloaded,
            "total": total,
            "library": install_path or default_dir,
            "queued": not is_active,
            "status": status if is_active else "queued",
            "indeterminate": bool(is_active and (total <= 0 or (
                downloaded <= 0 and status != "downloading"))),
        }
        if total > 0 and downloaded > 0:
            row["percent"] = max(0.0, min(100.0, downloaded / total * 100.0))
        if is_active:
            active_count += 1
        if is_active or include_queued:
            rows.append(row)

    paths = [row["library"] for row in rows if row.get("library")] or ([default_dir] if default_dir else [])
    return rows, {
        "installed": True,
        "running": running,
        "active": active_count,
        "note": note,
        "paths": paths,
    }


# ---------------------------------------------------------------------------
# Shared output
# ---------------------------------------------------------------------------


def filesystem_space(paths):
    """Free/total bytes across distinct filesystems used by the launchers."""
    seen = set()
    free = 0
    total = 0
    for path in paths:
        if not path:
            continue
        probe = path
        while probe and not os.path.exists(probe):
            parent = os.path.dirname(probe)
            if parent == probe:
                probe = ""
                break
            probe = parent
        if not probe:
            continue
        try:
            dev = os.stat(probe).st_dev
        except OSError:
            continue
        if dev in seen:
            continue
        seen.add(dev)
        try:
            usage = shutil.disk_usage(probe)
        except OSError:
            continue
        free += usage.free
        total += usage.total
    return free, total


def main():
    parser = argparse.ArgumentParser(description="Report in-flight game downloads")
    parser.add_argument("--include-queued", action="store_true",
                        help="also report downloads accepted but not started")
    args = parser.parse_args()

    # Bound the whole scan so a library with hundreds of thousands of files
    # cannot hold up the bar's refresh.
    deadline = time.monotonic() + WALK_BUDGET_SECONDS

    steam, steam_meta = steam_rows(args.include_queued, deadline)
    heroic, heroic_meta = heroic_rows(args.include_queued)
    # Give the Epic chunk walk its own small budget so a large Steam scan
    # cannot make an active Epic download look empty.
    epic, epic_meta = epic_rows(args.include_queued, time.monotonic() + 0.8)

    rows = steam + heroic + epic
    active = [row for row in rows if not row.get("queued")]
    queued = [row for row in rows if row.get("queued")]
    if not args.include_queued:
        rows = active
    else:
        rows = active + sorted(queued, key=lambda row: str(row.get("name", "")).lower())

    sources = {
        "steam": steam_meta,
        "heroic": heroic_meta,
        "epic": epic_meta,
    }
    running = any(meta.get("running") for meta in sources.values())
    installed = [meta for meta in sources.values() if meta.get("installed")]
    paths = []
    for meta in sources.values():
        paths.extend(meta.get("paths") or [])
    free_bytes, total_bytes = filesystem_space(paths)

    if not installed:
        note = "No supported game launchers found"
    elif not running:
        note = "No game launchers running"
    else:
        note = ""

    # Keep the old top-level keys for clients that still read them, while the
    # per-source map is the authoritative view.
    meta = {
        "running": running,
        "downloads": len(active),
        "libraries": len({os.path.realpath(path) for path in paths if path}),
        "freeBytes": free_bytes,
        "totalBytes": total_bytes,
        "note": note,
        "steamRoot": steam_meta.get("steamRoot", ""),
        "sources": sources,
    }

    out = []
    for row in rows:
        out.append(json.dumps(row, separators=(",", ":")))
    out.append(json.dumps({"__meta": meta}, separators=(",", ":")))
    sys.stdout.write("\n".join(out) + "\n")


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
            "running": False, "downloads": 0,
            "note": "Timed out reading game downloads"}}))
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    prepare_environment()
    main_with_signals()
