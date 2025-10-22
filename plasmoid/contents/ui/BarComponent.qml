import QtQuick
import QtQuick.Layouts
import org.kde.plasma.components as PlasmaComponents3
import org.kde.kirigami as Kirigami

Item {
    id: barComp

    property string label: ""
    property var barData: ({ticks: [], total: 0, maxPossible: 100, percentage: 0})

    implicitHeight: Kirigami.Units.gridUnit * 2.5

    ColumnLayout {
        anchors.fill: parent
        spacing: Kirigami.Units.smallSpacing

        // Label
        PlasmaComponents3.Label {
            text: barComp.label
            font.pointSize: Kirigami.Theme.smallFont.pointSize
            font.weight: Font.Medium
            color: Kirigami.Theme.textColor
            opacity: 0.8
        }

        // Bar
        Item {
            Layout.fillWidth: true
            Layout.preferredHeight: Kirigami.Units.gridUnit * 1.5

            // Background
            Rectangle {
                id: barBackground
                anchors.fill: parent
                radius: 4
                color: Qt.rgba(0, 0, 0, 0.2)
                border.width: 1
                border.color: Qt.rgba(1, 1, 1, 0.1)

                // Fill
                Rectangle {
                    id: barFill
                    height: parent.height
                    width: parent.width * Math.min(barComp.barData.percentage / 100, 1.0)
                    radius: 4
                    opacity: 0.75
                    gradient: Gradient {
                        orientation: Gradient.Horizontal
                        GradientStop { position: 0.0; color: "#B8664E" }
                        GradientStop { position: 1.0; color: "#D8896D" }
                    }

                    Behavior on width {
                        NumberAnimation { duration: 600; easing.type: Easing.OutCubic }
                    }
                }

                // Ticks
                Repeater {
                    model: barComp.barData.ticks

                    Item {
                        x: barBackground.width * modelData.position - width / 2
                        width: 2
                        height: barBackground.height

                        property bool hasSpace: {
                            const pos = barBackground.width * modelData.position
                            const minSpace = 30

                            // Check distance from edges
                            if (pos < minSpace || pos > barBackground.width - minSpace) {
                                return false
                            }

                            // Check distance from other ticks
                            for (let i = 0; i < barComp.barData.ticks.length; i++) {
                                if (i !== model.index) {
                                    const otherPos = barBackground.width * barComp.barData.ticks[i].position
                                    if (Math.abs(pos - otherPos) < minSpace) {
                                        return false
                                    }
                                }
                            }
                            return true
                        }

                        // Tick line
                        Rectangle {
                            anchors.fill: parent
                            color: Kirigami.Theme.textColor
                            opacity: 0.5

                            Behavior on x {
                                NumberAnimation { duration: 600; easing.type: Easing.OutCubic }
                            }
                        }

                        // Label below tick
                        PlasmaComponents3.Label {
                            anchors.horizontalCenter: parent.horizontalCenter
                            anchors.top: parent.bottom
                            anchors.topMargin: 2
                            text: modelData.value + "%"
                            font.pointSize: Kirigami.Theme.smallFont.pointSize * 0.75
                            font.weight: Font.Normal
                            color: Kirigami.Theme.textColor
                            opacity: 0.7
                            visible: parent.hasSpace && parent.x >= 0 && parent.x <= barBackground.width
                        }
                    }
                }
            }

            // Percentage label overlaid on the right
            Rectangle {
                anchors.right: barBackground.right
                anchors.rightMargin: Kirigami.Units.smallSpacing
                anchors.verticalCenter: parent.verticalCenter
                width: percentLabel.width + Kirigami.Units.smallSpacing
                height: percentLabel.height + 2
                radius: 2
                color: Qt.rgba(0, 0, 0, 0.4)

                PlasmaComponents3.Label {
                    id: percentLabel
                    anchors.centerIn: parent
                    text: Math.round(barComp.barData.percentage) + "%"
                    font.pointSize: Kirigami.Theme.smallFont.pointSize * 0.85
                    font.weight: Font.Medium
                    color: Kirigami.Theme.textColor
                }
            }
        }
    }
}
