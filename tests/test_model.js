// Unit tests for the Game Downloads bar widget's pure helpers.
//
// Run with: node tests/test_model.js

const assert = require("assert");
const path = require("path");

const M = require(path.join(__dirname, "..", "Model.js"));

let passed = 0;
let failed = 0;

function test(name, fn) {
  try {
    fn();
    passed++;
    console.log("  ok   " + name);
  } catch (error) {
    failed++;
    console.log("  FAIL " + name);
    console.log("       " + error.message);
  }
}

function group(name) {
  console.log("\n" + name);
}

group("byte formatting");

test("zero and sub-kilobyte", () => {
  assert.strictEqual(M.formatBytes(0), "0 B");
  assert.strictEqual(M.formatBytes(999), "999 B");
});

test("thousands scale, one decimal below ten", () => {
  assert.strictEqual(M.formatBytes(1500), "1.5 kB");
  assert.strictEqual(M.formatBytes(12500000), "13 MB");
});

test("large downloads", () => {
  assert.strictEqual(M.formatBytes(73089514868), "73 GB");
  assert.strictEqual(M.formatBytes(137200339924), "137 GB");
});

group("rate formatting");

test("matches Steam's Mbps presentation", () => {
  assert.strictEqual(M.formatRate(41234567), "330 Mbps");
  assert.strictEqual(M.formatRate(1000000), "8.0 Mbps");
  assert.strictEqual(M.formatRate(0), "");
});

group("eta formatting");

test("seconds, minutes, hours and days", () => {
  assert.strictEqual(M.formatEta(30), "under a minute left");
  assert.strictEqual(M.formatEta(60), "about 1 minute left");
  assert.strictEqual(M.formatEta(300), "about 5 minutes left");
  assert.strictEqual(M.formatEta(7200), "about 2 hours left");
  assert.strictEqual(M.formatEta(9000), "about 2h 30m left");
  assert.strictEqual(M.formatEta(0), "");
});

group("source labels");

test("human labels and glyphs", () => {
  assert.strictEqual(M.sourceLabel("steam"), "Steam");
  assert.strictEqual(M.sourceLabel("heroic"), "Heroic");
  assert.strictEqual(M.sourceLabel("epic"), "Epic Games");
  assert.strictEqual(M.runnerLabel("legendary"), "Epic");
  assert.strictEqual(M.runnerLabel("gog"), "GOG");
  assert.strictEqual(M.sourceGlyph("steam"), M.GLYPH_STEAM);
  assert.strictEqual(M.sourceGlyph("heroic"), M.GLYPH_GAME);
});

group("two-phase accounting");

// Steam runs a compressed download and an uncompressed staging phase at once.
// The manifest records both sizes and they describe the same bytes at
// different compressions, so only the staging size is a valid denominator.
test("total is never the sum of both phases", () => {
  const row = { downloaded: 500, total: 1000, staged: 250 };
  assert.strictEqual(M.totalBytes(row), 1000);
  assert.strictEqual(M.doneBytes(row), 500);
  assert.strictEqual(M.fraction(row), 0.5);
  assert.strictEqual(M.percent(row), 50);
});

test("a stale staged field cannot inflate progress", () => {
  const row = { downloaded: 500, total: 1000, staged: 250 };
  assert.strictEqual(M.doneBytes(row), M.toNumber(row.downloaded));
});

test("real world figures reproduce Steam's percentage", () => {
  const row = { downloaded: 70400000000, total: 73089514868 };
  assert.strictEqual(M.percent(row), 96);
});

group("fraction guards");

test("zero total never divides by zero", () => {
  assert.strictEqual(M.fraction({ downloaded: 5, total: 0 }), 0);
  assert.strictEqual(M.fraction({}), 0);
  assert.strictEqual(M.fraction(null), 0);
});

test("progress clamps to 100%", () => {
  assert.strictEqual(M.fraction({ downloaded: 200, total: 100 }), 1);
  assert.strictEqual(M.percent({ downloaded: 200, total: 100 }), 100);
});

test("negative values are floored at zero", () => {
  assert.strictEqual(M.fraction({ downloaded: -5, total: 100 }), 0);
});

group("rate derivation");

test("no previous sample on the first poll", () => {
  assert.strictEqual(M.rateFor({ downloaded: 100 }, null, 1000), 0);
});

test("growth over the poll window", () => {
  assert.strictEqual(M.rateFor({ downloaded: 200 }, { downloaded: 100, at: 998 }, 1000), 50);
});

test("a shrinking tree yields no rate", () => {
  assert.strictEqual(M.rateFor({ downloaded: 50 }, { downloaded: 100, at: 998 }, 1000), 0);
});

test("stale or impossible gaps are ignored", () => {
  assert.strictEqual(M.rateFor({ downloaded: 200 }, { downloaded: 100, at: 999.9 }, 1000), 0);
  assert.strictEqual(M.rateFor({ downloaded: 200 }, { downloaded: 100, at: 500 }, 1000), 0);
});

group("eta");

test("remaining divided by rate", () => {
  assert.strictEqual(M.etaFor({ downloaded: 0, total: 1000 }, 100), 10);
});

test("no rate means no estimate", () => {
  assert.strictEqual(M.etaFor({ downloaded: 0, total: 1000 }, 0), 0);
});

test("finished downloads have no eta", () => {
  assert.strictEqual(M.etaFor({ downloaded: 1000, total: 1000 }, 100), 0);
});

group("helper utilities");

test("clampInt bounds and fallback", () => {
  assert.strictEqual(M.clampInt(5, 1, 60), 5);
  assert.strictEqual(M.clampInt(0, 1, 60), 1);
  assert.strictEqual(M.clampInt(999, 1, 60), 60);
  assert.strictEqual(M.clampInt("nonsense", 3, 60), 3);
});

test("indexByKey uses stable source/app keys", () => {
  const index = M.indexByKey([
    { source: "steam", appid: 1 },
    { key: "heroic:legendary:abc", source: "heroic", appid: "abc" }
  ]);
  assert.strictEqual(index["steam:1"].appid, 1);
  assert.strictEqual(index["heroic:legendary:abc"].appid, "abc");
});

test("scriptPath strips the file:// prefix", () => {
  assert.strictEqual(M.scriptPath("file:///tmp/a.py"), "/tmp/a.py");
  assert.strictEqual(M.scriptPath("/tmp/a.py"), "/tmp/a.py");
});

group("helper stdout parsing");

function row(appid, name, downloaded, total, extra) {
  return JSON.stringify(Object.assign({
    key: "steam:" + appid,
    source: "steam", sourceName: "Steam",
    appid: appid, name: name, downloaded: downloaded, total: total,
    library: "/steam", idle: 0
  }, extra || {}));
}

function heroicRow(appid, name, downloaded, total, extra) {
  return JSON.stringify(Object.assign({
    key: "heroic:legendary:" + appid,
    source: "heroic", sourceName: "Heroic",
    runner: "legendary", runnerName: "Epic",
    appid: appid, name: name, downloaded: downloaded, total: total,
    library: "/heroic"
  }, extra || {}));
}

const meta = JSON.stringify({
  __meta: {
    running: true, downloads: 1, libraries: 2, note: "",
    sources: {
      steam: { installed: true, running: true, active: 1 },
      heroic: { installed: true, running: false, active: 0 },
      epic: { installed: false, running: false, active: 0 }
    }
  }
});

test("parses rows and meta", () => {
  const parsed = M.parse(row(2842040, "Star Wars Outlaws", 100, 200) + "\n" + meta);
  assert.strictEqual(parsed.rows.length, 1);
  assert.strictEqual(parsed.rows[0].name, "Star Wars Outlaws");
  assert.strictEqual(parsed.meta.running, true);
  assert.strictEqual(parsed.meta.sources.steam.installed, true);
});

test("tolerates blank and malformed lines", () => {
  const parsed = M.parse("\n\nnot json\n" + meta + "\n");
  assert.strictEqual(parsed.rows.length, 0);
  assert.strictEqual(parsed.meta.downloads, 1);
});

test("empty input", () => {
  const parsed = M.parse("");
  assert.strictEqual(parsed.rows.length, 0);
  assert.deepStrictEqual(parsed.meta, {});
});

test("parses explicit Heroic percentage and runner", () => {
  const parsed = M.parse(heroicRow("abc", "Heroic Game", 100, 200, {
    percent: 42.5, rate: 1000000, eta: 300
  }) + "\n" + meta);
  const state = M.derive(parsed.rows, parsed.meta, {}, 1000);
  assert.strictEqual(state.primary.sourceName, "Heroic");
  assert.strictEqual(state.primary.runnerName, "Epic");
  assert.strictEqual(state.primary.percent, 42);
  assert.strictEqual(state.primary.fraction, 0.425);
});

group("widget state");

test("idle when nothing is downloading", () => {
  const parsed = M.parse(meta);
  const state = M.derive(parsed.rows, parsed.meta, {}, 1000);
  assert.strictEqual(state.state, "idle");
  assert.strictEqual(M.pillText(state.primary), "");
});

test("stopped when no launchers are running", () => {
  const parsed = M.parse(JSON.stringify({
    __meta: {
      running: false, downloads: 0, note: "No game launchers running",
      sources: { steam: { installed: true, running: false, active: 0 } }
    }
  }));
  const state = M.derive(parsed.rows, parsed.meta, {}, 1000);
  assert.strictEqual(state.state, "stopped");
  assert.strictEqual(state.status, "No game launchers running");
});

test("a helper timeout keeps its explanatory note", () => {
  const parsed = M.parse(JSON.stringify({
    __meta: { running: false, downloads: 0, note: "Timed out reading game downloads" }
  }));
  const state = M.derive(parsed.rows, parsed.meta, {}, 1000);
  assert.strictEqual(state.state, "stopped");
  assert.strictEqual(state.status, "Timed out reading game downloads");
});

test("downloading with a percentage pill", () => {
  const parsed = M.parse(row(1, "Game", 50, 100) + "\n" + meta);
  const state = M.derive(parsed.rows, parsed.meta, {}, 1000);
  assert.strictEqual(state.state, "downloading");
  assert.strictEqual(M.pillText(state.primary), "50%");
  assert.strictEqual(state.status, "Game · 50%");
});

test("the pill reports the download closest to finishing", () => {
  const parsed = M.parse([
    row(1, "AlmostDone", 90, 100),
    row(2, "JustStarted", 5, 100),
    meta
  ].join("\n"));
  const state = M.derive(parsed.rows, parsed.meta, {}, 1000);
  assert.strictEqual(state.primary.name, "AlmostDone");
  assert.strictEqual(state.active.length, 2);
});

test("queued downloads are listed but never active", () => {
  const parsed = M.parse(row(9, "Queued", 0, 500, { queued: true }) + "\n" + meta);
  const state = M.derive(parsed.rows, parsed.meta, {}, 1000);
  assert.strictEqual(state.active.length, 0);
  const rows = M.buildRows(state.rows);
  assert.strictEqual(rows[0].kind, "queued");
  assert.strictEqual(rows[0].subtitle, "Queued in Steam");
  assert.strictEqual(M.pillText(state.primary), "");
});

test("indeterminate download shows the icon without a number", () => {
  const parsed = M.parse(JSON.stringify({
    key: "epic:abc", source: "epic", sourceName: "Epic Games",
    appid: "abc", name: "Epic Game", downloaded: 0, total: 0,
    queued: false, status: "installing", indeterminate: true
  }) + "\n" + meta);
  const state = M.derive(parsed.rows, parsed.meta, {}, 1000);
  assert.strictEqual(state.primary.indeterminate, true);
  assert.strictEqual(M.pillText(state.primary), "");
  assert.strictEqual(state.status, "Epic Game · Installing…");
  const rowData = M.buildRows(state.active)[0];
  assert.ok(/Epic Games · Installing/.test(rowData.subtitle), rowData.subtitle);
});

test("popup subtitle reports source, bytes, rate and eta", () => {
  // Two polls four seconds apart with 100 MB of growth: a 25 MB/s rate, which
  // is what makes the download glyph, the throughput and the eta appear.
  const before = M.parse(row(1, "Game", 20000000000, 137200339924) + "\n" + meta);
  const first = M.derive(before.rows, before.meta, {}, 1000);
  const after = M.parse(row(1, "Game", 20100000000, 137200339924) + "\n" + meta);
  const second = M.derive(after.rows, after.meta, M.indexByKey(first.rows), 1004);

  assert.strictEqual(second.primary.rate, 25000000);
  const rowData = M.buildRows(second.active)[0];
  assert.strictEqual(rowData.percent, 14);
  assert.strictEqual(rowData.glyph, M.GLYPH_DOWNLOAD);
  assert.ok(/^Steam · 20 GB of 137 GB/.test(rowData.subtitle), rowData.subtitle);
  assert.ok(/Mbps/.test(rowData.subtitle), rowData.subtitle);
  assert.ok(/left$/.test(rowData.subtitle), rowData.subtitle);
});

test("an idle download shows the paused glyph and omits the rate", () => {
  const parsed = M.parse(row(1, "Game", 50, 100) + "\n" + meta);
  const state = M.derive(parsed.rows, parsed.meta, {}, 1000);
  const rowData = M.buildRows(state.active)[0];
  assert.strictEqual(rowData.glyph, M.GLYPH_PAUSED);
  assert.ok(!/Mbps/.test(rowData.subtitle), rowData.subtitle);
});

test("multiple sources stay distinct in the active list", () => {
  const parsed = M.parse([
    row(1, "Steam Game", 50, 100),
    heroicRow("abc", "Heroic Game", 25, 100),
    meta
  ].join("\n"));
  const state = M.derive(parsed.rows, parsed.meta, {}, 1000);
  assert.strictEqual(state.active.length, 2);
  const rows = M.buildRows(state.active);
  assert.ok(/Steam/.test(rows[0].subtitle) || /Steam/.test(rows[1].subtitle));
  assert.ok(/Heroic · Epic/.test(rows[0].subtitle) || /Heroic · Epic/.test(rows[1].subtitle));
});

group("download notifications");

test("a nearly complete row reports finished", () => {
  const notices = M.downloadNotifications(
    "downloading",
    [{ source: "steam", sourceName: "Steam", appid: 1, name: "Game", total: 1000, downloaded: 1000, percent: 100 }],
    "idle", [], { sources: { steam: { running: true } } }
  );
  assert.strictEqual(notices.length, 1);
  assert.strictEqual(notices[0].title, "Steam download finished");
  assert.strictEqual(notices[0].body, "Game finished");
  assert.strictEqual(notices[0].finished, true);
});

test("an unfinished row reports stopped at its last percentage", () => {
  const notices = M.downloadNotifications(
    "downloading",
    [{ source: "steam", sourceName: "Steam", appid: 1, name: "Game", total: 1000, downloaded: 400, percent: 40 }],
    "idle", [], { sources: { steam: { running: true } } }
  );
  assert.strictEqual(notices[0].title, "Steam download stopped");
  assert.strictEqual(notices[0].body, "Game stopped at 40%");
});

test("a closed launcher is reported as interrupted", () => {
  const notices = M.downloadNotifications(
    "downloading",
    [{ source: "steam", sourceName: "Steam", appid: 1, name: "Game", total: 1000, downloaded: 400, percent: 40 }],
    "stopped", [], { sources: { steam: { running: false } } }
  );
  assert.strictEqual(notices[0].title, "Steam closed during a download");
});

test("each source gets its own notice", () => {
  const notices = M.downloadNotifications(
    "downloading",
    [
      { source: "steam", sourceName: "Steam", appid: 1, name: "Steam Game", total: 1000, downloaded: 1000, percent: 100 },
      { source: "heroic", sourceName: "Heroic", appid: "abc", name: "Heroic Game", total: 0, downloaded: 0, percent: 0 }
    ],
    "idle", [],
    { sources: { steam: { running: true }, heroic: { running: true } } }
  );
  assert.strictEqual(notices.length, 2);
  assert.strictEqual(notices[0].source, "steam");
  assert.strictEqual(notices[1].source, "heroic");
});

test("the first sample and continuing downloads stay quiet", () => {
  assert.deepStrictEqual(M.downloadNotifications("idle", [], "downloading", [{}]), []);
  assert.deepStrictEqual(M.downloadNotifications(
    "downloading",
    [{ source: "steam", sourceName: "Steam", appid: 1, name: "Game", total: 1000, downloaded: 400, percent: 40 }],
    "downloading",
    [{ source: "steam", sourceName: "Steam", appid: 1 }],
    { sources: { steam: { running: true } } }
  ), []);
});

console.log("\n" + passed + " passed, " + failed + " failed");
process.exit(failed === 0 ? 0 : 1);
