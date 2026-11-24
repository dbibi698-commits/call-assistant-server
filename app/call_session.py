"""Live call sessions: the conversation loop, transfer and bridging.

v2 (Dograh-pattern upgrade): the call runs as an explicit turn-state
machine — greeting -> listening -> thinking -> speaking -> listening
— so the caller page always shows what the AI is doing, and a turn
never stalls. Barge-in is only confirmed after 0.15 s of sustained
caller speech (Dograh's UserAggregator pattern), so a cough does not
cut the greeting off. Every AI turn ends with a question or a nudge
(Dograh's turn-taking rule), so the caller always knows it is their
turn. Transfer has two paths: naming the owner (Murshid / Sadhin)
transfers at once; a vague "connect koro" asks once "Apni ki owner
er sathe kotha bolte chan?" and transfers only on yes. The AI never
ends the call itself — only the caller ending it, a goodbye, or an
owner-busy transfer timeout ends it.

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
                    {"event":"state","state":"listening|thinking|speaking|..."}
                    {"event":"transferred"}               (owner joined)
                    {"event":"call-ended","reason":"..."}

Conversation loop (listen-first, never hangs up by itself):
  greeting -> listen -> transcribe -> Gemini reply -> speak -> listen ...
  Empty STT -> "Ami thik shunte parini, arekbar bolun" and listen again.

Transfer (the ONLY connection to the user's phone):
  Caller says owner / Murshid / Sadhin -> the AI takes the CALLER'S
  NAME FIRST (v2.1: "ট্রান্সফার করার আগে আপনার নামটা বলুন?"), and
  only then says "এক মিনিট, <name> — কানেক্ট করছি" and transfers.
  If the caller refuses the name twice (or stays silent twice) the
  transfer still happens, with the name "Naam dey ni" — the transfer
  is never blocked forever, and the call is never hung up. The
  transfer notification carries the name, so the phone alert shows
  WHO is calling. A notification + join link is created, and the
  server waits TRANSFER_WAIT_SECONDS for the owner to open
  /join/<room_id> and join the SAME call. If the owner joins, the AI
  steps aside and audio is bridged caller <-> owner (server forwards
  the frames; recording keeps running). If nobody joins, the AI
  tells the caller the owner is busy and ends politely.
  No live listening, no live audio on the user's phone at any other time.

Name capture (v2.1): the name is also asked ONCE, early, right after
  the greeting ("আপনার নামটা কী, বলুন?") when the /call page did not
  supply one — but only once, and it never blocks the conversation:
  if the caller talks about something else instead, the AI just
  listens and answers normally. A name given at any point is stored
  in caller_name and used in the record, summary and notifications.
"""

import asyncio
import base64
import json
import logging
import re
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
# Barge-in guard (Dograh pattern: the UserAggregator owns interruption,
# it is only confirmed after real speech, not a noise spike). The AI
# stops speaking only after 0.15 s of sustained caller speech.
BARGE_IN_CONFIRM_S = 0.15

# Turn states (Dograh pipeline pattern: the call is always in exactly
# one state, and the caller page shows it — "AI shunche / AI bolche").
STATE_GREETING = "greeting"
STATE_LISTENING = "listening"
STATE_THINKING = "thinking"
STATE_SPEAKING = "speaking"
STATE_TRANSFERRING = "transferring"
STATE_BRIDGED = "bridged"
STATE_ENDING = "ending"

# Direct transfer: the caller named the owner — transfer at once.
DIRECT_TRANSFER_WORDS = [
    "murshid", "sadhin", "sadin", "boss", "owner",
    "মুরশিদ", "সাধিন", "সাদিন", "বস", "ওনার",
]
# Ambiguous transfer: the caller wants *a* connection but did not name
# the owner — ask once ("Apni ki owner er sathe kotha bolte chan?")
# and transfer only on yes (Dograh handoff-confirmation pattern).
AMBIGUOUS_TRANSFER_WORDS = [
    "connect", "কানেক্ট", "phone ta dao", "call ta dao", "dite hobe",
    "ফোনটা দাও", "কলটা দাও", "লাগাও",
]
YES_WORDS = ["হ্যাঁ", "ha", "haan", "yes", "dao", "দাও", "kor", "করো", "korun", "করুন"]
NO_WORDS = ["না", "na", "no", "lagbe na", "লাগবে না"]
# Goodbye: the caller ends the call politely — AI says goodbye too,
# then ends. The AI NEVER ends the call on its own otherwise.
# NOTE: short words ("bye", "বাই", "ha", "na") are matched as whole
# tokens only — substring matching would false-fire ("সবাই" contains
# "বাই", "কথা" contains "ha", "জানিনা" contains "na") and end a call
# the caller never meant to end.
GOODBYE_PHRASES = [
    "goodbye", "আল্লাহ হাফেজ", "আল্লাহ হাফিজ",
    "ফোন রাখছি", "রাখছি", "রাখলাম",
]
GOODBYE_TOKENS = ["bye", "বাই"]
GOODBYE_LINE_BN = "ঠিক আছে, ধন্যবাদ। আবার কথা হবে।"
TRANSFER_CONFIRM_BN = "আপনি কি ওনার-এর সাথে কথা বলতে চান?"
TRANSFER_CANCEL_BN = "ঠিক আছে, তাহলে বলুন — আমি শুনছি।"

# v2.1 — name before transfer. The name the record/notification
# carries when the caller would not give one (transfer still happens).
NAME_NOT_GIVEN = "Naam dey ni"
# Refusal to give a name, matched as phrases or as whole tokens.
# ("না" / "na" as a token only — a name token is never "না".)
NAME_REFUSAL_PHRASES = [
    "নাম বলবো না", "নাম বলব না", "নাম বলতে চাই না", "নাম দেবো না",
    "নাম দেব না", "নাম লাগবে না", "বলবো না", "বলব না",
    "বলতে চাই না", "naam bolbo na", "bolbo na",
    "don't want to say", "won't say",
]
NAME_REFUSAL_TOKENS = ["না", "na", "no"]
# Words that prove an utterance is NOT a name (an issue/question).
NAME_NOT_NAME_WORDS = [
    "জানতে", "চাই", "করতে", "হয়েছে", "সমস্যা", "কেন", "কিভাবে",
    "কখন", "কোথায়", "কী", "কি", "কেমন", "কত", "আছে", "নেই",
]


def _extract_name(text: str) -> str:
    """Pull a likely name out of an utterance, or "" if none.

    Handles "আমি রহিম", "আমার নাম রহিম", "রহিম বলছি" (X speaking).
    At most 4 words — anything longer is a sentence, not a name.
    """
    t = (text or "").strip().strip("।.,!? ")
    low = t.lower()
    for suffix in (" বলছি", " bolchi", " bolchhi"):
        if low.endswith(suffix):
            t = t[: -len(suffix)].strip()
            low = t.lower()
            break
    for prefix in ("আমার নামটা", "আমার নাম", "amar naamta", "amar naam",
                   "amar nam", "আমি", "ami"):
        if low.startswith(prefix):
            t = t[len(prefix):].strip(" :—-")
            break
    toks = [w for w in re.split(r"[\s,।!?]+", t) if w]
    if not toks or len(toks) > 4:
        return ""
    return " ".join(toks)


def _looks_like_name(text: str) -> bool:
    """True when the utterance is plausibly just a name."""
    if not _extract_name(text):
        return False
    if wants_transfer_direct(text) or wants_transfer_ambiguous(text):
        return False
    if is_goodbye(text):
        return False
    # A yes/no ("হ্যাঁ", "না") or a greeting/thanks is an answer,
    # not a name — never store "হ্যাঁ" as the caller's name.
    if _contains_token(text, YES_WORDS) or _contains_token(text, NO_WORDS):
        return False
    if _contains_token(text, ["ধন্যবাদ", "হ্যালো", "hello", "hi"]):
        return False
    low = text.lower()
    return not any(w in low for w in NAME_NOT_NAME_WORDS)


def _is_name_refusal(text: str) -> bool:
    return _contains(text, NAME_REFUSAL_PHRASES) or _contains_token(
        text, NAME_REFUSAL_TOKENS
    )


def _tokens(text: str) -> list[str]:
    return [t for t in re.split(r"[\s,।!?]+", text.lower()) if t]


def _contains(text: str, words: list[str]) -> bool:
    low = text.lower()
    return any(w in low for w in words)


def _contains_token(text: str, words: list[str]) -> bool:
    toks = _tokens(text)
    return any(w in toks for w in words)


def wants_transfer_direct(text: str) -> bool:
    return _contains(text, DIRECT_TRANSFER_WORDS)


def wants_transfer_ambiguous(text: str) -> bool:
    return _contains(text, AMBIGUOUS_TRANSFER_WORDS)


def is_goodbye(text: str) -> bool:
    return _contains(text, GOODBYE_PHRASES) or _contains_token(text, GOODBYE_TOKENS)


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
        # v2 turn-state machine (Dograh pipeline pattern).
        self.state = STATE_LISTENING
        self._interrupt_since: float | None = None
        self._transfer_pending = False     # asked "owner ke chaichen?" — awaiting yes/no
        # v2.1 — name before transfer: asked for the name first and the
        # next utterance is the answer; refusals/silence counted so a
        # transfer is never blocked forever.
        self._awaiting_name_for_transfer = False
        self._name_refusals = 0
        self._transfer_trigger = ""      # what the caller said to ask for transfer
        # v2.1 — early name ask: asked once, right after the greeting,
        # only when the name is still unknown; soft — never blocks.
        self._name_asked = False
        self._expecting_name_soft = False
        self.record = store.create_record(room_id, self.caller_label)
        ROOMS[room_id] = self

    async def _set_state(self, state: str):
        """Move to a new turn state and tell the caller page about it."""
        self.state = state
        log.info("room=%s state=%s", self.room_id, state)
        await self.send_caller({"event": "state", "state": state})

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

    # --------------------------------------------------------- name (v2.1)
    def _set_caller_name(self, name: str):
        """Store the caller's name everywhere it shows up: the record,
        the call label (used by the 'Ei phone koreche' notification),
        and the transfer notification."""
        name = (name or "").strip()
        self.caller_name = name
        self.record["from_name"] = name
        if name:
            self.caller_label = name

    def _transfer_line(self) -> str:
        """'Ek minute, <name> — connect korchhi' when the name is
        known; the plain line when it is not (or was refused)."""
        name = self.caller_name.strip()
        if name and name != NAME_NOT_GIVEN:
            return f"এক মিনিট, {name} — কানেক্ট করছি।"
        return settings.TRANSFER_LINE_BN

    # ------------------------------------------------------------- speech
    async def speak(self, text: str):
        """Speak to the caller: server TTS audio, or browser TTS fallback.

        Turn states: SPEAKING while the reply plays (or is spoken by
        the browser), then back to LISTENING — the loop never stalls,
        because every turn ends by listening again.
        """
        self.transcript.append({"role": "ai", "text": text})
        self.record["transcript"] = self.transcript
        await self._set_state(STATE_SPEAKING)
        mulaw = await asyncio.to_thread(ai.synthesise, text)
        if mulaw:
            await self._play_mulaw(mulaw)
        else:
            # No TTS key: the caller's browser speaks it (free fallback).
            await self.send_caller({"event": "say", "text": text})
        if self.state == STATE_SPEAKING:
            await self._set_state(STATE_LISTENING)

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
        await self._set_state(STATE_GREETING)
        await self.speak(settings.GREETING_BN)
        # v2.1: ask the caller's name ONCE, right after the greeting,
        # only when it is still unknown (the /call page did not supply
        # one). Soft ask — if the caller talks about something else
        # instead, the conversation just goes on normally.
        if not self.caller_name.strip() and not self._name_asked:
            self._name_asked = True
            self._expecting_name_soft = True
            await self.speak(settings.NAME_ASK_BN)

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
                # Barge-in, Dograh-style: interruption is only confirmed
                # after sustained caller speech (0.15 s), not a cough
                # or a noise spike cutting the greeting off mid-word.
                if self._interrupt_since is None:
                    self._interrupt_since = now
                elif now - self._interrupt_since >= BARGE_IN_CONFIRM_S:
                    self._interrupt_since = None
                    await self.stop_playback()
                    await self._set_state(STATE_LISTENING)
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
            self._interrupt_since = None
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
        """A caller utterance, from server STT or browser STT.

        Turn loop (Dograh pipeline pattern): LISTENING -> THINKING ->
        SPEAKING -> LISTENING. The AI never ends the call itself —
        only the caller ending it, a goodbye, or an owner-busy
        transfer timeout ends it.

        v2.1 name handling, in order:
        * awaiting a name for transfer — the utterance IS the answer
          (name, refusal, or something else); a transfer is never
          blocked forever: two refusals/silences/not-a-name answers
          transfer anyway with "Naam dey ni".
        * soft early name ask — a name-like answer is stored and
          acknowledged; anything else just continues the conversation.
        """
        if not text:
            self._empty_heard += 1
            # Silence while waiting for a name counts as a refusal:
            # two of them and the transfer goes ahead anyway.
            if self._awaiting_name_for_transfer:
                self._name_refusals += 1
                if self._name_refusals >= 2:
                    self._set_caller_name(NAME_NOT_GIVEN)
                    await self._do_transfer("")
                    return
            if self._empty_heard <= 2:
                await self.speak(settings.NOT_HEARD_LINE_BN)
            return
        self._empty_heard = 0
        self.transcript.append({"role": "caller", "text": text})
        self.record["transcript"] = self.transcript
        # Goodbye: caller ends politely — AI says goodbye too, then ends.
        # (Wins even while a name is being awaited.)
        if is_goodbye(text):
            await self.speak(GOODBYE_LINE_BN)
            await self.end("caller-goodbye")
            return
        # v2.1 — awaiting a name before transfer: this utterance is
        # the answer. Capture it, or count a refusal and retry once.
        if self._awaiting_name_for_transfer:
            if _is_name_refusal(text):
                self._name_refusals += 1
                if self._name_refusals >= 2:
                    self._set_caller_name(NAME_NOT_GIVEN)
                    await self._do_transfer("")
                else:
                    await self.speak(settings.NAME_RETRY_BN)
                return
            name = _extract_name(text)
            if name and _looks_like_name(text):
                self._set_caller_name(name)
                await self._do_transfer("")
                return
            # Not a name and not a refusal (caller talked about
            # something else): ask once more, then transfer anyway.
            self._name_refusals += 1
            if self._name_refusals >= 2:
                self._set_caller_name(NAME_NOT_GIVEN)
                await self._do_transfer("")
            else:
                await self.speak(settings.NAME_RETRY_BN)
            return
        # Transfer confirmation: an earlier ambiguous "connect koro"
        # asked "owner ke chaichen?" — this utterance answers it.
        if self._transfer_pending:
            self._transfer_pending = False
            if _contains_token(text, YES_WORDS):
                await self.start_transfer(text)
                return
            if _contains_token(text, NO_WORDS):
                await self.speak(TRANSFER_CANCEL_BN)
                return
            # Neither yes nor no: treat as a normal utterance.
        # v2.1 — soft early name ask: a name-like answer is stored
        # and acknowledged; a transfer request or anything else just
        # continues the conversation normally (flag cleared either way).
        if self._expecting_name_soft:
            self._expecting_name_soft = False
            name = _extract_name(text)
            if name and _looks_like_name(text):
                self._set_caller_name(name)
                await self.speak(f"ধন্যবাদ, {name}। বলুন, কী হয়েছে?")
                return
        if wants_transfer_direct(text):
            await self.start_transfer(text)
            return
        if wants_transfer_ambiguous(text):
            self._transfer_pending = True
            await self.speak(TRANSFER_CONFIRM_BN)
            return
        await self._set_state(STATE_THINKING)
        reply, _source = await asyncio.to_thread(ai.brain_reply, self.transcript, text)
        await self.speak(reply)

    # ------------------------------------------------------------ transfer
    async def start_transfer(self, trigger_text: str):
        """Begin a transfer. v2.1 — NAME FIRST: if the caller's name
        is still unknown, do NOT transfer yet — ask for the name and
        wait for the answer (see on_caller_text). The transfer itself
        happens in _do_transfer, only once the name is known or the
        caller has refused it twice."""
        self.transfer_requested = True
        self.record["transfer_requested"] = True
        self._transfer_pending = False
        self._transfer_trigger = trigger_text
        if not self.caller_name.strip():
            self._awaiting_name_for_transfer = True
            self._name_refusals = 0
            await self._set_state(STATE_TRANSFERRING)
            await self.speak(settings.NAME_BEFORE_TRANSFER_BN)
            return
        await self._do_transfer(trigger_text)

    async def _do_transfer(self, trigger_text: str):
        """The transfer itself: notify the owner (WITH the caller
        name, so the phone alert shows WHO is calling), say the
        transfer line with the name, and wait for the owner to join
        the same call via /join/<room_id>."""
        self._awaiting_name_for_transfer = False
        self.transfer_status = "waiting"
        trigger_text = trigger_text or self._transfer_trigger
        await self._set_state(STATE_TRANSFERRING)
        display = self.caller_name.strip() or self.caller_label
        store.add_notification(
            "transfer",
            f"Ei phone koreche — {display}",
            call_id=self.room_id,
            body=f"Transfer: {display} Murshid/Sadhin ke chaiche. "
                 f"Join korte tap koro: {self.join_url}  |  "
                 f"Caller boleche: {trigger_text}",
            caller_name=display,
        )
        await self.speak(self._transfer_line())
        await self._set_state(STATE_TRANSFERRING)
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
        await self._set_state(STATE_BRIDGED)
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
        await self._set_state(STATE_ENDING)
        await self.send_caller({"event": "call-ended", "reason": reason})
        await self.send_owner({"event": "call-ended", "reason": reason})
        await self.finalize()

    async def finalize(self):
        if self._finalized:
            return
        self._finalized = True
        self.record["summary"] = await asyncio.to_thread(ai.summarise_call, self.transcript)
        self.record["transfer_status"] = self.transfer_status
        self.record["turn_state"] = self.state
        self.record["language"] = "bn-IN"
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
