#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────────
# install.sh — Install rapidwebs-sysstable (package + Hermes plugin)
#
# Usage:
#   ./install.sh                        # Install everything
#   ./install.sh --package-only         # Only pip-install the Python package
#   ./install.sh --plugin-only          # Only install the Hermes plugin
#   ./install.sh --help                 # Show this help
#
# The plugin can also be installed directly from GitHub:
#   hermes plugins install gh:stephanos8926-lgtm/rapidwebs-sysstable/hermes-plugin/rapidwebs-sysstable
# ──────────────────────────────────────────────────────────────────────────────

set -euo pipefail

# ── Config ────────────────────────────────────────────────────────────────────

PACKAGE_NAME="rw-sysstable"
PLUGIN_NAME="rapidwebs-sysstable"
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_SRC="$PROJECT_ROOT/hermes-plugin/$PLUGIN_NAME"
HERMES_PLUGINS_DIR="${HERMES_PLUGINS_DIR:-$HOME/.hermes/plugins}"
PLUGIN_INSTALL_DIR="$HERMES_PLUGINS_DIR/$PLUGIN_NAME"

SHOW_HELP=false
INSTALL_PACKAGE=true
INSTALL_PLUGIN=true

# ── Parse args ────────────────────────────────────────────────────────────────

for arg in "$@"; do
    case "$arg" in
        --help)      SHOW_HELP=true ;;
        --package-only) INSTALL_PLUGIN=false ;;
        --plugin-only)  INSTALL_PACKAGE=false ;;
        *)
            echo "❌ Unknown option: $arg"
            echo "   Usage: $0 [--help] [--package-only] [--plugin-only]"
            exit 1
            ;;
    esac
done

show_help() {
    cat <<'EOF'
Install rapidwebs-sysstable — Python package + Hermes plugin.

Usage:
  ./install.sh                        Install everything
  ./install.sh --package-only         Only pip-install the Python package
  ./install.sh --plugin-only          Only install the Hermes plugin
  ./install.sh --help                 Show this help

The plugin can also be installed directly from GitHub:
  hermes plugins install gh:stephanos8926-lgtm/rapidwebs-sysstable/hermes-plugin/rapidwebs-sysstable

Prerequisites:
  - Python >= 3.10
  - Linux (reads /proc; psutil sensors)
  - Hermes Agent (for plugin integration, optional for package-only)
EOF
    exit 0
}

$SHOW_HELP && show_help

# ── Install Python package ────────────────────────────────────────────────────

if $INSTALL_PACKAGE; then
    echo "📦 Installing $PACKAGE_NAME Python package..."

    if command -v uv &>/dev/null; then
        echo "   Using uv (fast)..."
        uv pip install -e "${PROJECT_ROOT}[dev]" 2>/dev/null || uv pip install -e "${PROJECT_ROOT}"
    elif command -v pip &>/dev/null; then
        echo "   Using pip..."
        pip install -e "${PROJECT_ROOT}[dev]" 2>/dev/null || pip install -e "${PROJECT_ROOT}"
    else
        echo "❌ No Python package manager found (pip or uv)."
        echo "   Install Python 3.10+ first: https://python.org"
        exit 1
    fi

    echo "   ✅ $PACKAGE_NAME installed"
    echo ""
fi

# ── Install Hermes plugin ────────────────────────────────────────────────────

if $INSTALL_PLUGIN; then
    echo "🔌 Installing $PLUGIN_NAME Hermes plugin..."

    if [ ! -d "$PLUGIN_SRC" ]; then
        echo "⚠️  Plugin source not found at $PLUGIN_SRC"
        echo "   (This is expected if you downloaded just the wheel from PyPI.)"
        echo "   Install from GitHub instead:"
        echo "   hermes plugins install gh:stephanos8926-lgtm/$PACKAGE_NAME/hermes-plugin/$PLUGIN_NAME"
        INSTALL_PLUGIN=false
    else
        mkdir -p "$PLUGIN_INSTALL_DIR"
        cp "$PLUGIN_SRC/__init__.py" "$PLUGIN_INSTALL_DIR/__init__.py"
        cp "$PLUGIN_SRC/plugin.yaml" "$PLUGIN_INSTALL_DIR/plugin.yaml"
        chmod 644 "$PLUGIN_INSTALL_DIR/__init__.py" "$PLUGIN_INSTALL_DIR/plugin.yaml"
        echo "   ✅ Plugin files copied to $PLUGIN_INSTALL_DIR"

        if command -v hermes &>/dev/null; then
            if hermes config set "plugins.$PLUGIN_NAME.enabled" true 2>/dev/null; then
                echo "   ✅ Plugin enabled via hermes config"
            else
                echo "   ⚠️  Could not auto-enable plugin."
                echo "   Run: hermes config set plugins.$PLUGIN_NAME.enabled true"
            fi
        else
            echo "   ⚠️  Hermes CLI not found. Plugin files installed but not enabled."
            echo "   Install Hermes: https://hermes-agent.nousresearch.com/docs"
            echo "   Then enable: hermes config set plugins.$PLUGIN_NAME.enabled true"
        fi
    fi
fi

# ── Summary ───────────────────────────────────────────────────────────────────

echo ""
echo "✅ Installation complete!"
echo ""
if $INSTALL_PACKAGE; then
    echo "   Package:  $PACKAGE_NAME installed"
    echo "   CLI:      sysstable --help"
fi
if $INSTALL_PLUGIN && [ -d "$PLUGIN_INSTALL_DIR" ]; then
    echo "   Plugin:   $PLUGIN_NAME installed at $PLUGIN_INSTALL_DIR"
fi
echo ""
echo "   Next steps:"
echo "     1. Initialize: sysstable init"
echo "     2. Start:      sysstable start"
echo "     3. Monitor:    sysstable status"
echo "     4. Verify:     hermes plugin list"
echo ""
echo "   Systemd (persistent):"
echo "     cp docs/sysstable.service ~/.config/systemd/user/"
echo "     systemctl --user daemon-reload"
echo "     systemctl --user enable --now sysstable"