#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────────
# install.sh — Install the rapidwebs-sysstable Hermes Plugin
#
# Usage:
#   ./install.sh              # Install plugin to ~/.hermes/plugins/
#   ./install.sh --help       # Show this help
#   ./install.sh --force      # Overwrite existing installation
#
# The plugin can also be installed directly from GitHub:
#   hermes plugins install gh:stephanos8926-lgtm/rapidwebs-sysstable/hermes-plugin/rapidwebs-sysstable
# ──────────────────────────────────────────────────────────────────────────────

set -euo pipefail

PLUGIN_NAME="rapidwebs-sysstable"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_SRC="$SCRIPT_DIR"
HERMES_PLUGINS_DIR="${HERMES_PLUGINS_DIR:-$HOME/.hermes/plugins}"
INSTALL_DIR="$HERMES_PLUGINS_DIR/$PLUGIN_NAME"

show_help() {
    cat <<'EOF'
Install the rapidwebs-sysstable Hermes Plugin.

Usage:
  ./install.sh              Install plugin to ~/.hermes/plugins/
  ./install.sh --help       Show this help
  ./install.sh --force      Overwrite existing installation

The plugin can also be installed directly from GitHub:
  hermes plugins install gh:stephanos8926-lgtm/rapidwebs-sysstable/hermes-plugin/rapidwebs-sysstable
EOF
    exit 0
}

FORCE=false
for arg in "$@"; do
    case "$arg" in
        --help) show_help ;;
        --force) FORCE=true ;;
        *)
            echo "Unknown option: $arg"
            echo "Usage: $0 [--help] [--force]"
            exit 1
            ;;
    esac
done

# ── Check prerequisites ──────────────────────────────────────────────────────

if ! command -v hermes &>/dev/null; then
    echo "⚠️  Hermes CLI not found in PATH."
    echo "   Install Hermes first: https://hermes-agent.nousresearch.com/docs"
    echo ""
    echo "   To install manually, copy this directory to:"
    echo "   $INSTALL_DIR"
    echo "   Then run: hermes config set plugins.$PLUGIN_NAME.enabled true"
    exit 1
fi

echo "🔍 Found Hermes CLI: $(command -v hermes)"

# ── Check for existing installation ──────────────────────────────────────────

if [ -d "$INSTALL_DIR" ] && [ "$FORCE" != true ]; then
    echo "⚠️  Plugin already installed at $INSTALL_DIR"
    echo "   Use --force to overwrite, or upgrade with:"
    echo "   hermes plugins install gh:stephanos8926-lgtm/rapidwebs-sysstable/hermes-plugin/rapidwebs-sysstable"
    exit 0
fi

# ── Install ──────────────────────────────────────────────────────────────────

echo "📦 Installing $PLUGIN_NAME plugin..."
mkdir -p "$INSTALL_DIR"

cp "$PLUGIN_SRC/__init__.py" "$INSTALL_DIR/__init__.py"
cp "$PLUGIN_SRC/plugin.yaml" "$INSTALL_DIR/plugin.yaml"
chmod 644 "$INSTALL_DIR/__init__.py" "$INSTALL_DIR/plugin.yaml"

echo "   ✓ Copied plugin files to $INSTALL_DIR"

# ── Enable plugin ────────────────────────────────────────────────────────────

if hermes config set "plugins.$PLUGIN_NAME.enabled" true 2>/dev/null; then
    echo "   ✓ Plugin enabled via hermes config"
else
    echo "   ⚠️  Could not auto-enable plugin."
    echo "   Run: hermes config set plugins.$PLUGIN_NAME.enabled true"
fi

echo ""
echo "✅ $PLUGIN_NAME v$(head -2 "$PLUGIN_SRC/plugin.yaml" | grep version | cut -d'"' -f2) installed!"
echo ""
echo "   Next steps:"
echo "     1. Start the sysstabled daemon: sysstable start"
echo "     2. Verify plugin reads state: hermes plugin list"
echo "     3. Configure thresholds: sysstable init"