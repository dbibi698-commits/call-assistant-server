"""Mith Hera Call Assistant — OWN FREE SERVER.

No Plivo, no Twilio, no paid telephony provider. Calls come in over
the internet (WebRTC page) straight to this server, the server AI
answers in Bengali, records both sides, and the user hears NOTHING
live — only a notification. The ONLY connection to the user's phone
is a transfer, and only when the caller asks for the owner.

Run locally:
    pip install -r requirements.txt
    uvicorn app.main:app --host 0.0.0.0 --port 8000

v3: the caller leg runs on the real Pipecat pipeline (see
app/pipecat_engine.py). If Pipecat cannot be imported, the v2.1
CallSession loop below still answers calls exactly as before.
"""

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os

from fastapi import FastAPI, Form, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response

from . import ai, audio, call_session, store
from .config import settings

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("mithhera.server")

# v3 Pipecat engine — optional: if the import fails (pipecat not
# installed), the v2.1 handler below keeps serving calls.
try:
    from . import pipecat_engine

    PIPECAT_AVAILABLE = True
except Exception as exc:  # noqa: BLE001 — any import failure = v2.1 mode
    pipecat_engine = None  # type: ignore[assignment]
    PIPECAT_AVAILABLE = False
    log.warning("pipecat engine unavailable, v2.1 handler in use: %s", exc)

app = FastAPI(title="Mith Hera Call Assistant Server")
STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static")
CALL_PAGE = os.path.join(STATIC_DIR, "call.html")

HONEST_LIMIT_BN = (
    "Sotti kotha: ei server-e internet call (WebRTC) ashe — browser theke "
    "call korle AI answer korbe. Normal mobile dialler theke call korle "
    "seta server-e ashte pare na, karon normal call-er jonno ekta phone "
    "number lage, ar kono code-i free-te number banate pare na. "
    "Dialler call chaile pore ekta number (Plivo trial ba onno provider) "
    "attach korte hobe."
)
HONEST_LIMIT_EN = (
    "Honest limit: this server answers internet (WebRTC) calls from a "
    "browser. A normal mobile-dialler call cannot reach a server on its "
    "own — a normal call needs a phone number from a carrier/provider, "
    "and no code can create one for free. Dialler calls need a number "
    "attached later (e.g. a Plivo trial number)."
)


# ------------------------------------------------------------------ pages
@app.get("/", response_class=HTMLResponse)
def index():
    return f"""<!DOCTYPE html><html lang="bn"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Mith Hera Call Assistant — Server</title>
<style>body{{background:#070303;color:#fff;font-family:system-ui,sans-serif;
margin:0;display:flex;justify-content:center}} .c{{max-width:560px;padding:32px 20px}}
h1 span{{color:#FF2D2D}} a.btn{{display:block;text-align:center;padding:16px;margin:14px 0;
border-radius:999px;background:#FF2D2D;color:#fff;text-decoration:none;font-weight:700;font-size:18px}}
p{{color:#ccc;line-height:1.6}} .limit{{border:1px solid rgba(255,45,45,.5);
border-radius:16px;padding:14px;background:rgba(255,45,45,.08)}}</style></head>
<body><div class="c"><h1>মিঠ হেরা <span>Call Assistant</span> — Server</h1>
<p>AI call assistant server. Call ashe internet diye, AI Bengali-te kotha bole,
recording + transcript save hoy. User-er phone-e kichu shona jay na — shudhu
notification ashe.</p>
<a class="btn" href="/call">📞 Call koro (WebRTC, free)</a>
<a class="btn" style="background:#333" href="/calls">📋 Call history / CHATS (JSON)</a>
<a class="btn" style="background:#333" href="/notifications">🔔 Notifications (JSON)</a>
<a class="btn" style="background:#333" href="/health">❤️ Health check</a>
<div class="limit"><b>Sotti kotha / Honest limit:</b><p>{HONEST_LIMIT_BN}</p>
<p>{HONEST_LIMIT_EN}</p></div>
<p style="font-size:12px;color:#888">Owner transfer: caller "Murshid / Sadhin ke dao"
bolle AI "Ek minute, connect korchhi" bole, ar owner <b>/join/&lt;call-id&gt;</b>
link theke oi ek-i call-e join koren — tokhon AI sore jay.</p>
</div></body></html>"""


@app.get("/health")
def health():
    return {
        "ok": True,
        "service": "mith-hera-call-assistant-server",
        "version": "3.0-pipecat",
        "pipeline": (
            "Pipecat pipeline (pipecat-ai, BSD-2): FastAPI WebSocket "
            "transport (mu-law 8 kHz) -> BridgeProcessor -> Sarvam STT "
            "or browser STT -> user aggregator (Silero VAD + SmartTurn "
            "V3 default stop strategy, barge-in) -> ConversationProcessor "
            "(greeting, name-before-transfer, transfer confirm, goodbye) "
            "-> Gemini or scripted Bengali LLM -> Sarvam Bulbul TTS or "
            "browser TTS -> TapProcessor (recording) -> transport. "
            "The v2.1 CallSession loop stays as the fallback when "
            "Pipecat is unavailable."
        ),
        "pipecat": (
            pipecat_engine.engine_info()
            if PIPECAT_AVAILABLE
            else {"engine": "v2.1-fallback", "reason": "pipecat not installed"}
        ),
        "ai": ai.status(),
        "supabase": store.supabase_configured(),
        "owner_phone_set": bool(settings.OWNER_PHONE),
        "provider_adapter_enabled": settings.ENABLE_PLIVO,
        "active_calls": len(call_session.ROOMS),
    }


@app.get("/call")
@app.get("/call/{room_id}")
def call_page(room_id: str = ""):
    return FileResponse(CALL_PAGE, media_type="text/html")


@app.get("/join/{room_id}")
def join_page(room_id: str):
    return FileResponse(CALL_PAGE, media_type="text/html")


@app.post("/api/rooms")
def create_room(request: Request):
    """Create a call room; returns the links to share with a caller."""
    room_id = store.new_call_id()
    session = call_session.get_or_create_session(room_id)
    base = settings.BASE_URL.rstrip("/") if settings.BASE_URL else str(request.base_url).rstrip("/")
    return {
        "room_id": room_id,
        "call_url": f"{base}/call/{room_id}",
        "join_url": f"{base}/join/{room_id}",
        "note": "Caller-ke call_url dao. Owner shudhu transfer-er somoy join_url khulbe.",
    }


# -------------------------------------------------------------- recordings
@app.get("/calls")
def list_calls():
    return {"calls": store.list_records()}


@app.get("/calls/{call_id}")
def get_call(call_id: str):
    rec = store.get_record(call_id)
    if not rec:
        return JSONResponse({"error": "call not found"}, status_code=404)
    return rec


@app.get("/notifications")
def list_notifications():
    return {"notifications": store.list_notifications()}


@app.get("/recordings/{call_id}/{leg}.wav")
def get_recording(call_id: str, leg: str):
    if leg not in ("inbound", "outbound", "conversation"):
        return JSONResponse({"error": "leg must be inbound, outbound or conversation"}, status_code=400)
    path = os.path.join(settings.RECORDINGS_DIR, call_id, f"{leg}.wav")
    if not os.path.exists(path):
        return JSONResponse({"error": "recording not found"}, status_code=404)
    return FileResponse(path, media_type="audio/wav")


@app.get("/voice/greeting")
def greeting_audio():
    """Test endpoint: the greeting as audio (proves TTS works)."""
    mulaw = ai.synthesise(settings.GREETING_BN)
    if not mulaw:
        return JSONResponse(
            {
                "error": "no TTS configured",
                "message": "Google Cloud TTS ba Sarvam key bosano hoy ni — "
                "server TTS korte parbe na, browser TTS fallback use hobe.",
            },
            status_code=503,
        )
    return Response(content=audio.mulaw_to_wav_bytes(mulaw), media_type="audio/wav")


# ------------------------------------------------------- websocket (calls)
async def _handle_ws(websocket: WebSocket, room_id: str | None, role: str):
    await websocket.accept()
    session: call_session.CallSession | None = None
    try:
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            event = msg.get("event")
            if event == "start":
                start = msg.get("start", {}) or {}
                rid = room_id or start.get("callId") or store.new_call_id()
                session = call_session.get_or_create_session(rid)
                session.record["from_number"] = (
                    start.get("from") or start.get("From") or "WebRTC caller"
                )
                if start.get("fromName"):
                    session._set_caller_name(start["fromName"])
                session.caller_label = (
                    session.record["from_name"] or session.record["from_number"]
                )
                mode = (start.get("customParameters") or {}).get("mode") or start.get("mode")
                if role == "owner":
                    session.owner_ws = websocket
                    await session.owner_joined()
                else:
                    session.caller_ws = websocket
                    if mode == "ownerbusy":
                        await session.speak(settings.BUSY_LINE_BN)
                        await session.end("owner-busy")
                    else:
                        await session.greet()
            elif event == "media" and session:
                payload = (msg.get("media") or {}).get("payload", "")
                if not payload:
                    continue
                try:
                    mulaw = base64.b64decode(payload)
                except Exception:
                    continue
                if role == "owner":
                    await session.on_owner_audio(mulaw)
                else:
                    await session.on_caller_audio(mulaw)
            elif event == "transcript" and session and role == "caller":
                # Browser STT fallback (no server STT key).
                await session.on_caller_text((msg.get("text") or "").strip())
            elif event == "dtmf":
                log.info("DTMF %s room=%s", msg.get("dtmf"), session.room_id if session else "?")
            elif event == "stop" and session:
                await session.end("caller-ended")
                break
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        log.warning("WS error: %s", exc)
    finally:
        if session:
            if role == "owner" and session.owner_ws is websocket:
                session.owner_ws = None
                if session.bridged:
                    session.bridged = False
                    # Owner left mid-transfer: AI resumes politely.
                    # v3: through the pipeline (the transport owns the
                    # caller socket); v2.1: raw speak, as before.
                    if session.pipecat_mode and PIPECAT_AVAILABLE:
                        asyncio.create_task(pipecat_engine.owner_left(session))
                    else:
                        asyncio.create_task(
                            session.speak("Owner call theke beriye gechen. Ami abar achi, bolun.")
                        )
            elif role == "caller" and session.caller_ws is websocket:
                session.caller_ws = None
                await session.finalize()


# --------------------------------------------------- websocket (v3 Pipecat)
async def _v21_caller_loop(websocket: WebSocket, session: call_session.CallSession):
    """The v2.1 caller loop, for a session whose "start" was already
    consumed. Used ONLY when the Pipecat pipeline failed before it
    started (the transport never read a message yet, so the v2.1
    loop can safely take the same socket over)."""
    session.pipecat_mode = False
    await session.greet()
    try:
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            event = msg.get("event")
            if event == "media":
                payload = (msg.get("media") or {}).get("payload", "")
                if payload:
                    try:
                        await session.on_caller_audio(base64.b64decode(payload))
                    except Exception:
                        continue
            elif event == "transcript":
                await session.on_caller_text((msg.get("text") or "").strip())
            elif event == "stop":
                await session.end("caller-ended")
                break
    except WebSocketDisconnect:
        pass


async def _handle_ws_pipecat(websocket: WebSocket, room_id: str | None):
    """Caller leg on the Pipecat pipeline (v3). Reads "start", then
    hands the socket to the engine. The owner leg still goes through
    _handle_ws (role=owner) — the owner socket is raw, the engine
    reaches it through the session's owner_audio_queue."""
    await websocket.accept()
    session: call_session.CallSession | None = None
    try:
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            if msg.get("event") == "start":
                start = msg.get("start", {}) or {}
                rid = room_id or start.get("callId") or store.new_call_id()
                session = call_session.get_or_create_session(rid)
                session.record["from_number"] = (
                    start.get("from") or start.get("From") or "WebRTC caller"
                )
                if start.get("fromName"):
                    session._set_caller_name(start["fromName"])
                session.caller_label = (
                    session.record["from_name"] or session.record["from_number"]
                )
                mode = (start.get("customParameters") or {}).get("mode") or start.get("mode")
                session.caller_ws = websocket
                if mode == "ownerbusy":
                    # Owner-busy mode (Plivo after-stream path): speak
                    # the busy line, then end — no pipeline needed.
                    await session.speak(settings.BUSY_LINE_BN)
                    await session.end("owner-busy")
                else:
                    try:
                        await pipecat_engine.run_call(websocket, session, rid)
                    except pipecat_engine.EngineNotStarted:
                        log.warning(
                            "pipecat did not start for %s, v2.1 fallback", rid
                        )
                        await _v21_caller_loop(websocket, session)
                return
            # Any pre-start event is dropped.
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        log.warning("WS pipecat error: %s", exc)
    finally:
        if session:
            session.caller_ws = None
            await session.finalize()


@app.websocket("/ws/call/{room_id}")
async def ws_call(websocket: WebSocket, room_id: str):
    role = websocket.query_params.get("role", "caller")
    if role == "caller" and PIPECAT_AVAILABLE:
        await _handle_ws_pipecat(websocket, room_id)
    else:
        await _handle_ws(websocket, room_id, role)


@app.websocket("/media-stream")
async def ws_media_stream(websocket: WebSocket):
    """Same engine, Plivo-compatible path (optional provider adapter)."""
    if PIPECAT_AVAILABLE:
        await _handle_ws_pipecat(websocket, None)
    else:
        await _handle_ws(websocket, None, "caller")


# ------------------------------------------- OPTIONAL provider adapter ----
# OFF by default (ENABLE_PLIVO=false). Plivo XML shape, kept so a rented
# Indian number can be attached later without changing the AI engine.
# Plivo classic <Speak> has NO Bengali voice, so the greeting is spoken
# by the server over the bidirectional stream, not by <Speak>.
def _plivo_enabled():
    return settings.ENABLE_PLIVO


def _base_url(request: Request) -> str:
    if settings.BASE_URL:
        return settings.BASE_URL.rstrip("/")
    return str(request.base_url).rstrip("/")


def _plivo_xml_response(stream_url: str, mode: str = "") -> str:
    extra = f' extraHeaders="mode={mode}"' if mode else ""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<Response>\n"
        f'  <Stream bidirectional="true" contentType="audio/x-mulaw;rate=8000" '
        f'keepCallAlive="true" streamTimeout="3600"{extra}>\n'
        f"    {stream_url}\n"
        "  </Stream>\n"
        '  <Redirect method="POST">/provider/plivo/voice/after-stream</Redirect>\n'
        "</Response>"
    )


def _xml_response(xml: str) -> Response:
    return Response(content=xml, media_type="application/xml")


def _validate_plivo(request: Request, form: dict) -> bool:
    """Classic Plivo signature: base64(HMAC-SHA1(token, url+sorted params)).

    Only enforced when PLIVO_VALIDATE_SIGNATURE=true and a signature
    header is present; otherwise the request is accepted (logged).
    """
    if not settings.PLIVO_VALIDATE_SIGNATURE:
        return True
    sig = request.headers.get("X-Plivo-Signature", "")
    token = settings.PLIVO_AUTH_TOKEN
    if not sig or not token:
        return True
    url = str(request.url)
    data = url + "".join(f"{k}{v}" for k, v in sorted(form.items()))
    expect = base64.b64encode(
        hmac.new(token.encode(), data.encode(), hashlib.sha1).digest()
    ).decode()
    return hmac.compare_digest(expect, sig)


@app.post("/provider/plivo/voice/answer")
async def plivo_answer(request: Request):
    if not _plivo_enabled():
        return JSONResponse(
            {"error": "provider adapter is OFF (ENABLE_PLIVO=false)"}, status_code=404
        )
    form = dict(await request.form())
    if not _validate_plivo(request, form):
        return JSONResponse({"error": "bad Plivo signature"}, status_code=403)
    base = _base_url(request)
    mode = request.query_params.get("mode", "")
    ws_url = base.replace("http://", "ws://").replace("https://", "wss://")
    return _xml_response(_plivo_xml_response(f"{ws_url}/media-stream", mode))


@app.post("/provider/plivo/voice/after-stream")
async def plivo_after_stream(request: Request):
    if not _plivo_enabled():
        return JSONResponse({"error": "provider adapter is OFF"}, status_code=404)
    form = dict(await request.form())
    call_uuid = form.get("CallUUID", "")
    session = call_session.get_session(call_uuid)
    if session and session.transfer_requested and not session.bridged and settings.OWNER_PHONE:
        dial_xml = (
            '<?xml version="1.0" encoding="UTF-8"?>\n<Response>\n'
            f'  <Dial callerId="{settings.PLIVO_NUMBER or ""}" timeout="30" '
            'action="/provider/plivo/voice/transfer-action" method="POST">\n'
            f"    <Number>{settings.OWNER_PHONE}</Number>\n"
            "  </Dial>\n</Response>"
        )
        return _xml_response(dial_xml)
    return _xml_response(
        '<?xml version="1.0" encoding="UTF-8"?>\n<Response>\n  <Hangup/>\n</Response>'
    )


@app.post("/provider/plivo/voice/transfer-action")
async def plivo_transfer_action(request: Request):
    if not _plivo_enabled():
        return JSONResponse({"error": "provider adapter is OFF"}, status_code=404)
    form = dict(await request.form())
    status = form.get("DialCallStatus", "")
    if status == "completed":
        return _xml_response(
            '<?xml version="1.0" encoding="UTF-8"?>\n<Response>\n  <Hangup/>\n</Response>'
        )
    # Owner did not answer: go back to the AI stream, owner-busy mode.
    return _xml_response(
        '<?xml version="1.0" encoding="UTF-8"?>\n<Response>\n'
        '  <Redirect method="POST">/provider/plivo/voice/answer?mode=ownerbusy</Redirect>\n'
        "</Response>"
    )


@app.post("/provider/plivo/voice/recording")
async def plivo_recording(request: Request):
    if not _plivo_enabled():
        return JSONResponse({"error": "provider adapter is OFF"}, status_code=404)
    form = dict(await request.form())
    call_uuid = form.get("CallUUID", "")
    rec = store.get_record(call_uuid)
    if rec and form.get("RecordingUrl"):
        rec["recording_url"] = form["RecordingUrl"]
    return JSONResponse({"ok": True})


@app.post("/provider/plivo/voice/stream-status")
async def plivo_stream_status(request: Request):
    if not _plivo_enabled():
        return JSONResponse({"error": "provider adapter is OFF"}, status_code=404)
    form = dict(await request.form())
    log.info("Plivo stream status: %s", form)
    if form.get("Event") == "stopped":
        session = call_session.get_session(form.get("CallUUID", ""))
        if session:
            await session.finalize()
    return JSONResponse({"ok": True})
