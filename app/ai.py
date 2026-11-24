"""Mith Hera Call Assistant — AI layer (v2, Dograh-pattern upgrade).

BENGALI FIRST — the whole layer is Bengali by default:
  * Brain: Gemini, system prompt written IN Bengali (not English).
    The model must always reply in natural, human-like Bengali,
    never switch to English, never cut the caller off, and end every
    turn with a question or a nudge so the caller knows it is their
    turn to speak (Dograh's turn-taking rule: a turn that ends with
    no handoff cue stalls the call — see Dograh
    api/services/voice_prompting_guide/topics/turn_taking.py).
  * STT chain, Bengali (bn-IN) first — Sarvam Saarika (India-native,
    Bengali-optimised; Dograh lists saarika:v2.5 / saaras:v3 with
    bn-IN in api/services/configuration/options/sarvam.py) ->
    Google Cloud Speech-to-Text bn-IN (phone_call model) ->
    Gemini audio transcription (prompted to transcribe in the
    language spoken, Bengali if Bengali). If none is configured,
    the WebRTC page does STT in the caller's own browser
    (webkitSpeechRecognition, lang bn-IN) and sends text.
  * TTS chain, Bengali (bn-IN) first — Sarvam Bulbul (Bengali voice,
    Dograh lists bulbul:v3/v4-flash with Bengali voices such as
    roopa_bn_conversational / arnab_bn_conversation) ->
    Google Cloud Text-to-Speech bn-IN. If neither is configured,
    the WebRTC page speaks the reply with the caller's own browser
    speech synthesis (lang bn-IN, free, no key).

No-key mode: if GEMINI_API_KEY is also missing, the brain answers
with short scripted Bengali replies (all in Bengali script, each
ending with an invitation to keep talking) so a call never dies
silently and never answers in English.

Every key comes from an environment variable only. NEVER baked in.
"""

import base64
import io
import json
import logging
import time
import wave as wave_mod

import requests

from . import audio
from .config import settings

log = logging.getLogger("mithhera.ai")

GEMINI_MODELS = ["gemini-3.5-flash", "gemini-3.5-flash-lite", "gemini-flash-latest"]
GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/models"

# System prompt — written IN BENGALI on purpose. The model must think
# and answer in Bengali, like a polite human on the phone.
SYSTEM_PROMPT_BN = (
    "তুমি মিঠ হেরা — মুর্শিদের কল অ্যাসিস্ট্যান্ট। তুমি ফোনে একজন "
    "কলারের সাথে কথা বলছো, মুর্শিদের হয়ে।\n"
    "নিয়ম:\n"
    "১. সবসময় স্বাভাবিক, মানুষের মতো বাংলায় উত্তর দাও। কখনো ইংরেজিতে "
    "বলবে না — কলার ইংরেজিতে বললেও তুমি বাংলায় উত্তর দেবে।\n"
    "২. ১-২টা ছোট বাক্যে উত্তর দাও, ফোনে কথা বলার মতো করে।\n"
    "৩. কলার যা বলেছে শুধু সেটারই উত্তর দাও — নিজে থেকে তথ্য বানাবে না, "
    "কলার যা বলেনি সেটা নিয়ে কথা বলবে না।\n"
    "৪. কলার কথা শেষ না হওয়া পর্যন্ত কখনো মাঝখানে থামাবে না, আর নিজে "
    "থেকে কল কাটবে না।\n"
    "৫. প্রতিটি উত্তরের শেষে একটা প্রশ্ন বা ইঙ্গিত দাও (যেমন: 'আর কিছু "
    "বলবেন?', 'বলুন, শুনছি') — যাতে কলার বুঝতে পারে এখন তার বলার পালা। "
    "প্রশ্ন ছাড়া উত্তর শেষ করবে না।\n"
    "৬. কলার যদি ওনার / মুর্শিদ / সাধিনকে চায়, তাহলে বলো তুমি কানেক্ট "
    "করছো।\n"
    "৭. কিছু বুঝতে না পারলে বলো: 'আমি ঠিক শুনতে পারিনি, আরেকবার বলুন।'"
)

# Short scripted replies used ONLY when no Gemini key is configured,
# so a call still works instead of dying silently. All Bengali, and
# every line ends with an invitation/question (Dograh turn-taking rule).
# The "didn't hear" line is NOT in this rotation — it is only spoken
# when the caller really was not heard (empty utterance), so a caller
# who spoke clearly never gets told "I didn't hear you".
SCRIPTED_ACK = [
    "আমি মিঠ হেরা, মুর্শিদের কল অ্যাসিস্ট্যান্ট। আপনি কী বলতে চান, বলুন?",
    "বুঝতে পেরেছি। আপনার কথাটা আমি মুর্শিদকে জানিয়ে দেবো। আর কিছু বলবেন?",
    "ঠিক আছে, আমি শুনছি। আপনি আর কী বলতে চান, বলুন?",
    "আপনার কথাটা নোট করে রাখছি। আর কিছু জানাতে চান?",
]
SCRIPTED_FALLBACK = SCRIPTED_ACK


def _gemini_generate(prompt: str) -> str | None:
    """Try each model in the chain; return the first reply, else None."""
    key = settings.GEMINI_API_KEY
    if not key:
        return None
    payload = {
        "system_instruction": {"parts": [{"text": SYSTEM_PROMPT_BN}]},
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.7, "maxOutputTokens": 200},
    }
    for model in GEMINI_MODELS:
        try:
            r = requests.post(
                f"{GEMINI_BASE}/{model}:generateContent",
                params={"key": key},
                json=payload,
                timeout=15,
            )
            if r.status_code != 200:
                log.warning("Gemini %s -> HTTP %s", model, r.status_code)
                continue
            data = r.json()
            parts = data.get("candidates", [{}])[0].get("content", {}).get("parts", [])
            text = "".join(p.get("text", "") for p in parts).strip()
            if text:
                return text
        except Exception as exc:  # network / timeout — try next model
            log.warning("Gemini %s error: %s", model, exc)
    return None


def brain_reply(transcript: list[dict], user_text: str) -> tuple[str, str]:
    """Reply to the caller. Returns (reply_text, source).

    source is 'gemini' or 'scripted' (no-key fallback). The reply is
    always Bengali — the system prompt is Bengali and the scripted
    fallback lines are Bengali.
    """
    history = "\n".join(
        f"{t['role']}: {t['text']}" for t in transcript[-10:]
    )
    prompt = (
        f"এখন পর্যন্ত কল:\n{history}\n\nকলার এইমাত্র বললো: {user_text}\n"
        "বাংলায় উত্তর দাও, ১-২টা ছোট বাক্যে, শেষে একটা প্রশ্ন দিয়ে।"
    )
    reply = _gemini_generate(prompt)
    if reply:
        return reply, "gemini"
    # No key / all models failed — scripted Bengali acknowledgement,
    # never silent, never the "didn't hear" line for a heard utterance.
    idx = len(transcript) % len(SCRIPTED_ACK)
    return SCRIPTED_ACK[idx], "scripted"


def summarise_call(transcript: list[dict]) -> str:
    """One-line Bengali summary of the call for CHATS history."""
    if not transcript:
        return "কোনো কথা হয়নি।"
    if not settings.GEMINI_API_KEY:
        first = transcript[0]["text"][:80]
        return f"কলার বলেছে: {first}"
    prompt = (
        "এই ফোন কলের এক লাইনের বাংলা সারাংশ লেখো। কলার কী চেয়েছিল "
        "সেটা বলো।\n\n"
        + "\n".join(f"{t['role']}: {t['text']}" for t in transcript)
    )
    out = _gemini_generate(prompt)
    return out.strip() if out else "কল হয়েছে, সারাংশ পাওয়া যায়নি।"


# ---------------------------------------------------------------- STT ----
# Chain order is Bengali-first: Sarvam Saarika (bn-IN, India-native)
# -> Google Cloud STT (bn-IN, phone_call model) -> Gemini audio.
# Each step returns None when its key is missing or it errors, so the
# next one is tried. "" means "audio heard, but no speech in it".
def stt_sarvam(mulaw: bytes) -> str | None:
    """Sarvam AI Saarika STT, Bengali (bn-IN) first.

    POST https://api.sarvam.ai/speech-to-text-transcribe, multipart
    file upload (WAV), model saarika:v2.5 (or saaras:v3), language
    bn-IN. Response JSON carries "transcript". Any error or an
    unexpected shape returns None so the chain falls through.
    """
    key = settings.SARVAM_API_KEY
    if not key:
        return None
    try:
        wav = audio.mulaw_to_wav_bytes(mulaw)
        files = {"file": ("audio.wav", wav, "audio/wav")}
        data = {
            "model": settings.SARVAM_STT_MODEL,
            "language_code": settings.SARVAM_STT_LANGUAGE,
        }
        r = requests.post(
            "https://api.sarvam.ai/speech-to-text-transcribe",
            headers={"api-subscription-key": key},
            files=files,
            data=data,
            timeout=25,
        )
        if r.status_code != 200:
            log.warning("Sarvam STT HTTP %s: %s", r.status_code, r.text[:200])
            return None
        transcript = (r.json().get("transcript") or "").strip()
        return transcript
    except Exception as exc:
        log.warning("Sarvam STT error: %s", exc)
        return None


def stt_google_cloud(mulaw: bytes) -> str | None:
    """Google Cloud Speech-to-Text, mu-law 8 kHz, Bengali first."""
    key = settings.GOOGLE_API_KEY
    if not key:
        return None
    try:
        r = requests.post(
            "https://speech.googleapis.com/v1/speech:recognize",
            params={"key": key},
            json={
                "config": {
                    "encoding": "MULAW",
                    "sampleRateHertz": 8000,
                    "languageCode": "bn-IN",
                    "alternativeLanguageCodes": ["en-IN", "hi-IN"],
                    "model": "phone_call",
                    "enableAutomaticPunctuation": True,
                },
                "audio": {"content": base64.b64encode(mulaw).decode()},
            },
            timeout=20,
        )
        if r.status_code != 200:
            log.warning("Cloud STT HTTP %s: %s", r.status_code, r.text[:200])
            return None
        results = r.json().get("results", [])
        if not results:
            return ""
        return results[0].get("alternatives", [{}])[0].get("transcript", "").strip()
    except Exception as exc:
        log.warning("Cloud STT error: %s", exc)
        return None


def stt_gemini_audio(mulaw: bytes) -> str | None:
    """Gemini audio transcription fallback (WAV PCM16 8 kHz)."""
    key = settings.GEMINI_API_KEY
    if not key:
        return None
    try:
        wav_b64 = base64.b64encode(audio.mulaw_to_wav_bytes(mulaw)).decode()
        payload = {
            "contents": [
                {
                    "parts": [
                        {
                            "inline_data": {
                                "mime_type": "audio/wav",
                                "data": wav_b64,
                            }
                        },
                        {"text": "এই ফোন কলের অডিওটি হুবহু লিখে দাও। "
                         "শুধু ট্রান্সক্রিপশন দাও, যে ভাষায় বলা হয়েছে "
                         "সেই ভাষায় (বাংলা হলে বাংলায়)। কোনো কথা না "
                         "থাকলে কিছুই লিখো না।"},
                    ]
                }
            ]
        }
        for model in GEMINI_MODELS:
            r = requests.post(
                f"{GEMINI_BASE}/{model}:generateContent",
                params={"key": key},
                json=payload,
                timeout=20,
            )
            if r.status_code != 200:
                continue
            parts = r.json().get("candidates", [{}])[0].get("content", {}).get("parts", [])
            text = "".join(p.get("text", "") for p in parts).strip()
            if text:
                return text
            return ""
        return None
    except Exception as exc:
        log.warning("Gemini STT error: %s", exc)
        return None


def transcribe(mulaw: bytes) -> str | None:
    """STT with fallback (Bengali-first chain). None = no STT configured;
    '' = no speech heard."""
    out = stt_sarvam(mulaw)
    if out is not None:
        return out
    out = stt_google_cloud(mulaw)
    if out is not None:
        return out
    out = stt_gemini_audio(mulaw)
    if out is not None:
        return out
    return None


# ---------------------------------------------------------------- TTS ----
# Chain order is Bengali-first: Sarvam Bulbul (Bengali-native voice)
# -> Google Cloud TTS (bn-IN). Both return mu-law 8 kHz bytes.
def tts_sarvam(text: str) -> bytes | None:
    """Sarvam AI Bulbul TTS (Bengali voice, 8 kHz wav -> mu-law)."""
    key = settings.SARVAM_API_KEY
    if not key:
        return None
    try:
        r = requests.post(
            "https://api.sarvam.ai/text-to-speech",
            headers={"api-subscription-key": key},
            json={
                "inputs": [text],
                "target_language_code": "bn-IN",
                "speaker": settings.SARVAM_SPEAKER,
                "pitch": 0,
                "pace": 1.0,
                "loudness": 1.0,
                "speech_sample_rate": 8000,
                "enable_preprocessing": True,
                "model": settings.SARVAM_TTS_MODEL,
                "output_audio_codec": "wav",
            },
            timeout=25,
        )
        if r.status_code != 200:
            log.warning("Sarvam TTS HTTP %s: %s", r.status_code, r.text[:200])
            return None
        raw = base64.b64decode(r.json()["audios"][0])
        with wave_mod.open(io.BytesIO(raw), "rb") as w:
            rate = w.getframerate()
            pcm = w.readframes(w.getnframes())
        if rate != 8000:
            pcm = audio.resample_pcm16_linear(pcm, rate, 8000)
        return audio.pcm16_to_mulaw(pcm)
    except Exception as exc:
        log.warning("Sarvam TTS error: %s", exc)
        return None


def tts_google_cloud(text: str) -> bytes | None:
    """Google Cloud TTS, Bengali voice -> mu-law 8 kHz bytes (or None)."""
    key = settings.GOOGLE_API_KEY
    if not key:
        return None
    try:
        r = requests.post(
            "https://texttospeech.googleapis.com/v1/text:synthesize",
            params={"key": key},
            json={
                "input": {"text": text},
                "voice": {"languageCode": "bn-IN", "name": "bn-IN-Standard-A"},
                "audioConfig": {
                    "audioEncoding": "LINEAR16",
                    "sampleRateHertz": 8000,
                },
            },
            timeout=20,
        )
        if r.status_code != 200:
            log.warning("Cloud TTS HTTP %s: %s", r.status_code, r.text[:200])
            return None
        pcm = base64.b64decode(r.json()["audioContent"])
        return audio.pcm16_to_mulaw(pcm)
    except Exception as exc:
        log.warning("Cloud TTS error: %s", exc)
        return None


def synthesise(text: str) -> bytes | None:
    """TTS with fallback (Bengali-first chain). None = no TTS configured
    (the caller's browser speaks it with bn-IN speech synthesis)."""
    out = tts_sarvam(text)
    if out:
        return out
    out = tts_google_cloud(text)
    if out:
        return out
    return None


def tts_available() -> bool:
    return bool(settings.SARVAM_API_KEY or settings.GOOGLE_API_KEY)


def stt_available() -> bool:
    return bool(
        settings.SARVAM_API_KEY or settings.GOOGLE_API_KEY or settings.GEMINI_API_KEY
    )


def status() -> dict:
    """What the server can actually do, for /health. Chains are listed
    in the order they are tried; a step is 'configured' only when its
    key is set — no key, no claim."""
    return {
        "language": "bn-IN",
        "gemini": bool(settings.GEMINI_API_KEY),
        "google_stt_tts": bool(settings.GOOGLE_API_KEY),
        "sarvam_tts": bool(settings.SARVAM_API_KEY),
        "sarvam_stt": bool(settings.SARVAM_API_KEY),
        "stt_available": stt_available(),
        "tts_available": tts_available(),
        "stt_chain": [
            {"provider": "sarvam-saarika", "language": "bn-IN",
             "model": settings.SARVAM_STT_MODEL,
             "configured": bool(settings.SARVAM_API_KEY)},
            {"provider": "google-cloud-stt", "language": "bn-IN",
             "model": "phone_call",
             "configured": bool(settings.GOOGLE_API_KEY)},
            {"provider": "gemini-audio", "language": "auto (Bengali if spoken)",
             "model": GEMINI_MODELS[0],
             "configured": bool(settings.GEMINI_API_KEY)},
            {"provider": "browser-stt", "language": "bn-IN",
             "model": "webkitSpeechRecognition",
             "configured": True},
        ],
        "tts_chain": [
            {"provider": "sarvam-bulbul", "language": "bn-IN",
             "model": settings.SARVAM_TTS_MODEL,
             "speaker": settings.SARVAM_SPEAKER,
             "configured": bool(settings.SARVAM_API_KEY)},
            {"provider": "google-cloud-tts", "language": "bn-IN",
             "model": "bn-IN-Standard-A",
             "configured": bool(settings.GOOGLE_API_KEY)},
            {"provider": "browser-tts", "language": "bn-IN",
             "model": "speechSynthesis",
             "configured": True},
        ],
    }
