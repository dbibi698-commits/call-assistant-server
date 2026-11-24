"""Configuration for the Mith Hera Call Assistant server.

OWN FREE SERVER — no Plivo, no Twilio, no paid telephony provider.
Calls come in over the internet (WebRTC page) straight to this server.
NOTHING of the user's phone is used: no speaker, no mic, no dialer,
no call audio on his phone. The ONLY connection to his phone is a
transfer, and only when the caller asks for the owner — then the owner
joins the SAME call from a link (free, over the internet).

Every secret comes from an environment variable. NOTHING is hard-coded:
no API keys, no phone numbers, no tokens. If a value is missing the
server still starts, but the matching feature reports itself as
unavailable on /health instead of crashing.
"""

import os


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


class Settings:
    # Public base URL of this server, e.g.
    # https://mith-hera-call-server.onrender.com
    # Used to build join links and callback URLs. If unset, links are
    # built from the incoming request Host header.
    BASE_URL: str = _env("BASE_URL")

    # --- Brain (Gemini) — API key only, model chain lives in ai.py. ---
    GEMINI_API_KEY: str = _env("GEMINI_API_KEY")

    # Google Cloud API key (Speech-to-Text + Text-to-Speech APIs enabled).
    # An AI Studio key does NOT work for these; it must be a Google Cloud
    # console key. If unset, the WebRTC page falls back to the caller's
    # own browser speech recognition + speech synthesis (free, no key).
    GOOGLE_API_KEY: str = _env("GOOGLE_API_KEY")

    # Optional fallback TTS: Sarvam AI Bulbul (Bengali). Used only when
    # Google Cloud TTS is not configured or returns an error.
    SARVAM_API_KEY: str = _env("SARVAM_API_KEY")
    SARVAM_SPEAKER: str = _env("SARVAM_SPEAKER", "roopa")
    # Sarvam models (Dograh lists bulbul:v3/v4-flash + saarika:v2.5 /
    # saaras:v3; v2 defaults kept because they match the Sarvam REST
    # API shape this server uses — override by env if needed).
    SARVAM_TTS_MODEL: str = _env("SARVAM_TTS_MODEL", "bulbul:v2")
    SARVAM_STT_MODEL: str = _env("SARVAM_STT_MODEL", "saarika:v2.5")
    SARVAM_STT_LANGUAGE: str = _env("SARVAM_STT_LANGUAGE", "bn-IN")

    # --- Supabase — recording + transcript storage for the app's CHATS. ---
    SUPABASE_URL: str = _env("SUPABASE_URL")
    SUPABASE_KEY: str = _env("SUPABASE_KEY")
    SUPABASE_SERVICE_KEY: str = _env("SUPABASE_SERVICE_KEY") or _env("SUPABASE_KEY")
    SUPABASE_BUCKET: str = _env("SUPABASE_BUCKET", "call-recordings")

    # Owner's own mobile number, E.164. Used ONLY as the transfer target
    # label / PSTN dial target if a provider is ever attached. NEVER
    # hard-coded, and never called unless the caller asks for the owner.
    OWNER_PHONE: str = _env("OWNER_PHONE")

    # How long the AI waits for the owner to join after a transfer
    # request before telling the caller the owner is busy (seconds).
    TRANSFER_WAIT_SECONDS: int = int(_env("TRANSFER_WAIT_SECONDS", "30") or 30)

    # Where call recordings are saved on this server (also uploaded to
    # Supabase when configured). Never deleted by the server.
    RECORDINGS_DIR: str = _env("RECORDINGS_DIR", "recordings")

    # --- OPTIONAL provider adapter (OFF by default) ----------------------
    # A Plivo/Twilio-compatible webhook shape, for later, IF the user
    # ever chooses to rent a number. The server runs and demonstrates
    # the full AI call flow WITHOUT it, via the WebRTC page.
    ENABLE_PLIVO: bool = _env("ENABLE_PLIVO", "false").lower() == "true"
    PLIVO_AUTH_ID: str = _env("PLIVO_AUTH_ID")
    PLIVO_AUTH_TOKEN: str = _env("PLIVO_AUTH_TOKEN")
    PLIVO_NUMBER: str = _env("PLIVO_NUMBER")
    PLIVO_VALIDATE_SIGNATURE: bool = (
        _env("PLIVO_VALIDATE_SIGNATURE", "false").lower() == "true"
    )

    # Default language of the AI. Bengali first, per the user's spec.
    DEFAULT_LANG: str = _env("DEFAULT_LANG", "bn-IN")

    # Greeting spoken the instant a call is answered.
    GREETING_BN: str = _env(
        "GREETING_BN", "হ্যালো, আমি মুর্শিদের অ্যাসিস্ট্যান্ট, বলো কী হলো?"
    )
    TRANSFER_LINE_BN: str = _env("TRANSFER_LINE_BN", "এক মিনিট, কানেক্ট করছি।")
    # v2.1 — name first: the AI takes the caller's name BEFORE a
    # transfer (and asks once, early, if it still does not know it).
    # The transfer line with a name is built as
    # "এক মিনিট, <name> — কানেক্ট করছি।" in call_session.py.
    NAME_ASK_BN: str = _env(
        "NAME_ASK_BN", "আপনার নামটা কী, বলুন?"
    )
    NAME_BEFORE_TRANSFER_BN: str = _env(
        "NAME_BEFORE_TRANSFER_BN", "ট্রান্সফার করার আগে আপনার নামটা বলুন?"
    )
    NAME_RETRY_BN: str = _env(
        "NAME_RETRY_BN", "আপনার নামটা একটু বলবেন? না বললেও আমি কানেক্ট করে দেবো।"
    )
    BUSY_LINE_BN: str = _env(
        "BUSY_LINE_BN", "ওনার এখন ব্যস্ত আছেন, একটু পরে আবার কল করুন। ধন্যবাদ।"
    )
    NOT_HEARD_LINE_BN: str = _env(
        "NOT_HEARD_LINE_BN", "আমি ঠিক শুনতে পারিনি, আরেকবার বলুন।"
    )


settings = Settings()
