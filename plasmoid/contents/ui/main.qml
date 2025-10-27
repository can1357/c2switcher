import QtQuick
import QtQuick.Layouts
import org.kde.plasma.plasmoid
import org.kde.plasma.core as PlasmaCore
import org.kde.plasma.plasma5support as Plasma5Support
import org.kde.kirigami as Kirigami

PlasmoidItem {
    id: root

    Plasmoid.icon: "view-statistics"

    // Account data
    property var accounts: []
    property int totalUsage: 0
    property int opusUsage: 0
    property int activeSessions: 0
    property string resetTime: ""
    property bool loading: true
    property bool hasError: false

    // DataSource for executing commands
    Plasma5Support.DataSource {
        id: executable
        engine: "executable"
        connectedSources: []

        onNewData: (sourceName, data) => {
            const exitCode = data["exit code"]
            const stdout = data["stdout"]
            const stderr = data["stderr"]

            console.log("Command output:", sourceName, "exit:", exitCode)

            if (sourceName.includes("usage")) {
                loading = false
                if (exitCode === 0 && stdout) {
                    try {
                        const parsed = JSON.parse(stdout)
                        updateAccounts(parsed)
                        hasError = false
                    } catch (e) {
                        console.error("Failed to parse JSON:", e, "stdout:", stdout)
                        hasError = true
                    }
                } else {
                    console.error("c2switcher failed:", stderr)
                    hasError = true
                }
                // Disconnect after receiving data
                executable.disconnectSource(sourceName)
            } else if (sourceName.includes("switch") || sourceName.includes("pick")) {
                if (exitCode === 0) {
                    console.log("Switch successful")
                    // Refresh data after switching
                    Qt.callLater(refreshData)
                } else {
                    console.error("Switch failed:", stderr)
                }
                executable.disconnectSource(sourceName)
            }
        }
    }

    // Timer for periodic updates (every 60 seconds)
    Timer {
        id: updateTimer
        interval: 60000 // 60 seconds
        running: true
        repeat: true
        triggeredOnStart: true
        onTriggered: refreshData()
    }

    // Refresh data function
    function refreshData() {
        console.log("Refreshing usage data...")
        loading = true
        const cmd = "c2switcher usage --json"
        executable.connectSource(cmd)
    }

    // Update accounts from JSON data
    function updateAccounts(accountsArray) {
        accounts = accountsArray

        // Calculate total usage (average of all accounts)
        let totalSevenDay = 0
        let totalOpus = 0
        let count = 0

        for (let i = 0; i < accounts.length; i++) {
            const acc = accounts[i]
            if (acc.usage) {
                if (acc.usage.seven_day && acc.usage.seven_day.utilization !== null) {
                    totalSevenDay += acc.usage.seven_day.utilization
                    count++
                }
                if (acc.usage.seven_day_opus && acc.usage.seven_day_opus.utilization !== null) {
                    totalOpus += acc.usage.seven_day_opus.utilization
                }
            }
        }

        totalUsage = count > 0 ? Math.round(totalSevenDay / count) : 0
        opusUsage = count > 0 ? Math.round(totalOpus / count) : 0
    }

    // Switch to optimal account
    function switchToOptimal() {
        console.log("Switching to optimal account...")
        const cmd = "c2switcher pick --switch"
        executable.connectSource(cmd)
    }

    // Switch to specific account
    function switchToAccount(index) {
        console.log("Switching to account:", index)
        const cmd = `c2switcher switch ${index}`
        executable.connectSource(cmd)
    }

    // Compact representation (panel icon)
    compactRepresentation: CompactView {
        usage: root.opusUsage
        loading: root.loading
        hasError: root.hasError

        MouseArea {
            anchors.fill: parent
            onClicked: root.expanded = !root.expanded
        }
    }

    // Full representation (popup)
    fullRepresentation: FullView {
        accounts: root.accounts
        loading: root.loading
        hasError: root.hasError

        onRefreshRequested: root.refreshData()
        onSwitchToOptimal: root.switchToOptimal()
        onSwitchToAccount: (index) => root.switchToAccount(index)
    }

    switchWidth: Kirigami.Units.gridUnit * 24
    switchHeight: Kirigami.Units.gridUnit * 18
}
