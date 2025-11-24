import QtQuick
import QtQuick.Layouts
import org.kde.plasma.components as PlasmaComponents3
import org.kde.kirigami as Kirigami

Item {
    id: usageBar

    property var accounts: []

    // Calculate data for both bars
    property var sonnetData: calculateBarData("seven_day_sonnet")
    property var overallData: calculateBarData("seven_day")

    function calculateBarData(usageKey) {
        let data = {
            ticks: [],
            total: 0,
            maxPossible: accounts.length * 100,
            percentage: 0
        }

        let cumulative = 0

        for (let i = 0; i < accounts.length; i++) {
            const acc = accounts[i]
            let usage = 0

            if (acc.usage && acc.usage[usageKey] && acc.usage[usageKey].utilization !== null) {
                usage = acc.usage[usageKey].utilization
            }

            cumulative += usage
            data.ticks.push({
                name: acc.nickname || acc.email || "Account " + acc.index,
                value: usage,
                cumulative: cumulative,
                position: cumulative / data.maxPossible, // Normalized 0-1
                color: getColorForIndex(i)
            })
        }

        data.total = cumulative
        data.percentage = data.maxPossible > 0 ? (cumulative / data.maxPossible) * 100 : 0

        return data
    }

    function getColorForIndex(index) {
        const colors = [
            "#CC785C",  // Warm orange-red
            "#E69A7B",  // Light orange
            "#F0AB8A",  // Peach
            "#D88A6D",  // Medium orange
            "#C47259"   // Dark orange
        ]
        return colors[index % colors.length]
    }

    ColumnLayout {
        anchors.fill: parent
        spacing: Kirigami.Units.largeSpacing

        // Sonnet Bar
        BarComponent {
            Layout.fillWidth: true
            Layout.preferredHeight: Kirigami.Units.gridUnit * 2.5
            label: "Sonnet Usage"
            barData: usageBar.sonnetData
        }

        // Overall Bar
        BarComponent {
            Layout.fillWidth: true
            Layout.preferredHeight: Kirigami.Units.gridUnit * 2.5
            label: "Overall Usage (7d)"
            barData: usageBar.overallData
        }

        // Legend
        RowLayout {
            Layout.fillWidth: true

            Item {
                Layout.fillWidth: true
            }

            Flow {
                spacing: Kirigami.Units.largeSpacing

                Repeater {
                    model: accounts

                    RowLayout {
                        spacing: Kirigami.Units.smallSpacing

                        Rectangle {
                            width: Kirigami.Units.gridUnit * 1.2
                            height: Kirigami.Units.gridUnit * 0.8
                            radius: 2
                            color: getColorForIndex(index)
                            opacity: 0.7
                            border.width: 1
                            border.color: Qt.rgba(1, 1, 1, 0.15)
                        }

                        PlasmaComponents3.Label {
                            text: modelData.nickname || modelData.email || "Account " + modelData.index
                            font.pointSize: Kirigami.Theme.smallFont.pointSize * 0.9
                            font.weight: Font.Normal
                            opacity: 0.7
                        }
                    }
                }
            }
        }
    }

    // Update when accounts change
    onAccountsChanged: {
        sonnetData = calculateBarData("seven_day_sonnet")
        overallData = calculateBarData("seven_day")
    }
}
