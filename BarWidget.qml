import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui
import "Model.js" as Model

// Game Downloads
//
// Live download progress for Steam, Heroic and the native Epic Games
// Launcher in one bar pill. None of the launchers exposes a supported
// progress API, so the shared service reads what they already write:
// Steam's staging tree, Heroic's download queue and progress log, and the
// EGLv2 operation journal plus its chunk cache.
//
// The pill only exists while something is downloading, and carries the
// percentage of the download closest to finishing. Click it for per-game
// detail (source, bytes, throughput, time remaining) and to open the
// relevant launcher; right-click refreshes immediately.
//
// Optional configuration on the layout entry in shell.json:
//   {
//     "id": "davidjm.steam-download",
//     "interval": 3,      // seconds between measurements
//     "showQueued": true  // list downloads a launcher has queued
//   }
BarWidget {
  id: root
  moduleName: "davidjm.steam-download"

  // ---- state --------------------------------------------------------------

  // The shared service owns the poller and the previous sample, so a docked
  // laptop does not run one Steam scan per monitor.
  readonly property var svc: bar && bar.shell ? bar.shell.serviceFor(root.moduleName) : null

  readonly property var rows: root.svc && root.svc.rows ? root.svc.rows : []
  readonly property var active: root.svc && root.svc.active ? root.svc.active : []
  readonly property var primary: root.svc ? root.svc.primary : null
  readonly property string state: root.svc ? root.svc.state : "idle"
  readonly property string status: root.svc ? root.svc.status : "No active game downloads"
  readonly property bool running: root.svc ? root.svc.running : true
  readonly property var sources: root.svc && root.svc.sources ? root.svc.sources : ({})
  readonly property bool installed: root.svc ? root.svc.installed : true
  readonly property string note: root.svc ? root.svc.note : ""
  readonly property string updatedLabel: root.svc ? root.svc.updatedLabel : ""
  readonly property real freeBytes: root.svc ? root.svc.freeBytes : 0
  readonly property string diskLabel: root.freeBytes > 0
    ? Model.formatBytes(root.freeBytes) + " free" : ""
  readonly property bool fetchBusy: root.svc ? root.svc.fetchBusy : false

  readonly property bool downloading: state === "downloading"
  readonly property var popupRows: Model.buildRows(root.rows)
  readonly property string pill: Model.pillText(root.primary)
  readonly property string launcherName: root.primary ? root.primary.sourceName : "launcher"

  readonly property string icon: root.downloading
    ? (root.primary && root.primary.rate > 0 ? Model.GLYPH_DOWNLOAD : Model.GLYPH_PAUSED)
    : Model.GLYPH_GAME

  readonly property color activeColor: root.downloading
    ? (root.primary && root.primary.rate > 0 ? "#9ece6a" : "#e0af68")
    : (root.bar ? root.bar.foreground : Color.foreground)

  visible: root.downloading || root.opened
  implicitWidth: content.implicitWidth
  implicitHeight: content.implicitHeight

  // ---- polling ------------------------------------------------------------

  function refresh() {
    if (root.svc) root.svc.refresh()
  }

  function applySettings() {
    if (root.svc) root.svc.configure(
      setting("interval", 3),
      setting("showQueued", true) !== false,
      setting("notifyDownloads", true) !== false,
      setting("inhibitIdle", true) !== false)
  }

  onSettingsChanged: root.applySettings()
  Component.onCompleted: {
    root.applySettings()
    if (root.svc) root.svc.registerWidget(root)
  }
  Component.onDestruction: if (root.svc && typeof root.svc.unregisterWidget === "function") root.svc.unregisterWidget(root)

  property bool opened: false
  onOpenedChanged: if (root.svc) root.svc.noteOpened(root, root.opened)
  function open() { root.opened = true }
  function close() { root.opened = false }
  function togglePanel() { root.opened = !root.opened }

  function defaultSource() {
    if (root.primary) return root.primary.source
    var order = ["steam", "heroic", "epic"]
    for (var i = 0; i < order.length; i++) {
      var running = root.sources[order[i]]
      if (running && running.running) return order[i]
    }
    for (var j = 0; j < order.length; j++) {
      var installed = root.sources[order[j]]
      if (installed && installed.installed) return order[j]
    }
    return "steam"
  }

  function openLauncher(source) {
    var target = String(source || root.defaultSource())
    var appId = "steam"
    var command = "uwsm-app -- steam"
    if (target === "heroic") {
      appId = "heroic"
      command = "uwsm-app -- heroic"
    } else if (target === "epic") {
      appId = "EpicGamesLauncher"
      command = "uwsm-app -- epic-games-launcher"
    }
    // No launcher ships an omarchy-launch-* wrapper; or-focus keeps this
    // idempotent, so clicking while the launcher is already up just raises it.
    if (root.bar && typeof root.bar.run === "function")
      root.bar.run("omarchy-launch-or-focus " + appId + " " + Util.shellQuote(command))
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
        else if (buttonCode === Qt.MiddleButton) root.openLauncher("")
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
                ? "Game Downloads · " + root.active.length + " active"
                : "Game Downloads"
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
                : (root.running ? "Nothing downloading right now"
                  : (root.note !== "" ? root.note : "No game launchers running"))
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
            text: "No downloads in progress. Start one in Steam, Heroic, or Epic Games and it will appear here."
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
            text: root.running
                  ? root.updatedLabel + (root.diskLabel !== "" ? " · " + root.diskLabel : "")
                  : (root.note !== "" ? root.note : "")
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
            text: root.primary ? "Open " + root.primary.sourceName : "Open launcher"
            fontSize: Style.font.caption
            bordered: true
            tooltipText: "Bring the download's launcher to the front"
            onClicked: root.openLauncher("")
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
