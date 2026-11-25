"""Compatibility entry point.

``uvicorn server:app`` keeps working, but the application now lives in ``app/main.py``
(preferred: ``uvicorn app.main:app``). The original single-file backend is preserved
unchanged in ``legacy/server.py``.
"""
from app.main import app, create_app  # noqa: F401
