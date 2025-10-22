import QtQuick
import QtQuick.Layouts
import org.kde.plasma.components as PlasmaComponents3
import org.kde.kirigami as Kirigami

ColumnLayout {
    id: indicator

    property string label: ""
    property var value: null
    property bool highlight: false

    spacing: Kirigami.Units.smallSpacing / 2

    PlasmaComponents3.Label {
        text: indicator.label
        font.pointSize: Kirigami.Theme.smallFont.pointSize * 0.85
        color: Kirigami.Theme.disabledTextColor
        opacity: 0.7
        Layout.alignment: Qt.AlignHCenter
    }

    Rectangle {
        Layout.preferredWidth: Kirigami.Units.gridUnit * 3
        Layout.preferredHeight: Kirigami.Units.gridUnit
        radius: 3
        color: {
            if (indicator.value === null) {
                return Qt.rgba(Kirigami.Theme.backgroundColor.r, Kirigami.Theme.backgroundColor.g, Kirigami.Theme.backgroundColor.b, 0.5)
            }

            // Subtle background based on usage level
            if (indicator.value >= 90) {
                return Qt.rgba(0.8, 0.3, 0.2, 0.2)  // Subtle red tint
            }
            if (indicator.value >= 70) {
                return Qt.rgba(0.9, 0.6, 0.3, 0.2)  // Subtle orange tint
            }
            return Qt.rgba(0.4, 0.7, 0.3, 0.15)  // Subtle green tint
        }

        border.width: 1
        border.color: {
            if (indicator.highlight) {
                return Kirigami.Theme.highlightColor
            }
            if (indicator.value === null) {
                return Qt.rgba(Kirigami.Theme.textColor.r, Kirigami.Theme.textColor.g, Kirigami.Theme.textColor.b, 0.2)
            }
            if (indicator.value >= 90) {
                return Qt.rgba(0.8, 0.3, 0.2, 0.4)
            }
            if (indicator.value >= 70) {
                return Qt.rgba(0.9, 0.6, 0.3, 0.4)
            }
            return Qt.rgba(0.4, 0.7, 0.3, 0.3)
        }

        Behavior on color {
            ColorAnimation { duration: 300 }
        }

        Behavior on border.color {
            ColorAnimation { duration: 300 }
        }

        PlasmaComponents3.Label {
            anchors.centerIn: parent
            text: indicator.value !== null ? indicator.value + "%" : "—"
            font.pointSize: Kirigami.Theme.smallFont.pointSize * 0.95
            font.weight: Font.Medium
            color: Kirigami.Theme.textColor
            opacity: 0.85
        }
    }
}
