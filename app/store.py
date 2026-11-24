"""Call records, notifications and recordings.

Everything is saved server-side, both sides of every call, and never
deleted by the server. Two layers:
  1. Always: local files under RECORDINGS_DIR/<call_id>/ + in-memory
     records (the app can read them via GET /calls).
  2. When Supabase is configured: the same record is inserted into the
     `call_records` table, the wav files are uploaded to the
     `call-recordings` storage bucket, and notifications go into
     `call_notifications` — so the app's CHATS history can play them.
"""

import json
import logging
import os
import threading
import time
import uuid

import requests

from . import audio
from .config import settings

log = logging.getLogger("mithhera.store")

_records: dict[str, dict] = {}
_notifications: list[dict] = []
_lock = threading.Lock()


def new_call_id() -> str:
    return uuid.uuid4().hex[:12]


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def create_record(call_id: str, from_number: str, from_name: str = "") -> dict:
    rec = {
        "call_id": call_id,
        "from_number": from_number or "Unknown",
        "from_name": from_name or "",
        "to_number": "Mith Hera AI (server)",
        "started_at": now_iso(),
        "ended_at": None,
        "duration_seconds": 0,
        "transcript": [],
        "summary": "",
        "recording_url": "",
        "recording_inbound_url": "",
        "recording_outbound_url": "",
        "recording_conversation_url": "",
        "language": "bn-IN",
        "transfer_requested": False,
        "transferred": False,
        "transfer_status": "",
    }
    with _lock:
        _records[call_id] = rec
    return rec


def get_record(call_id: str) -> dict | None:
    with _lock:
        return _records.get(call_id)


def list_records() -> list[dict]:
    with _lock:
        return sorted(_records.values(), key=lambda r: r["started_at"], reverse=True)


def add_notification(kind: str, title: str, call_id: str = "", body: str = "",
                     caller_name: str = "") -> dict:
    """A notification for the user's phone, e.g. 'Ei phone koreche'.
    v2.1: a transfer notification carries the caller name, so the
    phone alert can show WHO is calling."""
    note = {
        "id": uuid.uuid4().hex[:12],
        "kind": kind,  # 'call' | 'transfer'
        "title": title,
        "body": body,
        "call_id": call_id,
        "caller_name": caller_name,
        "created_at": now_iso(),
        "read": False,
    }
    with _lock:
        _notifications.append(note)
    _supabase_insert("call_notifications", note)
    return note


def list_notifications() -> list[dict]:
    with _lock:
        return sorted(_notifications, key=lambda n: n["created_at"], reverse=True)


def save_recordings(call_id: str, inbound_mulaw: bytes, outbound_mulaw: bytes) -> dict:
    """Save both legs + one combined conversation.wav locally
    (+ Supabase when configured).

    conversation.wav mixes caller + AI/owner into a single file, so
    the app can play the whole call with one player — the same idea
    as Dograh's recording router keeping one recording per call.
    Returns {'inbound': ..., 'outbound': ..., 'conversation': ...}.
    """
    base = os.path.join(settings.RECORDINGS_DIR, call_id)
    os.makedirs(base, exist_ok=True)
    inbound_wav = audio.mulaw_to_wav_bytes(inbound_mulaw)
    outbound_wav = audio.mulaw_to_wav_bytes(outbound_mulaw)
    conversation_wav = audio.mulaw_to_wav_bytes(
        audio.mix_mulaw(inbound_mulaw, outbound_mulaw)
    )
    in_path = os.path.join(base, "inbound.wav")
    out_path = os.path.join(base, "outbound.wav")
    conv_path = os.path.join(base, "conversation.wav")
    with open(in_path, "wb") as f:
        f.write(inbound_wav)
    with open(out_path, "wb") as f:
        f.write(outbound_wav)
    with open(conv_path, "wb") as f:
        f.write(conversation_wav)
    urls = {
        "inbound": f"/recordings/{call_id}/inbound.wav",
        "outbound": f"/recordings/{call_id}/outbound.wav",
        "conversation": f"/recordings/{call_id}/conversation.wav",
    }
    # Upload to Supabase Storage (best effort — local files always exist).
    if settings.SUPABASE_URL and settings.SUPABASE_SERVICE_KEY:
        for leg, data in (("inbound", inbound_wav), ("outbound", outbound_wav),
                          ("conversation", conversation_wav)):
            up = _supabase_upload(
                f"{call_id}/{leg}.wav", data, "audio/wav"
            )
            if up:
                urls[leg] = up
    rec = get_record(call_id)
    if rec:
        rec["recording_inbound_url"] = urls["inbound"]
        rec["recording_outbound_url"] = urls["outbound"]
        rec["recording_conversation_url"] = urls["conversation"]
        rec["recording_url"] = urls["conversation"]
    return urls


def finalize_record(call_id: str) -> dict | None:
    """Fill in end time / summary and push the record to Supabase."""
    rec = get_record(call_id)
    if not rec:
        return None
    rec["ended_at"] = now_iso()
    try:
        from datetime import datetime

        start = datetime.strptime(rec["started_at"], "%Y-%m-%dT%H:%M:%SZ")
        end = datetime.strptime(rec["ended_at"], "%Y-%m-%dT%H:%M:%SZ")
        rec["duration_seconds"] = max(0, int((end - start).total_seconds()))
    except Exception:
        pass
    _supabase_insert("call_records", _supabase_row(rec))
    return rec


# ------------------------------------------------------------ Supabase ----
def _supabase_insert(table: str, row: dict) -> bool:
    if not (settings.SUPABASE_URL and settings.SUPABASE_KEY):
        return False
    try:
        r = requests.post(
            f"{settings.SUPABASE_URL.rstrip('/')}/rest/v1/{table}",
            headers={
                "apikey": settings.SUPABASE_KEY,
                "Authorization": f"Bearer {settings.SUPABASE_KEY}",
                "Content-Type": "application/json",
                "Prefer": "return=minimal",
            },
            json=row,
            timeout=15,
        )
        if r.status_code not in (200, 201, 204):
            log.warning("Supabase insert %s -> %s %s", table, r.status_code, r.text[:200])
            return False
        return True
    except Exception as exc:
        log.warning("Supabase insert %s error: %s", table, exc)
        return False


def _supabase_upload(path: str, data: bytes, content_type: str) -> str | None:
    try:
        url = (
            f"{settings.SUPABASE_URL.rstrip('/')}/storage/v1/object/"
            f"{settings.SUPABASE_BUCKET}/{path}"
        )
        r = requests.post(
            url,
            headers={
                "apikey": settings.SUPABASE_SERVICE_KEY,
                "Authorization": f"Bearer {settings.SUPABASE_SERVICE_KEY}",
                "Content-Type": content_type,
                "x-upsert": "true",
            },
            data=data,
            timeout=30,
        )
        if r.status_code not in (200, 201):
            log.warning("Supabase upload %s -> %s %s", path, r.status_code, r.text[:200])
            return None
        return url
    except Exception as exc:
        log.warning("Supabase upload %s error: %s", path, exc)
        return None


def _supabase_row(rec: dict) -> dict:
    return {
        "call_uuid": rec["call_id"],
        "from_number": rec["from_number"],
        "to_number": rec["to_number"],
        "started_at": rec["started_at"],
        "ended_at": rec["ended_at"],
        "duration_seconds": rec["duration_seconds"],
        "transcript": json.dumps(rec["transcript"], ensure_ascii=False),
        "summary": rec["summary"],
        "recording_url": rec["recording_url"],
        "recording_inbound_url": rec["recording_inbound_url"],
        "recording_outbound_url": rec["recording_outbound_url"],
        "recording_conversation_url": rec.get("recording_conversation_url", ""),
        "language": rec.get("language", "bn-IN"),
        "transfer_requested": rec["transfer_requested"],
        "transferred": rec["transferred"],
    }


def supabase_configured() -> bool:
    return bool(settings.SUPABASE_URL and settings.SUPABASE_KEY)
