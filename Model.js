// Pure helper functions for the Steam Download bar widget. No Qt/QML
// dependencies so the logic stays unit-testable outside the shell.

var GLYPH_DOWNLOAD = "\uf019"   // nf-fa-arrow_circle_down
var GLYPH_PAUSED = "\uf04b"    // nf-fa-pause_circle
var GLYPH_QUEUED = "\uf017"    // nf-fa-clock_o
var GLYPH_STEAM = "\uf6a8"     // nf-br-steam
var GLYPH_ERROR = "\uf071"     // nf-fa-exclamation_triangle

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

// Key rows by appid so the next poll can pair them with their predecessors
// and derive a rate. Keys are strings in JS, which is what the lookup wants.
function indexByAppid(rows) {
  var out = {}
  for (var i = 0; i < (rows || []).length; i++) out[rows[i].appid] = rows[i]
  return out
}

// Bytes on disk, as measured from the staging tree. This is already the
// decompressed size, so nothing is added to it: the manifest's BytesStaged
// is both a double count and stale.
function doneBytes(row) {
  if (!row) return 0
  return Math.max(0, toNumber(row.downloaded))
}

// Fraction complete, clamped to [0, 1]. Steam reports 0 when a download has
// not been sized yet, so a total of 0 is reported as 0 rather than NaN.
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
// from the growth of the staging tree between polls. Returns 0 for the
// first sample, for a shrinking tree (Steam committing and deleting as it
// goes), and for a gap too long to be meaningful.
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

// Binary units, matching how Steam sizes a download. Steam itself shows MB
// for small downloads and GB beyond that, on a 1000-based scale.
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

// Parse the helper's stdout into widget state.
//
// Raw lines, one JSON object each:
//   {"appid":2842040,"name":"...","downloaded":N,"total":N,
//    "library":"...","idle":0.0,"queued":false}
//   {"__meta":{"running":true,"steamRoot":"...","downloads":1}}
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
    if (obj.appid === undefined) continue
    rows.push({
      appid: toNumber(obj.appid),
      name: String(obj.name || "Steam download"),
      downloaded: toNumber(obj.downloaded),
      total: toNumber(obj.total),
      library: String(obj.library || ""),
      idle: toNumber(obj.idle),
      queued: obj.queued === true
    })
  }
  return { rows: rows, meta: meta }
}

// Widget-wide view of the parse, given the previous poll's rows so the rate
// can be derived from the growth of each staging tree.
//
// `previous` is a map of appid -> row carrying the extra `at` timestamp the
// widget stamps on ingest, which is what makes the delta meaningful.
function derive(rows, meta, previous, nowSeconds) {
  var enriched = []
  for (var i = 0; i < (rows || []).length; i++) {
    var row = rows[i]
    var prior = previous ? previous[row.appid] : null
    var rate = rateFor(row, prior, nowSeconds)
    enriched.push({
      appid: row.appid,
      name: row.name,
      downloaded: row.downloaded,
      total: row.total,
      library: row.library,
      queued: row.queued,
      at: nowSeconds,
      rate: rate,
      fraction: fraction(row),
      percent: percent(row),
      eta: etaFor(row, rate)
    })
  }

  // A single download is the common case and gets the bar pill to itself.
  // With several running, the bar shows the one closest to finishing so it
  // visibly ticks down, and the popup lists the rest.
  var active = []
  for (var j = 0; j < enriched.length; j++) if (!enriched[j].queued) active.push(enriched[j])

  active.sort(function(a, b) { return b.fraction - a.fraction })

  var note = String((meta && meta.note) || "")
  var running = meta ? meta.running === true : false

  var state, status
  if (active.length === 0) {
    if (!running && note !== "") {
      state = "stopped"
      status = note
    } else {
      state = "idle"
      status = "No active Steam downloads"
    }
  } else {
    state = "downloading"
    status = active[0].name + " · " + active[0].percent + "%"
  }

  return {
    rows: enriched,
    active: active,
    primary: active.length > 0 ? active[0] : null,
    state: state,
    status: status,
    running: running,
    note: note,
    libraries: meta ? toNumber(meta.libraries) : 0
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
    if (item.queued) {
      rows.push({
        kind: "queued",
        name: item.name,
        subtitle: "Queued in Steam",
        fraction: 0,
        percent: 0,
        glyph: GLYPH_QUEUED,
        color: "#7aa2f7"
      })
      continue
    }

    // "13.4 GB of 137.2 GB · 305 Mbps · about 32 minutes left", with the
    // parts that cannot be determined simply left out.
    var parts = [formatBytes(item.downloaded) + " of " + formatBytes(item.total)]
    var rate = formatRate(item.rate)
    if (rate !== "") parts.push(rate)
    var eta = formatEta(item.eta)
    if (eta !== "") parts.push(eta)

    rows.push({
      kind: "download",
      name: item.name,
      subtitle: parts.join(" · "),
      fraction: item.fraction,
      percent: item.percent,
      glyph: item.rate > 0 ? GLYPH_DOWNLOAD : GLYPH_PAUSED,
      color: item.rate > 0 ? "#9ece6a" : "#e0af68"
    })
  }
  return rows
}

// Bar pill label: the percentage while downloading, otherwise nothing.
function pillText(primary) {
  if (!primary || primary.queued) return ""
  return primary.percent + "%"
}

// Notify when a running download stops. Steam exposes no pause/complete
// signal, so a nearly-full staging tree is treated as finished and anything
// else as stopped. The first sample has no previous active rows and stays
// quiet.
function downloadNotifications(previousState, previousActive, nextState, nextActive) {
  if (!Array.isArray(previousActive) || previousActive.length === 0) return []
  if (Array.isArray(nextActive) && nextActive.length > 0) return []
  var primary = previousActive[0]
  var count = previousActive.length
  var finished = primary.total > 0 && primary.downloaded >= primary.total * 0.995
  var title = ""
  var body = ""
  if (nextState === "stopped") {
    title = "Steam closed during a download"
    body = count > 1 ? count + " downloads were interrupted" : primary.name + " was interrupted"
  } else if (finished) {
    title = "Steam download finished"
    body = count > 1 ? count + " downloads finished" : primary.name + " finished"
  } else {
    title = "Steam download stopped"
    body = count > 1 ? count + " downloads stopped" : primary.name + " stopped at " + primary.percent + "%"
  }
  return [{ "title": title, "body": body, "appid": primary.appid, "finished": finished }]
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    GLYPH_DOWNLOAD: GLYPH_DOWNLOAD,
    GLYPH_PAUSED: GLYPH_PAUSED,
    GLYPH_QUEUED: GLYPH_QUEUED,
    GLYPH_STEAM: GLYPH_STEAM,
    GLYPH_ERROR: GLYPH_ERROR,
    toNumber: toNumber,
    clampInt: clampInt,
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
