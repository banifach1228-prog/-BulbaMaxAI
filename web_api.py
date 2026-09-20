"""Compatibility entrypoint.

The Mini App API lives in miniapp_server.py. This module intentionally
re-exports the same server so an accidental start of web_api.py cannot
launch a second, incompatible API on port 8080.
"""
from miniapp_server import HOST, PORT, Handler, run_server

__all__ = ["HOST", "PORT", "Handler", "run_server"]


if __name__ == "__main__":
    run_server()
