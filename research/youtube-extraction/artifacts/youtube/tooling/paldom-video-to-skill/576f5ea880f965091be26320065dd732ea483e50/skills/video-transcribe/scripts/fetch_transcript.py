#!/usr/bin/env python3
"""Acquire metadata + the best available timestamped transcript for a video.

Ladder: manual captions -> auto captions in the video's ORIGINAL language ->
local ASR. Emits metadata.json + transcript.json and a deterministic quality
verdict. No LLM is involved anywhere in this file.

Usage:
    fetch_transcript.py <url-or-file> --out DIR [--lang en] [--asr-model small]
                                      [--no-asr] [--force]
                                      [--cookies-from-browser chrome]
                                      [--sleep-requests 2]
    fetch_transcript.py --self-check

Errors go to stderr as one JSON object {"error","message","fix"} and exit 1.
"""

from __future__ import annotations

import argparse
import itertools
import json
import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path

MEDIA_EXT = {
    ".mp4",
    ".mkv",
    ".mov",
    ".webm",
    ".avi",
    ".m4v",
    ".flv",
    ".mp3",
    ".m4a",
    ".wav",
    ".aac",
    ".ogg",
    ".opus",
    ".flac",
}
# Region/script subtags look like "US", "BR", "Hans" — a lowercase 2-3 letter
# suffix that differs from the base is a SOURCE language, i.e. a translation.
REGION_RE = re.compile(r"^[A-Z]{2}$|^[A-Z][a-z]{3}$|^[0-9]{3}$")
# Repetition detection — see repetition_ratio() for the measurements behind these.
REP_WINDOW = 1000
REP_MIN_CHARS = 400
REP_FAIL = 0.93
REP_WARN = 0.80

# Extra yt-dlp args (cookies / pacing) applied to EVERY invocation. Set once in
# main(); a remedy that only reached one of the three calls would look like it
# had failed.
YTDLP_EXTRA: list[str] = []


def die(code: str, message: str, fix: str) -> None:
    json.dump({"error": code, "message": message, "fix": fix}, sys.stderr)
    sys.stderr.write("\n")
    sys.exit(1)


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    if cmd and cmd[0] == "yt-dlp":
        cmd = [cmd[0], *YTDLP_EXTRA, *cmd[1:]]
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


# --------------------------------------------------------------------------- #
# track selection
# --------------------------------------------------------------------------- #
def classify_track(code: str, base_lang: str) -> str:
    """original | regional | translation, for a YouTube caption language code.

    YouTube names auto-translations "<target>-<source>": `ab-en` is "Abkhazian
    from English". Regional/script variants (`en-US`, `zh-Hans`) are NOT
    translations. `<lang>-orig` is the untranslated auto track.
    """
    if code == base_lang:
        return "original"
    if "-" not in code:
        return "translation" if code != base_lang else "original"
    head, _, tail = code.partition("-")
    if head != base_lang:
        return "translation"
    if tail == "orig":
        return "original"
    return "regional" if REGION_RE.match(tail) else "translation"


TRANSLATION_RE = re.compile(r"^([a-z]{2,3})-([a-z]{2,3})$")


def infer_base_lang(info: dict, want_lang: str) -> str:
    """The video's own spoken language.

    `language` is often absent (yt-dlp reports NA on plenty of videos). The
    reliable fallback: YouTube names auto-translations "<target>-<source>", so
    the source subtag shared by the translation list IS the original language.
    """
    declared = info.get("language")
    if declared:
        return declared.split("-")[0].lower()
    sources: dict[str, int] = {}
    for code in info.get("automatic_captions") or {}:
        m = TRANSLATION_RE.match(code)
        if m and m.group(2) != m.group(1):
            sources[m.group(2)] = sources.get(m.group(2), 0) + 1
    if sources:
        return max(sources, key=lambda k: sources[k])
    return (want_lang or "en").split("-")[0].lower()


def _best_ext(fmts) -> str | None:
    exts = {f.get("ext") for f in fmts if isinstance(f, dict)}
    # json3 carries clean per-event timing; vtt is the universal fallback.
    return "json3" if "json3" in exts else ("vtt" if "vtt" in exts else None)


def pick_track(info: dict, want_lang: str) -> tuple[str, str, str] | None:
    """Return (kind, lang_code, ext) for the best caption track, or None.

    Manual always wins: it preserves the creator's punctuation, casing and
    speaker labels, all of which auto-captions destroy.
    """
    base = infer_base_lang(info, want_lang)

    # Manual tracks are creator-uploaded and are never auto-translations, so the
    # translation filter must NOT run on them: YouTube names real CC tracks with
    # opaque ids like "en-uYU-mmqFLq8", which that filter would discard.
    manual: list[tuple[int, str, str]] = []
    for code, fmts in (info.get("subtitles") or {}).items():
        if code in ("live_chat", "rechat"):
            continue
        ext = _best_ext(fmts)
        if not ext:
            continue
        head = code.split("-")[0].lower()
        if head == base:
            manual.append((0 if code == base else 1, code, ext))
        else:
            manual.append((2, code, ext))  # other language: last resort
    if manual:
        manual.sort()
        if manual[0][0] < 2:
            return ("manual", manual[0][1], manual[0][2])

    auto: list[tuple[int, str, str]] = []
    for code, fmts in (info.get("automatic_captions") or {}).items():
        if code in ("live_chat", "rechat"):
            continue
        if classify_track(code, base) == "translation":
            continue  # machine translation of a machine transcript: compounds error
        ext = _best_ext(fmts)
        if ext:
            auto.append((0 if code == base else 1, code, ext))
    if auto:
        auto.sort()
        return ("auto", auto[0][1], auto[0][2])

    if manual:  # wrong-language manual subs beat nothing at all
        manual.sort()
        return ("manual", manual[0][1], manual[0][2])
    return None


# --------------------------------------------------------------------------- #
# caption parsing
# --------------------------------------------------------------------------- #
TS_RE = re.compile(r"(\d+):(\d{2}):(\d{2})[.,](\d{3})")
TAG_RE = re.compile(r"<[^>]+>")


def _vtt_ms(t: str) -> int:
    h, m, s, ms = TS_RE.match(t).groups()
    return ((int(h) * 60 + int(m)) * 60 + int(s)) * 1000 + int(ms)


def parse_vtt(text: str) -> list[dict]:
    """Parse WEBVTT into segments, dropping the rolling-caption duplication
    that inflates YouTube auto-captions 2-3x."""
    segs: list[dict] = []
    for block in re.split(r"\n\s*\n", text):
        lines = [ln for ln in block.splitlines() if ln.strip()]
        if not lines:
            continue
        cue = next((ln for ln in lines if "-->" in ln), None)
        if not cue:
            continue
        start_s, _, end_s = cue.partition("-->")
        m1, m2 = TS_RE.search(start_s), TS_RE.search(end_s)
        if not (m1 and m2):
            continue
        body = " ".join(lines[lines.index(cue) + 1 :])
        body = TAG_RE.sub("", body).strip()
        if not body:
            continue
        segs.append(
            {"start_ms": _vtt_ms(m1.group(0)), "end_ms": _vtt_ms(m2.group(0)), "text": body}
        )
    return dedupe_rolling(segs)


def parse_json3(text: str) -> list[dict]:
    data = json.loads(text)
    segs = []
    for ev in data.get("events") or []:
        if "segs" not in ev or ev.get("tStartMs") is None:
            continue
        body = "".join(s.get("utf8", "") for s in ev["segs"]).strip()
        if not body or body == "\n":
            continue
        start = int(ev["tStartMs"])
        segs.append(
            {
                "start_ms": start,
                "end_ms": start + int(ev.get("dDurationMs") or 0),
                "text": " ".join(body.split()),
            }
        )
    return dedupe_rolling(segs)


def word_overlap(prev: list[str], cur: list[str]) -> int:
    """Longest suffix of `prev` that is also a prefix of `cur`, in words."""
    for k in range(min(len(prev), len(cur)), 0, -1):
        if prev[-k:] == cur[:k]:
            return k
    return 0


def dedupe_rolling(segs: list[dict]) -> list[dict]:
    """Collapse YouTube's rolling captions.

    Auto-captions scroll: successive cues read "the quick brown" / "quick brown
    fox" / "brown fox jumps". Only the newly revealed words are kept. Matching a
    plain prefix is not enough — the window SHIFTS, so the repeat is an
    overlapping suffix/prefix pair, and missing it inflates a transcript two to
    three times and makes the repetition gate fire on a healthy video.
    """
    out: list[dict] = []
    prev_raw: list[str] = []  # the PREVIOUS CUE as displayed, not as trimmed
    for seg in segs:
        words = seg["text"].split()
        if not words:
            continue
        raw = words
        if prev_raw:
            k = word_overlap(prev_raw, words)
            if k == len(words):  # nothing new in this cue
                if out:
                    out[-1]["end_ms"] = seg["end_ms"]
                prev_raw = raw
                continue
            words = words[k:]
        prev_raw = raw
        if words:
            out.append({**seg, "text": " ".join(words)})
    return out


# --------------------------------------------------------------------------- #
# quality gate (deterministic; thresholds calibrated per transcript kind)
# --------------------------------------------------------------------------- #
def assess(segs: list[dict], duration_s: float | None, kind: str) -> dict:
    factor = {"manual": 0.7, "auto": 0.85, "asr": 1.0}.get(kind, 1.0)
    text = "".join(s["text"] for s in segs)
    effective = "".join(c.lower() for c in text if c.isalnum())
    n = len(effective)
    minutes = (duration_s / 60.0) if duration_s and duration_s > 0 else None

    expected_min = 20 if minutes is None else max(20, min(400, round(minutes * 20 * factor)))
    hard_min = max(3, round(expected_min * 0.2))
    warn_cpm = 12.0 * factor
    cpm = round(n / minutes, 3) if minutes else None

    rep = repetition_ratio(effective)
    coverage, max_gap = timing_metrics(segs, duration_s)

    fails, warns = [], []
    if n == 0:
        fails.append("empty_transcript")
    elif n < hard_min:
        fails.append("suspiciously_short")
    elif n < expected_min:
        warns.append("short_for_duration")
    if (
        cpm is not None
        and minutes
        and minutes >= 1
        and cpm < warn_cpm
        and "suspiciously_short" not in fails
    ):
        warns.append("low_characters_per_minute")
    # Needs a full window to mean anything; see repetition_ratio for thresholds.
    if n >= REP_MIN_CHARS:
        if rep >= REP_FAIL:
            fails.append("highly_repetitive")
        elif rep >= REP_WARN:
            warns.append("repetitive")
    if coverage is not None:
        if coverage < 0.10:
            fails.append("very_low_timed_coverage")
        elif coverage < 0.35:
            warns.append("low_timed_coverage")
        if duration_s and max_gap is not None and max_gap > max(30_000, duration_s * 1000 * 0.4):
            warns.append("large_segment_gap")

    return {
        "status": "fail" if fails else ("warning" if warns else "pass"),
        "reasons": list(dict.fromkeys(fails + warns)),
        "metrics": {
            "effective_chars": n,
            "segments": len(segs),
            "chars_per_minute": cpm,
            "repetition_ratio": round(rep, 4),
            "timed_coverage_ratio": round(coverage, 4) if coverage is not None else None,
            "max_segment_gap_ms": max_gap,
        },
    }


def repetition_ratio(effective: str) -> float:
    """Worst per-window share of repeated 4-grams. Catches Whisper's silence
    loops and un-deduplicated rolling captions.

    Measured WINDOWED, not whole-transcript: a whole-transcript 4-gram ratio
    saturates with length (healthy prose scores 0.48 at 1 min but 0.96 at
    60 min), so a global threshold rejects every long video. Per 1000-char
    window the metric is flat in length — measured on synthetic corpora:

        healthy prose (1-120 min) 0.48-0.53   legit filler-heavy speech 0.71
        legit enumerated steps    0.86        undeduped rolling captions 0.97
        looping ASR over silence  0.98

    Hence the thresholds below sit above enumerated procedures (a normal
    tutorial transcript) and below genuine loops.
    """
    if len(effective) < REP_MIN_CHARS:
        return 0.0
    worst = 0.0
    for i in range(0, max(1, len(effective) - REP_WINDOW + 1), REP_WINDOW):
        chunk = effective[i : i + REP_WINDOW]
        if len(chunk) < 200:
            continue
        grams = [chunk[j : j + 4] for j in range(len(chunk) - 3)]
        worst = max(worst, 1.0 - (len(set(grams)) / len(grams)))
    return worst


def timing_metrics(segs, duration_s):
    if not segs or not duration_s or duration_s <= 0:
        return None, None
    total = sum(max(0, s["end_ms"] - s["start_ms"]) for s in segs)
    ordered = sorted(segs, key=lambda s: s["start_ms"])
    gap = max((b["start_ms"] - a["end_ms"] for a, b in itertools.pairwise(ordered)), default=0)
    return min(1.0, total / (duration_s * 1000)), max(0, gap)


# --------------------------------------------------------------------------- #
# acquisition
# --------------------------------------------------------------------------- #
def probe_metadata(url: str) -> dict:
    if not shutil.which("yt-dlp"):
        die(
            "missing_dependency",
            "yt-dlp is not installed but the input is a URL.",
            "Install it: `uv tool install yt-dlp` (or `pipx install yt-dlp` / `brew install yt-dlp`).",
        )
    p = run(["yt-dlp", "--dump-json", "--skip-download", "--no-playlist", url])
    if p.returncode != 0:
        diagnose_ytdlp(p.stderr)
    try:
        return json.loads(p.stdout.splitlines()[0])
    except (ValueError, IndexError):
        die(
            "metadata_unparseable",
            "yt-dlp returned no parseable JSON for this URL.",
            "Run the same URL with `yt-dlp --dump-json` manually to see the raw failure.",
        )


def diagnose_ytdlp(stderr: str) -> None:
    low = stderr.lower()
    if "sign in to confirm" in low or "not a bot" in low:
        die(
            "bot_check",
            "YouTube served a bot check instead of the video.",
            "Run `yt-dlp -U` first. Then `--cookies-from-browser chrome` (export from a "
            "private window and close it; do not log out). Datacenter/VPN IPs get the "
            "strictest treatment and no flag fixes that — use a residential connection.",
        )
    if "429" in low or "too many requests" in low:
        die(
            "rate_limited",
            "YouTube rate-limited the request (HTTP 429).",
            "Wait several minutes, then retry. Install impersonation support — "
            "`uv tool install 'yt-dlp[default,curl-cffi]'` — because yt-dlp warns that "
            "without an impersonate target YouTube is far more likely to throttle. "
            "Avoid running several extractions back to back.",
        )
    if "private video" in low or "members-only" in low or ("age" in low and "restrict" in low):
        die(
            "restricted",
            "The video is private, members-only, or age-restricted.",
            "Pass browser cookies via `--cookies-from-browser <browser>`, or use a local file.",
        )
    if "unavailable" in low or "removed" in low:
        die(
            "unavailable",
            "The video is unavailable or has been removed.",
            "Verify the URL opens in a browser.",
        )
    die(
        "ytdlp_failed",
        f"yt-dlp failed: {stderr.strip()[:400]}",
        "Run `yt-dlp -U` to update, then retry. If it persists, run the URL manually to see the full error.",
    )


def error_lines(stderr: str) -> str:
    """yt-dlp prints chatty WARNINGs before the real ERROR. Truncating stderr
    from the front hides the actual cause, so pull the ERROR lines out."""
    lines = [ln for ln in stderr.splitlines() if ln.startswith("ERROR")]
    return " | ".join(lines) if lines else stderr.strip()[-300:]


def fetch_captions(url: str, kind: str, lang: str, ext: str, out: Path) -> tuple[str | None, str]:
    """Returns (caption_text, failure_reason). A failure here is reported, not
    fatal.

    YouTube throttles its caption (`timedtext`) endpoint INDEPENDENTLY of media
    download: observed in practice returning HTTP 429 for captions while the
    audio stream downloaded fine. Treating a caption 429 as fatal would deny the
    user a perfectly obtainable ASR transcript, so the ladder continues — but
    loudly, and the reason is recorded in metadata.json so the provenance says
    why a lower rung was used.
    """
    stem = out / "captions"
    cmd = [
        "yt-dlp",
        "--skip-download",
        "--no-playlist",
        "--write-subs" if kind == "manual" else "--write-auto-subs",
        "--sub-langs",
        lang,
        "--sub-format",
        f"{ext}/vtt",
        "-o",
        str(stem),
        url,
    ]
    p = run(cmd)
    hits = [h for h in sorted(out.glob("captions*")) if h.stat().st_size > 0]
    if not hits:
        for stray in out.glob("captions*"):  # never leave a 0-byte artefact
            stray.unlink(missing_ok=True)
        detail = error_lines(p.stderr)
        reason = (
            "rate_limited" if re.search(r"429|too many requests", detail, re.I) else "unavailable"
        )
        sys.stderr.write(f"[captions] {kind}/{lang} {reason}: {detail}\n")
        return None, f"{reason}: {detail[:200]}"
    return hits[0].read_text(encoding="utf-8", errors="replace"), ""


def extract_audio(src: str, out: Path) -> Path:
    if not shutil.which("ffmpeg"):
        die(
            "missing_dependency",
            "ffmpeg is required to extract audio for ASR.",
            "Install it: `brew install ffmpeg` (macOS) or `sudo apt install ffmpeg`.",
        )
    wav = out / "audio.wav"
    p = run(
        [
            "ffmpeg",
            "-hide_banner",
            "-nostdin",
            "-y",
            "-i",
            src,
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            str(wav),
        ]
    )
    if p.returncode != 0 or not wav.exists():
        die(
            "audio_extract_failed",
            f"ffmpeg could not extract audio: {p.stderr.strip()[-300:]}",
            "Check the file plays, and that it has an audio track (`ffprobe <file>`).",
        )
    return wav


def transcribe(wav: Path, model: str) -> list[dict]:
    """Local ASR. whisper.cpp is preferred on Apple Silicon (Metal/CoreML);
    faster-whisper is CPU-only there but is the portable fallback."""
    cli = shutil.which("whisper-cli") or shutil.which("whisper.cpp")
    if cli:
        segs = whisper_cpp(cli, wav, model)
        if segs is not None:
            return segs
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        apple = platform.system() == "Darwin" and platform.machine() == "arm64"
        die(
            "no_asr_backend",
            "No caption track was available and no local ASR backend is installed.",
            (
                "Install whisper.cpp for Metal acceleration: `brew install whisper-cpp` and "
                "download a model (e.g. ggml-small.bin)."
                if apple
                else "Install faster-whisper: `uv pip install faster-whisper`."
            )
            + " Or re-run with --no-asr to fail fast instead of transcribing.",
        )
    # vad_filter is not optional: without it Whisper invents text over silence.
    mdl = WhisperModel(model, device="auto", compute_type="int8")
    segments, _ = mdl.transcribe(str(wav), vad_filter=True, word_timestamps=False)
    return [
        {"start_ms": int(s.start * 1000), "end_ms": int(s.end * 1000), "text": s.text.strip()}
        for s in segments
        if s.text.strip()
    ]


def whisper_cpp(cli: str, wav: Path, model: str) -> list[dict] | None:
    mdl = os.environ.get("WHISPER_CPP_MODEL")
    if not mdl or not Path(mdl).is_file():
        sys.stderr.write(
            "[asr] whisper-cli found but WHISPER_CPP_MODEL is unset or missing; "
            "falling back to faster-whisper.\n"
        )
        return None
    out = wav.with_suffix("")
    p = run([cli, "-m", mdl, "-f", str(wav), "-oj", "-of", str(out), "-np", "-nt"])
    js = out.with_suffix(".json")
    if p.returncode != 0 or not js.is_file():
        sys.stderr.write(f"[asr] whisper.cpp failed: {p.stderr.strip()[:200]}\n")
        return None
    data = json.loads(js.read_text())
    segs = []
    for s in data.get("transcription", []):
        off = s.get("offsets") or {}
        txt = (s.get("text") or "").strip()
        if txt:
            segs.append(
                {"start_ms": int(off.get("from", 0)), "end_ms": int(off.get("to", 0)), "text": txt}
            )
    return segs or None


# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("source", nargs="?", help="video URL or local media path")
    ap.add_argument("--out", type=Path, help="output directory")
    ap.add_argument("--lang", default="en", help="preferred language when the video declares none")
    ap.add_argument("--asr-model", default="small", help="ASR model size (default: small)")
    ap.add_argument("--no-asr", action="store_true", help="fail instead of falling back to ASR")
    ap.add_argument(
        "--cookies-from-browser",
        metavar="BROWSER",
        help="pass browser cookies to yt-dlp (chrome, firefox, safari, ...) — "
        "the documented remedy for a rate-limited or bot-checked IP",
    )
    ap.add_argument(
        "--sleep-requests",
        type=float,
        metavar="SECS",
        help="seconds to sleep between yt-dlp requests",
    )
    ap.add_argument("--force", action="store_true", help="overwrite existing outputs")
    ap.add_argument("--self-check", action="store_true", help="run built-in assertions and exit")
    args = ap.parse_args()

    if args.self_check:
        return self_check()
    if not args.source or not args.out:
        ap.error("source and --out are required")

    if args.cookies_from_browser:
        YTDLP_EXTRA.extend(["--cookies-from-browser", args.cookies_from_browser])
    if args.sleep_requests:
        YTDLP_EXTRA.extend(["--sleep-requests", str(args.sleep_requests)])

    out: Path = args.out
    out.mkdir(parents=True, exist_ok=True)
    tpath, mpath = out / "transcript.json", out / "metadata.json"
    if tpath.exists() and not args.force:
        die(
            "exists",
            f"{tpath} already exists.",
            "Pass --force to overwrite, or choose another --out.",
        )

    src = args.source
    is_url = src.startswith(("http://", "https://"))
    local = Path(src).expanduser()

    if is_url:
        info = probe_metadata(src)
        meta = {
            "source": "youtube" if "youtube" in (info.get("extractor") or "").lower() else "url",
            "video_id": info.get("id"),
            "title": info.get("title"),
            "channel": info.get("channel") or info.get("uploader"),
            "url": info.get("webpage_url") or src,
            "duration_s": info.get("duration"),
            "upload_date": info.get("upload_date"),
            "language": info.get("language"),
            "chapters": info.get("chapters") or [],
        }
        segs, kind, track = [], None, None
        caption_failure = ""
        picked = pick_track(info, args.lang)
        if picked:
            k, code, ext = picked
            raw, caption_failure = fetch_captions(src, k, code, ext, out)
            if raw:
                segs = parse_json3(raw) if raw.lstrip().startswith("{") else parse_vtt(raw)
                kind, track = k, code
        else:
            caption_failure = "no caption track offered for this video"
        meta["caption_failure"] = caption_failure
        if not segs:
            if args.no_asr:
                die(
                    "no_captions",
                    f"No usable caption track ({caption_failure or 'none offered'}) "
                    "and --no-asr was set.",
                    "Re-run without --no-asr to transcribe locally, or wait out the rate limit.",
                )
            sys.stderr.write(
                f"[ladder] captions unusable ({caption_failure or 'none offered'}); "
                "falling back to local ASR\n"
            )
            p = run(
                [
                    "yt-dlp",
                    "-x",
                    "--audio-format",
                    "wav",
                    "--no-playlist",
                    "-o",
                    str(out / "audio.%(ext)s"),
                    src,
                ]
            )
            if p.returncode != 0:
                diagnose_ytdlp(p.stderr)
            segs, kind, track = transcribe(out / "audio.wav", args.asr_model), "asr", None
    else:
        if not local.is_file():
            die(
                "not_found",
                f"{src} is neither a URL nor an existing file.",
                "Pass a http(s) URL or a path to a local media file.",
            )
        if local.suffix.lower() not in MEDIA_EXT:
            die(
                "unsupported_input",
                f"{local.suffix} is not a recognised media extension.",
                f"Supported: {', '.join(sorted(MEDIA_EXT))}",
            )
        if args.no_asr:
            die(
                "no_captions",
                "Local files have no caption track and --no-asr was set.",
                "Re-run without --no-asr.",
            )
        dur = run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "csv=p=0",
                str(local),
            ]
        ).stdout.strip()
        meta = {
            "source": "local-file",
            "video_id": None,
            "title": local.stem,
            "channel": None,
            "url": None,
            "duration_s": float(dur) if dur else None,
            "upload_date": None,
            "language": args.lang,
            "chapters": [],
        }
        segs, kind, track = transcribe(extract_audio(str(local), out), args.asr_model), "asr", None

    quality = assess(segs, meta.get("duration_s"), kind or "asr")
    meta.update(
        {"transcript_source": kind, "caption_track": track, "transcript_quality": quality["status"]}
    )
    tpath.write_text(
        json.dumps(
            {"kind": kind, "track": track, "quality": quality, "segments": segs},
            indent=2,
            ensure_ascii=False,
        )
    )
    mpath.write_text(json.dumps(meta, indent=2, ensure_ascii=False))

    print(
        json.dumps(
            {
                "transcript": str(tpath),
                "metadata": str(mpath),
                "kind": kind,
                "segments": len(segs),
                "quality": quality["status"],
                "reasons": quality["reasons"],
            },
            indent=2,
        )
    )
    if quality["status"] == "fail":
        sys.stderr.write(
            json.dumps(
                {
                    "error": "transcript_quality_fail",
                    "message": f"Transcript rejected: {', '.join(quality['reasons'])}.",
                    "fix": "Inspect transcript.json. Retry with a different --lang, or with "
                    "--asr-model medium if the audio is hard. Do not build notes on this.",
                }
            )
            + "\n"
        )
        return 1
    return 0


def self_check() -> int:
    assert classify_track("en", "en") == "original"
    assert classify_track("en-orig", "en") == "original"
    assert classify_track("en-US", "en") == "regional"
    assert classify_track("zh-Hans", "zh") == "regional"
    assert classify_track("ab-en", "ab") == "translation", "auto-translation must be rejected"
    assert classify_track("de", "en") == "translation"

    # Original language is inferable from the shared source subtag of the
    # translation list even when yt-dlp reports language: NA.
    assert (
        infer_base_lang({"automatic_captions": {"en": [], "ab-en": [], "af-en": []}}, "xx") == "en"
    )
    assert infer_base_lang({"language": "de-DE"}, "en") == "de"

    real_auto = {
        "language": None,
        "subtitles": {},
        "automatic_captions": {
            "en": [{"ext": "vtt"}],
            "de": [{"ext": "vtt"}],
            "ab-en": [{"ext": "vtt"}, {"ext": "json3"}],
        },
    }
    assert pick_track(real_auto, "en") == ("auto", "en", "vtt"), pick_track(real_auto, "en")

    german = {
        "language": "de",
        "subtitles": {},
        "automatic_captions": {"de": [{"ext": "json3"}], "en-de": [{"ext": "json3"}]},
    }
    assert pick_track(german, "en") == ("auto", "de", "json3"), (
        "must not take the en-de translation"
    )

    # YouTube names real CC tracks with opaque ids; manual must still win.
    cc = {
        "language": "en",
        "subtitles": {"en-uYU-mmqFLq8": [{"ext": "vtt"}]},
        "automatic_captions": {"en": [{"ext": "json3"}]},
    }
    assert pick_track(cc, "en") == ("manual", "en-uYU-mmqFLq8", "vtt"), pick_track(cc, "en")
    assert pick_track({"subtitles": {}, "automatic_captions": {}}, "en") is None

    vtt = (
        "WEBVTT\n\n00:00:01.000 --> 00:00:03.000\nhello there\n\n"
        "00:00:03.000 --> 00:00:05.000\nhello there world\n\n"
        "00:00:05.000 --> 00:00:07.000\n<c>styled</c> tail\n"
    )
    segs = parse_vtt(vtt)
    assert [s["text"] for s in segs] == ["hello there", "world", "styled tail"], segs
    assert segs[0]["start_ms"] == 1000 and segs[0]["end_ms"] == 3000

    # Rolling captions SHIFT their window; matching a plain prefix misses them
    # entirely and inflates the transcript ~3x.
    roll = [
        {"start_ms": i * 1000, "end_ms": i * 1000 + 1000, "text": t}
        for i, t in enumerate(
            ["the quick brown", "quick brown fox", "brown fox jumps", "fox jumps over"]
        )
    ]
    assert [s["text"] for s in dedupe_rolling(roll)] == [
        "the quick brown",
        "fox",
        "jumps",
        "over",
    ], dedupe_rolling(roll)
    # A cue that adds nothing extends the previous segment instead of repeating it.
    same = [
        {"start_ms": 0, "end_ms": 1000, "text": "hello world"},
        {"start_ms": 1000, "end_ms": 2000, "text": "hello world"},
    ]
    assert dedupe_rolling(same) == [{"start_ms": 0, "end_ms": 2000, "text": "hello world"}]
    assert word_overlap(["a", "b"], ["c", "d"]) == 0
    assert word_overlap([], ["a"]) == 0

    # A scrolling 3-word window must reconstruct the original word stream
    # exactly, and must strip the ~3x bloat it introduces.
    stream = [f"w{i % 97}" for i in range(600)]
    win = [
        {"start_ms": i * 700, "end_ms": i * 700 + 2100, "text": " ".join(stream[i : i + 3])}
        for i in range(len(stream) - 2)
    ]
    rebuilt = " ".join(s["text"] for s in dedupe_rolling(win)).split()
    assert rebuilt == stream, f"lost {len(stream) - len(rebuilt)} words"
    raw_chars = sum(len(c["text"]) for c in win)
    assert raw_chars / sum(len(s["text"]) for s in dedupe_rolling(win)) > 2.5

    j3 = json.dumps(
        {
            "events": [
                {"tStartMs": 500, "dDurationMs": 900, "segs": [{"utf8": "a "}, {"utf8": "b"}]},
                {"tStartMs": 1400, "segs": [{"utf8": "\n"}]},
            ]
        }
    )
    assert parse_json3(j3) == [{"start_ms": 500, "end_ms": 1400, "text": "a b"}]

    import random

    assert repetition_ratio("abcd" * 400) >= REP_FAIL, "looping ASR must read as repetitive"

    # Length invariance: the whole-transcript version of this metric saturated
    # with duration and failed every long video. Healthy prose must stay under
    # REP_WARN at 1 minute AND at 2 hours.
    random.seed(7)
    vocab = [
        "the",
        "quick",
        "brown",
        "fox",
        "jumps",
        "over",
        "lazy",
        "dog",
        "retrieval",
        "augmented",
        "generation",
        "embedding",
        "vector",
        "database",
        "index",
        "chunk",
        "semantic",
        "search",
        "latency",
        "throughput",
        "model",
        "context",
        "window",
        "token",
        "cost",
        "inference",
        "prompt",
        "cache",
        "evaluation",
    ]
    for minutes in (1, 30, 120):
        prose = " ".join(random.choice(vocab) for _ in range(minutes * 150))  # noqa: S311 - jitter and humanisation timing, never cryptographic
        eff = "".join(c.lower() for c in prose if c.isalnum())
        assert repetition_ratio(eff) < REP_WARN, f"healthy {minutes}min prose flagged"

    # A legitimate step-by-step tutorial is repetitive but must not FAIL.
    steps = "".join(f"step{i}clickthebuttonthenwait" for i in range(300))
    assert REP_WARN <= repetition_ratio(steps) < REP_FAIL, "enumerated steps must warn, not fail"

    good = [
        {
            "start_ms": i * 1000,
            "end_ms": i * 1000 + 900,
            "text": " ".join(random.choice(vocab) for _ in range(12)),  # noqa: S311 - jitter and humanisation timing, never cryptographic
        }
        for i in range(60)
    ]
    assert assess(good, 60.0, "auto")["status"] == "pass", assess(good, 60.0, "auto")
    assert assess([], 60.0, "auto")["status"] == "fail"
    loop = [
        {"start_ms": i * 1000, "end_ms": i * 1000 + 1000, "text": "thanks for watching"}
        for i in range(120)
    ]
    assert "highly_repetitive" in assess(loop, 120.0, "asr")["reasons"]
    sparse = [{"start_ms": 0, "end_ms": 1000, "text": "x" * 50}]
    assert assess(sparse, 600.0, "asr")["status"] == "fail"
    print("self-check OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
