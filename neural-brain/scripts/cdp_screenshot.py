"""
Capture a PNG screenshot of a specific browser tab via CDP.

Usage:
  python cdp_screenshot.py <target_id_prefix> <output_path>

Example:
  python cdp_screenshot.py FE556D91 C:/Users/User/Desktop/ig-ref.png
"""
import asyncio
import base64
import json
import sys

import httpx
import websockets


async def capture(target_prefix: str, out_path: str) -> None:
    # List tabs
    r = httpx.get("http://localhost:9222/json", timeout=5.0)
    tabs = r.json()
    match = next((t for t in tabs if t.get("id", "").startswith(target_prefix)), None)
    if not match:
        raise RuntimeError(f"No tab matches prefix '{target_prefix}'")

    ws_url = match["webSocketDebuggerUrl"]
    print(f"connecting: {match['url'][:80]}")

    async with websockets.connect(ws_url, max_size=32 * 1024 * 1024) as ws:
        # Page.captureScreenshot returns {data: base64-png}
        await ws.send(json.dumps({
            "id": 1,
            "method": "Page.captureScreenshot",
            "params": {"format": "png", "captureBeyondViewport": False},
        }))
        while True:
            msg = await ws.recv()
            data = json.loads(msg)
            if data.get("id") == 1:
                if "result" in data and "data" in data["result"]:
                    png = base64.b64decode(data["result"]["data"])
                    with open(out_path, "wb") as f:
                        f.write(png)
                    print(f"saved: {out_path} ({len(png)} bytes)")
                    return
                else:
                    raise RuntimeError(f"capture failed: {data}")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(1)
    asyncio.run(capture(sys.argv[1], sys.argv[2]))
