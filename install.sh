#!/bin/bash

# rapidwebs-sysstable Hermes Plugin Installer
#
# Installs the rapidwebs-sysstable Hermes plugin and provides instructions
# for enabling it.

# Define plugin details
PLUGIN_NAME="rapidwebs-sysstable"
INSTALL_DIR="$HOME/.hermes/plugins/${PLUGIN_NAME}/"
PLUGIN_SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}" )" && pwd)/hermes-plugin/${PLUGIN_NAME}"

# --- Helper Functions ---

# Function to print usage information
usage() {
  echo "Usage: $0 [--help]"
  echo "Installs the ${PLUGIN_NAME} Hermes plugin."
  echo ""
  echo "Options:"
  echo "  --help    Display this help message and exit"
  echo ""
  echo "After installation, you may need to enable the plugin via 'hermes config set plugins.${PLUGIN_NAME}.enabled true'"
}

# Function to check for Hermes CLI
check_hermes() {
  if ! command -v hermes &> /dev/null;
  then
    echo "Error: Hermes CLI not found. Please install Hermes first."
    echo "See: https://docs.hermes-agent.dev/installation/cli"
    exit 1
  fi
}

# Function to install the plugin
install_plugin() {
  echo "Installing ${PLUGIN_NAME} plugin to ${INSTALL_DIR}..."

  # Create installation directory if it doesn't exist
  mkdir -p "$INSTALL_DIR"
  if [ $? -ne 0 ]; then
    echo "Error: Could not create installation directory: ${INSTALL_DIR}"
    exit 1
  fi

  # Copy plugin files
  cp -a "$PLUGIN_SOURCE_DIR/." "$INSTALL_DIR"
  if [ $? -ne 0 ]; then
    echo "Error: Failed to copy plugin files from ${PLUGIN_SOURCE_DIR} to ${INSTALL_DIR}"
    exit 1
  fi

  echo "Plugin installed successfully."
}

# Function to enable the plugin
enable_plugin() {
  echo "Attempting to enable The ${PLUGIN_NAME} plugin..."
  if hermes config get plugins.${PLUGIN_NAME}.enabled &> /dev/null;
  then
    if hermes config get plugins.${PLUGIN_NAME}.enabled | grep -q "true"; then
      echo "Plugin is already enabled."
    else
      echo "Enabling plugin via 'hermes config set plugins.${PLUGIN_NAME}.enabled true'..."
      hermes config set plugins.${PLUGIN_NAME}.enabled true
      if [ $? -ne 0 ]; then
        echo "Warning: Failed to automatically enable plugin. Please enable it manually:"
        echo "  hermes config set plugins.${PLUGIN_NAME}.enabled true"
      else
        echo "Plugin enabled. You may need to restart Hermes for changes to take effect."
      fi
    fi
  else
    echo "Plugin configuration not found. Please enable it manually:"
    echo "  hermes config set plugins.${PLUGIN_NAME}.enabled true"
  fi
}

# --- Main Execution ---

# Parse command-line arguments
if [[ "$1" == "--help" ]]; then
  usage
  exit 0
fi

# Check if running in the correct directory
if [ ! -d "$PLUGIN_SOURCE_DIR" ]; then
  echo "Error: Script must be run from the project root directory."
  echo "Expected to find plugin source at: ${PLUGIN_SOURCE_DIR}"
  exit 1
fi

# Check for Hermes CLI
check_hermes

# Install the plugin
install_plugin

# Enable the plugin
enable_plugin

echo "Installation complete."
exit 0
