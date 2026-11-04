import QtQuick
import Quickshell
import Quickshell.Io
import "Model.js" as Model

// One game-download poller for the whole shell. Steam, Heroic and the native
// Epic Games Launcher each keep their own private download state; the helper
// reads those files and normalises them into one row list. Keeping the helper
// here means a docked laptop runs one Python process and one scan, not one per
// bar monitor.
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
  property string status: "No active game downloads"
  property bool running: true
  property var sources: ({})
  property bool installed: true
  property string note: ""
  property string updatedLabel: ""
  property real freeBytes: 0
  property real totalBytes: 0
  property bool fetchBusy: false
  property int intervalSeconds: 3
  property bool showQueued: true
  property bool notifyDownloads: true
  property bool inhibitIdle: true
  property bool idleLeaseHeld: false

  // Previous poll's rows keyed by source/app id, each carrying the timestamp
  // it was taken at. The rate is the growth of the staging tree between two
  // polls; Heroic rows can also supply their own rate.
  property var previous: ({})

  readonly property bool downloading: state === "downloading"

  // Active downloads are measured at the user's interval so speed and ETA stay
  // steady. An idle launcher is checked less often, and a machine with none
  // running is checked only occasionally.
  readonly property int pollIntervalMs: root.downloading
    ? root.intervalSeconds * 1000
    : (root.running ? 5000 : 15000)

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

  function configure(interval, queued, notify, inhibit) {
    var nextInterval = Model.clampInt(interval, 1, 60)
    var nextQueued = queued !== false
    var nextNotify = notify !== false
    var nextInhibit = inhibit !== false
    var pollChanged = nextInterval !== root.intervalSeconds || nextQueued !== root.showQueued
    var inhibitChanged = nextInhibit !== root.inhibitIdle
    root.intervalSeconds = nextInterval
    root.showQueued = nextQueued
    root.notifyDownloads = nextNotify
    root.inhibitIdle = nextInhibit
    if (inhibitChanged) {
      if (nextInhibit && root.downloading) root.renewIdleLease()
      else if (!nextInhibit) root.releaseIdleLease()
    }
    if (pollChanged && !root.fetchBusy) root.refresh()
  }

  function renewIdleLease() {
    if (!root.inhibitIdle || !root.downloading) return
    root.idleLeaseHeld = true
    Quickshell.execDetached(["/usr/bin/omarchy-shell", "-q", "media-idle",
      "acquire", "game-downloads", "900"])
  }

  function releaseIdleLease() {
    if (!root.idleLeaseHeld) return
    root.idleLeaseHeld = false
    Quickshell.execDetached(["/usr/bin/omarchy-shell", "-q", "media-idle",
      "releaseLease", "game-downloads"])
  }

  function notificationTarget(source) {
    if (String(source) === "heroic") return ["/usr/bin/heroic"]
    if (String(source) === "epic") return ["/usr/bin/epic-games-launcher"]
    return ["/usr/bin/xdg-open", "steam://open/downloads"]
  }

  function sendNotification(notice) {
    if (!notice || !notice.title) return
    var command = ["/usr/share/omarchy/bin/omarchy-notification-send",
      "--app-name", "Game Downloads", notice.title, notice.body || "",
      "--exec"]
    command = command.concat(root.notificationTarget(notice.source))
    Quickshell.execDetached(command)
  }

  function refresh() {
    if (root.fetchBusy) return

    // -E pins the interpreter to a known start state, and the helper has a
    // SIGALRM deadline of its own so a slow filesystem cannot hang the bar.
    fetcher.clearEnvironment = true
    fetcher.command = ["/usr/bin/python3", "-E",
      Model.scriptPath(Qt.resolvedUrl("game-downloads.py"))]
    if (root.showQueued) fetcher.command.push("--include-queued")

    root.fetchBusy = true
    fetcher.running = true
    watchdog.start()
  }

  function ingest(raw) {
    watchdog.stop()
    root.fetchBusy = false

    var successful = String(raw || "").trim() !== ""
    var previousState = root.state
    var previousActive = root.active
    var wasDownloading = root.downloading
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
      var prior = root.previous[row.key]
      if (prior && root.downloading && prior.rate > 0) row.rate = prior.rate
    }

    root.rows = state.rows
    root.active = state.active
    root.primary = state.primary
    root.state = state.state
    root.status = state.status
    root.running = state.running
    root.sources = state.sources
    root.installed = state.installed
    root.note = state.note
    root.freeBytes = state.freeBytes
    root.totalBytes = state.totalBytes
    root.previous = Model.indexByKey(state.rows)
    root.updatedLabel = "Updated " + Model.clockTime(new Date())

    if (!successful) return
    if (root.notifyDownloads) {
      var notices = Model.downloadNotifications(previousState, previousActive,
        state.state, state.active, { sources: state.sources })
      for (var n = 0; n < notices.length; n++) root.sendNotification(notices[n])
    }
    if (root.downloading && !wasDownloading) root.renewIdleLease()
    else if (!root.downloading && wasDownloading) root.releaseIdleLease()
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
      root.status = root.downloading ? root.status : "Game download check timed out"
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

  // Renew well before the 15-minute lease expires. The lease lives in the
  // media-idle service, so a paused/closed launcher releases it promptly.
  Timer {
    interval: 300000
    repeat: true
    running: root.downloading && root.inhibitIdle
    onTriggered: root.renewIdleLease()
  }

  Component.onDestruction: root.releaseIdleLease()

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
