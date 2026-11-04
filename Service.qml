import QtQuick
import Quickshell
import Quickshell.Io
import "Model.js" as Model

// One Steam download poller for the whole shell. Steam has no supported
// progress API on Linux, so the helper reads the client's app manifests and
// the size of the staging tree. Keeping the helper here means a docked laptop
// runs one Python process and one directory walk, not one per bar monitor.
Item {
  id: root

  readonly property string moduleName: "davidjm.steam-download"

  // The bar widget exists once per monitor, but the IPC target must exist
  // exactly once. The widgets register here, and the service forwards
  // open/close/toggle to the panel that is actually live.
  property var widgets: []
  property var activeWidget: null

  property var rows: []
  property var active: []
  property var primary: null
  property string state: "idle"
  property string status: "No active Steam downloads"
  property bool running: true
  property string note: ""
  property string updatedLabel: ""
  property bool fetchBusy: false
  property int intervalSeconds: 3
  property bool showQueued: true

  // Previous poll's rows keyed by appid, each carrying the timestamp it was
  // taken at. The rate is the growth of the staging tree between two polls.
  property var previous: ({})

  readonly property bool downloading: state === "downloading"

  // A closed Steam client is checked slowly; an active download is measured at
  // the user's configured interval so speed and ETA stay steady.
  readonly property int pollIntervalMs: (root.running ? root.intervalSeconds : 15) * 1000

  function registerWidget(widget) {
    if (!widget || root.widgets.indexOf(widget) !== -1) return
    var next = root.widgets.slice(0)
    next.push(widget)
    root.widgets = next
    if (!root.activeWidget) root.activeWidget = widget
  }

  function unregisterWidget(widget) {
    var next = root.widgets.filter(function(candidate) { return candidate !== widget })
    root.widgets = next
    if (root.activeWidget === widget) root.activeWidget = next.length ? next[0] : null
  }

  function noteOpened(widget, opened) {
    if (opened) root.activeWidget = widget
    else if (root.activeWidget === widget) root.activeWidget = null
  }

  function panelWidget() {
    if (root.activeWidget && root.widgets.indexOf(root.activeWidget) !== -1) return root.activeWidget
    return root.widgets.length ? root.widgets[0] : null
  }

  function openPanel() {
    var widget = root.panelWidget()
    if (widget) widget.open()
  }

  function closePanel() {
    for (var i = 0; i < root.widgets.length; i++) {
      if (root.widgets[i] && root.widgets[i].opened) root.widgets[i].close()
    }
  }

  function togglePanel() {
    for (var i = 0; i < root.widgets.length; i++) {
      if (root.widgets[i] && root.widgets[i].opened) {
        root.closePanel()
        return
      }
    }
    root.openPanel()
  }

  function configure(interval, queued) {
    var nextInterval = Model.clampInt(interval, 1, 60)
    var nextQueued = queued !== false
    if (nextInterval === root.intervalSeconds && nextQueued === root.showQueued) return
    root.intervalSeconds = nextInterval
    root.showQueued = nextQueued
    if (!root.fetchBusy) root.refresh()
  }

  function refresh() {
    if (root.fetchBusy) return

    // -E pins the interpreter to a known start state, and the helper has a
    // SIGALRM deadline of its own so a slow filesystem cannot hang the bar.
    fetcher.clearEnvironment = true
    fetcher.command = ["/usr/bin/python3", "-E",
      Model.scriptPath(Qt.resolvedUrl("steam-downloads.py"))]
    if (root.showQueued) fetcher.command.push("--include-queued")

    root.fetchBusy = true
    fetcher.running = true
    watchdog.start()
  }

  function ingest(raw) {
    watchdog.stop()
    root.fetchBusy = false

    var parsed = Model.parse(raw)
    // Now, not the helper's clock: the rate is a delta between two polls
    // taken by this same process, so both ends share one timeline.
    var state = Model.derive(parsed.rows, parsed.meta, root.previous, Date.now() / 1000)

    // Carry the rate forward for one poll. A download that pauses between
    // two measurements would otherwise read as 0 B/s and the bar would
    // flicker to the paused glyph on every hiccup.
    for (var i = 0; i < state.rows.length; i++) {
      var row = state.rows[i]
      if (row.rate > 0) continue
      var prior = root.previous[row.appid]
      if (prior && root.downloading && prior.rate > 0) row.rate = prior.rate
    }

    root.rows = state.rows
    root.active = state.active
    root.primary = state.primary
    root.state = state.state
    root.status = state.status
    root.running = state.running
    root.note = state.note
    root.previous = Model.indexByAppid(state.rows)
    root.updatedLabel = "Updated " + Model.clockTime(new Date())
  }

  Process {
    id: fetcher
    command: []
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: root.ingest(text)
    }
    onExited: function(exitCode) {
      // If the stream already parsed rows this is a no-op. A failure with no
      // usable stdout is the only thing that should move the widget off its
      // last known state, and even then it keeps whatever it last saw.
      if (root.fetchBusy) root.ingest("")
    }
  }

  // Independent watchdog: the shell is the final authority on how long a
  // poll may run. The helper bounds its own walk, but a stalled mount can
  // still outlive that.
  Timer {
    id: watchdog
    interval: 15000
    repeat: false
    onTriggered: {
      if (!root.fetchBusy) return
      root.fetchBusy = false
      fetcher.running = false
      root.state = root.downloading ? root.state : "idle"
      root.status = root.downloading ? root.status : "Steam check timed out"
    }
  }

  Timer {
    id: pollTimer
    interval: root.pollIntervalMs
    running: true
    repeat: true
    triggeredOnStart: true
    onTriggered: root.refresh()
  }

  IpcHandler {
    target: root.moduleName

    function refresh(): void { root.refresh() }
    function open(): void { root.openPanel() }
    function close(): void { root.closePanel() }
    function show(): void { root.openPanel() }
    function hide(): void { root.closePanel() }
    function toggle(): void { root.togglePanel() }
  }
}
