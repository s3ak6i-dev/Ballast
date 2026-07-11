"""Ballast dashboard (M4): FastAPI backend + live web UI.

Run with:  python -m ballast.dashboard   →  http://127.0.0.1:8080
Requires the dashboard extra:  pip install ballast[dashboard]
"""

from .app import create_app

__all__ = ["create_app"]
