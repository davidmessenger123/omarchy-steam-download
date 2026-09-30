"""Unit tests for the Steam Download helper.

These cover the parsing and measurement rules that the bar widget depends on,
with particular attention to the two-phase accounting: the compressed transfer
size and the uncompressed staging size describe the same bytes at different
compressions, so only the staging size may be used as the denominator.
"""

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HELPER = os.path.join(ROOT, "steam-downloads.py")

_spec = importlib.util.spec_from_file_location("steam_downloads", HELPER)
sd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sd)


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


class ParseManifest(unittest.TestCase):
    def test_reads_scalars(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_manifest(tmp, 2842040, "Star Wars Outlaws",
                                  flags=1026, to_download=64110825056,
                                  to_stage=73089514868)
            meta = sd.parse_manifest(path)
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
            self.assertIsNone(sd.parse_manifest(path))

    def test_missing_file(self):
        self.assertIsNone(sd.parse_manifest("/nonexistent/appmanifest_1.acf"))

    def test_non_numeric_sizes_default_to_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "appmanifest_7.acf")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write('"AppState"\n{\n\t"appid"\t\t"7"\n'
                             '\t"BytesToStage"\t\t"not-a-number"\n}\n')
            meta = sd.parse_manifest(path)
        self.assertEqual(meta["to_stage"], 0)


class LibraryDiscovery(unittest.TestCase):
    def test_finds_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_steam_root(tmp)
            saved = os.environ.get("HOME")
            os.environ["HOME"] = tmp
            try:
                self.assertEqual(sd.find_steam_root(), root)
            finally:
                if saved is None:
                    os.environ.pop("HOME", None)
                else:
                    os.environ["HOME"] = saved

    def test_libraryfolders_paths(self):
        text = '"libraryfolders"\n{\n\t"0"\n\t{\n\t\t"path"\t\t"/games/Steam"\n\t}\n}\n'
        self.assertEqual(sd.parse_vdf_paths(text), ["/games/Steam"])

    def test_missing_vdf_falls_back_to_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_steam_root(tmp)
            self.assertEqual(sd.library_roots(root), [root])

    def test_nonexistent_library_entries_are_dropped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_steam_root(tmp)
            with open(os.path.join(root, "steamapps", "libraryfolders.vdf"),
                      "w", encoding="utf-8") as handle:
                handle.write('"libraryfolders"\n{\n\t"0"\n\t{\n'
                             '\t\t"path"\t\t"/definitely/not/here"\n\t}\n}\n')
            self.assertEqual(sd.library_roots(root), [root])


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
            total, newest, truncated = sd.tree_bytes(tmp, time.monotonic() + 5)
        self.assertCloseToBytes(total, 100000)
        self.assertFalse(truncated)
        self.assertGreater(newest, 0)

    def test_rounds_up_to_whole_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "tiny.bin"), "wb") as handle:
                handle.write(b"x" * 1)
            total, _, _ = sd.tree_bytes(tmp, time.monotonic() + 5)
        self.assertEqual(total, self.BLOCK)

    def test_sparse_file_falls_back_to_apparent_size(self):
        # A sparse file reports zero allocated blocks until it is written.
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "sparse.bin")
            with open(path, "wb") as handle:
                handle.truncate(50000)
            total, _, _ = sd.tree_bytes(tmp, time.monotonic() + 5)
        self.assertEqual(total, 50000)

    def test_missing_directory(self):
        total, newest, _ = sd.tree_bytes("/nonexistent/path", time.monotonic() + 5)
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
            entries = sd.snapshot([root], time.monotonic() + 5)

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
            entries = sd.snapshot([root], time.monotonic() + 5)
        self.assertEqual(entries[0]["total"], 500)


class EndToEnd(unittest.TestCase):
    def run_helper(self, home):
        env = {"HOME": home, "PATH": os.environ.get("PATH", "")}
        out = subprocess.run([sys.executable, "-E", HELPER],
                             capture_output=True, text=True, env=env, timeout=30)
        rows = [json.loads(line) for line in out.stdout.splitlines() if line.strip()]
        return rows

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
            rows = self.run_helper(tmp)

        games = [r for r in rows if "appid" in r]
        meta = [r for r in rows if "__meta" in r][0]["__meta"]
        self.assertEqual(len(games), 1)
        self.assertEqual(games[0]["name"], "Active Game")
        self.assertEqual(games[0]["total"], 4000)
        self.assertGreaterEqual(games[0]["downloaded"], 1000)
        self.assertLess(games[0]["downloaded"] - 1000, 4096 * 16)
        self.assertEqual(meta["downloads"], 1)

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
            stale = time.time() - (sd.SETTLE_SECONDS + 120)
            os.utime(blob, (stale, stale))
            rows = self.run_helper(tmp)

        games = [r for r in rows if "appid" in r]
        meta = [r for r in rows if "__meta" in r][0]["__meta"]
        self.assertEqual(games, [])
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
            rows = self.run_helper(tmp)

        names = [r.get("name") for r in rows if "appid" in r]
        self.assertEqual(names, ["Good Game"])

    def test_no_steam_installed(self):
        with tempfile.TemporaryDirectory() as tmp:
            rows = self.run_helper(tmp)
        meta = [r for r in rows if "__meta" in r][0]["__meta"]
        self.assertFalse(meta["running"])
        self.assertEqual(meta["downloads"], 0)
        self.assertIn("not installed", meta["note"])

    def test_queued_download_is_opt_in(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_steam_root(tmp)
            steamapps = os.path.join(root, "steamapps")
            # Uninstalled flag, update not started, no staging directory.
            write_manifest(steamapps, 444, "Queued Game", flags=2,
                           to_download=1000, to_stage=2000)
            rows = self.run_helper(tmp)
            self.assertEqual([r for r in rows if "appid" in r], [])

            env = {"HOME": tmp, "PATH": os.environ.get("PATH", "")}
            out = subprocess.run([sys.executable, "-E", HELPER, "--include-queued"],
                                 capture_output=True, text=True, env=env, timeout=30)
            queued = [json.loads(l) for l in out.stdout.splitlines() if l.strip()]
            games = [r for r in queued if "appid" in r]
            self.assertEqual(len(games), 1)
            self.assertTrue(games[0]["queued"])
            self.assertEqual(games[0]["downloaded"], 0)


if __name__ == "__main__":
    unittest.main()
