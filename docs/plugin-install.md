# Installing the rapidwebs-sysstable Hermes Plugin

This document provides instructions for installing and configuring the `rapidwebs-sysstable` Hermes plugin.

## About the Plugin

The `rapidwebs-sysstable` plugin enhances Hermes by integrating system stability monitoring. It reads system metrics and status from a background daemon, providing context to Hermes to manage resource pressure and prevent overload.

## Installation Methods

There are two primary methods for installing the plugin:

### 1. Local Source Installation

This method is recommended when you have the plugin source code available locally (e.g., after cloning the repository).

**Prerequisites:**
*   Ensure you have the Hermes CLI installed.
*   Navigate to the root of the `rapidwebs-sysstable` project in your terminal.

**Steps:**
1.  Make the install script executable:
    ```bash
    chmod +x install.sh
    ```
2.  Run the installation script:
    ```bash
    ./install.sh
    ```
3.  The script will:
    *   Check if the Hermes CLI is installed.
    *   Copy the plugin files to `~/.hermes/plugins/rapidwebs-sysstable/`.
    *   Attempt to enable the plugin using `hermes config set plugins.rapidwebs-sysstable.enabled true`. If automatic enabling fails, it will provide manual instructions.

### 2. Direct GitHub Installation

You can install the plugin directly from its GitHub repository using the Hermes CLI.

**Command:**
```bash
hermes plugins install gh:stephanos8926-lgtm/rapidwebs-sysstable/hermes-plugin/rapidwebs-sysstable
```

This command will fetch the plugin from the specified GitHub repository and install it into your Hermes plugins directory.

## Verifying Installation

After installation, you can verify that the plugin is recognized and active.

### Listing Installed Plugins

Use the Hermes CLI to list all installed plugins:
```bash
hermes plugin list
```
You should see `rapidwebs-sysstable` in the list, with its status indicating it's enabled.

### Checking `state.json`

The plugin relies on a `state.json` file managed by the `sysstabled` daemon. This file indicates the current system resource pressure. If the plugin is active, you should expect communication between the daemon and Hermes. The `state.json` file is typically located at `~/.hermes/plugins/rapidwebs-sysstable/state.json`.

## Configuration Reference

The `rapidwebs-sysstable` plugin's behavior is controlled by configuration settings, primarily managed by the `sysstabled` daemon and potentially through Hermes's configuration system.

### `sysstabled` Configuration

The `sysstabled` daemon's configuration (e.g., thresholds, data retention) is typically managed via its own configuration files or command-line arguments. Refer to the `sysstabled` documentation for details on:
*   Setting system resource thresholds (e.g., RAM, CPU, disk usage, temperature).
*   Configuring data retention policies for historical metrics.
*   Managing the daemon's frequency of checks.

### Hermes Plugin Configuration

You can enable or disable the plugin in Hermes:
*   **Enable/Disable:**
    ```bash
    # Enable
    hermes config set plugins.rapidwebs-sysstable.enabled true

    # Disable
    hermes config set plugins.rapidwebs-sysstable.enabled false
    ```

### Environment Variables

*   `SYSSTABLE_STATE_PATH`: Override the default path for the `state.json` file (default: `~/.hermes/plugins/rapidwebs-sysstable/state.json`).

## Troubleshooting

### Plugin Not Appearing in `hermes plugin list`

*   **Check Installation Path:** Ensure the plugin was copied to the correct directory (`~/.hermes/plugins/rapidwebs-sysstable/`).
*   **Verify `hermes config`:** Make sure the plugin is enabled via `hermes config set plugins.rapidwebs-sysstable.enabled true`.
*   **Restart Hermes:** Sometimes, Hermes needs to be restarted for newly installed plugins to be recognized.

### Plugin Not Affecting Behavior (e.g., No Warnings)

*   **Check `sysstabled` Daemon:** Ensure the `sysstabled` background daemon is running and active. Check its logs or status (e.g., `systemctl --user status sysstable` if installed as a service).
*   **Verify `state.json`:** Confirm that `state.json` exists and is being updated by the `sysstabled` daemon. The path is usually `~/.hermes/plugins/rapidwebs-sysstable/state.json`.
*   **Check System Load:** The plugin only injects context or blocks operations when system resource usage crosses configured thresholds (yellow, orange, red, critical). Ensure your system is under sufficient load for these thresholds to be met.
*   **Review `sysstabled` Logs:** Examine the logs of the `sysstabled` daemon for any errors related to reading metrics or writing `state.json`.

### Installation Script Errors

*   **Hermes Not Found:** Ensure the `hermes` command is available in your PATH.
*   **Permissions:** Run the `./install.sh` script with `chmod +x` to ensure it has execute permissions.
*   **Incorrect Directory:** Verify you are running the `install.sh` script from the root of the `rapidwebs-sysstable` project directory.
