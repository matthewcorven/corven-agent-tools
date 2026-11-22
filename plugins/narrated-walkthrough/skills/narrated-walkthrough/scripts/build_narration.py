#!/usr/bin/env python3
"""Build a narrated, synchronized walkthrough deck from a timeline plan.

Synthesizes narration locally (OpenAI-compatible /v1/audio/speech), measures each
clip, derives exact start offsets, and renders a single-file deck whose audio,
diagrams, captions and scrubber are all driven by that one timeline.

  python3 build_narration.py                 # build (only missing clips)
  python3 build_narration.py --validate      # check the plan, synthesize nothing
  python3 build_narration.py --serve         # build + serve on :8765
  python3 build_narration.py --stop          # stop the server
  python3 build_narration.py --only 04-x --voice af_heart   # re-voice one scene

Exit codes: 0 ok, 1 build/runtime failure, 2 invalid plan (validation).
"""
from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import json
import math
import pathlib
import re
import shutil
import statistics
import struct
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
ROOT = SCRIPT_DIR  # replaced with the plan's directory once the plan is located

# The ONLY network reference in this builder, reachable solely through --cdn.
# Pinned to an exact version: a floating major would let a walkthrough recorded
# today render differently six months from now, which is the wrong shape for an
# artifact whose whole point is a frozen timeline.
CDN_MERMAID = "https://cdn.jsdelivr.net/npm/mermaid@12.3.0/dist/mermaid.min.js"

# Phrases that make an invitation to respond unmistakable in spoken narration.
# Deliberately broad: the contract is "the listener is asked", not a fixed script.
INVITATION = re.compile(
    r"\b(tell me|let me know|which (one|way)|your call|your move|"
    r"answer|respond|reply|say (it|which)|pick one|your choice|"
    r"what would you like|where do you want)\b",
    re.IGNORECASE,
)
FAREWELL = re.compile(r"\b(thanks for watching|thank you for watching|that'?s all)\b", re.IGNORECASE)


class PlanError(Exception):
    """The plan is internally inconsistent or breaks the disposition contract."""


def synth(
    text: str,
    out: pathlib.Path,
    endpoint: str,
    model: str,
    voice: str,
    *,
    ref_audio_b64: str | None = None,
    ref_text: str | None = None,
    language: str | None = None,
    temperature: float | None = None,
) -> None:
    payload: dict = {"model": model, "input": text, "voice": voice, "response_format": "wav"}
    if ref_audio_b64 is not None:
        payload["ref_audio"] = ref_audio_b64
    if ref_text is not None:
        payload["ref_text"] = ref_text
    if language is not None:
        payload["language"] = language
    if temperature is not None:
        payload["temperature"] = temperature

    def _post(body_dict: dict) -> bytes:
        req = urllib.request.Request(
            endpoint,
            data=json.dumps(body_dict).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=180) as resp:
            return resp.read()

    try:
        out.write_bytes(_post(payload))
    except urllib.error.HTTPError as exc:
        # Some speech-to-speech / built-in-voice models reject ref_audio cloning;
        # gracefully retry once with native voice conditioning only.
        if ref_audio_b64 is not None:
            fallback_payload = {
                k: v for k, v in payload.items() if k not in ("ref_audio", "ref_text")
            }
            print(
                f"  note: model {model} rejected ref_audio (HTTP {exc.code}); retrying with native voice '{voice}'",
                flush=True,
            )
            try:
                out.write_bytes(_post(fallback_payload))
                return
            except urllib.error.URLError as retry_exc:
                raise SystemExit(
                    f"TTS request failed ({endpoint}, model={model}): {retry_exc}\n"
                    "Is the local server running? Try: curl -s <host>/v1/models"
                ) from retry_exc
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:240]
        except Exception:
            pass
        raise SystemExit(
            f"TTS request failed ({endpoint}, model={model}): HTTP {exc.code} {detail}\n"
            "Is the local server running? Try: curl -s <host>/v1/models"
        ) from exc
    except urllib.error.URLError as exc:
        raise SystemExit(
            f"TTS request failed ({endpoint}, model={model}): {exc}\n"
            "Is the local server running? Try: curl -s <host>/v1/models"
        ) from exc


def duration(path: pathlib.Path) -> float:
    """Clip length in seconds, read from the RIFF header.

    The synth request pins ``response_format: "wav"``, so a non-WAV payload means the
    server ignored that field. That is a server contract breach, not something to paper
    over with a platform-specific probe: fail with the fix in the message.
    """
    data = path.read_bytes()
    if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise SystemExit(
            f"{path.name} is not RIFF/WAVE (first bytes {data[:4]!r}). The TTS server "
            "ignored response_format=wav; fix the server or pin a compatible model."
        )
    pos, byte_rate, data_bytes = 12, None, None
    while pos + 8 <= len(data):
        chunk_id = data[pos:pos + 4]
        (size,) = struct.unpack("<I", data[pos + 4:pos + 8])
        body = pos + 8
        if chunk_id == b"fmt ":
            _, _channels, _rate, byte_rate, _, _bits = struct.unpack("<HHIIHH", data[body:body + 16])
        elif chunk_id == b"data":
            data_bytes = size if size not in (0, 0xFFFFFFFF) else len(data) - body
        pos = body + size + (size & 1)
    if not byte_rate or data_bytes is None:
        raise SystemExit(f"{path.name}: WAV header has no fmt or data chunk")
    return data_bytes / byte_rate


def estimate_syllables(text: str) -> int:
    """Estimate spoken syllables for articulation-rate measurement (stdlib only)."""
    words = re.findall(r"[A-Za-z0-9]+(?:'[A-Za-z]+)?", text or "")
    total = 0
    for w in words:
        letters = re.sub(r"[^a-z]", "", w.lower())
        if not letters:
            total += max(1, len(w))
            continue
        groups = re.findall(r"[aeiouy]+", letters)
        count = len(groups)
        if letters.endswith("e") and count > 1:
            count -= 1
        total += max(1, count)
    return max(1, total)


def _read_pcm16_wav(path: pathlib.Path) -> tuple[int, int, list[int]]:
    """Read a 16-bit PCM RIFF/WAVE file into (sample_rate, channels, interleaved_int16)."""
    data = path.read_bytes()
    if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise PlanError(f"{path.name}: expected RIFF/WAVE for pace balancing")
    pos, audio_fmt, channels, sample_rate, bits = 12, None, None, None, None
    pcm_bytes = None
    while pos + 8 <= len(data):
        chunk_id = data[pos:pos + 4]
        (size,) = struct.unpack("<I", data[pos + 4:pos + 8])
        body = pos + 8
        if chunk_id == b"fmt ":
            audio_fmt, channels, sample_rate, _, _, bits = struct.unpack("<HHIIHH", data[body:body + 16])
        elif chunk_id == b"data":
            nbytes = size if size not in (0, 0xFFFFFFFF) else len(data) - body
            pcm_bytes = data[body:body + nbytes]
        pos = body + size + (size & 1)
    if audio_fmt != 1 or bits != 16 or not channels or not sample_rate or pcm_bytes is None:
        raise PlanError(f"{path.name}: pace balancing requires 16-bit PCM WAV (fmt={audio_fmt}, bits={bits})")
    n_samples = len(pcm_bytes) // 2
    samples = list(struct.unpack(f"<{n_samples}h", pcm_bytes[: n_samples * 2]))
    return sample_rate, channels, samples


def _write_pcm16_wav(path: pathlib.Path, sample_rate: int, channels: int, samples: list[int]) -> None:
    clamped = [max(-32768, min(32767, int(round(s)))) for s in samples]
    raw = struct.pack(f"<{len(clamped)}h", *clamped) if clamped else b""
    byte_rate = sample_rate * channels * 2
    block_align = channels * 2
    fmt = struct.pack("<HHIIHH", 1, channels, sample_rate, byte_rate, block_align, 16)
    riff_size = 4 + (8 + len(fmt)) + (8 + len(raw))
    payload = (
        b"RIFF" + struct.pack("<I", riff_size) + b"WAVE"
        + b"fmt " + struct.pack("<I", len(fmt)) + fmt
        + b"data" + struct.pack("<I", len(raw)) + raw
    )
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(payload)
    tmp.replace(path)


def _active_speech_bounds(
    samples: list[int],
    sample_rate: int,
    channels: int,
    cushion_ms: float = 90.0,
) -> tuple[int, int, float, float, float]:
    """Locate soft-consonant-safe speech bounds AND true voiced speaking duration.

    Returns ``(start_frame, end_frame, span_seconds, voiced_seconds, seg_rms)``.
    Using a low edge threshold (1.5% of peak RMS) plus a 90 ms cushion preserves
    quiet initial fricatives/plosives and trailing breath decay; measuring
    ``voiced_seconds`` (excluding internal pauses >= 120 ms) prevents long
    mid-clip pauses from hiding fast articulation speed.
    """
    n_frames = len(samples) // channels
    if n_frames == 0:
        return 0, 0, 0.0, 0.0, 0.0
    win = max(1, int(sample_rate * 0.02))
    rms_blocks: list[float] = []
    for f0 in range(0, n_frames, win):
        f1 = min(n_frames, f0 + win)
        acc = 0.0
        count = 0
        for fi in range(f0 * channels, f1 * channels, channels):
            v = samples[fi] / 32768.0
            acc += v * v
            count += 1
        rms_blocks.append(math.sqrt(acc / max(1, count)))
    peak_rms = max(rms_blocks) if rms_blocks else 0.0
    if peak_rms < 1e-4:
        dur = n_frames / sample_rate
        return 0, n_frames, dur, dur, 0.0

    # Conservative edge detector keeps soft consonants & trailing syllables intact
    edge_thresh = max(0.002, peak_rms * 0.015)
    edge_active = [i for i, r in enumerate(rms_blocks) if r >= edge_thresh]
    if not edge_active:
        dur = n_frames / sample_rate
        return 0, n_frames, dur, dur, peak_rms

    cushion = int(round(sample_rate * (cushion_ms / 1000.0)))
    start_f = max(0, edge_active[0] * win - cushion)
    end_f = min(n_frames, (edge_active[-1] + 1) * win + cushion)
    span_sec = max(0.05, ((edge_active[-1] - edge_active[0] + 1) * win) / sample_rate)

    # Voiced articulation detector excludes internal silent pauses >= 120 ms (6 x 20ms blocks)
    voiced_thresh = max(0.006, peak_rms * 0.05)
    voiced_blocks = [1 if r >= voiced_thresh else 0 for r in rms_blocks[edge_active[0] : edge_active[-1] + 1]]
    # Bridge brief consonant closures (< 120 ms = < 6 blocks) as continuous articulation
    idx_b = 0
    while idx_b < len(voiced_blocks):
        if voiced_blocks[idx_b] == 0:
            j = idx_b
            while j < len(voiced_blocks) and voiced_blocks[j] == 0:
                j += 1
            if (j - idx_b) < 6 and idx_b > 0 and j < len(voiced_blocks):
                for k in range(idx_b, j):
                    voiced_blocks[k] = 1
            idx_b = j
        else:
            idx_b += 1
    voiced_sec = max(0.05, sum(voiced_blocks) * (win / sample_rate))

    # Active-segment RMS for loudness normalization
    acc = 0.0
    seg_count = 0
    for fi in range(start_f * channels, end_f * channels, channels):
        v = samples[fi] / 32768.0
        acc += v * v
        seg_count += 1
    seg_rms = math.sqrt(acc / max(1, seg_count))
    return start_f, end_f, span_sec, voiced_sec, seg_rms


def _hann_fade(samples: list[float], sample_rate: int, channels: int, fade_ms: float = 35.0) -> None:
    """Apply smooth raised-cosine (Hann) fade-in and fade-out to eliminate edge clicks."""
    n_frames = len(samples) // channels
    fade_f = min(n_frames // 2, max(1, int(round(sample_rate * (fade_ms / 1000.0)))))
    for fi in range(fade_f):
        scale = 0.5 * (1.0 - math.cos(math.pi * fi / float(fade_f)))
        for c in range(channels):
            samples[fi * channels + c] *= scale
            samples[(n_frames - 1 - fi) * channels + c] *= scale


def split_sentences(text: str) -> list[str]:
    """Split narration text into individual sentences while preserving punctuation."""
    parts = [p.strip() for p in re.split(r"(?<=[.!?])\s+", (text or "").strip()) if p.strip()]
    return parts if parts else ([text.strip()] if (text or "").strip() else [])


def build_master_audio(scenes: list[dict], root: pathlib.Path) -> str | None:
    """Compile all per-scene 16-bit PCM WAVs into a single seamless ``audio/master.wav``.

    Using one continuous master WAV eliminates every browser audio-source switch,
    buffer gap, and wall-clock race at scene boundaries.
    """
    wav_paths = [root / sc["audio"] for sc in scenes]
    if not wav_paths or not all(p.exists() for p in wav_paths):
        return None
    sr0 = ch0 = None
    merged: list[int] = []
    for p in wav_paths:
        try:
            sr, ch, samples = _read_pcm16_wav(p)
        except PlanError:
            return None
        if sr0 is None:
            sr0, ch0 = sr, ch
        elif sr != sr0 or ch != ch0:
            return None
        merged.extend(samples)
    if sr0 is None or ch0 is None or not merged:
        return None
    master_path = root / "audio" / "master.wav"
    master_path.parent.mkdir(parents=True, exist_ok=True)
    _write_pcm16_wav(master_path, sr0, ch0, merged)
    return "audio/master.wav"


def _atempo_chain(factor: float) -> str:
    """Build an ffmpeg atempo filter chain supporting any positive tempo factor."""
    parts: list[str] = []
    rem = factor
    while rem < 0.5:
        parts.append("atempo=0.5")
        rem /= 0.5
    while rem > 2.0:
        parts.append("atempo=2.0")
        rem /= 2.0
    parts.append(f"atempo={rem:.6f}")
    return ",".join(parts)


def balance_scene_pace(
    scenes: list[dict],
    root: pathlib.Path,
    pace_cfg: dict,
    freshly_synthesized: set[str],
) -> dict:
    """Balance speaking cadence, loudness, and lead/tail margins across all scene WAVs.

    Always reads from untouched raw masters stored under ``audio/.raw/`` so repeated builds
    or target_sps adjustments never compound time-stretches. Mutates the target WAVs in place
    so subsequent duration/start measurement stays 100% locked to the balanced audio.
    """
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise PlanError(
            "pace balancing requires 'ffmpeg' on PATH for pitch-preserving atempo time-stretching"
        )

    min_tempo = float(pace_cfg.get("min_tempo", 0.80))
    max_tempo = float(pace_cfg.get("max_tempo", 1.22))
    tolerance = float(pace_cfg.get("tolerance", 0.02))
    lead_ms = float(pace_cfg.get("lead_ms", 180.0))
    tail_ms = float(pace_cfg.get("tail_ms", 420.0))
    target_dbfs = pace_cfg.get("target_dbfs", -20.0)
    target_dbfs = None if target_dbfs is None else float(target_dbfs)

    raw_dir = root / "audio" / ".raw"
    raw_dir.mkdir(parents=True, exist_ok=True)

    # 1. Measure every scene's true VOICED speaking rate from its lossless raw master
    measured: list[dict] = []
    for sc in scenes:
        wav = root / sc["audio"]
        if not wav.exists():
            raise PlanError(f"pace balancing: missing WAV for scene '{sc['id']}' at {wav}")
        raw_master = raw_dir / wav.name
        if sc["id"] in freshly_synthesized or not raw_master.exists():
            shutil.copy2(wav, raw_master)
        sr, ch, samples = _read_pcm16_wav(raw_master)
        start_f, end_f, span_sec, voiced_sec, seg_rms = _active_speech_bounds(samples, sr, ch)
        syl = estimate_syllables(sc["narration"])
        raw_sps = syl / voiced_sec if voiced_sec > 0 else 0.0
        measured.append(
            {
                "scene": sc,
                "wav": wav,
                "raw_master": raw_master,
                "sr": sr,
                "ch": ch,
                "samples": samples,
                "start_f": start_f,
                "end_f": end_f,
                "span_sec": span_sec,
                "voiced_sec": voiced_sec,
                "raw_duration": len(samples) / (sr * ch),
                "seg_rms": seg_rms,
                "syllables": syl,
                "raw_sps": raw_sps,
            }
        )

    valid_rates = [m["raw_sps"] for m in measured if m["raw_sps"] > 0]
    if pace_cfg.get("target_sps") is not None:
        target_sps = float(pace_cfg["target_sps"])
    else:
        target_sps = statistics.median(valid_rates) if valid_rates else 6.8

    report_scenes: list[dict] = []
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = pathlib.Path(tmp)
        for m in measured:
            sc = m["scene"]
            sr, ch = m["sr"], m["ch"]
            trimmed = m["samples"][m["start_f"] * ch : m["end_f"] * ch]
            raw_tempo = (target_sps / m["raw_sps"]) if m["raw_sps"] > 0 else 1.0
            tempo = max(min_tempo, min(max_tempo, raw_tempo))
            if abs(tempo - 1.0) <= tolerance:
                tempo = 1.0

            if tempo != 1.0 and trimmed:
                in_wav = tmp_path / f"{sc['id']}-in.wav"
                out_wav = tmp_path / f"{sc['id']}-out.wav"
                _write_pcm16_wav(in_wav, sr, ch, trimmed)
                cmd = [
                    ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                    "-i", str(in_wav),
                    "-filter:a", _atempo_chain(tempo),
                    "-ar", str(sr), "-ac", str(ch),
                    "-c:a", "pcm_s16le",
                    str(out_wav),
                ]
                proc = subprocess.run(cmd, capture_output=True, text=True)
                if proc.returncode != 0:
                    raise PlanError(f"ffmpeg atempo failed on {sc['id']}: {proc.stderr.strip()}")
                sr, ch, active_samples = _read_pcm16_wav(out_wav)
            else:
                active_samples = list(trimmed)

            # Peak-safe RMS loudness leveling so adjacent scenes stay volume-balanced too
            if target_dbfs is not None and active_samples:
                acc = sum((s / 32768.0) ** 2 for s in active_samples)
                cur_rms = math.sqrt(acc / len(active_samples))
                peak = max(abs(s) for s in active_samples) / 32768.0
                if cur_rms > 1e-4 and peak > 1e-4:
                    want_rms = 10.0 ** (target_dbfs / 20.0)
                    max_safe_gain = (10.0 ** (-1.0 / 20.0)) / peak  # never exceed -1.0 dBFS peak
                    gain = min(want_rms / cur_rms, max_safe_gain, 3.0)
                    if abs(gain - 1.0) > 0.03:
                        active_samples = [s * gain for s in active_samples]

            # Smooth 35ms raised-cosine (Hann) fade eliminates clicks while keeping soft consonants
            _hann_fade(active_samples, sr, ch, fade_ms=35.0)

            lead_pad = [0] * (int(round(sr * (lead_ms / 1000.0))) * ch)
            tail_pad = [0] * (int(round(sr * (tail_ms / 1000.0))) * ch)
            final_samples = lead_pad + active_samples + tail_pad
            _write_pcm16_wav(m["wav"], sr, ch, final_samples)

            _, _, _, new_voiced_sec, _ = _active_speech_bounds(final_samples, sr, ch)
            balanced_sps = m["syllables"] / new_voiced_sec if new_voiced_sec > 0 else 0.0
            report_scenes.append(
                {
                    "id": sc["id"],
                    "syllables": m["syllables"],
                    "raw_sps": round(m["raw_sps"], 2),
                    "balanced_sps": round(balanced_sps, 2),
                    "tempo": round(tempo, 4),
                    "raw_duration": round(m["raw_duration"], 3),
                    "balanced_duration": round(duration(m["wav"]), 3),
                }
            )

    return {
        "target_sps": round(target_sps, 2),
        "lead_ms": lead_ms,
        "tail_ms": tail_ms,
        "target_dbfs": target_dbfs,
        "scenes": report_scenes,
    }


def validate(tl: dict) -> list[str]:
    """Return contract violations. An empty list means the plan may ship."""
    problems: list[str] = []
    scenes = tl.get("scenes") or []
    if not scenes:
        return ["plan has no scenes"]

    seen: set[str] = set()
    for i, sc in enumerate(scenes):
        sid = sc.get("id") or f"<scene {i}>"
        for field in ("id", "title", "mermaid", "narration", "audio"):
            if not sc.get(field):
                problems.append(f"{sid}: missing '{field}'")
        if sid in seen:
            problems.append(f"{sid}: duplicate id")
        seen.add(sid)
        if not (sc.get("captions") or []):
            problems.append(f"{sid}: no captions")
        if sc.get("narration") and FAREWELL.search(sc["narration"]):
            problems.append(
                f"{sid}: narration uses a broadcast sign-off; a walkthrough that leaves work "
                "open must invite a reply instead"
            )

    tts = tl.get("tts") or {}
    if isinstance(tts, dict):
        anchor_scene = tts.get("anchor_scene")
        ref_audio = tts.get("ref_audio")
        ref_text = tts.get("ref_text")
        if anchor_scene and anchor_scene not in seen:
            problems.append(f"tts.anchor_scene '{anchor_scene}' does not match any scene id")
        if anchor_scene and ref_audio:
            problems.append("tts: 'anchor_scene' and 'ref_audio' are mutually exclusive")
        if ref_text and not ref_audio:
            problems.append("tts: 'ref_text' requires 'ref_audio'")
        temp = tts.get("temperature")
        if temp is not None and (isinstance(temp, bool) or not isinstance(temp, (int, float)) or temp < 0):
            problems.append("tts.temperature must be a non-negative number")
        pace = tts.get("pace")
        if pace is not None:
            if not isinstance(pace, dict):
                problems.append("tts.pace must be an object")
            else:
                tsps = pace.get("target_sps")
                if tsps is not None and (isinstance(tsps, bool) or not isinstance(tsps, (int, float)) or tsps <= 0):
                    problems.append("tts.pace.target_sps must be a positive number")
                mint = pace.get("min_tempo", 0.76)
                maxt = pace.get("max_tempo", 1.25)
                if isinstance(mint, bool) or not isinstance(mint, (int, float)) or mint < 0.5 or mint > 1.0:
                    problems.append("tts.pace.min_tempo must be between 0.5 and 1.0")
                if isinstance(maxt, bool) or not isinstance(maxt, (int, float)) or maxt < 1.0 or maxt > 2.0:
                    problems.append("tts.pace.max_tempo must be between 1.0 and 2.0")
                tol = pace.get("tolerance", 0.03)
                if isinstance(tol, bool) or not isinstance(tol, (int, float)) or tol < 0 or tol >= 1.0:
                    problems.append("tts.pace.tolerance must be in [0, 1)")
                for ms_key in ("lead_ms", "tail_ms"):
                    val = pace.get(ms_key)
                    if val is not None and (isinstance(val, bool) or not isinstance(val, (int, float)) or val < 0):
                        problems.append(f"tts.pace.{ms_key} must be a non-negative number")

    disp = tl.get("disposition")
    if disp:
        if not disp.get("prompt"):
            problems.append("disposition: missing 'prompt'")
        if not disp.get("options"):
            problems.append("disposition: missing 'options'")
        last = scenes[-1]
        spoken = " ".join([last.get("narration", "")] + list(last.get("captions") or []))
        if not INVITATION.search(spoken):
            problems.append(
                f"disposition is set but the final scene ({last.get('id')}) never invites a "
                "reply — name the invitation in its narration and last caption"
            )
    return problems


MERMAID_GZIP = "mermaid.min.js.gz"


def resolve_mermaid(tl: dict, base: pathlib.Path, use_cdn: bool) -> str:
    """Resolve the Mermaid script to something the page can load.

    Offline is the only default. A vendored `.js` is embedded directly into the
    deck (no sibling file to lose); a vendored `.js.gz` is decompressed into the
    bundle; anything else is treated as a path or URL and passed through.
    """
    if use_cdn:
        return CDN_MERMAID
    src = tl.get("mermaid_source")
    if not src:
        raise PlanError(
            "mermaid_source not set. This builder ships no CDN default: name a local "
            "Mermaid path in the plan (offline, reproducible), or pass --cdn to opt into "
            f"the pinned build at {CDN_MERMAID}"
        )
    path = (base / src).resolve()
    if path.suffix == ".gz" and path.exists():
        return _inline_script(gzip.decompress(path.read_bytes()).decode("utf-8"))
    if path.exists() and path.suffix == ".js":
        return _inline_script(path.read_text(encoding="utf-8"))
    return src  # a URL, or a path the page resolves itself


def _inline_script(body: str) -> str:
    return "<script>\n" + body.replace("</script", "<\\/script") + "\n</script>"


def _detect_repo_slug(tl: dict, base: pathlib.Path | None = None) -> str | None:
    """Detect GitHub owner/repo deterministically from plan.repo, hud.artifacts, or git remote."""
    explicit = tl.get("repo")
    if isinstance(explicit, str) and re.match(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", explicit.strip()):
        return explicit.strip()

    counts: dict[str, int] = {}
    hud = tl.get("hud") or {}
    for art in (hud.get("artifacts") or []):
        url = str(art.get("url") or "")
        m = re.search(r"github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)(?:/|$)", url, re.I)
        if m:
            slug = m.group(1)
            counts[slug] = counts.get(slug, 0) + 1
    if counts:
        return max(counts.items(), key=lambda kv: kv[1])[0]

    for candidate_dir in (base, pathlib.Path.cwd()):
        if not candidate_dir or not candidate_dir.exists():
            continue
        try:
            proc = subprocess.run(
                ["git", "-C", str(candidate_dir), "config", "--get", "remote.origin.url"],
                capture_output=True, text=True, timeout=3,
            )
            if proc.returncode == 0:
                m = re.search(r"github\.com[:/]([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?$", proc.stdout.strip(), re.I)
                if m:
                    return m.group(1)
        except Exception:
            pass
    return None


def render(tl: dict) -> str:
    scenes = tl["scenes"]
    cues = []
    for sc in scenes:
        caps = sc["captions"]
        weights = [max(1, estimate_syllables(text)) for text in caps]
        total_w = float(sum(weights))
        cursor = float(sc["start"])
        scene_end = float(sc["start"]) + float(sc["duration"])
        for i, (text, w) in enumerate(zip(caps, weights)):
            end_t = scene_end if i == len(caps) - 1 else cursor + sc["duration"] * (w / total_w)
            cues.append(
                {
                    "t": round(cursor, 3),
                    "end": round(end_t, 3),
                    "text": text,
                    "scene": sc["id"],
                }
            )
            cursor = end_t

    repo_slug = _detect_repo_slug(tl, ROOT)
    data = json.dumps(
        {
            "title": tl["title"],
            **({"repo": repo_slug} if repo_slug else {}),
            "master_audio": tl.get("_master_audio_url"),
            "scenes": [
                {
                    "id": s["id"],
                    "title": s["title"],
                    "mermaid": s["mermaid"],
                    "start": s["start"],
                    "duration": s["duration"],
                    "audio": s["audio"],
                    "narration": s["narration"],
                    **({"kind": s["kind"]} if s.get("kind") else {}),
                    **({"choices": s["choices"]} if s.get("choices") else {}),
                    **({"meta": s["meta"]} if s.get("meta") else {}),
                }
                for s in scenes
            ],
            "cues": cues,
            "total": tl["total_duration"],
            "disposition": tl.get("disposition"),
            "hud": tl.get("hud"),
            "a2ui": tl.get("a2ui"),
            "tts": {
                "model": (tl.get("tts") or {}).get("model") or "none",
                "voice": (tl.get("tts") or {}).get("voice") or "none",
            },
        },
        ensure_ascii=False,
    )
    mermaid = tl["_mermaid_resolved"]
    tts_cfg = tl.get("tts") or {}
    return (
        template().replace("__DATA__", data)
        .replace("__TITLE__", tl["title"])
        .replace("__VOICE__", tts_cfg.get("voice") or "none")
        .replace("__MODEL__", tts_cfg.get("model") or "none")
        .replace("__MERMAID__", mermaid)
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", default="timeline.json")
    ap.add_argument("--voice", default=None)
    ap.add_argument("--model", default=None)
    ap.add_argument("--endpoint", default=None)
    ap.add_argument("--anchor-scene", default=None, help="scene id used as ref_audio/ref_text anchor for other scenes")
    ap.add_argument("--clear-anchor", action="store_true", help="remove saved anchor_scene/ref_audio/ref_text before synthesizing (for native-voice models)")
    ap.add_argument("--language", default=None, help="optional language hint forwarded to /v1/audio/speech")
    ap.add_argument("--temperature", type=float, default=None, help="optional sampling temperature forwarded to /v1/audio/speech")
    ap.add_argument("--only", default=None, help="re-synthesize a single scene id (overwriting its clip)")
    ap.add_argument("--force", action="store_true", help="re-synthesize all clips even when WAV files already exist")
    ap.add_argument("--balance-pace", action="store_true", help="normalize speaking speed, loudness, and margins across scenes before deriving timeline offsets")
    ap.add_argument("--target-sps", type=float, default=None, help="target speaking rate in syllables/sec (implies --balance-pace)")
    ap.add_argument("--validate", action="store_true", help="check the plan and exit")
    ap.add_argument(
        "--cdn",
        action="store_true",
        help="use the pinned Mermaid CDN build instead of a local mermaid_source",
    )
    ap.add_argument(
        "--no-audio",
        action="store_true",
        help="render from the durations already recorded in the plan; synthesize nothing",
    )
    ap.add_argument("--serve", action="store_true", help="serve the deck after building")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--stop", action="store_true", help="stop the deck server")
    args = ap.parse_args()

    if args.stop:
        subprocess.run(["pkill", "-f", f"http.server {args.port}"], check=False)
        print("stopped deck server")
        return 0

    global ROOT
    plan_path = pathlib.Path(args.plan)
    if not plan_path.is_absolute():
        plan_path = pathlib.Path.cwd() / plan_path
    plan_path = plan_path.resolve()
    if not plan_path.exists():
        print(f"FAIL no plan at {plan_path}")
        return 1
    ROOT = plan_path.parent
    tl = json.loads(plan_path.read_text())
    has_tts_input = bool(tl.get("tts")) or any(
        v is not None
        for v in (args.model, args.voice, args.endpoint, args.anchor_scene, args.language, args.temperature)
    )
    tts = tl.setdefault("tts", {}) if (has_tts_input or not args.no_audio) else {}
    if args.model is not None:
        tts["model"] = args.model
    if args.voice is not None:
        tts["voice"] = args.voice
    if args.endpoint is not None:
        tts["endpoint"] = args.endpoint
    if args.clear_anchor:
        tts.pop("anchor_scene", None)
        tts.pop("ref_audio", None)
        tts.pop("ref_text", None)
    if args.anchor_scene is not None:
        tts["anchor_scene"] = args.anchor_scene
    if args.language is not None:
        tts["language"] = args.language
    if args.temperature is not None:
        tts["temperature"] = args.temperature
    if args.balance_pace or args.target_sps is not None:
        pace_obj = dict(tts.get("pace") or {})
        pace_obj["enabled"] = True
        if args.target_sps is not None:
            pace_obj["target_sps"] = args.target_sps
        tts["pace"] = pace_obj
    missing_cfg = [] if args.no_audio else [
        k for k in ("endpoint", "model", "voice") if not tts.get(k)
    ]
    if missing_cfg:
        print(
            f"FAIL tts.{', tts.'.join(missing_cfg)} not configured. This builder ships no "
            "defaults: name the endpoint, model and voice in the plan (or pass --endpoint "
            "--model --voice). Discover what a local server offers with: "
            "curl -s <host>/v1/models"
        )
        return 2

    try:
        tl["_mermaid_resolved"] = resolve_mermaid(tl, plan_path.parent, args.cdn)
    except PlanError as exc:
        print(f"FAIL {exc}")
        return 2

    problems = validate(tl)
    if args.validate:
        for p in problems:
            print(f"FAIL {p}")
        print("plan ok" if not problems else f"{len(problems)} problem(s)")
        return 0 if not problems else 2

    if problems:
        for p in problems:
            print(f"FAIL {p}")
        print("\nrefusing to build: fix the plan, or drop 'disposition' if nothing is open")
        return 2

    if args.no_audio:
        stale = [s["id"] for s in tl["scenes"] if not s.get("duration")]
        if stale:
            print(f"FAIL --no-audio needs recorded durations; missing for {stale}")
            return 2
    if not args.no_audio:
        (ROOT / "audio").mkdir(exist_ok=True)
        anchor_id = tts.get("anchor_scene")
        ext_ref_path = tts.get("ref_audio")
        ext_ref_b64: str | None = None
        ext_ref_text: str | None = tts.get("ref_text")
        if ext_ref_path:
            resolved_ref = (ROOT / ext_ref_path).resolve()
            if not resolved_ref.exists():
                raise PlanError(f"tts.ref_audio not found: {resolved_ref}")
            ext_ref_b64 = base64.b64encode(resolved_ref.read_bytes()).decode("ascii")

        scenes_by_id = {s["id"]: s for s in tl["scenes"]}
        if args.only and args.only not in scenes_by_id:
            raise PlanError(f"--only '{args.only}' does not match any scene id")

        # Synthesize the anchor scene first so every subsequent clip can condition on it.
        ordered_scenes = list(tl["scenes"])
        if anchor_id and anchor_id in scenes_by_id:
            anchor_sc = scenes_by_id[anchor_id]
            ordered_scenes = [anchor_sc] + [s for s in ordered_scenes if s["id"] != anchor_id]

        anchor_b64: str | None = None
        anchor_text: str | None = None
        freshly_synthesized: set[str] = set()
        for scene in ordered_scenes:
            is_anchor = bool(anchor_id and scene["id"] == anchor_id)
            wav = ROOT / scene["audio"]
            targeted = args.only is None or scene["id"] == args.only
            need_synth = targeted and (args.force or args.only == scene["id"] or not wav.exists())

            if is_anchor:
                if need_synth:
                    print(f"  synth {scene['id']} (anchor) ...", flush=True)
                    synth(
                        scene["narration"],
                        wav,
                        tts["endpoint"],
                        tts["model"],
                        tts["voice"],
                        language=tts.get("language"),
                        temperature=tts.get("temperature"),
                    )
                    freshly_synthesized.add(scene["id"])
                if not wav.exists():
                    raise PlanError(
                        f"anchor scene '{anchor_id}' has no audio at {wav}; run without --only first"
                    )
                anchor_b64 = base64.b64encode(wav.read_bytes()).decode("ascii")
                anchor_text = scene["narration"]
                if targeted:
                    scene["duration"] = round(duration(wav), 3)
            elif targeted:
                if need_synth:
                    ref_b64 = anchor_b64 if anchor_id else ext_ref_b64
                    ref_txt = anchor_text if anchor_id else ext_ref_text
                    suffix = f" (anchored to {anchor_id})" if anchor_id else ""
                    print(f"  synth {scene['id']}{suffix} ...", flush=True)
                    synth(
                        scene["narration"],
                        wav,
                        tts["endpoint"],
                        tts["model"],
                        tts["voice"],
                        ref_audio_b64=ref_b64,
                        ref_text=ref_txt,
                        language=tts.get("language"),
                        temperature=tts.get("temperature"),
                    )
                    freshly_synthesized.add(scene["id"])
                scene["duration"] = round(duration(wav), 3)

        pace_cfg = tts.get("pace") if isinstance(tts, dict) else None
        pace_report = None
        if isinstance(pace_cfg, dict) and pace_cfg.get("enabled", True):
            pace_report = balance_scene_pace(tl["scenes"], ROOT, pace_cfg, freshly_synthesized)
            # Re-measure every scene's duration directly from its final balanced WAV so
            # cumulative starts, total_duration, and caption cues stay 100% locked in sync.
            for scene in tl["scenes"]:
                scene["duration"] = round(duration(ROOT / scene["audio"]), 3)
            raw_rates = [r["raw_sps"] for r in pace_report["scenes"]]
            bal_rates = [r["balanced_sps"] for r in pace_report["scenes"]]
            print(
                f"pace balanced: target {pace_report['target_sps']:.2f} syl/s | "
                f"raw {min(raw_rates):.2f}..{max(raw_rates):.2f} -> "
                f"balanced {min(bal_rates):.2f}..{max(bal_rates):.2f} syl/s"
            )
    else:
        pace_report = None

    missing = [s["id"] for s in tl["scenes"] if s.get("duration") is None]
    if missing:
        raise PlanError(f"no duration for {missing}; run without --only first")

    t0 = 0.0
    for scene in tl["scenes"]:
        scene["start"] = round(t0, 3)
        t0 += scene["duration"]
    tl["total_duration"] = round(t0, 3)

    # Compile a single seamless master WAV when all scene clips exist on disk so
    # playback never reloads <audio> sources or chops edges between scenes.
    master_rel = build_master_audio(tl["scenes"], ROOT)
    if master_rel:
        tl["master_audio"] = master_rel
        master_hash = hashlib.sha256((ROOT / master_rel).read_bytes()).hexdigest()[:8]
        tl["_master_audio_url"] = f"{master_rel}?v={master_hash}"
    else:
        tl.pop("master_audio", None)
        tl.pop("_master_audio_url", None)

    (ROOT / "slides.html").write_text(render(tl))
    resolved_mermaid = tl.pop("_mermaid_resolved", "")
    tl.pop("_master_audio_url", None)
    plan_path.write_text(json.dumps(tl, indent=2, ensure_ascii=False) + "\n")
    manifest = {
        "plugin": "narrated-walkthrough",
        "player": "player/player.html",
        "schema": "narrated-timeline/v1",
        "mermaid": "12.3.0" if "12.3.0" in resolved_mermaid else "local",
        "scenes": len(tl["scenes"]),
        "total_duration": tl["total_duration"],
        "generated_by": "build_narration.py",
    }
    if pace_report is not None:
        manifest["pace"] = pace_report
    (ROOT / "deck.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"total {tl['total_duration']}s across {len(tl['scenes'])} scenes")

    if args.serve:
        subprocess.run(["pkill", "-f", f"http.server {args.port}"], check=False)
        subprocess.Popen(
            [sys.executable, "-m", "http.server", str(args.port), "--bind", "127.0.0.1"],
            cwd=ROOT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        print(f"serving http://127.0.0.1:{args.port}/slides.html")
    return 0


PLAYER = pathlib.Path(__file__).resolve().parent.parent / "player" / "player.html"


def template() -> str:
    """The player is a shipped artifact, not a Python string literal.

    It is the same file every repo's deck is built from, so the UI of a narrated
    walkthrough is versioned and diffable instead of being duplicated per repo.
    """
    return PLAYER.read_text(encoding="utf-8")


if __name__ == "__main__":
    try:
        sys.exit(main())
    except PlanError as exc:
        print(f"FAIL {exc}")
        sys.exit(2)
