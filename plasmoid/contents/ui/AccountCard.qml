import QtQuick
import QtQuick.Controls as QQC2
import QtQuick.Layouts
import org.kde.plasma.components as PlasmaComponents3
import org.kde.kirigami as Kirigami

Rectangle {
    id: card

    property var accountData: ({})
    signal switchClicked()

    height: cardLayout.implicitHeight + Kirigami.Units.smallSpacing * 2
    radius: 4
    color: Qt.rgba(0, 0, 0, 0.25)
    border.width: 1
    border.color: Qt.rgba(1, 1, 1, 0.08)

    // Hover effect
    scale: clickArea.containsMouse ? 1.005 : 1.0

    Behavior on scale {
        NumberAnimation { duration: 100; easing.type: Easing.OutCubic }
    }

    Behavior on color {
        ColorAnimation { duration: 100 }
    }

    states: State {
        name: "hovered"
        when: clickArea.containsMouse
        PropertyChanges {
            target: card
            color: Qt.rgba(0, 0, 0, 0.35)
            border.color: Qt.rgba(1, 1, 1, 0.12)
        }
    }

    MouseArea {
        id: clickArea
        anchors.fill: parent
        hoverEnabled: true
        cursorShape: Qt.PointingHandCursor
        onClicked: card.switchClicked()

        QQC2.ToolTip.visible: containsMouse
        QQC2.ToolTip.text: "Click to switch to this account"
    }

    RowLayout {
        id: cardLayout
        anchors.fill: parent
        anchors.margins: Kirigami.Units.smallSpacing
        spacing: Kirigami.Units.largeSpacing

        // Account info
        ColumnLayout {
            Layout.fillWidth: true
            spacing: 2

            RowLayout {
                spacing: Kirigami.Units.smallSpacing

                PlasmaComponents3.Label {
                    text: accountData.index !== undefined ? "#" + accountData.index : ""
                    font.pointSize: Kirigami.Theme.smallFont.pointSize
                    font.weight: Font.Bold
                    color: Kirigami.Theme.disabledTextColor
                }

                PlasmaComponents3.Label {
                    text: accountData.nickname || "No nickname"
                    font.weight: Font.Bold
                    font.pointSize: Kirigami.Theme.smallFont.pointSize
                    Layout.fillWidth: true
                }
            }

            PlasmaComponents3.Label {
                text: accountData.email || ""
                font.pointSize: Kirigami.Theme.smallFont.pointSize * 0.9
                color: Kirigami.Theme.disabledTextColor
                elide: Text.ElideRight
                Layout.fillWidth: true
            }
        }

        // Usage indicators
        RowLayout {
            spacing: Kirigami.Units.smallSpacing
            Layout.alignment: Qt.AlignVCenter

            UsageIndicator {
                label: "5h"
                value: getUsageValue("five_hour")
            }

            UsageIndicator {
                label: "7d"
                value: getUsageValue("seven_day")
            }

            UsageIndicator {
                label: "Opus"
                value: getUsageValue("seven_day_opus")
                highlight: true
            }

            UsageIndicator {
                label: "Overuse"
                value: calculateOveruseRate()
                isRate: true
            }
        }
    }

    function getUsageValue(key) {
        if (!accountData.usage) return null
        if (!accountData.usage[key]) return null
        return accountData.usage[key].utilization
    }

    function calculateOveruseRate() {
        if (!accountData.usage) return null

        const sevenDay = accountData.usage.seven_day
        const sevenDayOpus = accountData.usage.seven_day_opus

        if (!sevenDay || !sevenDay.resets_at) return null

        const opusUtil = sevenDayOpus && sevenDayOpus.utilization !== null ? sevenDayOpus.utilization : 0
        const overallUtil = sevenDay.utilization !== null ? sevenDay.utilization : 0

        // Parse reset time
        const resetDate = new Date(sevenDay.resets_at)
        const now = new Date()
        const timeRemaining = resetDate - now

        if (timeRemaining <= 0) return null

        // Calculate time elapsed
        const sevenDaysMs = 7 * 24 * 60 * 60 * 1000
        const timeElapsed = sevenDaysMs - timeRemaining
        const expectedUsage = (timeElapsed / sevenDaysMs) * 100

        if (expectedUsage <= 0) return null

        // Calculate overuse rate
        const actualUsage = Math.max(opusUtil, overallUtil)
        const rate = (actualUsage / expectedUsage) * 100

        return Math.round(rate)
    }
}
