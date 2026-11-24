"""Audio helpers: G.711 mu-law encode/decode and WAV writing.

Plivo streams audio as mu-law 8 kHz (audio/x-mulaw;rate=8000).
Google Cloud STT accepts mu-law directly; Google Cloud TTS returns
LINEAR16, which we convert to mu-law for Plivo. Pure stdlib, no deps.
"""

import io
import math
import struct
import wave

SAMPLE_RATE = 8000

# --- mu-law decode table (G.711) -------------------------------------------
_MULAW_DECODE = []
for _i in range(256):
    _u = ~_i & 0xFF
    _sign = -1 if (_u & 0x80) else 1
    _exp = (_u >> 4) & 0x07
    _mant = _u & 0x0F
    _sample = ((0x10 + _mant) << (_exp + 3)) - 0x84
    _MULAW_DECODE.append(_sign * _sample)


def mulaw_to_pcm16(mulaw: bytes) -> bytes:
    """mu-law bytes -> little-endian signed 16-bit PCM bytes."""
    return b"".join(struct.pack("<h", _MULAW_DECODE[b]) for b in mulaw)


def pcm16_to_mulaw(pcm: bytes) -> bytes:
    """Little-endian signed 16-bit PCM bytes -> mu-law bytes."""
    out = bytearray()
    n = len(pcm) // 2
    samples = struct.unpack("<%dh" % n, pcm[: n * 2])
    for s in samples:
        out.append(_linear_to_mulaw(s))
    return bytes(out)


def _linear_to_mulaw(sample: int) -> int:
    # Standard G.711 mu-law encoding (BIAS = 0x84, CLIP = 32635).
    sign = 0x80 if sample < 0 else 0x00
    if sample < 0:
        sample = -sample
    if sample > 32635:
        sample = 32635
    sample += 0x84
    exp = 7
    for e in range(7, -1, -1):
        if sample & (1 << (e + 7)):
            exp = e
            break
    else:
        exp = 0
    mant = (sample >> (exp + 3)) & 0x0F
    return (~(sign | (exp << 4) | mant)) & 0xFF


def pcm16_rms(pcm: bytes) -> float:
    """Root-mean-square level of PCM16 audio; used for speech detection."""
    n = len(pcm) // 2
    if n == 0:
        return 0.0
    samples = struct.unpack("<%dh" % n, pcm[: n * 2])
    return math.sqrt(sum(s * s for s in samples) / n)


def pcm16_duration_seconds(pcm: bytes) -> float:
    return (len(pcm) // 2) / SAMPLE_RATE


def pcm16_to_wav_bytes(pcm: bytes, sample_rate: int = SAMPLE_RATE) -> bytes:
    """PCM16 bytes -> a complete .wav file in memory."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm)
    return buf.getvalue()


def mulaw_to_wav_bytes(mulaw: bytes) -> bytes:
    """mu-law 8 kHz bytes -> PCM16 .wav file in memory."""
    return pcm16_to_wav_bytes(mulaw_to_pcm16(mulaw))


def resample_pcm16_linear(pcm: bytes, from_rate: int, to_rate: int = SAMPLE_RATE) -> bytes:
    """Naive linear-interpolation resampler (good enough for voice)."""
    if from_rate == to_rate:
        return pcm
    n = len(pcm) // 2
    if n == 0:
        return b""
    samples = struct.unpack("<%dh" % n, pcm)
    out_len = int(n * to_rate / from_rate)
    out = bytearray()
    for i in range(out_len):
        pos = i * from_rate / to_rate
        j = int(pos)
        frac = pos - j
        s0 = samples[min(j, n - 1)]
        s1 = samples[min(j + 1, n - 1)]
        out += struct.pack("<h", int(s0 + (s1 - s0) * frac))
    return bytes(out)


def mix_mulaw(a: bytes, b: bytes) -> bytes:
    """Mix two mu-law 8 kHz streams into one (for conversation.wav).

    Both legs of a call in a single file, like Dograh's recording
    router keeps one combined recording per call. The shorter leg is
    padded with silence; each leg is halved first so the mix cannot
    clip.
    """
    pa = mulaw_to_pcm16(a)
    pb = mulaw_to_pcm16(b)
    na, nb = len(pa) // 2, len(pb) // 2
    n = max(na, nb)
    sa = struct.unpack("<%dh" % na, pa[: na * 2]) if na else ()
    sb = struct.unpack("<%dh" % nb, pb[: nb * 2]) if nb else ()
    out = bytearray()
    for i in range(n):
        x = (sa[i] if i < na else 0) // 2 + (sb[i] if i < nb else 0) // 2
        out += struct.pack("<h", max(-32768, min(32767, x)))
    return pcm16_to_mulaw(bytes(out))
