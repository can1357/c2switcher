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
        QQC2.ToolTip.text: getResetTimeText()
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
                label: getTimeRemaining("five_hour") || "5h"
                value: getUsageValue("five_hour")
            }

            UsageIndicator {
                label: getTimeRemaining("seven_day") || "7d"
                value: getUsageValue("seven_day")
            }

            UsageIndicator {
                label: (getTimeRemaining("seven_day_opus") ? getTimeRemaining("seven_day_opus") + " (O)" : "Opus")
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

    function getTimeRemaining(key) {
        if (!accountData.usage) return ""
        const window = accountData.usage[key]
        if (!window || !window.resets_at) return ""

        const now = new Date()
        const resetDate = new Date(window.resets_at)
        const timeRemaining = resetDate - now

        if (timeRemaining <= 0) return "soon"

        const totalHours = timeRemaining / (60 * 60 * 1000)

        if (totalHours >= 24) {
            const days = Math.round(totalHours / 24)
            return `${days}d`
        } else {
            const hours = Math.round(totalHours)
            return `${hours}h`
        }
    }

    function calculateOveruseRate() {
        if (!accountData.usage) return null

        const sevenDay = accountData.usage.seven_day
        const sevenDayOpus = accountData.usage.seven_day_opus

        // Need at least one reset window
        if (!sevenDay && !sevenDayOpus) return null

        const opusUtil = sevenDayOpus && sevenDayOpus.utilization !== null ? sevenDayOpus.utilization : null
        const overallUtil = sevenDay && sevenDay.utilization !== null ? sevenDay.utilization : null

        const now = new Date()
        const sevenDaysMs = 7 * 24 * 60 * 60 * 1000
        let worstRate = 0

        // Calculate Opus rate with its own reset window
        if (opusUtil !== null && sevenDayOpus && sevenDayOpus.resets_at) {
            const opusResetDate = new Date(sevenDayOpus.resets_at)
            const opusTimeRemaining = opusResetDate - now

            if (opusTimeRemaining > 0) {
                const opusElapsed = sevenDaysMs - opusTimeRemaining
                const opusExpected = (opusElapsed / sevenDaysMs) * 100

                if (opusExpected > 0) {
                    const opusRate = (opusUtil / opusExpected) * 100
                    worstRate = Math.max(worstRate, opusRate)
                }
            }
        }

        // Calculate Overall rate with its own reset window
        if (overallUtil !== null && sevenDay && sevenDay.resets_at) {
            const overallResetDate = new Date(sevenDay.resets_at)
            const overallTimeRemaining = overallResetDate - now

            if (overallTimeRemaining > 0) {
                const overallElapsed = sevenDaysMs - overallTimeRemaining
                const overallExpected = (overallElapsed / sevenDaysMs) * 100

                if (overallExpected > 0) {
                    const overallRate = (overallUtil / overallExpected) * 100
                    worstRate = Math.max(worstRate, overallRate)
                }
            }
        }

        return worstRate > 0 ? Math.round(worstRate) : null
    }

    function getResetTimeText() {
        if (!accountData.usage) {
            return "No usage data available"
        }

        const sevenDay = accountData.usage.seven_day
        const sevenDayOpus = accountData.usage.seven_day_opus
        const now = new Date()

        let lines = []

        // Format time remaining helper
        function formatTimeRemaining(timeRemaining) {
            const days = Math.floor(timeRemaining / (24 * 60 * 60 * 1000))
            const hours = Math.floor((timeRemaining % (24 * 60 * 60 * 1000)) / (60 * 60 * 1000))
            const minutes = Math.floor((timeRemaining % (60 * 60 * 1000)) / (60 * 1000))

            if (days > 0) {
                return `${days}d ${hours}h ${minutes}m`
            } else if (hours > 0) {
                return `${hours}h ${minutes}m`
            } else {
                return `${minutes}m`
            }
        }

        // Opus reset
        if (sevenDayOpus && sevenDayOpus.resets_at) {
            const opusResetDate = new Date(sevenDayOpus.resets_at)
            const opusTimeRemaining = opusResetDate - now

            if (opusTimeRemaining > 0) {
                lines.push(`Opus resets in ${formatTimeRemaining(opusTimeRemaining)}`)
            } else {
                lines.push("Opus: resetting soon...")
            }
        }

        // Overall reset
        if (sevenDay && sevenDay.resets_at) {
            const overallResetDate = new Date(sevenDay.resets_at)
            const overallTimeRemaining = overallResetDate - now

            if (overallTimeRemaining > 0) {
                lines.push(`Overall resets in ${formatTimeRemaining(overallTimeRemaining)}`)
            } else {
                lines.push("Overall: resetting soon...")
            }
        }

        return lines.length > 0 ? lines.join("\n") : "No reset time available"
    }
}
