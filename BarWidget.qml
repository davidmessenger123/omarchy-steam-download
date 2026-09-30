import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui
import "Model.js" as Model

// Steam Download
//
// Live Steam download progress for the bar. Steam has no supported
// progress API on Linux, so the helper reads what the client already
// writes: the per-game appmanifest for the totals, and the size of the
// `steamapps/downloading/<appid>/` staging tree for the bytes that have
// actually landed. The manifest's own `BytesDownloaded` is only rewritten
// when a download starts or stops, so it reads stale mid-download; the
// staging tree does not.
//
// The bar pill only exists while something is downloading, and carries the
// percentage of the download closest to finishing. Click it for per-game
// detail (bytes, throughput, time remaining) and to open Steam; right-click
// refreshes immediately.
//
// Optional configuration on the layout entry in shell.json:
//   {
//     "id": "davidjm.steam-download",
//     "interval": 3,      // seconds between measurements
//     "showQueued": true  // list downloads Steam has queued but not started
//   }
BarWidget {
  id: root
  moduleName: "davidjm.steam-download"

  // ---- state --------------------------------------------------------------

  property var rows: []            // every download, from the last poll
  property var active: []          // ...minus the queued ones
  property var primary: null       // the download the pill reports
  property string state: "idle"    // downloading | idle | stopped
  property string status: "No active Steam downloads"
  property bool running: true
  property string note: ""
  property string updatedLabel: ""
  property bool fetchBusy: false

  // Previous poll's rows keyed by appid, each carrying the timestamp it was
  // taken at. The rate is the growth of the staging tree between two polls,
  // so this is what makes the speed and ETA meaningful.
  property var previous: ({})

  readonly property bool downloading: state === "downloading"
  readonly property var popupRows: Model.buildRows(root.rows)
  readonly property string pill: Model.pillText(root.primary)

  readonly property string icon: root.downloading
    ? (root.primary && root.primary.rate > 0 ? Model.GLYPH_DOWNLOAD : Model.GLYPH_PAUSED)
    : Model.GLYPH_STEAM

  readonly property color activeColor: root.downloading
    ? (root.primary && root.primary.rate > 0 ? "#9ece6a" : "#e0af68")
    : (root.bar ? root.bar.foreground : Color.foreground)

  visible: root.downloading || root.opened
  implicitWidth: content.implicitWidth
  implicitHeight: content.implicitHeight

  // ---- polling ------------------------------------------------------------

  function refresh() {
    if (root.fetchBusy) return

    // Re-read settings fresh: shell.json can be edited at any time and the
    // host only updates this widget's `settings` in place.
    var showQueued = setting("showQueued", true) !== false

    // -E pins the interpreter to a known start state, and the helper has a
    // SIGALRM deadline of its own so a slow filesystem cannot hang the bar.
    fetcher.clearEnvironment = true
    fetcher.command = ["/usr/bin/python3", "-E",
      Model.scriptPath(Qt.resolvedUrl("steam-downloads.py"))]
    if (showQueued) fetcher.command.push("--include-queued")

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

  onSettingsChanged: function() {
    pollTimer.interval = Model.clampInt(setting("interval", 3), 1, 60) * 1000
    Qt.callLater(root.refresh)
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
    interval: Model.clampInt(setting("interval", 3), 1, 60) * 1000
    running: true
    repeat: true
    triggeredOnStart: true
    onTriggered: root.refresh()
  }

  // ---- IPC ----------------------------------------------------------------

  IpcHandler {
    target: root.moduleName
    function refresh(): void { root.refresh() }
    function open(): void { root.open() }
    function close(): void { root.close() }
    function show(): void { root.open() }
    function hide(): void { root.close() }
    function toggle(): void { root.togglePanel() }
  }

  property bool opened: false
  function open() { root.opened = true }
  function close() { root.opened = false }
  function togglePanel() { root.opened = !root.opened }

  function openSteam() {
    // Steam ships no omarchy-launch-steam; it is started through uwsm-app
    // like every other desktop app. or-focus keeps this idempotent, so
    // clicking the button while Steam is already up just raises it.
    if (root.bar && typeof root.bar.run === "function")
      root.bar.run("omarchy-launch-or-focus steam " + Util.shellQuote("uwsm-app -- steam"))
  }

  // ---- bar pill -----------------------------------------------------------

  // The pill is a download glyph plus the percentage of the download closest
  // to finishing, so it ticks down as that game lands and disappears entirely
  // when Steam goes quiet.
  Row {
    id: content
    anchors.centerIn: parent
    spacing: Style.space(4)

    BarIconButton {
      id: button
      anchors.verticalCenter: parent.verticalCenter
      bar: root.bar
      text: root.icon
      active: root.downloading
      activeColor: root.activeColor
      useActiveColor: root.downloading
      slotSize: Style.bar.iconSlot
      fontSize: Style.bar.iconFont
      tooltipText: root.status
      onPressed: function(buttonCode) {
        if (buttonCode === Qt.RightButton) root.refresh()
        else if (buttonCode === Qt.MiddleButton) root.openSteam()
        else root.togglePanel()
      }
    }

    Text {
      anchors.verticalCenter: parent.verticalCenter
      visible: root.pill !== ""
      text: root.pill
      color: root.bar ? root.bar.foreground : Color.foreground
      font.family: root.bar ? root.bar.fontFamily : Style.font.family
      font.pixelSize: Style.bar.iconFont
      font.bold: true
    }
  }

  // ---- popup: per-download detail ----------------------------------------

  KeyboardPanel {
    id: panel
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.opened
    padding: Style.spacing.popupPadding
    contentWidth: panel.fittedContentWidth(Style.space(340), Style.space(520))
    contentHeight: panel.fittedContentHeight(root.popupImplicitHeight, Style.space(520))

    PanelKeyCatcher {
      anchors.fill: parent
      onCloseRequested: root.close()

      Column {
        id: popupRoot
        width: parent.width
        spacing: Style.space(6)

        Row {
          id: headerRow
          width: parent.width
          spacing: Style.space(8)

          OpticalGlyph {
            anchors.verticalCenter: parent.verticalCenter
            width: Style.space(22)
            height: headerRow.height
            text: root.icon
            fontFamily: root.bar ? root.bar.fontFamily : Style.font.resolvedFamily
            fontSize: Style.font.icon
            color: root.activeColor
          }

          Column {
            width: parent.width - Style.space(22) - headerRow.spacing
            spacing: Style.space(2)

            Text {
              width: parent.width
              text: root.state === "downloading"
                ? "Steam Downloads · " + root.active.length + " active"
                : "Steam Downloads"
              color: Color.popups.text
              font.family: Style.font.family
              font.pixelSize: Style.font.body
              font.bold: true
              elide: Text.ElideRight
            }

            Text {
              width: parent.width
              text: root.downloading
                ? root.status
                : (root.running ? "Nothing downloading right now" : "Steam is not running")
              color: Color.muted
              font.family: Style.font.family
              font.pixelSize: Style.font.caption
              wrapMode: Text.WrapAtWordBoundaryOrAnywhere
            }
          }
        }

        PanelSeparator {}

        // Download list with a progress track under each row.
        Column {
          id: listColumn
          width: parent.width
          spacing: Style.space(10)

          Repeater {
            model: root.popupRows

            delegate: Component {
              Item {
                id: row
                required property var modelData
                width: listColumn.width
                height: Style.space(40)

                Column {
                  anchors.left: parent.left
                  anchors.right: parent.right
                  spacing: Style.space(5)

                  Row {
                    width: parent.width
                    spacing: Style.space(8)

                    OpticalGlyph {
                      anchors.verticalCenter: parent.verticalCenter
                      width: Style.space(16)
                      height: Style.space(18)
                      text: modelData.glyph
                      fontFamily: root.bar ? root.bar.fontFamily : Style.font.resolvedFamily
                      fontSize: Style.font.bodySmall
                      color: modelData.color
                    }

                    Text {
                      width: parent.width - Style.space(16) - Style.space(8) - Style.space(40)
                      anchors.verticalCenter: parent.verticalCenter
                      text: modelData.name
                      color: Color.popups.text
                      font.family: Style.font.family
                      font.pixelSize: Style.font.body
                      font.bold: true
                      elide: Text.ElideRight
                    }

                    Text {
                      anchors.verticalCenter: parent.verticalCenter
                      visible: modelData.kind === "download"
                      text: modelData.percent + "%"
                      color: modelData.color
                      font.family: Style.font.family
                      font.pixelSize: Style.font.bodySmall
                      font.bold: true
                    }
                  }

                  Text {
                    width: parent.width
                    visible: modelData.subtitle !== ""
                    text: modelData.subtitle
                    color: Color.muted
                    font.family: Style.font.family
                    font.pixelSize: Style.font.caption
                    elide: Text.ElideRight
                    maximumLineCount: 1
                  }

                  // Progress track. A queued row has nothing to fill yet,
                  // so it renders as a flat dim line rather than a bar.
                  Rectangle {
                    width: parent.width
                    height: Style.space(3)
                    radius: height / 2
                    color: Util.alpha(Color.popups.text, 0.12)

                    Rectangle {
                      width: parent.width * (modelData.kind === "download" ? modelData.fraction : 0)
                      height: parent.height
                      radius: parent.radius
                      color: modelData.color
                      Behavior on width {
                        NumberAnimation { duration: 280; easing.type: Easing.OutCubic }
                      }
                    }
                  }
                }
              }
            }
          }

          Text {
            width: parent.width
            visible: root.popupRows.length === 0
            text: "No downloads in progress. Start one in Steam and it will appear here."
            color: Color.muted
            font.family: Style.font.family
            font.pixelSize: Style.font.bodySmall
            wrapMode: Text.WrapAtWordBoundaryOrAnywhere
            horizontalAlignment: Text.AlignHCenter
          }
        }

        PanelSeparator {}

        Item {
          id: footerRow
          width: parent.width
          height: openButton.implicitHeight

          Text {
            anchors.left: parent.left
            anchors.verticalCenter: parent.verticalCenter
            width: parent.width - openButton.implicitWidth - refreshButton.implicitWidth - Style.space(16)
            text: root.running ? root.updatedLabel : (root.note !== "" ? root.note : "")
            color: Color.muted
            font.family: Style.font.family
            font.pixelSize: Style.font.caption
            verticalAlignment: Text.AlignVCenter
            elide: Text.ElideRight
            maximumLineCount: 1
          }

          Button {
            id: refreshButton
            anchors.right: parent.right
            anchors.verticalCenter: parent.verticalCenter
            text: "Refresh"
            iconText: ""
            iconSize: Style.font.caption
            fontSize: Style.font.caption
            bordered: true
            tooltipText: "Re-measure now (right-click the bar icon also refreshes)"
            onClicked: root.refresh()
          }

          Button {
            id: openButton
            anchors.right: refreshButton.left
            anchors.rightMargin: Style.space(8)
            anchors.verticalCenter: parent.verticalCenter
            text: "Open Steam"
            fontSize: Style.font.caption
            bordered: true
            tooltipText: "Bring the Steam client to the front"
            onClicked: root.openSteam()
          }
        }
      }
    }
  }

  readonly property real popupImplicitHeight: {
    var header = Style.space(34)
    var list = Style.space(10)
    for (var i = 0; i < root.popupRows.length; i++) list += Style.space(40) + Style.space(10)
    if (root.popupRows.length === 0) list = Style.space(46)
    var footer = Style.space(36)
    var gaps = Style.space(6) * 4
    return header + list + footer + gaps
  }
}
