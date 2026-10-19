"""ASGI entrypoint: ``uvicorn pulseops.api.main:app``."""

from pulseops.api.app import create_app

app = create_app()
