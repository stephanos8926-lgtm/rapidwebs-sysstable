"""Unix socket server — CLI ↔ daemon communication."""

from __future__ import annotations

import json
import logging
import os
import socket
import threading
from pathlib import Path
from typing import Any

logger = logging.getLogger("sysstable.socketd")


class SocketServer:
    """Unix socket server running inside the daemon."""

    def __init__(self, socket_path: str | Path):
        self.socket_path = Path(socket_path).expanduser()
        self._server: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._running = False

    def start(self, db_path: str) -> None:
        """Start the socket server in a background thread."""
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        if self.socket_path.exists():
            self.socket_path.unlink()

        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(str(self.socket_path))
        self._server.listen(1)
        os.chmod(str(self.socket_path), 0o600)
        self._running = True
        self._db_path = db_path

        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        logger.info("Socket server listening at %s", self.socket_path)

    def stop(self) -> None:
        self._running = False
        if self._server:
            try:
                self._server.close()
            except OSError:
                pass
        if self.socket_path.exists():
            self.socket_path.unlink()

    def _serve(self) -> None:
        while self._running:
            try:
                conn, _ = self._server.accept()  # type: ignore[union-attr]
                data = conn.recv(4096)
                if not data:
                    conn.close()
                    continue
                response = self._handle_request(data.decode().strip())
                conn.sendall(json.dumps(response).encode())
                conn.close()
            except (TimeoutError, OSError):
                continue

    def _handle_request(self, raw: str) -> dict[str, Any]:
        try:
            req = json.loads(raw)
        except json.JSONDecodeError:
            return {"error": "invalid JSON"}

        action = req.get("action", "")
        if action == "ping":
            return {"pong": True}
        if action == "metrics_latest":
            from .database import MetricsDB

            db = MetricsDB(self._db_path)
            latest = db.get_latest()
            db.close()
            return {"metrics": latest}
        if action == "metrics_recent":
            count = req.get("count", 5)
            from .database import MetricsDB

            db = MetricsDB(self._db_path)
            recent = db.query_recent(limit=count)
            db.close()
            return {"metrics": recent}
        if action == "count":
            from .database import MetricsDB

            db = MetricsDB(self._db_path)
            c = db.count()
            db.close()
            return {"count": c}
        if action == "stop":
            # Signal daemon to stop
            import os

            os.kill(os.getpid(), 15)
            return {"stopped": True}

        return {"error": f"unknown action: {action}"}


def query_daemon(socket_path: str | Path, action: str, **kwargs: Any) -> dict[str, Any]:
    """Query the daemon via unix socket. Used by the CLI."""
    sock_path = Path(socket_path).expanduser()
    if not sock_path.exists():
        return {"error": "daemon not running"}

    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(5)
        sock.connect(str(sock_path))
        payload = json.dumps({"action": action, **kwargs})
        sock.sendall(payload.encode())
        response = sock.recv(65536)
        sock.close()
        return json.loads(response.decode())
    except (TimeoutError, ConnectionRefusedError, OSError) as e:
        return {"error": f"daemon communication failed: {e}"}
