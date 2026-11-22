"""Mith Hera Call Assistant — AI layer.

Brain: Gemini, model chain with automatic fallback (never a 404/400
shown to the caller):
    gemini-3.5-flash -> gemini-3.5-flash-lite -> gemini-flash-latest
Key comes from the GEMINI_API_KEY env var only. NEVER baked in.

STT (speech to text, Bengali first):
    1. Google Cloud Speech-to-Text REST (GOOGLE_API_KEY) — mu-law 8 kHz.
    2. Fallback: Gemini audio transcription (WAV PCM16 8 kHz).
    3. If neither is configured, the WebRTC page does STT in the
       caller's own browser (webkitSpeechRecognition) and sends text.

TTS (text to speech, Bengali voice):
    1. Google Cloud Text-to-Speech REST (GOOGLE_API_KEY) — bn-IN voice,
       LINEAR16 8 kHz, converted to mu-law for the phone stream.
    2. Fallback: Sarvam AI Bulbul (SARVAM_API_KEY) — Bengali voice.
    3. If neither is configured, the WebRTC page speaks the reply with
       the caller's own browser speech synthesis (free, no key).

No-key mode: if GEMINI_API_KEY is also missing, the brain answers with
short scripted Bengali replies (clearly marked as fallback) so a call
never dies silently.
"""

import base64
import json
import logging
import time

import requests

from . import audio
from .config import settings

log = logging.getLogger("mithhera.ai")

GEMINI_MODELS = ["gemini-3.5-flash", "gemini-3.5-flash-lite", "gemini-flash-latest"]
GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
SYSTEM_PROMPT = (
    "You are Mith Hera, a Bengali call assistant answering a phone call "
    "for your owner Murshid. The caller is speaking to you on the phone. "
    "Reply in Bengali (romanized Bengali is fine if the caller uses it), "
    "in 1-2 short sentences, like a polite human on the phone. "
    "Answer ONLY what the caller actually said — never invent facts, "
    "never talk about things the caller did not mention. "
    "If the caller asks for the owner, Murshid or Sadhin, say you will "
    "connect them. Never end the call yourself. "
    "If you do not understand, say 'Ami thik shunte parini, arekbar bolun.'"
)

# Short scripted replies used ONLY when no Gemini key is configured,
# so a call still works instead of dying silently.
SCRIPTED_FALLBACK = [
    "Ami Mith Hera, Murshid-er call assistant. Apni ki bolte chan, bolun.",
    "Bujhte perechi. Apnar kotha ta ami Murshid ke janiye debo.",
    "Ami thik shunte parini, arekbar bolun.",
]


def _gemini_generate(prompt: str) -> str | None:
    """Try each model in the chain; return the first reply, else None."""
    key = settings.GEMINI_API_KEY
    if not key:
        return None
    payload = {
        "system_instruction": {"parts": [{"text": SYSTEM_PROMPT}]},
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

    source is 'gemini' or 'scripted' (no-key fallback).
    """
    history = "\n".join(
        f"{t['role']}: {t['text']}" for t in transcript[-10:]
    )
    prompt = (
        f"Call so far:\n{history}\n\nCaller just said: {user_text}\n"
        "Reply in Bengali, 1-2 short sentences."
    )
    reply = _gemini_generate(prompt)
    if reply:
        return reply, "gemini"
    # No key / all models failed — scripted fallback, never silent.
    idx = len(transcript) % len(SCRIPTED_FALLBACK)
    return SCRIPTED_FALLBACK[idx], "scripted"


def summarise_call(transcript: list[dict]) -> str:
    """One-line Bengali summary of the call for CHATS history."""
    if not transcript:
        return "Kono kotha hoyni."
    if not settings.GEMINI_API_KEY:
        first = transcript[0]["text"][:80]
        return f"Caller boleche: {first}"
    prompt = (
        "Summarise this phone call in ONE short Bengali sentence. "
        "Say what the caller wanted.\n\n"
        + "\n".join(f"{t['role']}: {t['text']}" for t in transcript)
    )
    out = _gemini_generate(prompt)
    return out.strip() if out else "Call hoyeche, summary paoa jayni."


# ---------------------------------------------------------------- STT ----
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
                        {"text": "Transcribe this phone call audio exactly. "
                         "Reply with ONLY the transcription, in the language "
                         "spoken (Bengali if Bengali). If there is no speech, "
                         "reply with nothing."},
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
    """STT with fallback. None = no STT configured; '' = no speech heard."""
    out = stt_google_cloud(mulaw)
    if out is not None:
        return out
    out = stt_gemini_audio(mulaw)
    if out is not None:
        return out
    return None


# ---------------------------------------------------------------- TTS ----
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


def tts_sarvam(text: str) -> bytes | None:
    """Sarvam AI Bulbul TTS fallback (Bengali voice, 8 kHz wav)."""
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
                "model": "bulbul:v2",
                "output_audio_codec": "wav",
            },
            timeout=25,
        )
        if r.status_code != 200:
            log.warning("Sarvam TTS HTTP %s: %s", r.status_code, r.text[:200])
            return None
        import io
        import wave as wave_mod

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


def synthesise(text: str) -> bytes | None:
    """TTS with fallback. None = no TTS configured (browser speaks)."""
    out = tts_google_cloud(text)
    if out:
        return out
    out = tts_sarvam(text)
    if out:
        return out
    return None


def tts_available() -> bool:
    return bool(settings.GOOGLE_API_KEY or settings.SARVAM_API_KEY)


def stt_available() -> bool:
    return bool(settings.GOOGLE_API_KEY or settings.GEMINI_API_KEY)


def status() -> dict:
    return {
        "gemini": bool(settings.GEMINI_API_KEY),
        "google_stt_tts": bool(settings.GOOGLE_API_KEY),
        "sarvam_tts": bool(settings.SARVAM_API_KEY),
        "stt_available": stt_available(),
        "tts_available": tts_available(),
    }
