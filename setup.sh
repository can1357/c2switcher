#!/usr/bin/env bash
# setup.sh - Unified installer for c2switcher and plasmoid
#
# Usage:
#   ./setup.sh              # Interactive mode (asks what to install)
#   ./setup.sh --all        # Install everything
#   ./setup.sh --cli        # Install CLI only
#   ./setup.sh --plasmoid   # Install plasmoid only (requires CLI)
#   ./setup.sh --uninstall  # Uninstall everything

set -e

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

# Configuration
INSTALL_DIR="/usr/local/bin"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLASMOID_DIR="$SCRIPT_DIR/plasmoid"
PLASMOID_ID="org.claudecode.usage.plasma"

# Parse arguments
INSTALL_CLI=false
INSTALL_PLASMOID=false
UNINSTALL=false
INTERACTIVE=true

for arg in "$@"; do
    case "$arg" in
        --all)
            INSTALL_CLI=true
            INSTALL_PLASMOID=true
            INTERACTIVE=false
            ;;
        --cli)
            INSTALL_CLI=true
            INTERACTIVE=false
            ;;
        --plasmoid)
            INSTALL_PLASMOID=true
            INTERACTIVE=false
            ;;
        --uninstall)
            UNINSTALL=true
            INTERACTIVE=false
            ;;
        *)
            echo -e "${RED}Unknown option: $arg${NC}"
            echo "Usage: $0 [--all|--cli|--plasmoid|--uninstall]"
            exit 1
            ;;
    esac
done

# Header
echo -e "${CYAN}════════════════════════════════════════════════════════════${NC}"
echo -e "${CYAN}  C2Switcher Setup${NC}"
echo -e "${CYAN}════════════════════════════════════════════════════════════${NC}"
echo

# Uninstall mode
if [[ "$UNINSTALL" == true ]]; then
    echo -e "${YELLOW}Uninstalling c2switcher...${NC}"
    echo

    # Remove CLI files
    if [[ -f "$INSTALL_DIR/c2switcher" ]] || [[ -f "$INSTALL_DIR/c2claude" ]]; then
        echo -e "${BLUE}Removing CLI tools...${NC}"
        sudo rm -f "$INSTALL_DIR/c2switcher" "$INSTALL_DIR/c2claude"
        echo -e "${GREEN}✓ CLI tools removed${NC}"
    fi

    # Remove plasmoid
    if kpackagetool6 --type=Plasma/Applet --show="$PLASMOID_ID" &> /dev/null; then
        echo -e "${BLUE}Removing plasmoid...${NC}"
        kpackagetool6 --type=Plasma/Applet --remove "$PLASMOID_ID"
        echo -e "${GREEN}✓ Plasmoid removed${NC}"
    fi

    echo
    echo -e "${GREEN}Uninstall complete!${NC}"
    echo -e "${YELLOW}Note: Database at ~/.c2switcher.db was NOT removed${NC}"
    echo "      Remove it manually if desired: rm ~/.c2switcher.db"
    echo
    exit 0
fi

# Interactive mode
if [[ "$INTERACTIVE" == true ]]; then
    echo "What would you like to install?"
    echo "  1) CLI tools only"
    echo "  2) CLI tools + KDE Plasmoid"
    echo "  3) KDE Plasmoid only (requires CLI already installed)"
    echo "  4) Uninstall everything"
    echo
    read -p "Enter choice [1-4]: " choice

    case "$choice" in
        1)
            INSTALL_CLI=true
            ;;
        2)
            INSTALL_CLI=true
            INSTALL_PLASMOID=true
            ;;
        3)
            INSTALL_PLASMOID=true
            ;;
        4)
            exec "$0" --uninstall
            ;;
        *)
            echo -e "${RED}Invalid choice${NC}"
            exit 1
            ;;
    esac
    echo
fi

# Check for sudo if installing CLI
if [[ "$INSTALL_CLI" == true ]]; then
    echo -e "${YELLOW}CLI installation requires sudo privileges${NC}"
    if ! sudo -v; then
        echo -e "${RED}Error: sudo access required${NC}"
        exit 1
    fi
    echo
fi

# Install CLI
if [[ "$INSTALL_CLI" == true ]]; then
    echo -e "${GREEN}Installing CLI tools...${NC}"
    echo

    # Create install directory if it doesn't exist
    if [[ ! -d "$INSTALL_DIR" ]]; then
        echo -e "${YELLOW}Creating $INSTALL_DIR${NC}"
        sudo mkdir -p "$INSTALL_DIR"
    fi

    # Check for Python dependencies
    echo -e "${BLUE}Checking Python dependencies...${NC}"
    missing_deps=()

    for dep in click rich requests psutil filelock; do
        if ! python3 -c "import $dep" 2>/dev/null; then
            missing_deps+=("$dep")
        fi
    done

    if [ ${#missing_deps[@]} -gt 0 ]; then
        echo -e "${YELLOW}Missing dependencies: ${missing_deps[*]}${NC}"
        echo "Installing dependencies..."
        pip3 install --user -r "$SCRIPT_DIR/requirements.txt"
        echo
    else
        echo -e "${GREEN}✓ All dependencies installed${NC}"
    fi

    # Install c2switcher
    echo -e "${BLUE}Installing c2switcher...${NC}"
    sudo cp "$SCRIPT_DIR/c2switcher.py" "$INSTALL_DIR/c2switcher"
    sudo chmod +x "$INSTALL_DIR/c2switcher"
    echo -e "${GREEN}✓ c2switcher installed${NC}"

    # Install c2claude
    echo -e "${BLUE}Installing c2claude...${NC}"
    sudo cp "$SCRIPT_DIR/c2claude" "$INSTALL_DIR/c2claude"
    sudo chmod +x "$INSTALL_DIR/c2claude"
    echo -e "${GREEN}✓ c2claude installed${NC}"

    echo

    # Check PATH
    if [[ ":$PATH:" != *":$INSTALL_DIR:"* ]]; then
        echo -e "${YELLOW}Warning: $INSTALL_DIR is not in your PATH${NC}"
        echo "Add the following to your ~/.bashrc or ~/.zshrc:"
        echo "  export PATH=\"$INSTALL_DIR:\$PATH\""
        echo
    else
        echo -e "${GREEN}✓ $INSTALL_DIR is in your PATH${NC}"
    fi
fi

# Install plasmoid
if [[ "$INSTALL_PLASMOID" == true ]]; then
    echo -e "${GREEN}Installing KDE Plasmoid...${NC}"
    echo

    # Check if c2switcher is installed
    if ! command -v c2switcher &> /dev/null; then
        echo -e "${YELLOW}⚠ Warning: c2switcher command not found${NC}"
        echo "  The plasmoid requires c2switcher to be installed first."
        echo
        read -p "Continue anyway? (y/N) " -n 1 -r
        echo
        if [[ ! $REPLY =~ ^[Yy]$ ]]; then
            exit 1
        fi
    fi

    # Check if plasmoid is already installed
    if kpackagetool6 --type=Plasma/Applet --show="$PLASMOID_ID" &> /dev/null; then
        echo -e "${BLUE}Plasmoid already installed, upgrading...${NC}"
        kpackagetool6 --type=Plasma/Applet --upgrade "$PLASMOID_DIR"
        echo -e "${GREEN}✓ Plasmoid upgraded${NC}"
        echo
        echo -e "${YELLOW}Note: You may need to restart plasmashell for changes to take effect:${NC}"
        echo "  systemctl --user restart plasma-plasmashell"
        echo "  Or remove and re-add the widget to your panel"
    else
        echo -e "${BLUE}Installing plasmoid...${NC}"
        kpackagetool6 --type=Plasma/Applet --install "$PLASMOID_DIR"
        echo -e "${GREEN}✓ Plasmoid installed${NC}"
    fi
    echo
fi

# Final message
echo -e "${CYAN}════════════════════════════════════════════════════════════${NC}"
echo -e "${GREEN}✅ Installation complete!${NC}"
echo -e "${CYAN}════════════════════════════════════════════════════════════${NC}"
echo

if [[ "$INSTALL_CLI" == true ]]; then
    echo -e "${BLUE}CLI Tools:${NC}"
    echo "  c2switcher - Manage Claude Code accounts"
    echo "  c2claude   - Run Claude with account switching"
    echo
    echo -e "${BLUE}Quick Start:${NC}"
    echo "  1. Add your first account:"
    echo "     $ c2switcher add"
    echo
    echo "  2. Check usage across accounts:"
    echo "     $ c2switcher usage"
    echo
    echo "  3. Run Claude with optimal account:"
    echo "     $ c2claude"
    echo
fi

if [[ "$INSTALL_PLASMOID" == true ]]; then
    echo -e "${BLUE}KDE Plasmoid:${NC}"
    echo "  1. Right-click on your panel"
    echo "  2. Click 'Add Widgets...'"
    echo "  3. Search for 'Claude Code Usage'"
    echo "  4. Drag it to your panel"
    echo
fi

echo -e "${CYAN}Documentation: ${NC}https://github.com/can1357/c2switcher"
echo
