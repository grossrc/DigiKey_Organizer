# gunicorn.conf.py
"""Production gunicorn config.

Two things happen here that the default command line cannot express:

1. The ngrok tunnel (if enabled) is opened once in the master process before
   workers fork, rather than once per worker.
2. A second loopback port is bound purely for tunnel traffic. nginx forwards LAN
   requests to the first port and ngrok forwards public requests to the second,
   so the app can tell where a request came from by looking at the listening
   socket instead of a Host header a caller could forge.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

lan_bind = os.getenv("GUNICORN_BIND", "127.0.0.1:5000")
public_port = int(os.getenv("MCP_PUBLIC_PORT", "5001"))

bind = [lan_bind, f"127.0.0.1:{public_port}"]
workers = int(os.getenv("GUNICORN_WORKERS", "2"))

# Workers read this to recognise requests that arrived through the tunnel.
os.environ["MCP_PUBLIC_PORT"] = str(public_port)


def on_starting(server):
    from mcp_server import maybe_start_tunnel

    # Workers inherit MCP_TUNNEL_HOST through the environment when forked.
    maybe_start_tunnel(port=public_port)
