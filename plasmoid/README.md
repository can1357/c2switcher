# Claude Code Usage - KDE Plasmoid

A beautiful KDE Plasma widget that displays Claude Code usage across all your accounts.

## Features

- **Live usage monitoring** - Polls every 60 seconds
- **Compact panel view** - Shows average Opus usage with color-coded badge
- **Detailed popup** - Shows combined usage bar with ticks for each account
- **Account cards** - View individual account usage (5h, 7d, Opus)
- **Quick switching** - Switch to optimal account or any specific account
- **No services required** - Directly executes c2switcher commands

## Preview

**Panel View:**
- Icon with percentage badge
- Green: <70% usage
- Orange: 70-90% usage
- Red: >90% usage
- Pulse animation when usage is critical

**Popup View:**
- Combined usage bar showing cumulative usage
- Ticks representing each account
- Color-coded legend
- Account cards with detailed usage
- Switch to optimal or specific account buttons

## Installation

```bash
cd /home/can/Projects/claude-switcher
./install-plasmoid.sh
```

Or manually:

```bash
kpackagetool6 --type=Plasma/Applet --install plasmoid
```

## Usage

1. Right-click on your panel
2. Click "Add Widgets..."
3. Search for "Claude Code Usage"
4. Drag to your panel

The widget will:
- Poll `c2switcher usage --json` every 60 seconds
- Show average Opus usage in the panel badge
- Display detailed usage when clicked
- Allow quick account switching

## Requirements

- KDE Plasma 6.0 or later
- c2switcher installed and in PATH
- At least one account configured in c2switcher

## Configuration

The widget has no configuration - it automatically discovers accounts from c2switcher.

Update frequency: 60 seconds (hardcoded in main.qml)

## Uninstall

```bash
kpackagetool6 --type=Plasma/Applet --remove org.claudecode.usage.plasma
```

## Architecture

- **main.qml** - Main logic, executes commands and manages state
- **CompactView.qml** - Panel icon with usage badge
- **FullView.qml** - Popup with detailed usage
- **UsageBar.qml** - Combined usage bar with ticks
- **AccountCard.qml** - Individual account display

No D-Bus services or background processes required!
