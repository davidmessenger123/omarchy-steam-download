// Pure helper functions for the Game Downloads bar widget. No Qt/QML
// dependencies so the logic stays unit-testable outside the shell.

var GLYPH_DOWNLOAD = "\uf019"   // nf-fa-arrow_circle_down
var GLYPH_PAUSED = "\uf04b"     // nf-fa-pause_circle
var GLYPH_QUEUED = "\uf017"     // nf-fa-clock_o
var GLYPH_STEAM = "\uf6a8"      // nf-br-steam
var GLYPH_GAME = "\uf11b"       // nf-fa-gamepad
var GLYPH_ERROR = "\uf071"      // nf-fa-exclamation_triangle

var SOURCE_LABELS = {
  steam: "Steam",
  heroic: "Heroic",
  epic: "Epic Games"
}

var RUNNER_LABELS = {
  legendary: "Epic",
  gog: "GOG",
  nile: "Amazon",
  zoom: "Zoom",
  sideload: "Sideload"
}

// Total uncompressed on-disk size Steam reported for this download, which is
// what its staging tree fills and therefore what its "Installing files"
// percentage is measured against. The compressed transfer size
// (BytesToDownload) is deliberately NOT included: it describes the same
// bytes at a different compression, so adding the two roughly halves the
// progress Steam shows.
function totalBytes(row) {
  if (!row) return 0
  return Math.max(0, toNumber(row.total))
}

function toNumber(value) {
  var n = Number(value)
  return isFinite(n) ? n : 0
}

function clampInt(value, min, max) {
  var n = parseInt(String(value), 10)
  if (isNaN(n)) return min
  return Math.max(min, Math.min(max, n))
}

function sourceLabel(source) {
  return SOURCE_LABELS[String(source || "").toLowerCase()] || "Game launcher"
}

function runnerLabel(runner) {
  return RUNNER_LABELS[String(runner || "").toLowerCase()] || ""
}

function sourceGlyph(source) {
  if (String(source || "").toLowerCase() === "steam") return GLYPH_STEAM
  return GLYPH_GAME
}

// Stable key for the previous-poll map. The helper emits `key`; the fallback
// keeps older rows and tests working.
function rowKey(row) {
  if (!row) return ""
  if (row.key) return String(row.key)
  var source = String(row.source || "steam")
  var id = row.appid !== undefined ? row.appid
    : (row.appid === 0 ? 0 : (row.appName || row.name || "unknown"))
  return source + ":" + id
}

function indexByKey(rows) {
  var out = {}
  for (var i = 0; i < (rows || []).length; i++) out[rowKey(rows[i])] = rows[i]
  return out
}

// Backwards-compatible alias used by older tests and callers.
function indexByAppid(rows) {
  return indexByKey(rows)
}

// Bytes on disk, as measured from the staging tree. This is already the
// decompressed size, so nothing is added to it: the manifest's BytesStaged
// is both a double count and stale.
function doneBytes(row) {
  if (!row) return 0
  return Math.max(0, toNumber(row.downloaded))
}

// Fraction complete from measured bytes, clamped to [0, 1]. A total of 0 is
// reported as 0 rather than NaN; callers that have an explicit percentage can
// use it instead (Heroic's progress log provides one).
function fraction(row) {
  var total = totalBytes(row)
  if (total <= 0) return 0
  var f = doneBytes(row) / total
  if (!isFinite(f) || f < 0) return 0
  return Math.min(1, f)
}

// Whole percent, which is what the bar pill shows.
function percent(row) {
  return Math.floor(fraction(row) * 100)
}

// Bytes per second from two consecutive samples of the same app. Steam's
// manifests are only rewritten at download start, so the rate has to come
// from the growth of the staging tree between polls. Heroic supplies a rate
// directly and that wins in derive().
function rateFor(row, previous, nowSeconds) {
  if (!row || !previous) return 0
  var before = toNumber(previous.downloaded)
  var after = toNumber(row.downloaded)
  var elapsed = nowSeconds - toNumber(previous.at)
  if (elapsed <= 0.5 || elapsed > 120) return 0

  var delta = after - before
  if (delta <= 0) return 0
  return delta / elapsed
}

// Remaining time in seconds, or 0 when it cannot be estimated. Guards
// against a near-zero rate producing an absurd ETA.
function etaFor(row, rate) {
  if (!row || !rate) return 0
  var remaining = totalBytes(row) - doneBytes(row)
  if (remaining <= 0) return 0
  var seconds = remaining / rate
  if (!isFinite(seconds) || seconds <= 0) return 0
  return seconds
}

// Decimal units, matching how Steam sizes a download. Heroic's logged byte
// strings are binary MiB internally, but the difference is below one percent
// for the summary line.
function formatBytes(bytes) {
  var n = toNumber(bytes)
  if (n <= 0) return "0 B"
  if (n < 1000) return n + " B"
  var units = ["kB", "MB", "GB", "TB", "PB"]
  var value = n / 1000
  var i = 0
  while (value >= 1000 && i < units.length - 1) {
    value = value / 1000
    i++
  }
  return (value < 10 ? value.toFixed(1) : Math.round(value)) + " " + units[i]
}

// Download rate, matching Steam's own "Mbps" presentation.
function formatRate(bytesPerSecond) {
  var n = toNumber(bytesPerSecond)
  if (n <= 0) return ""
  var mbps = (n * 8) / 1000000
  if (mbps < 10) return mbps.toFixed(1) + " Mbps"
  return Math.round(mbps) + " Mbps"
}

function pad2(n) {
  return n < 10 ? "0" + n : String(n)
}

// Remaining time as Steam words it: "about 5 minutes left".
function formatEta(seconds) {
  var n = toNumber(seconds)
  if (n <= 0) return ""
  if (n < 60) return "under a minute left"
  var minutes = Math.round(n / 60)
  if (minutes < 60) return "about " + minutes + " minute" + (minutes === 1 ? "" : "s") + " left"
  var hours = Math.floor(minutes / 60)
  var rest = minutes % 60
  if (hours < 24) {
    if (rest === 0) return "about " + hours + " hour" + (hours === 1 ? "" : "s") + " left"
    return "about " + hours + "h " + rest + "m left"
  }
  var days = Math.round(hours / 24)
  return "about " + days + " day" + (days === 1 ? "" : "s") + " left"
}

function clockTime(date) {
  if (!date) return ""
  return pad2(date.getHours()) + ":" + pad2(date.getMinutes())
}

function phaseLabel(status) {
  var value = String(status || "").toLowerCase()
  if (value === "starting") return "Starting…"
  if (value === "installing" || value === "prereqs") return "Installing…"
  if (value === "verifying") return "Verifying…"
  if (value === "repairing") return "Repairing…"
  if (value === "updating") return "Updating…"
  if (value === "preloading") return "Preloading…"
  if (value === "paused") return "Paused"
  return ""
}

// Parse the helper's stdout into widget state.
//
// Raw lines, one JSON object each:
//   {"key":"steam:123","source":"steam","sourceName":"Steam","appid":123,
//    "name":"...","downloaded":N,"total":N,"library":"...","queued":false}
//   {"key":"heroic:legendary:abc","source":"heroic","runner":"legendary",
//    "appid":"abc","name":"...","downloaded":N,"total":N,"percent":42.3,
//    "rate":N,"eta":N,"queued":false}
//   {"__meta":{"running":true,"downloads":1,"sources":{...}}}
function parse(raw) {
  var lines = String(raw || "").split("\n")
  var rows = []
  var meta = {}
  for (var i = 0; i < lines.length; i++) {
    var line = lines[i].trim()
    if (line === "") continue
    var obj = null
    try { obj = JSON.parse(line) } catch (e) { continue }
    if (!obj) continue
    if (obj.__meta) { meta = obj.__meta; continue }
    if (obj.appid === undefined && obj.key === undefined) continue
    var source = String(obj.source || "steam")
    var appid = obj.appid !== undefined ? obj.appid : obj.appName
    var row = {
      key: rowKey({ key: obj.key, source: source, appid: appid, appName: obj.appName, name: obj.name }),
      source: source,
      sourceName: String(obj.sourceName || sourceLabel(source)),
      runner: String(obj.runner || ""),
      runnerName: String(obj.runnerName || runnerLabel(obj.runner)),
      appid: appid,
      name: String(obj.name || "Game download"),
      downloaded: toNumber(obj.downloaded),
      total: toNumber(obj.total),
      library: String(obj.library || ""),
      queued: obj.queued === true,
      status: String(obj.status || ""),
      indeterminate: obj.indeterminate === true
    }
    if (obj.percent !== undefined && obj.percent !== null && isFinite(Number(obj.percent))) {
      row.percent = Math.max(0, Math.min(100, Number(obj.percent)))
    }
    row.rate = toNumber(obj.rate)
    row.eta = toNumber(obj.eta)
    rows.push(row)
  }
  return { rows: rows, meta: meta }
}

// Widget-wide view of the parse, given the previous poll's rows so the rate
// can be derived from the growth of each staging tree.
//
// `previous` is a map of key -> row carrying the extra `at` timestamp the
// widget stamps on ingest, which is what makes the delta meaningful.
function derive(rows, meta, previous, nowSeconds) {
  var enriched = []
  for (var i = 0; i < (rows || []).length; i++) {
    var row = rows[i]
    var key = rowKey(row)
    var prior = previous ? previous[key] : null
    var rate = row.rate > 0 ? row.rate : rateFor(row, prior, nowSeconds)
    var indeterminate = row.indeterminate === true
    var fractionValue = 0
    var percentValue = 0
    if (row.percent !== undefined && row.percent !== null) {
      var rawPercent = Math.max(0, Math.min(100, toNumber(row.percent)))
      fractionValue = rawPercent / 100
      percentValue = Math.floor(rawPercent)
      indeterminate = false
    } else if (!indeterminate && totalBytes(row) > 0) {
      fractionValue = fraction(row)
      percentValue = percent(row)
    } else {
      indeterminate = true
    }
    var eta = row.eta > 0 ? row.eta : etaFor(row, rate)
    enriched.push({
      key: key,
      source: row.source,
      sourceName: row.sourceName || sourceLabel(row.source),
      runner: row.runner,
      runnerName: row.runnerName || runnerLabel(row.runner),
      appid: row.appid,
      name: row.name,
      downloaded: row.downloaded,
      total: row.total,
      library: row.library,
      queued: row.queued,
      status: row.status,
      indeterminate: indeterminate,
      at: nowSeconds,
      rate: rate,
      fraction: fractionValue,
      percent: percentValue,
      eta: eta
    })
  }

  // A single download is the common case and gets the bar pill to itself.
  // With several running, the bar shows the one closest to finishing so it
  // visibly ticks down, and the popup lists the rest. Indeterminate rows go
  // after measured ones so the pill keeps a real percentage when it can.
  var active = []
  for (var j = 0; j < enriched.length; j++) if (!enriched[j].queued) active.push(enriched[j])
  active.sort(function(a, b) {
    if (a.indeterminate !== b.indeterminate) return a.indeterminate ? 1 : -1
    return b.fraction - a.fraction
  })

  var note = String((meta && meta.note) || "")
  var running = meta ? meta.running === true : false
  var sources = (meta && meta.sources) || {}
  var installedCount = 0
  for (var source in sources) {
    if (sources[source] && sources[source].installed) installedCount++
  }

  var state, status
  if (active.length === 0) {
    if (installedCount === 0) {
      state = "stopped"
      status = "No supported game launchers found"
    } else if (!running && note !== "") {
      state = "stopped"
      status = note
    } else {
      state = "idle"
      status = "No active game downloads"
    }
  } else {
    state = "downloading"
    var primary = active[0]
    status = primary.name + " · " + (primary.indeterminate
      ? (phaseLabel(primary.status) || primary.sourceName + " download")
      : primary.percent + "%")
  }

  return {
    rows: enriched,
    active: active,
    primary: active.length > 0 ? active[0] : null,
    state: state,
    status: status,
    running: running,
    note: note,
    sources: sources,
    installed: installedCount > 0,
    libraries: meta ? toNumber(meta.libraries) : 0,
    freeBytes: meta ? toNumber(meta.freeBytes) : 0,
    totalBytes: meta ? toNumber(meta.totalBytes) : 0
  }
}

// Strip the file:// prefix off a Qt.resolvedUrl() result so it can be handed
// to a Process command as a plain filesystem path.
function scriptPath(url) {
  return String(url || "").replace(/^file:\/\//, "")
}

// Popup rows, one per download.
//   {kind:"download", name, subtitle, fraction, percent, glyph, color, queued}
function buildRows(active) {
  var rows = []
  for (var i = 0; i < (active || []).length; i++) {
    var item = active[i]
    var sourceText = item.sourceName || sourceLabel(item.source)
    if (item.source === "heroic" && item.runnerName) sourceText += " · " + item.runnerName

    if (item.queued) {
      var queuedText = item.status === "paused" ? "Paused in " : "Queued in "
      rows.push({
        kind: "queued",
        name: item.name,
        subtitle: queuedText + sourceText,
        fraction: 0,
        percent: 0,
        glyph: GLYPH_QUEUED,
        sourceGlyph: sourceGlyph(item.source),
        color: "#7aa2f7"
      })
      continue
    }

    // "Steam · 13.4 GB of 137.2 GB · 305 Mbps · about 32 minutes left", with
    // the parts that cannot be determined simply left out.
    var parts = [sourceText]
    var phase = phaseLabel(item.status)
    if (item.indeterminate) {
      parts.push(phase || "Starting…")
    } else if (item.total > 0) {
      parts.push(formatBytes(item.downloaded) + " of " + formatBytes(item.total))
      var rate = formatRate(item.rate)
      if (rate !== "") parts.push(rate)
      var eta = formatEta(item.eta)
      if (eta !== "") parts.push(eta)
      if (phase !== "") parts.push(phase)
    } else {
      parts.push(formatBytes(item.downloaded) + " downloaded")
      if (phase !== "") parts.push(phase)
    }

    rows.push({
      kind: "download",
      name: item.name,
      subtitle: parts.join(" · "),
      fraction: item.fraction,
      percent: item.percent,
      glyph: item.rate > 0 ? GLYPH_DOWNLOAD : GLYPH_PAUSED,
      sourceGlyph: sourceGlyph(item.source),
      color: item.rate > 0 ? "#9ece6a" : "#e0af68"
    })
  }
  return rows
}

// Bar pill label: the percentage while downloading, otherwise nothing. An
// indeterminate download keeps the pill icon but has no number to show yet.
function pillText(primary) {
  if (!primary || primary.queued || primary.indeterminate) return ""
  return primary.percent + "%"
}

function groupBySource(rows) {
  var out = {}
  for (var i = 0; i < (rows || []).length; i++) {
    var row = rows[i]
    var source = row.source || "steam"
    if (!out[source]) out[source] = []
    out[source].push(row)
  }
  return out
}

// Notify when a source's running download stops. Each launcher exposes no
// pause/complete signal, so a nearly-full row is treated as finished and
// anything else as stopped. The first sample has no previous active rows and
// stays quiet. Sources are reported separately so a finished Steam download
// cannot hide an interrupted Heroic one.
function downloadNotifications(previousState, previousActive, nextState, nextActive, meta) {
  if (!Array.isArray(previousActive) || previousActive.length === 0) return []
  var previousBySource = groupBySource(previousActive)
  var nextBySource = groupBySource(nextActive)
  var sources = (meta && meta.sources) || {}
  var notices = []

  for (var source in previousBySource) {
    var previousRows = previousBySource[source]
    var nextRows = nextBySource[source] || []
    if (nextRows.length > 0) continue

    var primary = previousRows[0]
    var count = previousRows.length
    var sourceName = primary.sourceName || sourceLabel(source)
    var sourceMeta = sources[source] || {}
    var finished = (primary.total > 0 && primary.downloaded >= primary.total * 0.995) ||
      primary.percent >= 99.5 || primary.status === "completed"
    var title = ""
    var body = ""
    if (sourceMeta.running === false) {
      title = sourceName + " closed during a download"
      body = count > 1 ? count + " " + sourceName + " downloads were interrupted"
        : primary.name + " was interrupted"
    } else if (finished) {
      title = sourceName + " download finished"
      body = count > 1 ? count + " " + sourceName + " downloads finished"
        : primary.name + " finished"
    } else {
      title = sourceName + " download stopped"
      body = count > 1 ? count + " " + sourceName + " downloads stopped"
        : primary.name + " stopped at " + primary.percent + "%"
    }
    notices.push({
      source: source,
      sourceName: sourceName,
      title: title,
      body: body,
      appid: primary.appid,
      finished: finished
    })
  }
  return notices
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    GLYPH_DOWNLOAD: GLYPH_DOWNLOAD,
    GLYPH_PAUSED: GLYPH_PAUSED,
    GLYPH_QUEUED: GLYPH_QUEUED,
    GLYPH_STEAM: GLYPH_STEAM,
    GLYPH_GAME: GLYPH_GAME,
    GLYPH_ERROR: GLYPH_ERROR,
    sourceLabel: sourceLabel,
    runnerLabel: runnerLabel,
    sourceGlyph: sourceGlyph,
    phaseLabel: phaseLabel,
    rowKey: rowKey,
    toNumber: toNumber,
    clampInt: clampInt,
    indexByKey: indexByKey,
    indexByAppid: indexByAppid,
    totalBytes: totalBytes,
    doneBytes: doneBytes,
    fraction: fraction,
    percent: percent,
    rateFor: rateFor,
    etaFor: etaFor,
    formatBytes: formatBytes,
    formatRate: formatRate,
    formatEta: formatEta,
    clockTime: clockTime,
    parse: parse,
    derive: derive,
    buildRows: buildRows,
    pillText: pillText,
    downloadNotifications: downloadNotifications,
    scriptPath: scriptPath
  }
}
