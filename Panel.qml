import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui as Ui

Ui.Panel {
    id: root
    moduleName: "techluddite.pxe-dust"
    ipcTarget: "techluddite.pxe-dust"
    implicitWidth: button.implicitWidth
    implicitHeight: button.implicitHeight

    property var snapshot: ({state: "unknown", installed: false, prepared: false, interfaces: [], missing: []})
    property string message: ""
    property int selected: 0
    property string networkMode: "lan"
    property bool acknowledged: false
    readonly property bool busy: action.running
    readonly property bool serving: snapshot.state === "active"
    readonly property string interfaceName: snapshot.interfaces.length > selected
        ? snapshot.interfaces[selected].name : ""
    readonly property string helper: Qt.resolvedUrl("bin/pxe-dust").toString().replace(/^file:\/\//, "")

    function refresh() {
        if (!poll.running) {
            poll.buffer = ""
            poll.running = true
        }
    }
    function run(args) {
        if (busy) return
        message = args[0] === "prepare" ? "Verifying and preparing the image…" : "Waiting for authorization…"
        action.buffer = ""
        action.command = ["/usr/local/libexec/pxe-dust"].concat(args)
        action.running = true
    }
    function apply(raw) {
        try {
            var s = JSON.parse(raw)
            if (!s || typeof s.state !== "string" || typeof s.prepared !== "boolean" || typeof s.installed !== "boolean"
                || !Array.isArray(s.interfaces) || s.interfaces.length > 128
                || !Array.isArray(s.missing) || s.missing.length > 8) return
            if (["active", "inactive", "failed", "activating", "deactivating", "unknown"].indexOf(s.state) < 0) return
            if (["", "lan", "dedicated"].indexOf(s.mode) < 0 || typeof s.interface !== "string"
                || (s.interface !== "" && !/^[A-Za-z0-9][A-Za-z0-9_.-]{0,14}$/.test(s.interface))) return
            for (var i = 0; i < s.interfaces.length; i++) {
                if (typeof s.interfaces[i].name !== "string"
                    || !/^[A-Za-z0-9][A-Za-z0-9_.-]{0,14}$/.test(s.interfaces[i].name)) return
            }
            var previous = root.interfaceName
            var next = s.interfaces.findIndex(function(x) { return x.name === previous })
            root.snapshot = s
            if (s.state === "active") root.networkMode = s.mode
            root.selected = next >= 0 ? next : 0
            if (root.interfaceName !== previous) root.acknowledged = false
        } catch (_) { message = "Cannot read service status." }
    }
    onOpenedChanged: if (opened) refresh()

    Process {
        id: poll
        property string buffer: ""
        command: ["/usr/bin/python3", "-I", root.helper, "status"]
        clearEnvironment: true
        environment: ({PATH: "/usr/bin", LANG: "C"})
        stdout: SplitParser {
            splitMarker: ""
            onRead: function(chunk) {
                if (poll.buffer.length + chunk.length <= 32768) poll.buffer += chunk
                else { poll.signal(9); poll.buffer = "" }
            }
        }
        onExited: function(code) {
            if (code === 0) root.apply(buffer)
            else root.message = "Status unavailable. Check the helper installation."
            buffer = ""
        }
    }
    Process {
        id: action
        property string buffer: ""
        clearEnvironment: true
        environment: ({PATH: "/usr/bin", LANG: "C"})
        stdout: SplitParser {
            splitMarker: ""
            onRead: function(chunk) {
                if (action.buffer.length + chunk.length <= 4096) action.buffer += chunk
                else { action.signal(9); action.buffer = "" }
            }
        }
        onExited: function(code) {
            try {
                var result = JSON.parse(buffer)
                root.message = typeof result.message === "string" ? result.message.slice(0, 1000) : "Operation finished."
            } catch (_) {
                root.message = code === 0 ? "Operation finished." : "Action failed or authorization was cancelled. Install the system helper first."
            }
            buffer = ""
            root.refresh()
        }
    }
    Timer {
        interval: root.opened ? 5000 : 30000
        running: true
        repeat: true
        triggeredOnStart: true
        onTriggered: root.refresh()
    }
    Timer {
        interval: 60000
        running: poll.running
        onTriggered: poll.signal(9)
    }
    Component.onDestruction: {
        if (poll.running) poll.signal(15)
        // An authorized image preparation or system service outlives this widget.
    }

    Ui.BarIconButton {
        id: button
        anchors.fill: parent
        bar: root.bar
        text: root.serving ? "󰒍" : "󰛳"
        slotSize: Style.bar.statusSlot
        tooltipText: root.serving ? "Luddite PXE Dust — serving" : "Luddite PXE Dust"
        onPressed: root.toggle()
    }
    Ui.KeyboardPanel {
        anchorItem: button
        owner: root
        bar: root.bar
        open: root.opened
        contentWidth: fittedContentWidth(Style.space(420))
        contentHeight: fittedContentHeight(content.implicitHeight)
        focusTarget: content
        Flickable {
            anchors.fill: parent
            contentWidth: width
            contentHeight: content.implicitHeight
            clip: true
            Column {
            id: content
            width: parent.width
            spacing: 12
            Keys.onEscapePressed: root.close()
            Text {
                text: "Luddite PXE Dust"
                textFormat: Text.PlainText
                color: Color.foreground
                font.pixelSize: Style.font.display
            }
            Text {
                width: parent.width
                text: root.serving
                    ? (root.snapshot.mode === "lan" ? "Serving on your existing LAN" : "Serving on the dedicated network")
                    : "Your Ethernet installation station"
                textFormat: Text.PlainText
                color: root.serving ? Color.accent : Color.foreground
                font.pixelSize: Style.font.body
                wrapMode: Text.WordWrap
            }
            Text {
                width: parent.width
                visible: !root.snapshot.installed || root.snapshot.missing.length > 0
                text: "System setup required. Install dnsmasq, ipxe and the Luddite PXE Dust helper as described in the README."
                textFormat: Text.PlainText
                color: Color.foreground
                wrapMode: Text.WordWrap
            }
            Text {
                text: root.snapshot.prepared ? "Image: SHA-256 verified and cached" : "Prepare an Omarchy ISO"
                textFormat: Text.PlainText
                color: Color.foreground
            }
            Field {
                id: iso
                width: parent.width
                label: "Local ISO path"
                placeholder: "/path/to/omarchy.iso"
                visible: !root.serving
                enabled: !root.busy
            }
            Field {
                id: checksum
                width: parent.width
                label: "Published SHA-256"
                placeholder: "64 hexadecimal characters"
                maximumLength: 64
                visible: !root.serving
                enabled: !root.busy
            }
            Ui.Button {
                text: root.snapshot.prepared ? "Replace cached image" : "Verify and prepare image"
                visible: !root.serving
                enabled: root.snapshot.installed && root.snapshot.missing.length === 0 && !root.busy && iso.text.startsWith("/") && /^[a-fA-F0-9]{64}$/.test(checksum.text)
                focusable: true
                bordered: true
                onClicked: root.run(["prepare", iso.text, checksum.text])
            }
            Ui.Button {
                text: root.networkMode === "lan" ? "Mode: Existing LAN  ›" : "Mode: Dedicated network  ›"
                enabled: !root.serving && !root.busy
                focusable: true
                bordered: true
                onClicked: {
                    root.networkMode = root.networkMode === "lan" ? "dedicated" : "lan"
                    root.acknowledged = false
                }
            }
            Ui.Button {
                text: root.serving ? "Ethernet: " + root.snapshot.interface
                    : (root.interfaceName ? "Ethernet: " + root.interfaceName + "  ›" : "No Ethernet interface detected")
                enabled: !root.serving && !root.busy && root.snapshot.interfaces.length > 0
                focusable: true
                bordered: true
                onClicked: { root.selected = (root.selected + 1) % root.snapshot.interfaces.length; root.acknowledged = false }
            }
            Text {
                width: parent.width
                text: root.networkMode === "lan"
                    ? "Keep Ethernet connected. Your router supplies addresses; Luddite PXE Dust supplies boot information. Temporary firewall rules allow boot traffic from this LAN."
                    : "Connect only installation targets to this adapter or its dedicated switch. Disconnect the adapter in Network settings first."
                textFormat: Text.PlainText
                color: Color.foreground
                wrapMode: Text.WordWrap
                visible: !root.serving
            }
            Ui.Button {
                text: (root.acknowledged ? "☑ " : "☐ ") + (root.networkMode === "lan"
                    ? "Serve the installer on this LAN" : "This is a dedicated installation network")
                visible: !root.serving
                enabled: !root.busy
                focusable: true
                onClicked: root.acknowledged = !root.acknowledged
            }
            Ui.Button {
                text: root.busy ? "Working…" : (root.serving ? "Stop serving" : "Start serving")
                enabled: root.snapshot.installed && !root.busy && (root.serving || (root.snapshot.prepared && root.acknowledged && root.interfaceName !== "" && root.snapshot.missing.length === 0))
                focusable: true
                bordered: true
                onClicked: root.run(root.serving ? ["stop"] : ["start", root.interfaceName, root.networkMode === "lan" ? "--lan" : "--dedicated"])
            }
            Text {
                width: parent.width
                text: root.message
                textFormat: Text.PlainText
                color: Color.foreground
                wrapMode: Text.WordWrap
                visible: text !== ""
            }
            Text {
                width: parent.width
                text: "UEFI x86-64 · interactive installation\nBoot activity: journalctl -u pxe-dust -f"
                textFormat: Text.PlainText
                color: Color.foreground
                opacity: 0.6
                font.pixelSize: Style.font.caption
            }
        }
        }
    }
}
