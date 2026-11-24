"""Pipecat engine (server v3) for the Mith Hera call server.

The real Pipecat pipeline (pipecat-ai, BSD-2) runs the caller leg of
each call:

    transport.input()  (FastAPI WebSocket, mu-law 8 kHz — the same
                        wire format as the v2.1 server, so the /call
                        page and the v1.6.1 app keep working)
      -> BridgeProcessor   (records caller audio, forwards it to the
                            owner while a transfer is bridged)
      -> Sarvam STT       (keyed only; no-key calls get browser STT
                            transcripts injected by the serializer)
      -> user_aggregator  (Silero VAD + SmartTurn V3 — Pipecat's
                            DEFAULT stop strategy — decides when the
                            caller finished speaking; barge-in via
                            InterruptionFrame)
      -> ConversationProcessor (this app's brain decisions: greeting,
                            name-before-transfer, transfer confirm,
                            goodbye; normal turns pass through)
      -> LLM              (Gemini via OpenAI-compatible endpoint,
                            keyed only; ScriptedLLMProcessor when
                            no key — scripted Bengali replies)
      -> TTS              (Sarvam Bulbul v3, keyed only;
                            BrowserTTSProcessor when no key —
                            "say" events to the caller page)
      -> TapProcessor     (records AI + owner audio)
      -> transport.output() -> assistant_aggregator

Why Pipecat fits Render free: the pipeline with both ONNX models
(Silero VAD + SmartTurn V3) measures ~142 MB RSS in this sandbox;
the full venv is ~291 MB on disk. Render free gives 512 MB RAM.
No Docker, no Postgres, no Redis — one single-process FastAPI app,
exactly like v2.1.

Free vs keyed (env-only, never baked into code or the APK):
  * FREE forever: the engine itself, the WebRTC /call page, browser
    STT (bn-IN) + browser TTS (bn-IN), scripted Bengali replies.
    A call with NO keys set still works end-to-end in Bengali.
  * KEYED (optional, set in the Render dashboard only):
      GEMINI_API_KEY  -> smart Bengali replies (gemini-3.5-flash,
                         OpenAI-compatible endpoint)
      SARVAM_API_KEY  -> server-side Bengali STT (Saarika saaras:v4,
                         bn-IN) + TTS (Bulbul v3, voice roopa, bn-IN)
    Without them the pipeline runs the free fallbacks above.

Hard rules kept (same as v2.1):
  * Bengali only — system prompt in Bengali, replies in Bengali,
    never English; STT/TTS language bn-IN.
  * The AI NEVER ends a call by itself — only the caller ending it,
    a goodbye, or an owner-busy transfer timeout ends it.
  * Transfer only to Murshid/Sadhin — name asked first, transfer
    notification carries the caller's name.
  * The user's phone is never used for the call itself — the server
    answers independently, even when the phone has no internet.
    (The only phone ring is the transfer alert, when the phone is
    online at that moment.)
  * Recordings: inbound + outbound + combined conversation.wav,
    saved per call, never deleted.

The v2.1 CallSession loop stays as the fallback: if Pipecat cannot
be imported (or a call fails before the pipeline starts), main.py
routes the call through the v2.1 handler instead.
"""

import asyncio
import base64
import json
import logging
import time

import pipecat
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import (
    EndFrame,
    Frame,
    InputAudioRawFrame,
    LLMContextFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMRunFrame,
    OutputAudioRawFrame,
    OutputTransportMessageFrame,
    StartFrame,
    TextFrame,
    TranscriptionFrame,
    TTSSpeakFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.task import PipelineParams
from pipecat.pipeline.worker import PipelineWorker, ProcessorUnusablePolicy
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContext,
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.processors.frame_processor import FrameProcessor
from pipecat.serializers.plivo import PlivoFrameSerializer
from pipecat.services.openai.llm import OpenAILLMService
from pipecat.services.sarvam.stt import SarvamSTTService
from pipecat.services.sarvam.tts import SarvamTTSService
from pipecat.transcriptions.language import Language
from pipecat.transports.websocket.fastapi import (
    FastAPIWebsocketParams,
    FastAPIWebsocketTransport,
)
from pipecat.workers.runner import WorkerRunner

from . import ai, audio, store
from .call_session import (
    GOODBYE_LINE_BN,
    NAME_NOT_GIVEN,
    NO_WORDS,
    STATE_GREETING,
    STATE_LISTENING,
    STATE_SPEAKING,
    STATE_THINKING,
    STATE_TRANSFERRING,
    TRANSFER_CANCEL_BN,
    TRANSFER_CONFIRM_BN,
    YES_WORDS,
    CallSession,
    _contains_token,
    _extract_name,
    _is_name_refusal,
    _looks_like_name,
    is_goodbye,
    wants_transfer_ambiguous,
    wants_transfer_direct,
)
from .config import settings

log = logging.getLogger("mithhera.pipecat")

VERSION = "3.0-pipecat"
PIPECAT_VERSION = pipecat.__version__

# How long the AI waits, after speaking the transfer line, before
# deciding the owner is busy. v2.1 default (env TRANSFER_WAIT_SECONDS).
# The v2/v3 smoke tests set it to 5 s.
TRANSFER_WAIT_SECONDS = settings.TRANSFER_WAIT_SECONDS

# How long after a goodbye / owner-busy line the engine waits before
# ending the call, so the last words reach the caller. The line is
# spoken at roughly 12 characters per second of Bengali speech.
_END_WAIT_PER_CHAR_S = 1.0 / 12.0
_END_WAIT_MIN_S = 1.2
_END_WAIT_MAX_S = 8.0


class EngineNotStarted(Exception):
    """run_call failed BEFORE the pipeline started reading the socket.

    main.py catches this and falls back to the v2.1 handler on the
    same websocket (safe: the transport never read a message yet).
    Anything raised after the pipeline started is not this error —
    the call is then finalised, never re-routed.
    """


def _sarvam_stt_model() -> str:
    """The Sarvam streaming STT model (saaras:*). The v2.1 env
    SARVAM_STT_MODEL defaults to saarika:v2.5, which is the HTTP
    model — the Pipecat streaming service needs a saaras model, so
    anything else falls back to saaras:v4."""
    model = (settings.SARVAM_STT_MODEL or "").strip()
    return model if model.startswith("saaras:") else "saaras:v4"


def _sarvam_tts_model() -> str:
    """The Sarvam streaming TTS model. The v2.1 env SARVAM_TTS_MODEL
    defaults to bulbul:v2 (HTTP); the streaming service supports
    bulbul:v3 / bulbul:v4-flash, so anything else falls back to v3."""
    model = (settings.SARVAM_TTS_MODEL or "").strip()
    return model if model in ("bulbul:v3", "bulbul:v4-flash") else "bulbul:v3"


# ------------------------------------------------------------------ engine
def engine_info() -> dict:
    """What the pipeline is actually running — for /health."""
    sarvam = bool(settings.SARVAM_API_KEY)
    gemini = bool(settings.GEMINI_API_KEY)
    return {
        "engine": "pipecat",
        "version": VERSION,
        "pipecat": PIPECAT_VERSION,
        "vad": "silero",
        "turn_stop": "smart-turn-v3 (pipecat default stop strategy)",
        "language": "bn-IN",
        "stt": f"sarvam {_sarvam_stt_model()} bn-IN" if sarvam else "browser bn-IN (no key)",
        "llm": "gemini-3.5-flash (openai-compatible)" if gemini else "scripted bengali (no key)",
        "tts": f"sarvam {_sarvam_tts_model()} roopa bn-IN" if sarvam else "browser bn-IN (no key)",
        "free_mode": not (sarvam or gemini),
    }


def _end_wait_s(text: str) -> float:
    """Seconds to wait after speaking `text` before ending the call."""
    return min(_END_WAIT_MAX_S, max(_END_WAIT_MIN_S, len(text) * _END_WAIT_PER_CHAR_S))


# --------------------------------------------------------------- serializer
class MithHeraSerializer(PlivoFrameSerializer):
    """Plivo wire format (mu-law 8 kHz) + this server's extra events.

    The /call page sends exactly the same JSON the v2.1 server saw:
      {"event": "start", ...}   -> ignored (main.py consumed it)
      {"event": "media", ...}   -> InputAudioRawFrame (mu-law -> PCM)
      {"event": "transcript", "text": "..."}  -> TranscriptionFrame
          (browser STT, bn-IN — the no-key STT path)
      {"event": "stop"}         -> EndFrame (caller ended the call)
    Outbound: playAudio (mu-law), say (browser TTS), state,
    transferred, call-ended — all JSON, as in v2.1.
    """

    def __init__(self, stream_id: str):
        from pipecat.serializers.base_serializer import InputParams

        super().__init__(
            stream_id,
            params=InputParams(auto_hang_up=False),
        )

    async def deserialize(self, data) -> Frame | None:
        try:
            message = json.loads(data)
        except (json.JSONDecodeError, TypeError):
            return None
        event = message.get("event")
        if event == "transcript":
            text = (message.get("text") or "").strip()
            if not text:
                return None
            return TranscriptionFrame(
                text=text, user_id="caller", timestamp=time.time()
            )
        if event == "stop":
            # The caller ended the call (or the page closed). The
            # pipeline ends; ConversationProcessor sends "call-ended"
            # as the EndFrame flows through it.
            return EndFrame()
        return await super().deserialize(data)


# -------------------------------------------------------------- processors
class BridgeProcessor(FrameProcessor):
    """First processor after transport.input().

    * Records caller audio (PCM -> mu-law, appended to the session's
      inbound leg) on every InputAudioRawFrame.
    * While a transfer is bridged, forwards caller audio to the
      owner's websocket (playAudio) and does NOT pass it downstream
      — the AI stays out of the owner conversation.
    * Pumps owner audio from session.owner_audio_queue downstream
      as OutputAudioRawFrame (the owner talks to the caller through
      the pipeline; the TapProcessor records it).
    """

    def __init__(self, session: CallSession):
        super().__init__()
        self._session = session
        self._pump_task: asyncio.Task | None = None

    async def start(self, frame: StartFrame):
        await super().start(frame)
        self._pump_task = self.create_task(self._pump_owner_audio())

    async def _pump_owner_audio(self):
        while True:
            mulaw = await self._session.owner_audio_queue.get()
            if not self._session.bridged:
                continue
            pcm = audio.mulaw_to_pcm16(bytes(mulaw))
            await self.push_frame(
                OutputAudioRawFrame(audio=pcm, sample_rate=8000, num_channels=1)
            )

    async def process_frame(self, frame: Frame, direction):
        await super().process_frame(frame, direction)
        if isinstance(frame, InputAudioRawFrame):
            mulaw = audio.pcm16_to_mulaw(frame.audio)
            self._session.inbound_mulaw += mulaw
            if self._session.bridged:
                # Owner conversation: caller audio goes straight to
                # the owner, never to the AI / STT.
                await self._session._send(
                    self._session.owner_ws,
                    {
                        "event": "playAudio",
                        "media": {
                            "contentType": "audio/x-mulaw",
                            "sampleRate": 8000,
                            "payload": base64.b64encode(bytes(mulaw)).decode(),
                        },
                    },
                )
                return
            await self.push_frame(frame, direction)
            return
        await self.push_frame(frame, direction)

    async def cleanup(self):
        if self._pump_task is not None:
            self._pump_task.cancel()
        await super().cleanup()


class TapProcessor(FrameProcessor):
    """Records outbound audio (AI voice + bridged owner voice).

    Sits right before transport.output(): every OutputAudioRawFrame
    that reaches the caller is appended to the session's outbound
    leg. Nothing else is changed.
    """

    def __init__(self, session: CallSession):
        super().__init__()
        self._session = session

    async def process_frame(self, frame: Frame, direction):
        await super().process_frame(frame, direction)
        if isinstance(frame, OutputAudioRawFrame):
            self._session.outbound_mulaw += audio.pcm16_to_mulaw(frame.audio)
        await self.push_frame(frame, direction)


class BrowserTTSProcessor(FrameProcessor):
    """No-key TTS: AI text -> {"event": "say"} for the caller page.

    The page speaks it with the browser's Bengali speech synthesis
    (bn-IN, Bengali voice when the browser has one). TextFrames
    between LLMFullResponseStart/End are buffered into one "say";
    a TTSSpeakFrame (greeting, transfer line, goodbye...) is spoken
    at once. This is the free TTS path — it needs no key at all.
    """

    def __init__(self):
        super().__init__()
        self._in_response = False
        self._buffer = ""

    async def _flush(self):
        text = self._buffer.strip()
        self._buffer = ""
        if text:
            await self.push_frame(
                OutputTransportMessageFrame(message={"event": "say", "text": text})
            )

    async def process_frame(self, frame: Frame, direction):
        await super().process_frame(frame, direction)
        if isinstance(frame, LLMFullResponseStartFrame):
            self._in_response = True
            self._buffer = ""
            await self.push_frame(frame, direction)
            return
        if isinstance(frame, LLMFullResponseEndFrame):
            self._in_response = False
            await self._flush()
            await self.push_frame(frame, direction)
            return
        if isinstance(frame, TextFrame):
            if self._in_response:
                self._buffer += frame.text
            else:
                await self.push_frame(
                    OutputTransportMessageFrame(
                        message={"event": "say", "text": frame.text}
                    )
                )
            return
        if isinstance(frame, TTSSpeakFrame):
            await self.push_frame(
                OutputTransportMessageFrame(
                    message={"event": "say", "text": frame.text}
                )
            )
            return
        await self.push_frame(frame, direction)


class ScriptedLLMProcessor(FrameProcessor):
    """No-key LLM: scripted Bengali replies via ai.brain_reply().

    Runs on LLMContextFrame (the frame the user aggregator emits
    when a turn completes — the same trigger the real LLM service
    uses). The reply flows on as TextFrames inside a full-response
    pair, so the TTS and the assistant aggregator treat it exactly
    like a real LLM reply. Never silent: brain_reply always returns
    a Bengali line, with or without a Gemini key.
    """

    def __init__(self, context: LLMContext):
        super().__init__()
        self._context = context

    async def process_frame(self, frame: Frame, direction):
        await super().process_frame(frame, direction)
        if isinstance(frame, LLMContextFrame):
            messages = frame.context.get_messages()
            user_text = ""
            for msg in reversed(messages):
                if msg.get("role") == "user":
                    user_text = msg.get("content") or ""
                    break
            transcript = [
                {"role": m.get("role"), "text": m.get("content") or ""}
                for m in messages
                if m.get("role") in ("user", "assistant")
            ]
            reply, _source = await asyncio.to_thread(
                ai.brain_reply, transcript, user_text
            )
            await self.push_frame(LLMFullResponseStartFrame(), direction)
            await self.push_frame(TextFrame(text=reply), direction)
            await self.push_frame(LLMFullResponseEndFrame(), direction)
            return
        await self.push_frame(frame, direction)


class ConversationProcessor(FrameProcessor):
    """This app's brain decisions, on the Pipecat pipeline.

    Sits between the user aggregator and the LLM. It reads the last
    user message out of each LLMContextFrame and decides, with the
    SAME rules as v2.1's CallSession.on_caller_text (helpers imported
    from call_session.py, so a decision can never drift):

      goodbye           -> polite Bengali goodbye, then end
                          ("caller-goodbye")
      name for transfer -> capture / refusal / retry; two refusals
                          transfer anyway with "Naam dey ni"
      transfer confirm  -> yes transfers, no cancels
      soft name ask     -> name-like answer stored + acknowledged
      direct transfer   -> name first, then transfer at once
      ambiguous connect -> ask "owner ke chaichen?" first
      normal turn       -> pass the frame on to the LLM

    Handled turns never reach the LLM: the reply is spoken as a
    TTSSpeakFrame (keyed TTS speaks it, no-key BrowserTTS turns it
    into a "say" event) and added to the shared LLMContext, so the
    LLM's later turns still see the full conversation. The greeting
    is handled the same way, on the LLMRunFrame the engine queues
    when the caller connects.

    State events (greeting/listening/thinking/speaking/transferring)
    are sent to the caller page, exactly as v2.1 sent them.
    """

    def __init__(self, session: CallSession, context: LLMContext, engine: "CallEngine"):
        super().__init__()
        self._session = session
        self._context = context
        self._engine = engine

    # ---------------------------------------------------------- helpers
    async def _emit_state(self, state: str):
        self._session.state = state
        await self.push_frame(
            OutputTransportMessageFrame(message={"event": "state", "state": state})
        )

    async def _speak(self, text: str, state: str = STATE_SPEAKING):
        """Record an AI line, add it to the LLM context, speak it."""
        self._session.transcript.append({"role": "ai", "text": text})
        self._session.record["transcript"] = self._session.transcript
        self._context.add_message({"role": "assistant", "content": text})
        await self._emit_state(state)
        await self.push_frame(TTSSpeakFrame(text=text))
        await self._emit_state(STATE_LISTENING)

    async def _end_after_speaking(self, text: str, reason: str):
        """Speak `text`, then end the call after it has been heard."""
        await self._speak(text)
        self._engine.schedule_end(reason, _end_wait_s(text))

    async def _do_transfer(self, trigger_text: str):
        """Name-first transfer (v2.1 _do_transfer): notify the owner
        WITH the caller name, speak the transfer line, wait for the
        owner to join. The engine's wait task ends the call with the
        busy line if the owner never joins."""
        s = self._session
        s._awaiting_name_for_transfer = False
        s.transfer_status = "waiting"
        trigger_text = trigger_text or s._transfer_trigger
        display = s.caller_name.strip() or s.caller_label
        store.add_notification(
            "transfer",
            f"Ei phone koreche — {display}",
            call_id=s.room_id,
            body=f"Transfer: {display} Murshid/Sadhin ke chaiche. "
                 f"Join korte tap koro: {s.join_url}  |  "
                 f"Caller boleche: {trigger_text}",
            caller_name=display,
        )
        await self._speak(s._transfer_line(), state=STATE_TRANSFERRING)
        await self._emit_state(STATE_TRANSFERRING)
        self._engine.begin_transfer_wait()

    async def _start_transfer(self, trigger_text: str):
        """v2.1 start_transfer: name first, transfer only after."""
        s = self._session
        s.transfer_requested = True
        s.record["transfer_requested"] = True
        s._transfer_pending = False
        s._transfer_trigger = trigger_text
        if not s.caller_name.strip():
            s._awaiting_name_for_transfer = True
            s._name_refusals = 0
            await self._emit_state(STATE_TRANSFERRING)
            await self._speak(settings.NAME_BEFORE_TRANSFER_BN, state=STATE_TRANSFERRING)
            return
        await self._do_transfer(trigger_text)

    # ---------------------------------------------------------- decision
    async def _decide(self, text: str) -> str:
        """Handle one caller utterance. Returns "handled" or "normal".

        Port of CallSession.on_caller_text, in the same order. The
        caller text is already in session.transcript (added by the
        caller of _decide — see process_frame).
        """
        s = self._session
        if is_goodbye(text):
            await self._end_after_speaking(GOODBYE_LINE_BN, "caller-goodbye")
            return "handled"
        if s._awaiting_name_for_transfer:
            if _is_name_refusal(text):
                s._name_refusals += 1
                if s._name_refusals >= 2:
                    s._set_caller_name(NAME_NOT_GIVEN)
                    await self._do_transfer("")
                else:
                    await self._speak(settings.NAME_RETRY_BN)
                return "handled"
            name = _extract_name(text)
            if name and _looks_like_name(text):
                s._set_caller_name(name)
                await self._do_transfer("")
                return "handled"
            s._name_refusals += 1
            if s._name_refusals >= 2:
                s._set_caller_name(NAME_NOT_GIVEN)
                await self._do_transfer("")
            else:
                await self._speak(settings.NAME_RETRY_BN)
            return "handled"
        if s._transfer_pending:
            s._transfer_pending = False
            if _contains_token(text, YES_WORDS):
                await self._start_transfer(text)
                return "handled"
            if _contains_token(text, NO_WORDS):
                await self._speak(TRANSFER_CANCEL_BN)
                return "handled"
            # Neither yes nor no: fall through to a normal turn.
        if s._expecting_name_soft:
            s._expecting_name_soft = False
            name = _extract_name(text)
            if name and _looks_like_name(text):
                s._set_caller_name(name)
                await self._speak(f"ধন্যবাদ, {name}। বলুন, কী হয়েছে?")
                return "handled"
        if wants_transfer_direct(text):
            await self._start_transfer(text)
            return "handled"
        if wants_transfer_ambiguous(text):
            s._transfer_pending = True
            await self._speak(TRANSFER_CONFIRM_BN)
            return "handled"
        return "normal"

    async def _greet(self):
        """The greeting, on connect — two separate "say" events, as
        in v2.1: the greeting, then (only when the name is unknown)
        the soft name ask."""
        s = self._session
        await self._emit_state(STATE_GREETING)
        await self._speak(settings.GREETING_BN, state=STATE_GREETING)
        if not s.caller_name.strip():
            s._expecting_name_soft = True
            await self._speak(settings.NAME_ASK_BN)

    # ---------------------------------------------------------- frames
    def _last_user_text(self, context) -> str:
        for msg in reversed(context.get_messages()):
            if msg.get("role") == "user":
                return msg.get("content") or ""
        return ""

    async def process_frame(self, frame: Frame, direction):
        await super().process_frame(frame, direction)

        if isinstance(frame, EndFrame):
            # The caller ended the call (serializer "stop" event) or
            # the engine asked for an end (goodbye / owner-busy).
            # The "call-ended" event must reach the caller BEFORE
            # this EndFrame closes the websocket.
            if not self._session._end_sent:
                self._session._end_sent = True
                reason = self._session._end_reason or "caller-ended"
                await self.push_frame(
                    OutputTransportMessageFrame(
                        message={"event": "call-ended", "reason": reason}
                    )
                )
            await self.push_frame(frame, direction)
            return

        if isinstance(frame, LLMRunFrame):
            # The greeting trigger (queued on connect) — or a safety
            # net: a run frame for a turn the context frame already
            # handled is swallowed, never answered twice.
            messages = self._context.get_messages()
            if not any(m.get("role") == "user" for m in messages):
                await self._greet()
                return
            text = self._last_user_text(self._context)
            if text and text == self._session._last_handled_text:
                return
            if text:
                self._session.transcript.append({"role": "caller", "text": text})
                self._session.record["transcript"] = self._session.transcript
                self._session._last_handled_text = text
                if await self._decide(text) == "handled":
                    return
            await self.push_frame(frame, direction)
            return

        if isinstance(frame, LLMContextFrame):
            text = self._last_user_text(frame.context)
            if text:
                self._session.transcript.append({"role": "caller", "text": text})
                self._session.record["transcript"] = self._session.transcript
                self._session._last_handled_text = text
                if await self._decide(text) == "handled":
                    return
                await self._emit_state(STATE_THINKING)
            await self.push_frame(frame, direction)
            return

        await self.push_frame(frame, direction)


# ------------------------------------------------------------- the engine
class CallEngine:
    """One Pipecat pipeline per call, on a WorkerRunner.

    A fresh runner is created per call (the documented embedding
    for long-lived hosts) and discarded when the call ends, so no
    finished workers accumulate in the app.
    """

    def __init__(self, websocket, session: CallSession, room_id: str):
        self._websocket = websocket
        self._session = session
        self._room_id = room_id
        self._worker: PipelineWorker | None = None
        self._transfer_wait_task: asyncio.Task | None = None
        self._end_task: asyncio.Task | None = None
        self._started = False

    # ------------------------------------------------------------ build
    def _build(self):
        session = self._session
        serializer = MithHeraSerializer(stream_id=self._room_id)
        transport = FastAPIWebsocketTransport(
            websocket=self._websocket,
            params=FastAPIWebsocketParams(
                audio_in_enabled=True,
                audio_out_enabled=True,
                audio_in_sample_rate=8000,
                audio_out_sample_rate=8000,
                serializer=serializer,
                session_timeout=None,
            ),
        )
        context = LLMContext()
        user_agg, asst_agg = LLMContextAggregatorPair(
            context,
            user_params=LLMUserAggregatorParams(vad_analyzer=SileroVADAnalyzer()),
        )
        bridge = BridgeProcessor(session)
        tap = TapProcessor(session)
        conversation = ConversationProcessor(session, context, self)

        # STT: Sarvam Saarika when the key is set; otherwise the
        # browser transcripts (serializer) are the STT.
        stt = None
        if settings.SARVAM_API_KEY:
            stt = SarvamSTTService(
                api_key=settings.SARVAM_API_KEY,
                settings=SarvamSTTService.Settings(
                    model=_sarvam_stt_model(), language=Language.BN_IN
                ),
            )
        # LLM: Gemini (OpenAI-compatible) when the key is set;
        # scripted Bengali replies when it is not.
        if settings.GEMINI_API_KEY:
            llm = OpenAILLMService(
                api_key=settings.GEMINI_API_KEY,
                base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
                settings=OpenAILLMService.Settings(
                    model="gemini-3.5-flash",
                    system_instruction=ai.SYSTEM_PROMPT_BN,
                ),
            )
        else:
            llm = ScriptedLLMProcessor(context)
        # TTS: Sarvam Bulbul when the key is set; otherwise the
        # browser speaks the "say" events (free, bn-IN).
        if settings.SARVAM_API_KEY:
            tts = SarvamTTSService(
                api_key=settings.SARVAM_API_KEY,
                settings=SarvamTTSService.Settings(
                    model=_sarvam_tts_model(), voice="roopa", language=Language.BN_IN
                ),
            )
        else:
            tts = BrowserTTSProcessor()

        processors = [transport.input(), bridge]
        if stt is not None:
            processors.append(stt)
        processors += [
            user_agg,
            conversation,
            llm,
            tts,
            tap,
            transport.output(),
            asst_agg,
        ]
        pipeline = Pipeline(processors)
        self._worker = PipelineWorker(
            pipeline,
            params=PipelineParams(
                audio_in_sample_rate=8000,
                audio_out_sample_rate=8000,
            ),
            idle_timeout_secs=None,
            cancel_on_idle_timeout=False,
            processor_unusable_policy=ProcessorUnusablePolicy.CONTINUE,
            name=f"mithhera-{self._room_id}",
        )
        self._transport = transport
        return transport

    # ------------------------------------------------------------ control
    def begin_transfer_wait(self):
        """Wait for the owner to join; if they never do, say the
        busy line and end the call (owner-busy)."""
        session = self._session

        async def _wait():
            try:
                await asyncio.sleep(TRANSFER_WAIT_SECONDS)
            except asyncio.CancelledError:
                return
            if session.bridged:
                return
            session.transfer_status = "timeout"
            # Speak the busy line through the pipeline, then end.
            await self._worker.queue_frame(
                TTSSpeakFrame(text=settings.BUSY_LINE_BN)
            )
            self.schedule_end("owner-busy", _end_wait_s(settings.BUSY_LINE_BN))

        task = asyncio.create_task(_wait())
        self._transfer_wait_task = task
        session._transfer_wait_task = task

    def schedule_end(self, reason: str, delay_s: float):
        """End the call `delay_s` seconds from now (after the last
        spoken line has been heard). Replaces any pending end."""

        async def _end():
            try:
                await asyncio.sleep(delay_s)
            except asyncio.CancelledError:
                return
            await self.end_call(reason)

        if self._end_task is not None and not self._end_task.done():
            self._end_task.cancel()
        self._end_task = asyncio.create_task(_end())

    async def end_call(self, reason: str):
        """End the call: the EndFrame flows downstream and the
        ConversationProcessor sends "call-ended" first."""
        self._session._end_reason = reason
        self._session.state = "ending"
        if self._worker is not None:
            try:
                await self._worker.queue_frame(EndFrame())
            except Exception:
                log.exception("queue EndFrame failed")

    async def owner_left(self):
        """The owner hung up mid-bridge: the AI takes the call back."""
        if self._worker is not None:
            try:
                await self._worker.queue_frame(
                    TTSSpeakFrame(
                        text="ওনার কল থেকে বেরিয়ে গেছেন। আমি আবার আছি, বলুন।"
                    )
                )
            except Exception:
                log.exception("owner_left speak failed")

    # ------------------------------------------------------------ run
    async def run(self):
        """Run the pipeline until the call ends, then finalise.

        Raises EngineNotStarted if the pipeline could not be built
        or started (main.py then falls back to the v2.1 handler).
        """
        session = self._session
        session.pipecat_mode = True
        session._pipecat_engine = self
        try:
            transport = self._build()
        except Exception:
            log.exception("pipecat pipeline build failed")
            raise EngineNotStarted("build failed")

        # The engine sends caller events through the pipeline, never
        # a raw socket send (the transport owns the websocket now).
        async def _notify(msg: dict):
            if self._worker is not None:
                await self._worker.queue_frame(
                    OutputTransportMessageFrame(message=msg)
                )

        session.notify_fn = _notify

        @transport.event_handler("on_client_connected")
        async def _on_client_connected(transport, client):
            # Greet as soon as the pipeline starts.
            if self._worker is not None:
                await self._worker.queue_frames([LLMRunFrame()])

        @transport.event_handler("on_client_disconnected")
        async def _on_client_disconnected(transport, client):
            log.info("pipecat client disconnected: %s", self._room_id)

        @self._worker.event_handler("on_pipeline_finished")
        async def _on_pipeline_finished(worker, frame):
            log.info("pipecat pipeline finished: %s", self._room_id)

        @self._worker.event_handler("on_pipeline_error")
        async def _on_pipeline_error(worker, frame):
            # Never leave the caller in silence: if the LLM (or any
            # keyed service) errors, say a scripted Bengali line.
            log.warning("pipecat pipeline error: %s", frame)
            try:
                reply, _src = await asyncio.to_thread(
                    ai.brain_reply, session.transcript, ""
                )
                if self._worker is not None:
                    await self._worker.queue_frame(TTSSpeakFrame(text=reply))
            except Exception:
                log.exception("pipeline error fallback failed")

        runner = WorkerRunner()
        try:
            await runner.add_workers(self._worker)
        except Exception:
            log.exception("pipecat worker add failed")
            raise EngineNotStarted("worker add failed")
        self._started = True
        try:
            await runner.run()
        finally:
            if self._transfer_wait_task is not None and not self._transfer_wait_task.done():
                self._transfer_wait_task.cancel()
            if self._end_task is not None and not self._end_task.done():
                self._end_task.cancel()
            session.notify_fn = None
            session._pipecat_engine = None
            await session.finalize()


async def run_call(websocket, session: CallSession, room_id: str):
    """Run one caller leg on the Pipecat pipeline (see CallEngine)."""
    engine = CallEngine(websocket, session, room_id)
    await engine.run()


async def owner_left(session: CallSession):
    """The owner hung up mid-bridge — the AI takes the call back.

    main.py calls this from the owner-disconnect handler when the
    call runs on the Pipecat engine (the raw session.speak path
    cannot be used: the transport owns the caller websocket).
    The engine is looked up from the session's notify path — the
    engine instance is stored on the session by run_call.
    """
    engine = getattr(session, "_pipecat_engine", None)
    if engine is not None:
        await engine.owner_left()
