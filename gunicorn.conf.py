# gunicorn.conf.py
"""Production gunicorn config.

Used so the ngrok tunnel (if enabled) is opened once in the master process
before workers fork, rather than once per worker.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

bind = os.getenv("GUNICORN_BIND", "127.0.0.1:5000")
workers = int(os.getenv("GUNICORN_WORKERS", "2"))


def on_starting(server):
    from mcp_server import maybe_start_tunnel

    # Workers inherit MCP_TUNNEL_HOST through the environment when forked.
    maybe_start_tunnel(port=int(bind.rsplit(":", 1)[-1]))
