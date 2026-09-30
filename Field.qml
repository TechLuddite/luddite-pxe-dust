import QtQuick
import qs.Commons

Column {
    id: root
    property string label: ""
    property string placeholder: ""
    property int maximumLength: 4096
    property alias text: input.text
    spacing: 6
    Text {
        text: root.label
        textFormat: Text.PlainText
        color: Color.foreground
        font.pixelSize: Style.font.caption
    }
    Rectangle {
        width: parent.width
        height: 38
        color: "transparent"
        border.color: input.activeFocus ? Color.accent : Color.foreground
        border.width: 1
        radius: 4
        TextInput {
            id: input
            anchors.fill: parent
            anchors.margins: 9
            maximumLength: root.maximumLength
            color: Color.foreground
            font.pixelSize: Style.font.body
            clip: true
            selectByMouse: true
            activeFocusOnTab: true
        }
        Text {
            anchors.fill: input
            text: root.placeholder
            textFormat: Text.PlainText
            visible: input.text.length === 0
            color: Color.foreground
            opacity: 0.45
            font.pixelSize: Style.font.body
            elide: Text.ElideRight
        }
    }
}
