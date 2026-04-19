"""
One-command launcher for the NOVA browser-rendered dashboard.

1. Starts nova_ui_server.py on port 7336 in a daemon thread.
2. Waits for the server to be healthy.
3. Opens http://127.0.0.1:7336/ in the default browser (via CDP if Edge
   with --remote-debugging-port=9222 is running; else webbrowser.open).
4. Exits — leaves the server running.

If you want the full assistant daemon too (voice wake word, scheduler,
trading agent, drift monitor), run `python nova_assistant.py` in a
separate terminal AFTER this launcher.
"""
from __future__ import annotations

import os
import sys
import threading
import time
import webbrowser


def _start_server() -> None:
    """Boot nova_ui_server in-process on a daemon thread."""
    import uvicorn
    from nova_ui_server import app, HOST, PORT
    config = uvicorn.Config(app, host=HOST, port=PORT, log_level="warning")
    server = uvicorn.Server(config)
    threading.Thread(target=server.run, daemon=True, name="nova-ui-server").start()


def _wait_healthy(timeout: float = 10.0) -> bool:
    try:
        import httpx
    except Exception:
        time.sleep(1.0)
        return True
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            r = httpx.get("http://127.0.0.1:7336/health", timeout=1.0)
            if r.status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(0.2)
    return False


def _open_in_edge_cdp(url: str) -> bool:
    """If Edge is running with CDP on 9222, open the URL as a new tab there."""
    try:
        import httpx
        r = httpx.put(f"http://localhost:9222/json/new?{url}", timeout=1.5)
        return r.status_code == 200
    except Exception:
        return False


def main() -> None:
    print("[nova_launch_ui] booting server...")
    _start_server()

    if not _wait_healthy():
        print("[nova_launch_ui] WARNING: server didn't become healthy in 10s", file=sys.stderr)
    else:
        print("[nova_launch_ui] server online at http://127.0.0.1:7336")

    url = "http://127.0.0.1:7336/"
    if _open_in_edge_cdp(url):
        print(f"[nova_launch_ui] opened in existing Edge tab via CDP: {url}")
    else:
        print(f"[nova_launch_ui] opening via webbrowser.open: {url}")
        webbrowser.open(url)

    print("[nova_launch_ui] dashboard live. Leaving server running (daemon thread).")
    print("[nova_launch_ui] run `python nova_assistant.py` in another terminal for full voice daemon.")
    # Keep the main thread alive so the daemon server stays up.
    try:
        while True:
            time.sleep(30)
    except KeyboardInterrupt:
        print("[nova_launch_ui] shutting down")


if __name__ == "__main__":
    main()
