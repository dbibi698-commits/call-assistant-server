"""Smoke test for the Mith Hera Call Assistant server.

Starts the server locally (no API keys — the honest no-key path),
then checks:
  * /health returns 200
  * /call page loads
  * /api/rooms returns call + join URLs
  * a WebSocket call gets the Bengali greeting (as a "say" event, since
    no TTS key is set — the browser would speak it) and a scripted
    reply to a transcript, proving the listen-first loop works
  * on call end, a recording file is created and the call + the
    "Ei phone koreche" notification appear in /calls + /notifications
  * the optional Plivo adapter is OFF (404) by default

Usage:  python tests/smoke_test.py
"""

import asyncio
import json
import os
import subprocess
import sys
import time

import requests

BASE = "http://127.0.0.1:8123"
SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def check(name, ok, detail=""):
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {detail}")
    if not ok:
        raise SystemExit(1)


async def ws_call_test():
    import websockets

    room = "smoke-room-1"
    async with websockets.connect(f"ws://127.0.0.1:8123/ws/call/{room}?role=caller") as ws:
        await ws.send(json.dumps({
            "event": "start",
            "start": {"callId": room, "from": "+919999999999", "fromName": "Smoke Test"},
        }))
        # Greeting must arrive (as "say" — no TTS key in this test).
        msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
        check("WS greeting event", msg.get("event") == "say",
              f"got {msg.get('event')}")
        check("WS greeting text",
              "মুর্শিদের অ্যাসিস্ট্যান্ট" in msg.get("text", ""),
              f"got {msg.get('text', '')[:60]}")
        # Caller speaks (browser-STT text path) -> AI must reply.
        await ws.send(json.dumps({"event": "transcript", "text": "hello, ke bolche?"}))
        msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
        check("WS reply event", msg.get("event") == "say",
              f"got {msg.get('event')}")
        check("WS reply has text", bool(msg.get("text")), "")
        # End the call.
        await ws.send(json.dumps({"event": "stop"}))
        msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
        check("WS call-ended", msg.get("event") == "call-ended",
              f"got {msg.get('event')}")


def main():
    env = dict(os.environ)
    env["RECORDINGS_DIR"] = os.path.join(SERVER_DIR, "recordings-test")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app",
         "--host", "127.0.0.1", "--port", "8123"],
        cwd=SERVER_DIR, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        # Wait for the server.
        for _ in range(30):
            try:
                r = requests.get(f"{BASE}/health", timeout=2)
                if r.status_code == 200:
                    break
            except Exception:
                time.sleep(0.5)
        else:
            check("server starts", False, "no /health after 15s")

        r = requests.get(f"{BASE}/health", timeout=5)
        check("GET /health 200", r.status_code == 200, r.text[:120])
        check("health: gemini off without key",
              r.json()["ai"]["gemini"] is False, "")

        r = requests.get(f"{BASE}/call", timeout=5)
        check("GET /call page 200", r.status_code == 200, "")
        check("call page has Call button", "Call koro" in r.text, "")

        r = requests.get(f"{BASE}/join/smoke-room-1", timeout=5)
        check("GET /join page 200", r.status_code == 200, "")

        r = requests.post(f"{BASE}/api/rooms", timeout=5)
        check("POST /api/rooms 200", r.status_code == 200, "")
        check("room has call_url + join_url",
              "call_url" in r.json() and "join_url" in r.json(), "")

        r = requests.get(f"{BASE}/voice/greeting", timeout=10)
        check("GET /voice/greeting 503 without TTS key",
              r.status_code == 503, f"got {r.status_code}")

        r = requests.post(f"{BASE}/provider/plivo/voice/answer", timeout=5)
        check("Plivo adapter OFF by default (404)",
              r.status_code == 404, f"got {r.status_code}")

        asyncio.run(ws_call_test())

        # Recording + history must exist after the call.
        rec_dir = os.path.join(SERVER_DIR, "recordings-test", "smoke-room-1")
        check("recording inbound.wav created",
              os.path.exists(os.path.join(rec_dir, "inbound.wav")), rec_dir)
        check("recording outbound.wav created",
              os.path.exists(os.path.join(rec_dir, "outbound.wav")), rec_dir)

        r = requests.get(f"{BASE}/calls/smoke-room-1", timeout=5)
        check("GET /calls/<id> 200", r.status_code == 200, "")
        rec = r.json()
        check("record has transcript", len(rec.get("transcript", [])) >= 2,
              f"transcript={len(rec.get('transcript', []))}")
        check("record has summary", bool(rec.get("summary")), rec.get("summary", "")[:60])

        r = requests.get(f"{BASE}/notifications", timeout=5)
        notes = r.json()["notifications"]
        check("'Ei phone koreche' notification exists",
              any("Ei phone koreche" in n["title"] for n in notes),
              f"{len(notes)} notifications")
        print("\nALL SMOKE TESTS PASSED")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()


if __name__ == "__main__":
    main()
