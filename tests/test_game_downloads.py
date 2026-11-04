"""Unit tests for the Game Downloads helper.

The Steam tests pin the two-phase accounting: the compressed transfer size and
the uncompressed staging size describe the same bytes at different
compressions, so only the staging size may be used as the denominator.  The
Heroic and Epic tests use their real local formats: Heroic's
download-manager.json plus progress log, and EGLv2's queue.json journal plus
.egstore/chunk_cache.
"""

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HELPER = os.path.join(ROOT, "game-downloads.py")

_spec = importlib.util.spec_from_file_location("game_downloads", HELPER)
gd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gd)


def write_manifest(directory, appid, name, flags=1026, to_download=0, to_stage=0):
    """Write a minimal appmanifest ACF and return its path."""
    path = os.path.join(directory, "appmanifest_%d.acf" % appid)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write('"AppState"\n{\n')
        handle.write('\t"appid"\t\t"%d"\n' % appid)
        handle.write('\t"name"\t\t"%s"\n' % name)
        handle.write('\t"StateFlags"\t\t"%d"\n' % flags)
        handle.write('\t"BytesToDownload"\t\t"%d"\n' % to_download)
        handle.write('\t"BytesToStage"\t\t"%d"\n' % to_stage)
        handle.write("}\n")
    return path


def make_steam_root(base, steamapps=True):
    root = os.path.join(base, ".local", "share", "Steam")
    if steamapps:
        os.makedirs(os.path.join(root, "steamapps"), exist_ok=True)
    else:
        os.makedirs(root, exist_ok=True)
    return root


def make_home(base):
    os.makedirs(os.path.join(base, ".config"), exist_ok=True)
    os.makedirs(os.path.join(base, ".local", "share"), exist_ok=True)
    os.makedirs(os.path.join(base, ".local", "state"), exist_ok=True)
    return base


def write_heroic_queue(home, queue, finished=None):
    path = os.path.join(home, ".config", "heroic", "store", "download-manager.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump({"finished": finished or [], "queue": queue}, handle)
    return path


def write_heroic_library(home, games):
    path = os.path.join(home, ".config", "heroic", "store_cache",
                        "legendary_library.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    records = []
    for app_name, title in games.items():
        records.append({"app_name": app_name, "title": title})
    with open(path, "w", encoding="utf-8") as handle:
        json.dump({"library": records}, handle)
    return path


def write_heroic_log(home, lines):
    path = os.path.join(home, ".local", "state", "Heroic", "logs", "heroic.log")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
    return path


def heroic_queue_item(title="Heroic Game", runner="legendary", started=True,
                      folder="/tmp/heroic-game"):
    return {
        "params": {
            "appName": "heroic-app-id",
            "runner": runner,
            "path": folder,
            "gameInfo": {"title": title, "runner": runner},
            "size": "1.0 GB",
        },
        "type": "install",
        "startTime": int(time.time() * 1000) if started else 0,
    }


def write_epic_queue(home, operations):
    path = os.path.join(home, ".local", "share", "com.epicgames.eglv2", "queue.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump({"version": 1, "operations": operations}, handle)
    return path


def write_epic_catalog(home, app_id, title, folder):
    path = os.path.join(home, ".local", "share", "com.epicgames.eglv2",
                        "cache", "prod", "catalog-cache.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    data = {
        "items": {
            "catalog-id": {
                "item": {
                    "id": "catalog-id",
                    "title": title,
                    "releaseInfo": [{"appId": app_id, "platform": ["Windows"]}],
                    "customAttributes": {
                        "FolderName": {"type": "STRING", "value": folder},
                    },
                }
            }
        },
        "schemaVersion": 1,
        "locale": "en-US",
    }
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle)
    return path


def epic_operation(app_name="abc123", title_folder="Epic Game",
                   install_path="/tmp/epic-game", state="Installing",
                   download_bytes=100000, install_bytes=200000):
    return {
        "op_id": "op-1",
        "app_id": {
            "namespaceId": "namespace",
            "itemId": "item-id",
            "appName": app_name,
        },
        "kind": "install",
        "state": state,
        "install": {
            "install_path": install_path,
            "folder_name": title_folder,
            "installation_id": "installation-id",
        },
        "last_action_sizes": {
            "main": {
                "downloadBytes": download_bytes,
                "installBytes": install_bytes,
            }
        },
    }


def run_helper(home, *args, proc_override="", disable_proc=True):
    env = {"HOME": home, "PATH": os.environ.get("PATH", "")}
    if disable_proc:
        env["GAME_DOWNLOADS_DISABLE_PROC"] = "1"
    if proc_override:
        env["GAME_DOWNLOADS_PROC_OVERRIDE"] = proc_override
    out = subprocess.run([sys.executable, "-E", HELPER, *args],
                         capture_output=True, text=True, env=env, timeout=30)
    return [json.loads(line) for line in out.stdout.splitlines() if line.strip()]


def split_rows(rows):
    games = [row for row in rows if "__meta" not in row]
    meta = [row for row in rows if "__meta" in row][0]["__meta"]
    return games, meta


class ParseManifest(unittest.TestCase):
    def test_reads_scalars(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_manifest(tmp, 2842040, "Star Wars Outlaws",
                                  flags=1026, to_download=64110825056,
                                  to_stage=73089514868)
            meta = gd.parse_manifest(path)
        self.assertEqual(meta["appid"], 2842040)
        self.assertEqual(meta["name"], "Star Wars Outlaws")
        self.assertEqual(meta["flags"], 1026)
        self.assertEqual(meta["to_download"], 64110825056)
        self.assertEqual(meta["to_stage"], 73089514868)

    def test_rejects_garbage(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "appmanifest_1.acf")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("not a vdf at all {{{")
            self.assertIsNone(gd.parse_manifest(path))

    def test_missing_file(self):
        self.assertIsNone(gd.parse_manifest("/nonexistent/appmanifest_1.acf"))

    def test_non_numeric_sizes_default_to_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "appmanifest_7.acf")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write('"AppState"\n{\n\t"appid"\t\t"7"\n'
                             '\t"BytesToStage"\t\t"not-a-number"\n}\n')
            meta = gd.parse_manifest(path)
        self.assertEqual(meta["to_stage"], 0)


class LibraryDiscovery(unittest.TestCase):
    def test_finds_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_steam_root(tmp)
            saved = os.environ.get("HOME")
            os.environ["HOME"] = tmp
            try:
                self.assertEqual(gd.find_steam_root(), root)
            finally:
                if saved is None:
                    os.environ.pop("HOME", None)
                else:
                    os.environ["HOME"] = saved

    def test_libraryfolders_paths(self):
        text = '"libraryfolders"\n{\n\t"0"\n\t{\n\t\t"path"\t\t"/games/Steam"\n\t}\n}\n'
        self.assertEqual(gd.parse_vdf_paths(text), ["/games/Steam"])

    def test_missing_vdf_falls_back_to_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_steam_root(tmp)
            self.assertEqual(gd.library_roots(root), [root])

    def test_nonexistent_library_entries_are_dropped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_steam_root(tmp)
            with open(os.path.join(root, "steamapps", "libraryfolders.vdf"),
                      "w", encoding="utf-8") as handle:
                handle.write('"libraryfolders"\n{\n\t"0"\n\t{\n'
                             '\t\t"path"\t\t"/definitely/not/here"\n\t}\n}\n')
            self.assertEqual(gd.library_roots(root), [root])


class TreeMeasurement(unittest.TestCase):
    # Measurements come from allocated blocks (st_blocks * 512), so a size is
    # always rounded up to the filesystem's 4 KiB block. That is noise against
    # a real multi-gigabyte download, but tests must allow for it.
    BLOCK = 4096

    def assertCloseToBytes(self, measured, expected):
        self.assertGreaterEqual(measured, expected)
        self.assertLess(measured - expected, self.BLOCK * 16,
                        "measured %d is not a block-rounded %d" % (measured, expected))

    def test_counts_allocated_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            blob = os.path.join(tmp, "blob.bin")
            with open(blob, "wb") as handle:
                handle.write(b"x" * 100000)
            total, newest, truncated = gd.tree_bytes(tmp, time.monotonic() + 5)
        self.assertCloseToBytes(total, 100000)
        self.assertFalse(truncated)
        self.assertGreater(newest, 0)

    def test_rounds_up_to_whole_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "tiny.bin"), "wb") as handle:
                handle.write(b"x" * 1)
            total, _, _ = gd.tree_bytes(tmp, time.monotonic() + 5)
        self.assertEqual(total, self.BLOCK)

    def test_sparse_file_falls_back_to_apparent_size(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "sparse.bin")
            with open(path, "wb") as handle:
                handle.truncate(50000)
            total, _, _ = gd.tree_bytes(tmp, time.monotonic() + 5)
        self.assertEqual(total, 50000)

    def test_missing_directory(self):
        total, newest, _ = gd.tree_bytes("/nonexistent/path", time.monotonic() + 5)
        self.assertEqual(total, 0)
        self.assertEqual(newest, 0.0)


class SnapshotAccounting(unittest.TestCase):
    """The two-phase rule: staging size is the only valid denominator."""

    def test_total_is_staging_size_not_the_sum(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_steam_root(tmp)
            steamapps = os.path.join(root, "steamapps")
            write_manifest(steamapps, 2842040, "Star Wars Outlaws",
                           flags=1026, to_download=64110825056, to_stage=73089514868)
            download = os.path.join(steamapps, "downloading", "2842040")
            os.makedirs(download)
            with open(os.path.join(download, "data.bin"), "wb") as handle:
                handle.write(b"y" * 1000000)
            entries = gd.snapshot([root], time.monotonic() + 5)

        self.assertEqual(len(entries), 1)
        entry = entries[0]
        # Not 64110825056 + 73089514868: the compressed transfer size and the
        # uncompressed staging size are the same bytes at two compressions.
        self.assertEqual(entry["total"], 73089514868)
        self.assertGreaterEqual(entry["downloaded"], 1000000)
        self.assertLess(entry["downloaded"] - 1000000, 4096 * 16)

    def test_falls_back_to_download_size_when_no_staging(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_steam_root(tmp)
            steamapps = os.path.join(root, "steamapps")
            write_manifest(steamapps, 5, "No Staging", flags=1026,
                           to_download=500, to_stage=0)
            os.makedirs(os.path.join(steamapps, "downloading", "5"))
            entries = gd.snapshot([root], time.monotonic() + 5)
        self.assertEqual(entries[0]["total"], 500)


class SteamEndToEnd(unittest.TestCase):
    def test_reports_active_download(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_steam_root(tmp)
            steamapps = os.path.join(root, "steamapps")
            write_manifest(steamapps, 111, "Active Game",
                           flags=1026, to_download=2000, to_stage=4000)
            download = os.path.join(steamapps, "downloading", "111")
            os.makedirs(download)
            with open(os.path.join(download, "part.bin"), "wb") as handle:
                handle.write(b"z" * 1000)
            rows = run_helper(tmp)

        games, meta = split_rows(rows)
        active = [r for r in games if not r.get("queued")]
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["name"], "Active Game")
        self.assertEqual(active[0]["source"], "steam")
        self.assertEqual(active[0]["total"], 4000)
        self.assertGreaterEqual(active[0]["downloaded"], 1000)
        self.assertLess(active[0]["downloaded"] - 1000, 4096 * 16)
        self.assertEqual(meta["downloads"], 1)
        self.assertTrue(meta["sources"]["steam"]["installed"])

    def test_paused_download_is_dropped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_steam_root(tmp)
            steamapps = os.path.join(root, "steamapps")
            write_manifest(steamapps, 222, "Paused Game",
                           flags=1026, to_download=2000, to_stage=4000)
            download = os.path.join(steamapps, "downloading", "222")
            os.makedirs(download)
            blob = os.path.join(download, "part.bin")
            with open(blob, "wb") as handle:
                handle.write(b"z" * 1000)
            stale = time.time() - (gd.SETTLE_SECONDS + 120)
            os.utime(blob, (stale, stale))
            rows = run_helper(tmp)

        games, meta = split_rows(rows)
        active = [r for r in games if not r.get("queued")]
        self.assertEqual(active, [])
        self.assertEqual(meta["downloads"], 0)

    def test_malformed_sibling_does_not_break_a_valid_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_steam_root(tmp)
            steamapps = os.path.join(root, "steamapps")
            with open(os.path.join(steamapps, "appmanifest_999.acf"), "w") as handle:
                handle.write("garbage {{{")
            write_manifest(steamapps, 333, "Good Game",
                           flags=1026, to_download=1000, to_stage=2000)
            download = os.path.join(steamapps, "downloading", "333")
            os.makedirs(download)
            with open(os.path.join(download, "p.bin"), "wb") as handle:
                handle.write(b"q" * 500)
            rows = run_helper(tmp)

        games, _ = split_rows(rows)
        names = [r.get("name") for r in games if not r.get("queued")]
        self.assertEqual(names, ["Good Game"])

    def test_no_steam_installed(self):
        with tempfile.TemporaryDirectory() as tmp:
            rows = run_helper(tmp)
        _, meta = split_rows(rows)
        self.assertFalse(meta["sources"]["steam"]["installed"])
        self.assertEqual(meta["downloads"], 0)
        self.assertIn("No supported game launchers", meta["note"])

    def test_queued_download_is_opt_in(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_steam_root(tmp)
            steamapps = os.path.join(root, "steamapps")
            # Uninstalled flag, update not started, no staging directory.
            write_manifest(steamapps, 444, "Queued Game", flags=2,
                           to_download=1000, to_stage=2000)
            rows = run_helper(tmp)
            games, _ = split_rows(rows)
            self.assertEqual([r for r in games if not r.get("queued")], [])

            queued_rows = run_helper(tmp, "--include-queued")
            queued_games, _ = split_rows(queued_rows)
            self.assertEqual(len(queued_games), 1)
            self.assertTrue(queued_games[0]["queued"])
            self.assertEqual(queued_games[0]["downloaded"], 0)


class HeroicParsing(unittest.TestCase):
    def test_active_queue_and_log_progress(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_home(tmp)
            write_heroic_queue(tmp, [heroic_queue_item("Heroic Game")])
            write_heroic_log(tmp, [
                "(12:34:56) [INFO]: [Legendary]: Progress for Heroic Game: "
                "42.5%/1.2GB/00:05:00 Down: 12.3MB/s / Disk: 45.6MB/s",
            ])
            rows = run_helper(tmp, proc_override="heroic")

        games, meta = split_rows(rows)
        self.assertEqual(len(games), 1)
        row = games[0]
        self.assertEqual(row["source"], "heroic")
        self.assertEqual(row["name"], "Heroic Game")
        self.assertEqual(row["runnerName"], "Epic")
        self.assertAlmostEqual(row["percent"], 42.5, places=1)
        self.assertGreater(row["rate"], 0)
        self.assertEqual(row["eta"], 300)
        self.assertFalse(row["queued"])
        self.assertEqual(meta["sources"]["heroic"]["active"], 1)

    def test_queue_without_launcher_is_queued(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_home(tmp)
            write_heroic_queue(tmp, [heroic_queue_item("Heroic Game")])
            rows = run_helper(tmp, "--include-queued")

        games, meta = split_rows(rows)
        self.assertEqual(len(games), 1)
        self.assertTrue(games[0]["queued"])
        self.assertEqual(meta["sources"]["heroic"]["active"], 0)

    def test_queued_rows_are_opt_in(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_home(tmp)
            write_heroic_queue(tmp, [
                heroic_queue_item("First"),
                heroic_queue_item("Second"),
            ])
            games, _ = split_rows(run_helper(tmp))
            self.assertEqual(games, [])
            queued_games, _ = split_rows(run_helper(tmp, "--include-queued"))
            self.assertEqual(len(queued_games), 2)

    def test_malformed_queue_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_home(tmp)
            path = os.path.join(tmp, ".config", "heroic", "store",
                                "download-manager.json")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("{not json")
            games, meta = split_rows(run_helper(tmp))
            self.assertEqual(games, [])
            self.assertTrue(meta["sources"]["heroic"]["installed"])


class EpicParsing(unittest.TestCase):
    def test_active_operation_with_chunk_cache_and_title(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_home(tmp)
            game_dir = os.path.join(tmp, "EpicGame")
            cache = os.path.join(game_dir, ".egstore", "chunk_cache")
            os.makedirs(cache)
            with open(os.path.join(cache, "chunk.bin"), "wb") as handle:
                handle.write(b"x" * 25000)
            write_heroic_library(tmp, {"abc123": "Epic Game"})
            write_epic_queue(tmp, [epic_operation(
                app_name="abc123", title_folder="Folder Name",
                install_path=game_dir, download_bytes=100000)])
            rows = run_helper(tmp, proc_override="epic")

        games, meta = split_rows(rows)
        self.assertEqual(len(games), 1)
        row = games[0]
        self.assertEqual(row["source"], "epic")
        self.assertEqual(row["name"], "Epic Game")
        self.assertEqual(row["total"], 100000)
        self.assertGreaterEqual(row["downloaded"], 25000)
        self.assertGreater(row["percent"], 20)
        self.assertFalse(row["queued"])
        self.assertEqual(meta["sources"]["epic"]["active"], 1)

    def test_official_catalog_cache_resolves_title_and_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_home(tmp)
            game_dir = os.path.join(tmp, "EpicGame")
            write_epic_catalog(tmp, "deadbeef", "Official Title", "OpaqueFolder")
            write_epic_queue(tmp, [epic_operation(
                app_name="deadbeef", title_folder="OpaqueFolder",
                install_path=game_dir)])
            rows = run_helper(tmp, proc_override="epic")

        games, _ = split_rows(rows)
        self.assertEqual(games[0]["name"], "Official Title")

    def test_folder_name_is_used_when_no_catalog_title(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_home(tmp)
            game_dir = os.path.join(tmp, "EpicGame")
            write_epic_queue(tmp, [epic_operation(
                app_name="deadbeef", title_folder="Folder Name",
                install_path=game_dir)])
            rows = run_helper(tmp, proc_override="epic")

        games, _ = split_rows(rows)
        self.assertEqual(games[0]["name"], "Folder Name")

    def test_operation_without_launcher_is_queued(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_home(tmp)
            write_epic_queue(tmp, [epic_operation()])
            rows = run_helper(tmp, "--include-queued")

        games, meta = split_rows(rows)
        self.assertTrue(games[0]["queued"])
        self.assertEqual(meta["sources"]["epic"]["active"], 0)

    def test_terminal_and_uninstall_operations_are_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_home(tmp)
            write_epic_queue(tmp, [
                epic_operation(app_name="done", state="Completed"),
                epic_operation(app_name="gone", state="Queued", title_folder="Uninstall"),
            ])
            # Mark the second as an uninstall by changing its kind.
            path = os.path.join(tmp, ".local", "share", "com.epicgames.eglv2",
                                "queue.json")
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
            data["operations"][1]["kind"] = "uninstall"
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(data, handle)
            rows = run_helper(tmp, "--include-queued", proc_override="epic")

        games, _ = split_rows(rows)
        self.assertEqual(games, [])

    def test_malformed_queue_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_home(tmp)
            path = os.path.join(tmp, ".local", "share", "com.epicgames.eglv2",
                                "queue.json")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("{not json")
            games, meta = split_rows(run_helper(tmp))
            self.assertEqual(games, [])
            self.assertTrue(meta["sources"]["epic"]["installed"])


class MultiSource(unittest.TestCase):
    def test_meta_lists_every_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_home(tmp)
            make_steam_root(tmp)
            write_epic_queue(tmp, [])
            rows = run_helper(tmp)
        _, meta = split_rows(rows)
        self.assertIn("steam", meta["sources"])
        self.assertIn("heroic", meta["sources"])
        self.assertIn("epic", meta["sources"])
        self.assertGreaterEqual(meta["freeBytes"], 0)

    def test_no_launchers_at_all(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_home(tmp)
            rows = run_helper(tmp)
        _, meta = split_rows(rows)
        self.assertFalse(meta["running"])
        self.assertEqual(meta["downloads"], 0)
        self.assertEqual(meta["note"], "No supported game launchers found")


class QmlContract(unittest.TestCase):
    def test_service_owns_the_poller(self):
        root = Path(__file__).resolve().parents[1]
        service = (root / "Service.qml").read_text(encoding="utf-8")
        widget = (root / "BarWidget.qml").read_text(encoding="utf-8")
        self.assertIn("Process {", service)
        self.assertIn("game-downloads.py", service)
        self.assertIn("bar.shell.serviceFor(root.moduleName)", widget)
        self.assertIn("IpcHandler", service)
        self.assertNotIn("IpcHandler", widget)
        self.assertIn("registerWidget", widget)
        self.assertNotIn("Process {", widget)
        self.assertNotIn("pollTimer", widget)
        self.assertIn("game-downloads", service)


if __name__ == "__main__":
    unittest.main()
