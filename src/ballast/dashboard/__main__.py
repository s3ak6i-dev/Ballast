"""Entry point:  python -m ballast.dashboard  →  http://127.0.0.1:8080

Environment overrides (used by the Docker image):
    BALLAST_HOST  bind address        (default 127.0.0.1; 0.0.0.0 in Docker)
    BALLAST_PORT  port                (default 8080)
    BALLAST_DB    SQLite event log    (default ./ballast_events.db)
"""

import os

import uvicorn

from .app import create_app


def main() -> None:
    host = os.environ.get("BALLAST_HOST", "127.0.0.1")
    port = int(os.environ.get("BALLAST_PORT", "8080"))
    db_path = os.environ.get("BALLAST_DB", "ballast_events.db")
    uvicorn.run(create_app(db_path=db_path), host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
