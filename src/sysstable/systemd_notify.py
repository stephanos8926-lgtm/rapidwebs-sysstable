"""systemd integration utilities — watchdog, notify, and socket helpers."""

from __future__ import annotations

import logging
import os
import socket

logger = logging.getLogger(__name__)

_NOTIFY_SOCKET: str | None = None
_READY_SENT = False


def _get_notify_socket() -> str | None:
    """Return the NOTIFY_SOCKET path from the environment (if any)."""
    sock = os.environ.get("NOTIFY_SOCKET")
    if sock and sock.startswith("@"):
        # Abstract namespace socket — prepend null byte
        return "\0" + sock[1:]
    return sock


def sd_notify(state: str) -> bool:
    """Send a notification to systemd via the notify socket.

    Args:
        state: State string (e.g. "READY=1", "WATCHDOG=1", "STOPPING=1").

    Returns:
        True if notification was sent, False if not running under systemd.
    """
    global _NOTIFY_SOCKET
    if _NOTIFY_SOCKET is None:
        _NOTIFY_SOCKET = _get_notify_socket()

    if not _NOTIFY_SOCKET:
        return False  # Not running under systemd

    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        sock.connect(_NOTIFY_SOCKET)
        sock.sendall(state.encode())
        sock.close()
        return True
    except OSError as e:
        logger.debug("sd_notify failed: %s", e)
        return False


def notify_ready() -> None:
    """Notify systemd that the daemon has finished starting up."""
    global _READY_SENT
    if sd_notify("READY=1"):
        _READY_SENT = True
        logger.debug("Sent READY=1 to systemd")


def notify_watchdog() -> None:
    """Send watchdog ping to systemd (prevents service restart)."""
    if _READY_SENT:
        sd_notify("WATCHDOG=1")


def notify_stopping() -> None:
    """Notify systemd that the daemon is shutting down."""
    if _READY_SENT:
        sd_notify("STOPPING=1")
