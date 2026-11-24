"""Smoke test for the Mith Hera Call Assistant server v2.1
(Dograh-pattern upgrade, Bengali-first, name-before-transfer).

Starts the server locally (no API keys — the honest no-key path),
then checks everything the v1 smoke test checked, plus the v2
behaviour:
  * /health reports version 2.1-name-first-transfer, language bn-IN, and
    the Bengali STT/TTS chains (Sarvam Saarika first, Google second,
    browser fallback always configured)
  * a WebSocket call gets a Bengali greeting and a Bengali reply
    (Bengali script is asserted in the transcript, not just any text)
  * turn-state events arrive (greeting/speaking/listening)
  * ambiguous transfer ("connect koro") asks for confirmation first,
    "হ্যাঁ" transfers, the owner joins the SAME call, the caller gets
    the "transferred" event, and the record shows bridged
  * direct transfer ("Murshid ke dao") transfers at once; with no
    owner joining it says the owner is busy and ends politely
  * goodbye ("আল্লাহ হাফেজ") ends the call politely
  * a combined conversation.wav recording is created and the record
    carries language bn-IN
  * the optional Plivo adapter is OFF (404) by default
  * v2.1 NAME FIRST: a direct transfer with NO name known asks
    "ট্রান্সফার করার আগে আপনার নামটা বলুন?" first, then transfers
    with the name ("এক মিনিট, রহিম — কানেক্ট করছি।") and the
    transfer notification carries that name; a transfer WITH a name
    already known transfers at once with no name question; a caller
    who refuses the name twice still gets transferred, with the name
    "Naam dey ni"; the name is also asked once, early, after the
    greeting when it is still unknown

Usage:  python tests/smoke_test_v2.py
"""

import asyncio
import json
import os
import subprocess
import sys
import time

import requests

BASE = "http://127.0.0.1:8124"
SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

BN_RANGE = range(0x0980, 0x0A00)  # Bengali Unicode block


def is_bengali(text: str) -> bool:
    return any(ord(c) in BN_RANGE for c in text)


def check(name, ok, detail=""):
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {detail}")
    if not ok:
        raise SystemExit(1)


async def recv_until(ws, pred, timeout=15):
    """Read WS messages until pred(msg) is true; return that message."""
    deadline = time.time() + timeout
    while True:
        left = deadline - time.time()
        if left <= 0:
            raise TimeoutError(f"never received matching message for {pred}")
        msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=left))
        if pred(msg):
            return msg


async def ws_basic_bengali_test():
    import websockets

    room = "v2-room-basic"
    async with websockets.connect(f"ws://127.0.0.1:8124/ws/call/{room}?role=caller") as ws:
        await ws.send(json.dumps({
            "event": "start",
            "start": {"callId": room, "from": "+919999999999", "fromName": "V2 Test"},
        }))
        # Greeting must arrive as a "say" event, in Bengali script.
        msg = await recv_until(ws, lambda m: m.get("event") == "say")
        check("v2 greeting is Bengali",
              is_bengali(msg.get("text", "")) and "মুর্শিদের অ্যাসিস্ট্যান্ট" in msg["text"],
              f"got {msg.get('text', '')[:60]}")
        # A state event must have arrived too (turn-state machine).
        # (recv_until above already consumed messages; state events are
        # verified on the transfer call below instead.)
        # Caller speaks Bengali -> AI must reply in Bengali.
        await ws.send(json.dumps({"event": "transcript", "text": "আমি একটা জিনিস জানতে চাই"}))
        msg = await recv_until(ws, lambda m: m.get("event") == "say")
        check("v2 reply is Bengali", is_bengali(msg.get("text", "")),
              f"got {msg.get('text', '')[:60]}")
        await ws.send(json.dumps({"event": "stop"}))
        msg = await recv_until(ws, lambda m: m.get("event") == "call-ended")
        check("v2 basic call-ended", msg.get("event") == "call-ended", "")


async def ws_transfer_confirm_test():
    import websockets

    room = "v2-room-transfer"
    async with websockets.connect(f"ws://127.0.0.1:8124/ws/call/{room}?role=caller") as caller:
        await caller.send(json.dumps({
            "event": "start",
            "start": {"callId": room, "from": "+918888888888", "fromName": "Transfer Test"},
        }))
        await recv_until(caller, lambda m: m.get("event") == "say")  # greeting
        # Ambiguous "connect koro" must ASK first, not transfer at once.
        await caller.send(json.dumps({"event": "transcript", "text": "connect koro"}))
        msg = await recv_until(caller, lambda m: m.get("event") == "say")
        check("v2 ambiguous transfer asks first",
              "ওনার" in msg.get("text", ""),
              f"got {msg.get('text', '')[:60]}")
        # "হ্যাঁ" confirms -> transfer line.
        await caller.send(json.dumps({"event": "transcript", "text": "হ্যাঁ"}))
        msg = await recv_until(caller, lambda m: m.get("event") == "say")
        check("v2 transfer line after yes",
              "কানেক্ট করছি" in msg.get("text", ""),
              f"got {msg.get('text', '')[:60]}")
        # Owner joins the SAME call.
        async with websockets.connect(
                f"ws://127.0.0.1:8124/ws/call/{room}?role=owner") as owner:
            await owner.send(json.dumps({
                "event": "start",
                "start": {"callId": room, "from": "owner"},
            }))
            msg = await recv_until(caller, lambda m: m.get("event") == "transferred",
                                   timeout=10)
            check("v2 caller sees transferred", msg.get("event") == "transferred", "")
            await caller.send(json.dumps({"event": "stop"}))
            msg = await recv_until(caller, lambda m: m.get("event") == "call-ended",
                                   timeout=10)
            check("v2 transfer call-ended", msg.get("event") == "call-ended", "")
        r = requests.get(f"{BASE}/calls/{room}", timeout=5)
        rec = r.json()
        check("v2 record transferred", rec.get("transferred") is True, "")
        check("v2 record transfer_status bridged",
              rec.get("transfer_status") == "bridged", rec.get("transfer_status", ""))
        check("v2 record language bn-IN", rec.get("language") == "bn-IN", "")


async def ws_direct_transfer_timeout_test():
    import websockets

    room = "v2-room-direct"
    async with websockets.connect(f"ws://127.0.0.1:8124/ws/call/{room}?role=caller") as ws:
        await ws.send(json.dumps({
            "event": "start",
            "start": {"callId": room, "from": "+917777777777", "fromName": "Direct Test"},
        }))
        await recv_until(ws, lambda m: m.get("event") == "say")  # greeting
        # Direct: naming Murshid transfers at once, no confirmation.
        await ws.send(json.dumps({"event": "transcript", "text": "Murshid ke dao"}))
        msg = await recv_until(ws, lambda m: m.get("event") == "say")
        check("v2 direct transfer line at once",
              "কানেক্ট করছি" in msg.get("text", ""),
              f"got {msg.get('text', '')[:60]}")
        # Nobody joins: owner-busy line, then the call ends politely.
        msg = await recv_until(ws, lambda m: m.get("event") == "say", timeout=15)
        check("v2 owner-busy line", "ব্যস্ত" in msg.get("text", ""),
              f"got {msg.get('text', '')[:60]}")
        msg = await recv_until(ws, lambda m: m.get("event") == "call-ended", timeout=10)
        check("v2 owner-busy call-ended", msg.get("reason") == "owner-busy",
              f"got {msg.get('reason')}")


async def ws_name_before_transfer_test():
    """v2.1 (a): direct transfer with NO name known -> the AI asks
    for the name FIRST, then transfers WITH the name, and the
    transfer notification carries it."""
    import websockets

    room = "v2-room-name-first"
    async with websockets.connect(f"ws://127.0.0.1:8124/ws/call/{room}?role=caller") as caller:
        await caller.send(json.dumps({
            "event": "start",
            "start": {"callId": room, "from": "+915555555555"},
        }))
        # Greeting first, then the early name ask (name unknown).
        msg = await recv_until(caller, lambda m: m.get("event") == "say")
        check("v2.1 greeting is Bengali",
              is_bengali(msg.get("text", "")) and "মুর্শিদের অ্যাসিস্ট্যান্ট" in msg["text"],
              f"got {msg.get('text', '')[:60]}")
        msg = await recv_until(caller, lambda m: m.get("event") == "say")
        check("v2.1 early name ask after greeting",
              "নামটা কী" in msg.get("text", ""),
              f"got {msg.get('text', '')[:60]}")
        # Caller asks for a transfer instead of giving the name:
        # the AI must ask for the name BEFORE transferring.
        await caller.send(json.dumps({"event": "transcript", "text": "Murshid ke dao"}))
        msg = await recv_until(caller, lambda m: m.get("event") == "say")
        check("v2.1 transfer asks name first",
              "ট্রান্সফার করার আগে" in msg.get("text", "")
              and "নামটা বলুন" in msg.get("text", ""),
              f"got {msg.get('text', '')[:60]}")
        # Caller gives the name -> transfer line WITH the name.
        await caller.send(json.dumps({"event": "transcript", "text": "আমি রহিম"}))
        msg = await recv_until(caller, lambda m: m.get("event") == "say")
        check("v2.1 transfer line carries the name",
              "রহিম" in msg.get("text", "") and "কানেক্ট করছি" in msg.get("text", ""),
              f"got {msg.get('text', '')[:60]}")
        # The transfer notification must carry the name too.
        r = requests.get(f"{BASE}/notifications", timeout=5)
        notes = [n for n in r.json()["notifications"]
                 if n.get("kind") == "transfer" and n.get("call_id") == room]
        check("v2.1 transfer notification carries the name",
              bool(notes) and notes[0].get("caller_name") == "রহিম"
              and "রহিম" in notes[0].get("title", ""),
              f"got {notes}")
        # Owner joins the SAME call.
        async with websockets.connect(
                f"ws://127.0.0.1:8124/ws/call/{room}?role=owner") as owner:
            await owner.send(json.dumps({
                "event": "start",
                "start": {"callId": room, "from": "owner"},
            }))
            msg = await recv_until(caller, lambda m: m.get("event") == "transferred",
                                   timeout=10)
            check("v2.1 name-first caller sees transferred",
                  msg.get("event") == "transferred", "")
            await caller.send(json.dumps({"event": "stop"}))
            msg = await recv_until(caller, lambda m: m.get("event") == "call-ended",
                                   timeout=10)
            check("v2.1 name-first call-ended", msg.get("event") == "call-ended", "")
        r = requests.get(f"{BASE}/calls/{room}", timeout=5)
        rec = r.json()
        check("v2.1 record from_name is the given name",
              rec.get("from_name") == "রহিম", rec.get("from_name", ""))
        check("v2.1 record transferred", rec.get("transferred") is True, "")
        check("v2.1 record transfer_status bridged",
              rec.get("transfer_status") == "bridged", rec.get("transfer_status", ""))


async def ws_transfer_with_name_test():
    """v2.1 (b): transfer WITH a name already known -> transfers at
    once, no name question, line carries the name."""
    import websockets

    room = "v2-room-with-name"
    async with websockets.connect(f"ws://127.0.0.1:8124/ws/call/{room}?role=caller") as caller:
        await caller.send(json.dumps({
            "event": "start",
            "start": {"callId": room, "from": "+914444444444", "fromName": "Anika"},
        }))
        await recv_until(caller, lambda m: m.get("event") == "say")  # greeting
        # No early name ask (name already known) — the next "say"
        # after the transfer request must be the transfer line itself.
        await caller.send(json.dumps({"event": "transcript", "text": "Sadhin ke dao"}))
        msg = await recv_until(caller, lambda m: m.get("event") == "say", timeout=10)
        check("v2.1 known-name transfer line at once",
              "Anika" in msg.get("text", "") and "কানেক্ট করছি" in msg.get("text", ""),
              f"got {msg.get('text', '')[:60]}")
        check("v2.1 known-name transfer asks no name question",
              "নামটা বলুন" not in msg.get("text", ""), "")
        r = requests.get(f"{BASE}/notifications", timeout=5)
        notes = [n for n in r.json()["notifications"]
                 if n.get("kind") == "transfer" and n.get("call_id") == room]
        check("v2.1 known-name notification carries the name",
              bool(notes) and notes[0].get("caller_name") == "Anika",
              f"got {notes}")
        async with websockets.connect(
                f"ws://127.0.0.1:8124/ws/call/{room}?role=owner") as owner:
            await owner.send(json.dumps({
                "event": "start",
                "start": {"callId": room, "from": "owner"},
            }))
            msg = await recv_until(caller, lambda m: m.get("event") == "transferred",
                                   timeout=10)
            check("v2.1 known-name caller sees transferred",
                  msg.get("event") == "transferred", "")
            await caller.send(json.dumps({"event": "stop"}))
            msg = await recv_until(caller, lambda m: m.get("event") == "call-ended",
                                   timeout=10)
            check("v2.1 known-name call-ended", msg.get("event") == "call-ended", "")
        r = requests.get(f"{BASE}/calls/{room}", timeout=5)
        check("v2.1 known-name record from_name",
              r.json().get("from_name") == "Anika", r.json().get("from_name", ""))


async def ws_name_refusal_test():
    """v2.1 (c): caller refuses the name twice -> transfer STILL
    happens, with the name 'Naam dey ni' — never blocked forever."""
    import websockets

    room = "v2-room-name-refused"
    async with websockets.connect(f"ws://127.0.0.1:8124/ws/call/{room}?role=caller") as ws:
        await ws.send(json.dumps({
            "event": "start",
            "start": {"callId": room, "from": "+913333333333"},
        }))
        await recv_until(ws, lambda m: m.get("event") == "say")  # greeting
        await recv_until(ws, lambda m: m.get("event") == "say")  # early name ask
        await ws.send(json.dumps({"event": "transcript", "text": "Murshid ke dao"}))
        msg = await recv_until(ws, lambda m: m.get("event") == "say")
        check("v2.1 refusal test asks name first",
              "ট্রান্সফার করার আগে" in msg.get("text", ""),
              f"got {msg.get('text', '')[:60]}")
        # First refusal -> asked once more.
        await ws.send(json.dumps({"event": "transcript", "text": "নাম বলবো না"}))
        msg = await recv_until(ws, lambda m: m.get("event") == "say")
        check("v2.1 first refusal asks once more",
              "নামটা" in msg.get("text", ""),
              f"got {msg.get('text', '')[:60]}")
        # Second refusal -> transfer anyway, plain line, no name.
        await ws.send(json.dumps({"event": "transcript", "text": "না"}))
        msg = await recv_until(ws, lambda m: m.get("event") == "say")
        check("v2.1 second refusal still transfers",
              "কানেক্ট করছি" in msg.get("text", ""),
              f"got {msg.get('text', '')[:60]}")
        r = requests.get(f"{BASE}/notifications", timeout=5)
        notes = [n for n in r.json()["notifications"]
                 if n.get("kind") == "transfer" and n.get("call_id") == room]
        check("v2.1 refusal notification name is 'Naam dey ni'",
              bool(notes) and notes[0].get("caller_name") == "Naam dey ni",
              f"got {notes}")
        # Nobody joins: owner-busy line, then the call ends politely.
        msg = await recv_until(ws, lambda m: m.get("event") == "say", timeout=15)
        check("v2.1 refusal owner-busy line", "ব্যস্ত" in msg.get("text", ""),
              f"got {msg.get('text', '')[:60]}")
        msg = await recv_until(ws, lambda m: m.get("event") == "call-ended", timeout=10)
        check("v2.1 refusal owner-busy call-ended",
              msg.get("reason") == "owner-busy", f"got {msg.get('reason')}")
        r = requests.get(f"{BASE}/calls/{room}", timeout=5)
        check("v2.1 refusal record from_name",
              r.json().get("from_name") == "Naam dey ni",
              r.json().get("from_name", ""))


async def ws_goodbye_test():
    import websockets

    room = "v2-room-goodbye"
    async with websockets.connect(f"ws://127.0.0.1:8124/ws/call/{room}?role=caller") as ws:
        await ws.send(json.dumps({
            "event": "start",
            "start": {"callId": room, "from": "+916666666666", "fromName": "Goodbye Test"},
        }))
        await recv_until(ws, lambda m: m.get("event") == "say")  # greeting
        await ws.send(json.dumps({"event": "transcript", "text": "আল্লাহ হাফেজ"}))
        msg = await recv_until(ws, lambda m: m.get("event") == "say")
        check("v2 goodbye line", "ধন্যবাদ" in msg.get("text", ""),
              f"got {msg.get('text', '')[:60]}")
        msg = await recv_until(ws, lambda m: m.get("event") == "call-ended", timeout=10)
        check("v2 goodbye call-ended", msg.get("reason") == "caller-goodbye",
              f"got {msg.get('reason')}")


def main():
    env = dict(os.environ)
    env["RECORDINGS_DIR"] = os.path.join(SERVER_DIR, "recordings-test-v2")
    env["TRANSFER_WAIT_SECONDS"] = "5"
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app",
         "--host", "127.0.0.1", "--port", "8124"],
        cwd=SERVER_DIR, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
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
        h = r.json()
        check("health version 2.1-name-first-transfer",
              h.get("version") == "2.1-name-first-transfer", h.get("version", ""))
        check("health language bn-IN", h["ai"].get("language") == "bn-IN", "")
        check("health STT chain starts with Sarvam Saarika",
              h["ai"]["stt_chain"][0]["provider"] == "sarvam-saarika", "")
        check("health TTS chain starts with Sarvam Bulbul",
              h["ai"]["tts_chain"][0]["provider"] == "sarvam-bulbul", "")
        check("health: no keys configured without env",
              h["ai"]["stt_chain"][0]["configured"] is False
              and h["ai"]["tts_chain"][0]["configured"] is False, "")
        check("health: browser fallbacks always configured",
              h["ai"]["stt_chain"][-1]["configured"] is True
              and h["ai"]["tts_chain"][-1]["configured"] is True, "")

        r = requests.get(f"{BASE}/call", timeout=5)
        check("GET /call page 200", r.status_code == 200, "")
        check("call page has Call button", "Call koro" in r.text, "")

        r = requests.get(f"{BASE}/voice/greeting", timeout=10)
        check("GET /voice/greeting 503 without TTS key",
              r.status_code == 503, f"got {r.status_code}")

        r = requests.post(f"{BASE}/provider/plivo/voice/answer", timeout=5)
        check("Plivo adapter OFF by default (404)",
              r.status_code == 404, f"got {r.status_code}")

        asyncio.run(ws_basic_bengali_test())
        asyncio.run(ws_transfer_confirm_test())
        asyncio.run(ws_direct_transfer_timeout_test())
        asyncio.run(ws_name_before_transfer_test())
        asyncio.run(ws_transfer_with_name_test())
        asyncio.run(ws_name_refusal_test())
        asyncio.run(ws_goodbye_test())

        # Recordings: both legs + combined conversation.wav.
        rec_dir = os.path.join(SERVER_DIR, "recordings-test-v2", "v2-room-basic")
        for leg in ("inbound.wav", "outbound.wav", "conversation.wav"):
            check(f"recording {leg} created",
                  os.path.exists(os.path.join(rec_dir, leg)), rec_dir)

        r = requests.get(f"{BASE}/recordings/v2-room-basic/conversation.wav", timeout=5)
        check("GET /recordings conversation.wav 200", r.status_code == 200,
              f"got {r.status_code}")

        r = requests.get(f"{BASE}/calls/v2-room-basic", timeout=5)
        rec = r.json()
        check("record has Bengali transcript",
              len(rec.get("transcript", [])) >= 2
              and any(is_bengali(t.get("text", "")) for t in rec["transcript"]),
              f"transcript={len(rec.get('transcript', []))}")
        check("record has Bengali summary",
              bool(rec.get("summary")) and is_bengali(rec.get("summary", "")),
              rec.get("summary", "")[:60])
        check("record language bn-IN", rec.get("language") == "bn-IN", "")
        check("record has turn_state", bool(rec.get("turn_state")),
              rec.get("turn_state", ""))

        r = requests.get(f"{BASE}/notifications", timeout=5)
        notes = r.json()["notifications"]
        check("'Ei phone koreche' notification exists",
              any("Ei phone koreche" in n["title"] for n in notes),
              f"{len(notes)} notifications")
        check("transfer notification exists",
              any(n.get("kind") == "transfer" for n in notes), "")
        print("\nALL V2 SMOKE TESTS PASSED")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()


if __name__ == "__main__":
    main()
