"""Live call sessions: the conversation loop, transfer and bridging.

One CallSession per room (one call). Two roles can connect over the
WebSocket, using the same JSON protocol Plivo uses for audio streams
(start / media / stop in, playAudio / clearAudio out) so the optional
provider adapter can reuse this engine unchanged:

  Caller -> server: {"event":"start","start":{...}}
                    {"event":"media","media":{"payload":"<base64 mulaw>"}}
                    {"event":"transcript","text":"..."}   (browser STT fallback)
                    {"event":"stop"}
  Server -> caller: {"event":"playAudio","media":{"payload":"<base64 mulaw>"}}
                    {"event":"say","text":"..."}          (browser TTS fallback)
                    {"event":"clearAudio"}
                    {"event":"transferred"}               (owner joined)
                    {"event":"call-ended","reason":"..."}

Conversation loop (listen-first, never hangs up by itself):
  greeting -> listen -> transcribe -> Gemini reply -> speak -> listen ...
  Empty STT -> "Ami thik shunte parini, arekbar bolun" and listen again.

Transfer (the ONLY connection to the user's phone):
  Caller says owner / Murshid / Sadhin / "connect koro" -> AI says
  "Ek minute, connect korchhi", a notification + join link is created,
  and the server waits TRANSFER_WAIT_SECONDS for the owner to open
  /join/<room_id> and join the SAME call. If the owner joins, the AI
  steps aside and audio is bridged caller <-> owner (server forwards
  the frames; recording keeps running). If nobody joins, the AI tells
  the caller the owner is busy and ends politely.
  No live listening, no live audio on the user's phone at any other time.
"""

import asyncio
import base64
import json
import logging
import time

from . import ai, audio, store
from .config import settings

log = logging.getLogger("mithhera.session")

ROOMS: dict[str, "CallSession"] = {}

# Speech detection on 20 ms mu-law frames (160 bytes at 8 kHz).
SPEECH_RMS = 400.0
SILENCE_AFTER_SPEECH_S = 1.0
MIN_UTTERANCE_S = 0.4
MAX_UTTERANCE_S = 12.0

TRANSFER_WORDS = [
    "murshid", "sadhin", "sadin", "boss", "owner",
    "মুরশিদ", "সাধিন", "সাদিন", "বস", "ওনার",
    "connect", "কানেক্ট",
]


def wants_transfer(text: str) -> bool:
    low = text.lower()
    return any(w in low for w in TRANSFER_WORDS)


class CallSession:
    def __init__(self, room_id: str):
        self.room_id = room_id
        self.caller_ws = None
        self.owner_ws = None
        self.caller_label = "Unknown"
        self.caller_name = ""
        self.started = time.time()
        self.transcript: list[dict] = []
        self.inbound_mulaw = bytearray()   # caller audio (both legs recorded)
        self.outbound_mulaw = bytearray()  # AI + owner audio
        self._buffer = bytearray()         # current utterance (mu-law)
        self._buffer_pcm = bytearray()
        self._speech_started: float | None = None
        self._last_speech: float | None = None
        self._speaking = False             # AI audio playing now
        self._play_task: asyncio.Task | None = None
        self._send_lock = asyncio.Lock()
        self._finalized = False
        self.transfer_requested = False
        self.transfer_status = ""          # "" | "waiting" | "bridged" | "timeout"
        self.bridged = False
        self._empty_heard = 0
        self.record = store.create_record(room_id, self.caller_label)
        ROOMS[room_id] = self

    # ------------------------------------------------------------ helpers
    def base_url(self, request_host: str = "") -> str:
        if settings.BASE_URL:
            return settings.BASE_URL.rstrip("/")
        if request_host:
            scheme = "https" if not request_host.startswith("localhost") else "http"
            return f"{scheme}://{request_host}"
        return ""

    @property
    def join_url(self) -> str:
        base = self.base_url()
        return f"{base}/join/{self.room_id}" if base else f"/join/{self.room_id}"

    async def _send(self, ws, msg: dict):
        if ws is None:
            return
        async with self._send_lock:
            try:
                await ws.send_text(json.dumps(msg))
            except Exception as exc:
                log.debug("send failed: %s", exc)

    async def send_caller(self, msg: dict):
        await self._send(self.caller_ws, msg)

    async def send_owner(self, msg: dict):
        await self._send(self.owner_ws, msg)

    # ------------------------------------------------------------- speech
    async def speak(self, text: str):
        """Speak to the caller: server TTS audio, or browser TTS fallback."""
        self.transcript.append({"role": "ai", "text": text})
        self.record["transcript"] = self.transcript
        mulaw = await asyncio.to_thread(ai.synthesise, text)
        if mulaw:
            await self._play_mulaw(mulaw)
        else:
            # No TTS key: the caller's browser speaks it (free fallback).
            await self.send_caller({"event": "say", "text": text})

    async def _play_mulaw(self, mulaw: bytes):
        self.outbound_mulaw += mulaw
        self._speaking = True
        try:
            self._play_task = asyncio.current_task()
            for i in range(0, len(mulaw), 160):
                if not self._speaking:
                    break
                chunk = mulaw[i : i + 160]
                await self.send_caller(
                    {
                        "event": "playAudio",
                        "media": {
                            "contentType": "audio/x-mulaw",
                            "sampleRate": 8000,
                            "payload": base64.b64encode(chunk).decode(),
                        },
                    }
                )
                await asyncio.sleep(0.02)
        except asyncio.CancelledError:
            pass
        finally:
            self._speaking = False
            self._play_task = None

    async def stop_playback(self):
        if not (self._speaking or self._play_task):
            return  # nothing playing: do not send a stray clearAudio
        self._speaking = False
        if self._play_task and not self._play_task.done():
            self._play_task.cancel()
        await self.send_caller({"event": "clearAudio"})

    # --------------------------------------------------------- conversation
    async def greet(self):
        await self.speak(settings.GREETING_BN)

    async def on_caller_audio(self, mulaw: bytes):
        """One ~20 ms mu-law frame from the caller."""
        self.inbound_mulaw += mulaw
        if self.bridged:
            # Owner is on the line: forward caller audio to the owner.
            await self.send_owner(
                {
                    "event": "playAudio",
                    "media": {
                        "contentType": "audio/x-mulaw",
                        "sampleRate": 8000,
                        "payload": base64.b64encode(mulaw).decode(),
                    },
                }
            )
            return
        pcm = audio.mulaw_to_pcm16(mulaw)
        rms = audio.pcm16_rms(pcm)
        now = time.time()
        if rms >= SPEECH_RMS:
            if self._speaking:
                # Barge-in: caller interrupted the AI — stop speaking.
                await self.stop_playback()
            if self._speech_started is None:
                self._speech_started = now
                self._buffer = bytearray()
                self._buffer_pcm = bytearray()
            self._last_speech = now
            self._buffer += mulaw
            self._buffer_pcm += pcm
            if now - self._speech_started >= MAX_UTTERANCE_S:
                await self._finish_utterance()
        else:
            if self._speech_started is not None:
                self._buffer += mulaw
                self._buffer_pcm += pcm
                if self._last_speech and now - self._last_speech >= SILENCE_AFTER_SPEECH_S:
                    await self._finish_utterance()

    async def _finish_utterance(self):
        buf = bytes(self._buffer)
        pcm = bytes(self._buffer_pcm)
        self._buffer = bytearray()
        self._buffer_pcm = bytearray()
        self._speech_started = None
        self._last_speech = None
        if audio.pcm16_duration_seconds(pcm) < MIN_UTTERANCE_S:
            return
        text = await asyncio.to_thread(ai.transcribe, buf)
        if text is None:
            # No STT configured: browser STT (transcript events) is the
            # fallback; nothing to do for raw audio.
            return
        await self.on_caller_text(text.strip())

    async def on_caller_text(self, text: str):
        """A caller utterance, from server STT or browser STT."""
        if not text:
            self._empty_heard += 1
            if self._empty_heard <= 2:
                await self.speak(settings.NOT_HEARD_LINE_BN)
            return
        self._empty_heard = 0
        self.transcript.append({"role": "caller", "text": text})
        self.record["transcript"] = self.transcript
        if wants_transfer(text):
            await self.start_transfer(text)
            return
        reply, _source = await asyncio.to_thread(ai.brain_reply, self.transcript, text)
        await self.speak(reply)

    # ------------------------------------------------------------ transfer
    async def start_transfer(self, trigger_text: str):
        self.transfer_requested = True
        self.transfer_status = "waiting"
        self.record["transfer_requested"] = True
        store.add_notification(
            "transfer",
            f"Transfer — {self.caller_label} Murshid/Sadhin ke chaiche",
            call_id=self.room_id,
            body=f"Join korte tap koro: {self.join_url}  |  Caller boleche: {trigger_text}",
        )
        await self.speak(settings.TRANSFER_LINE_BN)
        # Wait for the owner to join; the join handler flips the flag.
        for _ in range(int(settings.TRANSFER_WAIT_SECONDS * 5)):
            if self.bridged:
                return
            await asyncio.sleep(0.2)
        if not self.bridged:
            self.transfer_status = "timeout"
            await self.speak(settings.BUSY_LINE_BN)
            await asyncio.sleep(0.5)
            await self.end("owner-busy")

    async def owner_joined(self):
        self.bridged = True
        self.transfer_status = "bridged"
        self.record["transferred"] = True
        await self.stop_playback()
        await self.send_caller({"event": "transferred"})
        await self.send_owner(
            {"event": "say", "text": "Owner join koreche. Caller-er sathe kotha bolun."}
        )

    async def on_owner_audio(self, mulaw: bytes):
        self.outbound_mulaw += mulaw
        if self.bridged:
            await self.send_caller(
                {
                    "event": "playAudio",
                    "media": {
                        "contentType": "audio/x-mulaw",
                        "sampleRate": 8000,
                        "payload": base64.b64encode(mulaw).decode(),
                    },
                }
            )

    # -------------------------------------------------------------- ending
    async def end(self, reason: str = "ended"):
        await self.send_caller({"event": "call-ended", "reason": reason})
        await self.send_owner({"event": "call-ended", "reason": reason})
        await self.finalize()

    async def finalize(self):
        if self._finalized:
            return
        self._finalized = True
        self.record["summary"] = await asyncio.to_thread(ai.summarise_call, self.transcript)
        self.record["transfer_status"] = self.transfer_status
        store.save_recordings(
            self.room_id, bytes(self.inbound_mulaw), bytes(self.outbound_mulaw)
        )
        store.finalize_record(self.room_id)
        store.add_notification(
            "call",
            f"Ei phone koreche — {self.caller_label}",
            call_id=self.room_id,
            body=self.record["summary"],
        )
        ROOMS.pop(self.room_id, None)


def get_session(room_id: str) -> CallSession | None:
    return ROOMS.get(room_id)


def get_or_create_session(room_id: str) -> CallSession:
    return ROOMS.get(room_id) or CallSession(room_id)
