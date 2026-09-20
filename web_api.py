"""Compatibility entrypoint.

The project has one HTTP API: miniapp_server.py. This module exists only so
older hosting commands that still start web_api.py continue to work.
"""
from miniapp_server import run_server


if __name__ == "__main__":
    run_server()
