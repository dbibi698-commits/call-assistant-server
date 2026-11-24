# Mith Hera Call Assistant — Server v3 (Pipecat engine)

Own free server. **No Plivo, no Twilio, no paid telephony provider.**
Calls come in over the internet (WebRTC page) straight to this server.
The server AI answers in Bengali, listens, replies, records both sides.
The user's phone stays silent — only a notification + a recording.

**Status: v3 built and locally verified. NOT deployed.** The live
server (https://mith-hera-call-server.onrender.com) still runs v2.1
until the user approves this version. Nothing was pushed to the live
repo.

**v3 change:** the caller leg now runs on the REAL Pipecat pipeline
(pipecat-ai 1.17.0, BSD-2) — the most popular open-source voice-agent
framework, and the engine Dograh itself is built on. Same wire
protocol as v2.1 (same /health + /calls + /notifications API shape,
same /call page, same mu-law 8 kHz WebSocket), so the v1.6.1 app
keeps working unchanged. What Pipecat adds: a real VAD (Silero) +
SmartTurn V3 turn-taking (Pipecat's default stop strategy) so the AI
stops cutting the caller off, barge-in (the caller can interrupt the
AI and the interrupted audio is cleared with `clearAudio`), a proper
LLM context aggregator, and a Plivo serializer that speaks exactly
the wire format this server already used. The v2.1 CallSession loop
stays in the code as the fallback if Pipecat cannot be imported.

**v3 behaviour changes vs v2.1 (honest):**
* The keyed LLM is a SINGLE model now — gemini-3.5-flash through
  Gemini's OpenAI-compatible endpoint. The 3-model fallback chain
  (3.5-flash → 3.5-flash-lite → flash-latest) still exists in the
  v2.1 fallback engine (app/ai.py), not in the Pipecat LLM service.
  If the single model errors, the pipeline says a scripted Bengali
  line instead of going silent.
* The keyed STT/TTS are the Pipecat Sarvam STREAMING services:
  STT saaras:v4 (bn-IN), TTS bulbul:v3, voice roopa (bn-IN).
  The env vars SARVAM_STT_MODEL / SARVAM_TTS_MODEL are honoured
  only when they name a streaming model (saaras:* / bulbul:v3 or
  bulbul:v4-flash); the v2.1 defaults (saarika:v2.5 / bulbul:v2)
  are HTTP models and are used only by the v2.1 fallback engine
  and the /voice/greeting test endpoint, not by the pipeline.
  GOOGLE_API_KEY (Google Cloud STT/TTS) is likewise only used by
  the v2.1 fallback engine — the Pipecat pipeline's keyed chain is
  Sarvam only, browser STT/TTS otherwise.
* With NO keys set, v3 works exactly like v2.1 did: browser STT
  (bn-IN) + scripted Bengali replies + browser TTS (bn-IN) — free
  forever, never English, never silent.

## Sotti kotha / Honest limit (please read first)

**Bengali:** Ei server-e **internet call** (WebRTC) ashe — browser theke
call korle AI answer korbe, kotha bolbe, recording save hobe. **Normal
mobile dialler theke call korle seta server-e ashte pare na**, karon
normal call-er jonno ekta phone number lage, ar kono code-i free-te
number banate pare na. Dialler call chaile pore ekta number (Plivo
trial ba onno provider) attach korte hobe.

**English:** this server answers internet (WebRTC) calls from a
browser. A normal mobile-dialler call cannot reach a server on its
own — it needs a phone number from a carrier/provider, and no code
can create one for free.

## What Dograh is, and what v2 took from it

Dograh (https://github.com/dograh-hq/dograh, 6,253 stars, BSD-2-Clause
licence, Copyright 2025 Zansat Technologies) is the best open-source
self-hosted Vapi/Retell alternative found in the research
(`~/workspace/research_notes/github-open-source-call-assistant-20261124-0635/report.md`).
It is a full voice-AI platform built on a Pipecat fork: visual
workflow builder, inbound + outbound calls, human handoff/transfer,
telephony via Twilio/Vonage/Telnyx/Plivo/Vobiz/Cloudonix/Asterisk ARI,
BYOK/local LLM, one Docker command.

**Dograh itself does NOT run here — only its patterns were used.**
Its `docker-compose.yaml` starts **9 services**: postgres (pgvector
pg17), redis, minio, dograh-init, nginx, coturn, api, ui, cloudflared.
Render's free tier is a **single web service with 512 MB RAM that
sleeps after 15 min idle** — it cannot run Postgres + Redis + MinIO +
a Python API + a Next.js UI + nginx + coturn. Dograh also needs a
phone number from a carrier and provider API keys to do anything.
So v2 reimplements Dograh's pipeline *patterns* inside this existing
lightweight FastAPI server (one process, stdlib audio, no database),
which already runs fine on Render free. No Dograh code was copied
into this repo; the patterns below were reimplemented from scratch.

### Patterns taken from Dograh (concrete, with file/function names)

1. **Turn-state machine** — Dograh's pipeline is a sequence of
   processors with explicit frames; v2 makes the same idea explicit
   in `app/call_session.py` (`CallSession.state`,
   `_set_state()`): every call is in exactly one state —
   `greeting → listening → thinking → speaking → listening` —
   and the server sends `{"event": "state", "state": ...}` so the
   caller page shows "AI shunche / AI bolche / AI bhabche".
2. **Turn-taking rule** — Dograh's voice-prompting guide
   (`api/services/voice_prompting_guide/topics/turn_taking.py`):
   *end every agent turn with a question or a clear nudge, or the
   call stalls in dead air.* Applied in `app/ai.py`: the Bengali
   system prompt (`SYSTEM_PROMPT_BN`, rule ৫) requires every reply
   to end with a question, and every scripted fallback line ends
   with an invitation ("আর কিছু বলবেন?"). Dograh's guide warns
   against enforcing this with a regex — v2 follows that and does
   it via the prompt only.
3. **Barge-in guard** — Dograh lets the UserAggregator own
   interruption frames (`should_interrupt=False` on the services in
   `api/services/pipecat/service_factory.py`), so a noise spike
   does not cut the agent off. v2 does the same in
   `app/call_session.py` (`on_caller_audio`, `BARGE_IN_CONFIRM_S
   = 0.15`): the AI stops speaking only after 0.15 s of sustained
   caller speech.
4. **Handoff / transfer** — Dograh transfers via a conference
   strategy (Plivo: `PlivoConferenceStrategy` in
   `api/services/telephony/providers/plivo/transport.py` +
   `strategies.py`; shared `call_transfer_manager`). v2 keeps its
   WebRTC bridge (caller ↔ owner audio forwarding) and adds the
   handoff discipline: naming the owner (Murshid/Sadhin) transfers
   at once; a vague "connect koro" first asks
   "আপনি কি ওনার-এর সাথে কথা বলতে চান?" and transfers only on yes
   (`DIRECT_TRANSFER_WORDS` / `AMBIGUOUS_TRANSFER_WORDS` /
   `_transfer_pending` in `app/call_session.py`).
5. **Recording router** — Dograh keeps one recording per call
   (`RecordingRouterProcessor` in
   `api/services/pipecat/agent_runtime_factory.py`). v2 adds the
   same: `audio.mix_mulaw()` + `store.save_recordings()` now write
   `conversation.wav` (both legs mixed into one file) next to
   `inbound.wav` / `outbound.wav`, served at
   `/recordings/<call-id>/conversation.wav`.
6. **Plivo transport shape** — Dograh's Plivo provider is a
   FastAPI WebSocket transport with an 8 kHz mu-law serializer and
   auth loaded per-organisation. v1 already had that shape
   (`/media-stream` + `/provider/plivo/*`); v2 keeps it unchanged
   and OFF by default.
7. **Sarvam Bengali voices** — Dograh's config
   (`api/services/configuration/options/sarvam.py`) lists
   `saarika:v2.5` / `saaras:v3` STT with `bn-IN` and Bulbul
   `bulbul:v3`/`v4-flash` TTS with Bengali voices
   (`roopa_bn_conversational`, `arnab_bn_conversation`). v2 wires
   the same provider as its Bengali-first STT/TTS (see below).

## Bengali-first: exactly what is wired

**Language: bn-IN everywhere.** The greeting, every reply, the
system prompt, the summary and the notifications are Bengali.

**v3 — the Pipecat pipeline chains** (what actually runs on a call;
`/health` → `pipecat` block reports them live):

| Layer | Keyed (needs key) | No key (free) |
|---|---|---|
| Brain | Gemini **gemini-3.5-flash** via the OpenAI-compatible endpoint, `system_instruction=SYSTEM_PROMPT_BN` (Bengali) | Scripted Bengali replies (`ScriptedLLMProcessor` → `ai.brain_reply`) |
| STT | **Sarvam Saarika streaming `saaras:v4`, bn-IN** (`SarvamSTTService`) | Caller browser STT (`webkitSpeechRecognition`, `lang=bn-IN`) injected as `TranscriptionFrame` by the serializer |
| TTS | **Sarvam Bulbul streaming `bulbul:v3`, voice `roopa`, bn-IN** (`SarvamTTSService`) | Caller browser speech synthesis (`lang=bn-IN`) via `{"event": "say"}` |
| Turn-taking | **Silero VAD + SmartTurn V3** (Pipecat's default stop strategy) + barge-in via `InterruptionFrame` + `clearAudio` | Same — turn-taking needs no key |

The table below is the **v2.1 fallback engine** (app/ai.py), which
still runs when Pipecat cannot be imported, and which also powers
the `/voice/greeting` test endpoint. Its chains are NOT the
pipeline's: Google Cloud STT/TTS and the 3-model Gemini chain exist
only there.

| Layer | Primary | Fallback | Needs a key? |
|---|---|---|---|
| Brain | Gemini, **system prompt in Bengali** (`SYSTEM_PROMPT_BN` in `app/ai.py`) — model must reply in natural Bengali, never English, end every turn with a question | Scripted Bengali replies (`SCRIPTED_ACK`) | Gemini: yes, `GEMINI_API_KEY`. Scripted: no |
| STT (caller speech → text) | **Sarvam Saarika** `saarika:v2.5`, `language_code=bn-IN` (`stt_sarvam` in `app/ai.py`) | Google Cloud STT `bn-IN` `phone_call` model → Gemini audio transcription → caller browser STT (`webkitSpeechRecognition`, `lang=bn-IN` in `static/call.html`) | Sarvam: `SARVAM_API_KEY`. Google: `GOOGLE_API_KEY`. Gemini: `GEMINI_API_KEY`. Browser: free, no key |
| TTS (reply → voice) | **Sarvam Bulbul** (`bulbul:v2`, speaker `roopa`; `bulbul:v4-flash` voices like `roopa_bn_conversational` available via env) (`tts_sarvam`) | Google Cloud TTS `bn-IN-Standard-A` → caller browser speech synthesis (`lang=bn-IN`, Bengali voice picked if present) | Sarvam: `SARVAM_API_KEY`. Google: `GOOGLE_API_KEY`. Browser: free, no key |

**With NO keys set** (how the live server runs today): the call
still works — the caller's browser does STT (bn-IN) and TTS (bn-IN)
and the brain answers with scripted Bengali lines. Nothing is ever
answered in English; the "didn't hear" line is only spoken when the
caller really was not heard (empty utterance), never as a rotation
filler. `/health` lists both chains with a `configured` flag per
step, so you can see what is actually available instead of guessing.

**Free vs paid:** the server code, the WebRTC path, browser STT/TTS
and scripted replies are free forever. Sarvam / Google Cloud /
Gemini usage is billed by those providers (their free tiers and
prices change — check their sites; nothing here hard-codes a price).
No key is ever baked into the code — all keys are env vars only.

## What v2 changed vs v1 (file list)

- `app/ai.py` — Bengali system prompt (`SYSTEM_PROMPT_BN`); STT
  chain Sarvam Saarika → Google bn-IN → Gemini audio; TTS chain
  Sarvam Bulbul → Google bn-IN; scripted fallback split so the
  "didn't hear" line is never used for a heard utterance;
  `status()` now reports language + both chains.
- `app/call_session.py` — turn-state machine (`state`,
  `_set_state()`); barge-in guard 0.15 s; transfer confirmation
  (direct vs ambiguous); goodbye detection with whole-token matching
  (so "সবাই" never ends a call and "কথা" never counts as "yes");
  `turn_state` + `language` saved on the record.
  **v2.1 — name before transfer:** `_set_caller_name()`,
  `_transfer_line()`, `_extract_name()` / `_looks_like_name()` /
  `_is_name_refusal()`; `start_transfer()` now asks for the name
  first when it is unknown and `_do_transfer()` transfers with the
  name in the line and the notification; the name is also asked
  once, early, right after the greeting; two refusals/silences still
  transfer, with the name "Naam dey ni".
- `app/audio.py` — `mix_mulaw()` for the combined recording.
- `app/store.py` — `conversation.wav` saved locally + uploaded to
  Supabase when configured; record carries
  `recording_conversation_url` + `language`. **v2.1:**
  `add_notification()` takes `caller_name`, so a transfer
  notification shows WHO is calling.
- `app/main.py` — `/health` reports `version:
  2.1-name-first-transfer`; `/recordings/<id>/conversation.wav` served.
- `app/config.py` — `SARVAM_STT_MODEL` (default `saarika:v2.5`),
  `SARVAM_TTS_MODEL` (default `bulbul:v2`),
  `SARVAM_STT_LANGUAGE` (default `bn-IN`). **v2.1:** `NAME_ASK_BN`,
  `NAME_BEFORE_TRANSFER_BN`, `NAME_RETRY_BN` (all Bengali, env-
  overridable).
- `static/call.html` — shows the turn state ("AI shunche / AI
  bolche...") from the server's `state` events. Browser STT/TTS
  were already `bn-IN` in v1 and are unchanged.
- `tests/smoke_test_v2.py` — new: Bengali greeting + Bengali reply
  asserted (Bengali script), ambiguous-transfer confirmation flow,
  direct transfer + owner-busy timeout, goodbye ending,
  conversation.wav, `/health` chains. **v2.1:** name-before-transfer
  (no name → ask → name → transfer with the name + notification
  carries it), transfer with a name already known (at once, no name
  question), name refused twice (still transfers, "Naam dey ni"),
  early name ask after the greeting.
- `tests/smoke_test.py` — updated only to skip v2 `state` events.
- `.env.example` — new Sarvam model vars documented.

Unchanged and still true: hard local mute on the user's phone
(notification + recording only); calls answered by the server even
when the user's phone has no internet; transfer only to Murshid /
Sadhin; recordings never deleted by the server; Plivo adapter OFF
by default.

## What v3 changed vs v2.1 (file list)

- `app/pipecat_engine.py` — NEW. The real Pipecat pipeline:
  `MithHeraSerializer` (Plivo wire format + `transcript` →
  `TranscriptionFrame`, `stop` → `EndFrame`, `auto_hang_up=False`);
  `BridgeProcessor` (records caller audio, forwards it to the owner
  while bridged, pumps owner audio downstream as
  `OutputAudioRawFrame`); `TapProcessor` (records AI + owner audio
  right before the output); `BrowserTTSProcessor` (no-key TTS →
  `{"event": "say"}`); `ScriptedLLMProcessor` (no-key LLM →
  `ai.brain_reply`, full-response frames); `ConversationProcessor`
  (greeting, name-before-transfer, transfer confirm, goodbye —
  decisions reuse the v2.1 helpers from `call_session.py`, so they
  cannot drift); `CallEngine` (one `WorkerRunner` per call,
  `idle_timeout_secs=None` — the AI never ends a call itself;
  transfer wait + end scheduling); `engine_info()` for `/health`.
- `app/call_session.py` — v3 hooks only: `owner_audio_queue`,
  `notify_fn` (events through the pipeline, never a raw send),
  `pipecat_mode` (owner audio routed to the queue), `_end_reason` /
  `_end_sent`, `_last_handled_text`, `_pipecat_engine`;
  `on_owner_audio()` routes to the queue in pipecat mode;
  `owner_joined()` cancels a running transfer wait. The v2.1 loop
  is otherwise untouched and stays the fallback.
- `app/main.py` — lazy Pipecat import (`PIPECAT_AVAILABLE`; the
  server still starts without pipecat); `/health` version
  `3.0-pipecat` + `pipecat` engine block; `_handle_ws_pipecat()`
  (caller leg → engine, `EngineNotStarted` → `_v21_caller_loop`
  fallback on the same socket); owner-left goes through the engine
  (`pipecat_engine.owner_left`) in pipecat mode.
- `requirements.txt` — `pipecat-ai[sarvam]==1.17.0` added.
  Measured: ~142 MB RSS with both ONNX models, ~135 MB for the
  running server process — fits Render free (512 MB RAM).
- `static/call.html` — `clearAudio` handling (stops scheduled AI
  audio + browser speech on barge-in).
- `tests/smoke_test_v3.py` — NEW: all v2 wire tests on the Pipecat
  pipeline + a bridged-audio test (owner audio → caller as
  `playAudio`, caller audio → owner as `playAudio`, both legs
  recorded with real audio in the WAVs) + `/health` pipecat block.
- `.gitignore` — `recordings-test-v3/` added.

## Run locally

```bash
cd call-assistant-server-v3
python3 -m venv .venv        # only if .venv is not already here
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
# then open http://127.0.0.1:8000/call
```

No keys needed to try it: the no-key path (browser STT/TTS +
scripted Bengali replies) works out of the box.

## Tests (all suites pass)

```bash
.venv/bin/python tests/smoke_test.py      # v1 suite, v2-compatible
.venv/bin/python tests/smoke_test_v2.py   # v2: Bengali, transfer, goodbye
.venv/bin/python tests/smoke_test_v3.py   # v3: same wire tests on the
                                          # Pipecat pipeline + bridged audio
```

## Deploy (ONLY after the user approves)

1. User approves this version in chat.
2. Copy the v3 files over the live repo
   (`dbibi698-commits/call-assistant-server`, branch `main`) —
   same Render Blueprint (`render.yaml`, free plan, unchanged).
3. Render redeploys automatically; check `/health` shows
   `version: 3.0-pipecat` and `pipecat.engine == "pipecat".
4. Env vars on Render stay as they are today (only `OWNER_PHONE`
   is set). To get smart Bengali replies, set `GEMINI_API_KEY` in
   the Render dashboard (never in chat, never in code). For server
   Bengali STT/TTS, set `SARVAM_API_KEY` (or `GOOGLE_API_KEY`).

Do NOT deploy on your own: the live server stays on v1 until the
user says so.
