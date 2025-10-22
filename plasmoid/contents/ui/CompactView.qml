import QtQuick
import QtQuick.Layouts
import org.kde.plasma.core as PlasmaCore
import org.kde.kirigami as Kirigami

Item {
    id: compact

    property int usage: 0
    property bool loading: false
    property bool hasError: false

    Layout.fillWidth: true
    Layout.fillHeight: true
    Layout.minimumWidth: Kirigami.Units.iconSizes.small
    Layout.minimumHeight: Kirigami.Units.iconSizes.small

    // Icon
    Kirigami.Icon {
        id: icon
        anchors.centerIn: parent
        width: Math.min(parent.width, parent.height) * 0.7
        height: width
        source: "view-statistics"
        opacity: hasError ? 0.5 : 1.0

        Behavior on opacity {
            NumberAnimation { duration: 200 }
        }

        // Hover effect
        scale: hoverArea.containsMouse ? 1.1 : 1.0

        Behavior on scale {
            NumberAnimation { duration: 150; easing.type: Easing.OutCubic }
        }
    }

    // Usage badge
    Rectangle {
        id: badge
        anchors.right: icon.right
        anchors.top: icon.top
        anchors.rightMargin: -Kirigami.Units.smallSpacing
        anchors.topMargin: -Kirigami.Units.smallSpacing
        width: Math.max(badgeText.width + Kirigami.Units.smallSpacing * 2, height)
        height: Kirigami.Units.gridUnit * 1.2
        radius: height / 2
        visible: !hasError && !loading

        // Color based on usage
        color: {
            if (usage >= 90) return "#CC785C"  // Red-orange for high usage
            if (usage >= 70) return "#E69A7B"  // Orange for medium usage
            return "#8BC34A"  // Green for low usage
        }

        Behavior on color {
            ColorAnimation { duration: 300 }
        }

        // Subtle gradient overlay
        Rectangle {
            anchors.fill: parent
            radius: parent.radius
            gradient: Gradient {
                GradientStop { position: 0.0; color: Qt.rgba(1, 1, 1, 0.2) }
                GradientStop { position: 1.0; color: Qt.rgba(0, 0, 0, 0.2) }
            }
        }

        // Badge text
        Text {
            id: badgeText
            anchors.centerIn: parent
            text: loading ? "..." : usage + "%"
            color: "white"
            font.pixelSize: Kirigami.Units.gridUnit * 0.7
            font.weight: Font.Bold
            font.family: "sans-serif"
        }
    }

    // Loading indicator
    Rectangle {
        anchors.centerIn: icon
        width: icon.width * 0.3
        height: width
        radius: width / 2
        color: Kirigami.Theme.highlightColor
        visible: loading

        SequentialAnimation on opacity {
            running: loading
            loops: Animation.Infinite
            NumberAnimation { from: 0.3; to: 1.0; duration: 600 }
            NumberAnimation { from: 1.0; to: 0.3; duration: 600 }
        }
    }

    // Pulse effect when high usage
    Rectangle {
        id: pulseRing
        anchors.centerIn: icon
        width: icon.width * 1.5
        height: width
        radius: width / 2
        color: "transparent"
        border.color: "#CC785C"
        border.width: 2
        visible: !hasError && usage >= 90

        SequentialAnimation on opacity {
            running: visible
            loops: Animation.Infinite
            NumberAnimation { from: 0.8; to: 0.0; duration: 1500 }
            PauseAnimation { duration: 500 }
        }

        SequentialAnimation on scale {
            running: visible
            loops: Animation.Infinite
            NumberAnimation { from: 0.8; to: 1.3; duration: 1500 }
            PauseAnimation { duration: 500 }
        }
    }

    MouseArea {
        id: hoverArea
        anchors.fill: parent
        hoverEnabled: true
    }
}
