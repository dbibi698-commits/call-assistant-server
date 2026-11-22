# Mith Hera Call Assistant — Server

Own free server. **No Plivo, no Twilio, no paid telephony provider.**
Calls come in over the internet (WebRTC page) straight to this server.
The server AI answers in Bengali, listens, replies, records both sides.

## Sotti kotha / Honest limit (please read first)

**Bengali:** Ei server-e **internet call** (WebRTC) ashe — browser theke call
korle AI answer korbe, kotha bolbe, recording save hobe. **Normal mobile
dialler theke call korle seta server-e ashte pare na**, karon normal call-er
jonno ekta phone number lage, ar kono code-i free-te number banate pare na.
Dialler call chaile pore ekta number attach korte hobe (jemon Plivo trial
number) — tokhon `ENABLE_PLIVO=true` kore provider adapter on korte hobe.

**English:** This server answers **internet (WebRTC) calls** from a browser.
A normal mobile-dialler call cannot reach a server on its own — a normal
call needs a phone number from a carrier/provider, and no code can create
one for free. Dialler calls need a number attached later.

## What works free, right now

1. **Caller** opens the call link (`/call/<id>`), taps **Call koro**, talks.
   The server AI answers immediately:
   > "হ্যালো, আমি মুর্শিদের অ্যাসিস্ট্যান্ট, বলো কী হলো?"
   then listens first, replies in Bengali to what the caller actually said,
   never hangs up by itself.
2. **User's phone hears NOTHING.** No speaker, no mic, no dialer, no live
   audio on his phone — the call never touches his phone at all.
   He only gets a notification: **"Ei phone koreche — \<number\>"** with
   time + summary (see `GET /notifications`, stored in Supabase too).
3. **Recording of EVERY call is saved safely, server-side, both sides**
   (`recordings/<call-id>/inbound.wav` + `outbound.wav`), uploaded to the
   Supabase `call-recordings` bucket when configured, never deleted,
   playable later from CHATS call history with transcript + summary
   (`GET /calls`, `GET /calls/<id>`).
4. **Transfer — the ONLY connection to the user's phone.** Caller says
   "owner ke dao / Murshid ke dao / Sadhin ke dao / connect koro" → AI says
   "এক মিনিট, কানেক্ট করছি।", a notification + join link is created, and
   the owner opens `/join/<call-id>` and joins the SAME call — AI steps
   aside, audio is bridged caller ↔ owner. If the owner does not join in
   30 seconds, the AI says the owner is busy and ends politely.
   No other connection to his phone exists — no live listening, no live
   audio, nothing.

## Architecture

```
Caller browser (/call) ──WebSocket──> FastAPI server ──> Gemini 3.5-flash chain
   mu-law 8 kHz audio      /ws/call/<id>      (brain, env key only)
   (or browser STT/TTS     listen-first loop  Google Cloud STT/TTS (env key)
    fallback, no key)      greeting + replies Sarvam Bulbul TTS (fallback)
                                              Supabase: call_records +
                                              call-recordings bucket
Owner browser (/join) ──WebSocket──> same room, bridged on transfer
```

* Brain: Gemini chain `gemini-3.5-flash → gemini-3.5-flash-lite →
  gemini-flash-latest`, key from `GEMINI_API_KEY` only, never baked in.
  No key → short scripted Bengali replies so a call never dies silently.
* STT: Google Cloud Speech-to-Text (Bengali, phone_call model) → Gemini
  audio transcription fallback → caller browser's `webkitSpeechRecognition`
  fallback (free, no key).
* TTS: Google Cloud TTS (`bn-IN-Standard-A`, 8 kHz → mu-law) → Sarvam
  Bulbul fallback → caller browser's `speechSynthesis` fallback (free).
* Audio format everywhere: mu-law 8 kHz (standard telephony).

## Deploy (Render free tier)

1. Push this folder to a GitHub repo (or use Render's "New Web Service"
   with this directory). Or use the Blueprint: `render.yaml` is included.
2. In Render, set the environment variables (see `.env.example`):
   `GEMINI_API_KEY` (required for smart replies),
   `GOOGLE_API_KEY` (Speech + TTS; without it the browser fallbacks run),
   `SUPABASE_URL`, `SUPABASE_KEY`, `SUPABASE_SERVICE_KEY` (recordings),
   `OWNER_PHONE` (transfer label/target, E.164, e.g. +91...),
   `BASE_URL` (the Render URL, e.g. `https://mith-hera-call-server.onrender.com`).
3. Run `schema.sql` once in the Supabase SQL editor (creates
   `call_records`, `call_notifications`, `call-recordings` bucket).
4. Deploy. `GET /health` must return 200. Render free sleeps after
   15 min idle — keep it awake with a 10-minute ping to `/health`
   (same pattern as the Nano Reels keep-alive cron).

## Try a free call (no number, no money)

1. `POST /api/rooms` → copy `call_url`.
2. Open `call_url` in a browser (Chrome on Android works), allow mic,
   tap **Call koro**. The AI greets in Bengali and talks with you.
3. Say "Murshid ke dao" → the AI says "Ek minute, connect korchhi" and a
   transfer notification appears at `GET /notifications`.
4. Open the `join_url` in another browser tab → you join as the owner,
   the AI steps aside, you talk to the caller directly.
5. End the call → `GET /calls` shows the record with transcript +
   summary, and `/recordings/<id>/inbound.wav` plays the caller's side.

## Normal dialler calls (later, optional, NOT free)

If a real mobile number is ever wanted: rent an Indian number from Plivo
(KYC: Udyam/GST/COI, INR account — business only), set `ENABLE_PLIVO=true`
+ `PLIVO_AUTH_ID` / `PLIVO_AUTH_TOKEN` / `PLIVO_NUMBER`, point the Plivo
application's Answer URL at `https://<server>/provider/plivo/voice/answer`,
and on the owner's phone dial unconditional call forwarding once:
`**21*<Plivo-number>#` (disable with `##21#`). Then dialler calls reach
the Plivo number → this server → AI. The provider adapter is a thin
layer; the AI engine is unchanged. No forwarding, no number = no dialler
calls — that is a carrier fact, not a code gap.

## Endpoints

| Endpoint | What it does |
|---|---|
| `GET /` | Landing page + honest limit |
| `GET /health` | 200 + which integrations are configured |
| `GET /call`, `GET /call/<id>` | Caller WebRTC page (Bengali, no login) |
| `GET /join/<id>` | Owner join page (transfer) |
| `POST /api/rooms` | Create a room → `call_url` + `join_url` |
| `WS /ws/call/<id>?role=caller\|owner` | Live call audio (mu-law JSON protocol) |
| `WS /media-stream` | Same engine, Plivo-compatible path |
| `GET /calls`, `GET /calls/<id>` | Call history (transcript + summary) |
| `GET /notifications` | "Ei phone koreche" + transfer notifications |
| `GET /recordings/<id>/inbound.wav` | Caller's side of a call |
| `GET /recordings/<id>/outbound.wav` | AI + owner side of a call |
| `GET /voice/greeting` | Greeting as audio (TTS test, 503 if no key) |
| `POST /provider/plivo/voice/*` | Optional Plivo adapter (OFF by default) |

## Local run

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
# open http://localhost:8000/  — /health must return 200
python tests/smoke_test.py   # starts the server, checks endpoints + WS
```

## Files

- `app/main.py` — FastAPI app, pages, WS, optional Plivo adapter
- `app/call_session.py` — conversation loop, transfer, bridging, recording
- `app/ai.py` — Gemini chain, STT, TTS (with fallbacks)
- `app/audio.py` — mu-law encode/decode, WAV
- `app/store.py` — records, notifications, Supabase
- `app/config.py` — env-only config, no secrets in code
- `static/call.html` — WebRTC call page (caller + owner)
- `schema.sql`, `render.yaml`, `.env.example`, `requirements.txt`
