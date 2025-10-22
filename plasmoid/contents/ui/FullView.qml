import QtQuick
import QtQuick.Controls as QQC2
import QtQuick.Layouts
import org.kde.plasma.components as PlasmaComponents3
import org.kde.plasma.extras as PlasmaExtras
import org.kde.kirigami as Kirigami

Item {
    id: fullView

    property var accounts: []
    property bool hasError: false
    property bool loading: false

    signal refreshRequested()
    signal switchToOptimal()
    signal switchToAccount(int index)

    Layout.fillWidth: true
    Layout.fillHeight: true
    Layout.minimumWidth: Kirigami.Units.gridUnit * 24
    Layout.minimumHeight: Kirigami.Units.gridUnit * 18

    // Dark semi-transparent background
    Rectangle {
        anchors.fill: parent
        color: Qt.rgba(0, 0, 0, 0.4)
        radius: 8
    }

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: Kirigami.Units.largeSpacing
        spacing: Kirigami.Units.largeSpacing

        // Minimal header with actions
        RowLayout {
            Layout.fillWidth: true
            spacing: Kirigami.Units.smallSpacing

            PlasmaComponents3.Label {
                text: "Claude Switcher"
                font.pointSize: Kirigami.Theme.defaultFont.pointSize * 1.1
                font.weight: Font.Medium
                opacity: 0.6
            }

            Item {
                Layout.fillWidth: true
            }

            // Refresh button
            PlasmaComponents3.ToolButton {
                icon.name: "view-refresh"
                onClicked: refreshRequested()
                enabled: !hasError && !loading
                opacity: 0.7

                QQC2.ToolTip.visible: hovered
                QQC2.ToolTip.text: "Refresh"
            }

            // Optimal switch button
            PlasmaComponents3.ToolButton {
                icon.name: "starred-symbolic"
                onClicked: switchToOptimal()
                enabled: !hasError && accounts.length > 0
                opacity: 0.7

                QQC2.ToolTip.visible: hovered
                QQC2.ToolTip.text: "Switch to optimal"
            }
        }

        // Main content
        Item {
            Layout.fillWidth: true
            Layout.fillHeight: true

            // Error view
            ColumnLayout {
                anchors.centerIn: parent
                spacing: Kirigami.Units.largeSpacing
                visible: hasError && !loading

                Kirigami.Icon {
                    source: "data-warning"
                    Layout.alignment: Qt.AlignHCenter
                    Layout.preferredWidth: Kirigami.Units.iconSizes.huge
                    Layout.preferredHeight: Kirigami.Units.iconSizes.huge
                    opacity: 0.5
                }

                PlasmaComponents3.Label {
                    text: "Error Loading Data"
                    font.pointSize: Kirigami.Theme.defaultFont.pointSize * 1.2
                    Layout.alignment: Qt.AlignHCenter
                }

                PlasmaComponents3.Label {
                    text: "Make sure c2switcher is installed"
                    opacity: 0.7
                    Layout.alignment: Qt.AlignHCenter
                }
            }

            // Loading view
            ColumnLayout {
                anchors.centerIn: parent
                spacing: Kirigami.Units.largeSpacing
                visible: loading && accounts.length === 0

                PlasmaComponents3.BusyIndicator {
                    Layout.alignment: Qt.AlignHCenter
                    running: true
                }

                PlasmaComponents3.Label {
                    text: "Loading usage data..."
                    Layout.alignment: Qt.AlignHCenter
                }
            }

            // Main content area
            ColumnLayout {
                anchors.fill: parent
                spacing: Kirigami.Units.largeSpacing
                visible: !hasError && accounts.length > 0

                // Usage bar
                UsageBar {
                    id: usageBar
                    Layout.fillWidth: true
                    Layout.preferredHeight: Kirigami.Units.gridUnit * 7.5
                    accounts: fullView.accounts
                }

                // Spacer
                Item {
                    Layout.fillWidth: true
                    Layout.preferredHeight: Kirigami.Units.largeSpacing
                }

                // Account list
                QQC2.ScrollView {
                    Layout.fillWidth: true
                    Layout.fillHeight: true

                    ListView {
                        id: accountList
                        clip: true
                        spacing: Kirigami.Units.smallSpacing

                        model: fullView.accounts

                        delegate: AccountCard {
                            width: ListView.view.width
                            accountData: modelData
                            onSwitchClicked: fullView.switchToAccount(model.index)
                        }
                    }
                }
            }
        }
    }
}
