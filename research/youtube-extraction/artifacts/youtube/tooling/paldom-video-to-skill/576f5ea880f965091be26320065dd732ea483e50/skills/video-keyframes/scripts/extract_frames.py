#!/usr/bin/env python3
"""Scene-aware keyframe capture + OCR + timeline digest.

Extracts the frames that show something NEW, not every frame. Threshold is
calibrated per video; near-duplicates are dropped by perceptual hash; the
result is timeline.json, a token-lean digest an agent reads before it looks
at any image.

Usage:
    extract_frames.py <file> --out DIR [--transcript T.json] [--max-frames N]
                             [--start MM:SS --end MM:SS] [--no-ocr] [--force]
    extract_frames.py frame-at <file> --out DIR <t> [<t> ...]
    extract_frames.py clip <file> --out DIR <t0> <t1> [--fps 2]
    extract_frames.py --self-check

Perceptual hashing and calibration use ffmpeg only — no Python image libraries.
Errors go to stderr as {"error","message","fix"} and exit 1.
"""

from __future__ import annotations

import argparse
import itertools
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

HASH_W, HASH_H = 9, 8  # dHash: 9x8 gray -> 64 bits of adjacent-pixel deltas
DEDUP_HAMMING = 6  # keep a frame only if it differs by more than this
FRAME_CAP = 100
MAX_FPS = 2.0
MIN_THRESHOLD = 0.004  # low enough for light slide decks (see threshold_from_scores)
MAX_THRESHOLD = 1.0  # ffmpeg scene scores are in [0,1]
DEFAULT_THRESHOLD = 0.05
MAX_UNOBSERVED_S = 120.0  # a stretch longer than this with no frame is a blind spot
PIN_COUNT = 8  # evenly spaced frames that dedup may never drop
PTS_RE = re.compile(r"pts_time:(\d+\.?\d*)")
SCORE_RE = re.compile(r"lavfi\.scene_score=([0-9.eE+-]+)")


def die(code: str, message: str, fix: str) -> None:
    json.dump({"error": code, "message": message, "fix": fix}, sys.stderr)
    sys.stderr.write("\n")
    sys.exit(1)


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, **kw)


def need(binary: str, install: str) -> str:
    p = shutil.which(binary)
    if not p:
        die("missing_dependency", f"{binary} is required but not on PATH.", install)
    return p


def parse_time(v: str | float | None) -> float | None:
    """SS, MM:SS or HH:MM:SS (optional .ms)."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    parts = str(v).strip().split(":")
    try:
        nums = [float(p) for p in parts]
    except ValueError:
        die("bad_timestamp", f"Cannot parse timestamp {v!r}.", "Use SS, MM:SS or HH:MM:SS.")
    secs = 0.0
    for n in nums:
        secs = secs * 60 + n
    return secs


def fmt_time(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}"


# --------------------------------------------------------------------------- #
# ffmpeg capability + probing
# --------------------------------------------------------------------------- #
def vfr_args(ffmpeg: str) -> list[str]:
    """ffmpeg 8 REMOVED -vsync (it hard-errors); older builds lack -fps_mode.
    Probe rather than hardcode either."""
    help_text = run([ffmpeg, "-hide_banner", "-h", "long"]).stdout.decode("utf-8", "replace")
    return ["-fps_mode", "vfr"] if "-fps_mode" in help_text else ["-vsync", "vfr"]


def probe_duration(path: str) -> float:
    need("ffprobe", "Install ffmpeg (which ships ffprobe): `brew install ffmpeg`.")
    out = (
        run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path])
        .stdout.decode()
        .strip()
    )
    try:
        return float(out)
    except ValueError:
        die(
            "probe_failed",
            f"ffprobe could not read a duration from {path}.",
            "Confirm the file is a playable video (`ffprobe <file>`).",
        )


# --------------------------------------------------------------------------- #
# perceptual hashing (ffmpeg-only)
# --------------------------------------------------------------------------- #
def dhash_bits(raw: bytes) -> int:
    """9x8 grayscale -> 64-bit difference hash."""
    bits = 0
    for row in range(HASH_H):
        base = row * HASH_W
        for col in range(HASH_W - 1):
            bits = (bits << 1) | (1 if raw[base + col] < raw[base + col + 1] else 0)
    return bits


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def hash_images(ffmpeg: str, paths: list[Path]) -> list[int]:
    """Hash every frame in ONE ffmpeg pass using the image2 demuxer."""
    if not paths:
        return []
    listing = "\n".join(f"file '{p.resolve()}'" for p in paths)
    p = subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            "-",
            "-vf",
            f"scale={HASH_W}:{HASH_H},format=gray",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "gray",
            "-",
        ],
        input=listing.encode(),
        capture_output=True,
    )
    size = HASH_W * HASH_H
    data = p.stdout
    if len(data) < size * len(paths):
        # concat demuxer can refuse odd inputs; fall back to one call per frame
        return [hash_one(ffmpeg, q) for q in paths]
    return [dhash_bits(data[i * size : (i + 1) * size]) for i in range(len(paths))]


def hash_one(ffmpeg: str, path: Path) -> int:
    p = run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(path),
            "-vf",
            f"scale={HASH_W}:{HASH_H},format=gray",
            "-frames:v",
            "1",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "gray",
            "-",
        ]
    )
    return dhash_bits(p.stdout) if len(p.stdout) >= HASH_W * HASH_H else 0


# --------------------------------------------------------------------------- #
# threshold calibration
# --------------------------------------------------------------------------- #
def collect_scene_scores(
    ffmpeg: str, path: str, start: float | None, end: float | None
) -> list[float]:
    """Measure this video's ACTUAL scene-score distribution, cheaply.

    Downscaling to 160px makes a full decode fast (a 14 s clip costs ~0.06 s)
    while barely moving the scores, so we threshold the same quantity we
    measured instead of a proxy for it.
    """
    pre = ["-ss", f"{start}"] if start is not None else []
    dur = ["-t", f"{end - (start or 0)}"] if end is not None else []
    p = run(
        [
            ffmpeg,
            "-hide_banner",
            *pre,
            "-i",
            path,
            *dur,
            "-vf",
            "scale=160:-2,select='gt(scene,0)',metadata=print",
            "-an",
            "-f",
            "null",
            "-",
        ]
    )
    return [float(m) for m in SCORE_RE.findall(p.stderr.decode("utf-8", "replace"))]


def threshold_from_scores(scores: list[float], target: int) -> tuple[float, float]:
    """Pick the threshold that admits roughly `target` frames.

    ffmpeg's `scene` value is NOT a fraction of pixels changed. Measured here:
    a full slide transition on light slides scores only ~0.03, so the
    conventional 0.3-0.4 defaults (and a 0.05 floor) detect NOTHING on the most
    common slide-deck case. Deriving the threshold from the observed
    distribution avoids baking in any constant that assumes a content type.
    """
    if len(scores) < 2:
        return DEFAULT_THRESHOLD, 0.0
    desc = sorted(scores, reverse=True)
    # The target-th largest score is the threshold that admits ~target frames.
    # No extra "noise floor" on top of this: on busy footage a median-based
    # floor overshoots the cap and selects everything. When a video really is
    # static, cand falls below MIN_THRESHOLD, almost nothing is selected, and
    # the coverage check hands over to interval sampling — the correct outcome.
    cand = desc[min(len(desc) - 1, max(0, target - 1))] * 0.9
    median = sorted(scores)[len(scores) // 2]
    confidence = min(1.0, desc[0] / (median * 10)) if median > 0 else 1.0
    return round(max(MIN_THRESHOLD, min(MAX_THRESHOLD, cand)), 5), round(confidence, 3)


# --------------------------------------------------------------------------- #
# budget
# --------------------------------------------------------------------------- #
def frame_budget(duration: float, cap: int = FRAME_CAP) -> int:
    if duration <= 0:
        return 1
    if duration <= 30:
        target = min(cap, max(12, round(duration)))
    elif duration <= 60:
        target = min(cap, 40)
    elif duration <= 180:
        target = min(cap, 60)
    elif duration <= 600:
        target = min(cap, 80)
    else:
        target = cap
    return min(target, max(1, round(MAX_FPS * duration)), cap)


# --------------------------------------------------------------------------- #
# extraction
# --------------------------------------------------------------------------- #
def scene_extract(
    ffmpeg: str, path: str, out: Path, thr: float, start: float | None, end: float | None
) -> list[tuple[float, Path]]:
    """One decode: writes JPEGs and prints their pts_time to stderr.

    `eq(n,0)` keeps the opening frame — scene detection never fires on frame 0,
    so without it the first thing the viewer sees is missing.
    """
    out.mkdir(parents=True, exist_ok=True)
    pre = ["-ss", f"{start}"] if start is not None else []
    dur = ["-t", f"{end - (start or 0)}"] if end is not None else []
    cmd = [
        ffmpeg,
        "-hide_banner",
        *pre,
        "-i",
        path,
        *dur,
        "-vf",
        f"select='eq(n\\,0)+gt(scene\\,{thr})',showinfo",
        *vfr_args(ffmpeg),
        "-q:v",
        "3",
        str(out / "scene_%04d.jpg"),
    ]
    p = run(cmd)
    stderr = p.stderr.decode("utf-8", "replace")
    times = [float(m) for m in PTS_RE.findall(stderr)]
    files = sorted(out.glob("scene_*.jpg"))
    off = start or 0.0
    return [(t + off, f) for t, f in zip(times, files, strict=False)]


def scene_coverage_poor(cands: list[tuple[float, Path]], lo: float, hi: float) -> bool:
    """True when scene detection left a blind spot worth interval-sampling.

    Frame COUNT is the wrong test: three frames is complete coverage of a
    three-scene 14-second clip but nothing at all in a two-hour lecture. What
    matters is whether any stretch went unobserved.
    """
    if len(cands) < 2:
        return True
    marks = [lo] + [t for t, _ in cands] + [hi]
    return max(b - a for a, b in itertools.pairwise(marks)) > MAX_UNOBSERVED_S


def interval_extract(
    ffmpeg: str,
    path: str,
    out: Path,
    duration: float,
    target: int,
    start: float | None,
    end: float | None,
) -> list[tuple[float, Path]]:
    """Fallback for near-static screencasts, where scene detection barely fires."""
    lo = start or 0.0
    hi = end if end is not None else duration
    span = max(0.1, hi - lo)
    step = max(span / max(1, target), 1.0 / MAX_FPS)
    out.mkdir(parents=True, exist_ok=True)
    picked = []
    t = lo
    idx = 0
    while t < hi and idx < target:
        dst = out / f"iv_{idx:04d}.jpg"
        p = run(
            [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-ss",
                f"{t}",
                "-i",
                path,
                "-frames:v",
                "1",
                "-q:v",
                "3",
                "-y",
                str(dst),
            ]
        )
        if p.returncode == 0 and dst.exists():
            picked.append((t, dst))
        t += step
        idx += 1
    return picked


def dedupe(
    ffmpeg: str, cands: list[tuple[float, Path]], pinned: set[int]
) -> tuple[list[tuple[float, Path, int]], list[Path]]:
    """Drop near-identical frames. Pinned indices survive regardless.

    dHash is luminance-only: a button turning red or a total turning green is
    invisible to it, so a UI flow can collapse to a single frame. Pinned,
    evenly spaced frames preserve the in-between states.
    """
    hashes = hash_images(ffmpeg, [p for _, p in cands])
    kept: list[tuple[float, Path, int]] = []
    kept_hashes: list[int] = []
    dropped: list[Path] = []
    for i, ((t, path), h) in enumerate(zip(cands, hashes, strict=False)):
        if (
            i in pinned
            or not kept_hashes
            or all(hamming(h, k) > DEDUP_HAMMING for k in kept_hashes)
        ):
            motion = min((hamming(h, k) for k in kept_hashes), default=64)
            kept.append((t, path, motion))
            kept_hashes.append(h)
        else:
            dropped.append(path)
    return kept, dropped


def fill_gaps(
    ffmpeg: str,
    path: str,
    out: Path,
    kept: list[tuple[float, Path, int]],
    lo: float,
    hi: float,
    budget: int,
) -> list[tuple[float, Path, int]]:
    """Re-sample stretches that DEDUP emptied.

    Coverage is checked before dedup, but dedup is what decides the final set:
    a run of visually similar shots collapses to one frame and can leave minutes
    unobserved. Measured on a 13.6-minute talk, dedup opened a 280 s blind spot
    under a 120 s limit — and it covered the chapter with all the commands in it.
    So the check has to run again on what actually survived.
    """
    if not kept:
        return kept
    room = budget - len(kept)
    if room <= 0:
        return kept
    kept = sorted(kept, key=lambda k: k[0])
    marks = [lo] + [t for t, _, _ in kept] + [hi]
    wanted: list[float] = []
    for a, b in itertools.pairwise(marks):
        span = b - a
        if span <= MAX_UNOBSERVED_S:
            continue
        n = int(span // MAX_UNOBSERVED_S)
        step = span / (n + 1)
        wanted.extend(a + step * (i + 1) for i in range(n))
    if not wanted:
        return kept
    added = []
    for t in wanted[:room]:
        dst = out / f"gap_{int(t * 1000):08d}.jpg"
        p = run(
            [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-ss",
                f"{t}",
                "-i",
                path,
                "-frames:v",
                "1",
                "-q:v",
                "3",
                "-y",
                str(dst),
            ]
        )
        if p.returncode == 0 and dst.exists():
            added.append((t, dst, -1))  # motion -1: sampled, not scene-detected
    if added:
        sys.stderr.write(
            f"[coverage] dedup left {len(wanted)} stretch(es) over "
            f"{MAX_UNOBSERVED_S:.0f}s unobserved; added {len(added)} sampled frame(s)\n"
        )
    return sorted(kept + added, key=lambda k: k[0])


def ocr_frames(frames: list[Path]) -> dict[str, str]:
    """OCR is best-effort: a missing tesseract degrades to frames-without-text,
    it never sinks the run."""
    if not shutil.which("tesseract"):
        sys.stderr.write(
            "[ocr] tesseract not found — continuing without on-screen text. "
            "Install with `brew install tesseract` to enable it.\n"
        )
        return {}
    texts = {}
    for f in frames:
        p = run(["tesseract", str(f), "stdout", "--psm", "6"])
        if p.returncode == 0:
            txt = " ".join(p.stdout.decode("utf-8", "replace").split())
            if txt:
                texts[f.name] = txt
    return texts


def transcript_span(segments: list[dict], start_ms: int, end_ms: int) -> str:
    """The speech covering a shot. This is what makes a figure caption free:
    no per-frame vision call is needed to say what a slide is about."""
    hits = [
        s["text"]
        for s in segments
        if s.get("end_ms", 0) > start_ms and s.get("start_ms", 0) < end_ms
    ]
    return " ".join(" ".join(hits).split())


# --------------------------------------------------------------------------- #
def cmd_extract(args) -> int:
    ffmpeg = need("ffmpeg", "Install it: `brew install ffmpeg` or `sudo apt install ffmpeg`.")
    src = Path(args.source).expanduser()
    if not src.is_file():
        die(
            "not_found",
            f"{args.source} is not a file.",
            "video-keyframes needs a LOCAL media file. Download the video first "
            "(e.g. `yt-dlp -o video.mp4 <url>`), then point this script at it.",
        )
    out: Path = args.out
    tl = out / "timeline.json"
    if tl.exists() and not args.force:
        die("exists", f"{tl} already exists.", "Pass --force to overwrite.")
    frames_dir = out / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)

    duration = probe_duration(str(src))
    start, end = parse_time(args.start), parse_time(args.end)
    window = (end or duration) - (start or 0.0)
    target = args.max_frames or frame_budget(window)

    scores = collect_scene_scores(ffmpeg, str(src), start, end)
    thr, confidence = threshold_from_scores(scores, target)
    cands = scene_extract(ffmpeg, str(src), frames_dir, thr, start, end)
    strategy = "scene"
    if scene_coverage_poor(cands, start or 0.0, end or duration):
        sys.stderr.write(
            f"[strategy] scene detection left long unobserved stretches at "
            f"threshold {thr} ({len(cands)} frame(s)); adding interval sampling\n"
        )
        for _, f in cands:
            f.unlink(missing_ok=True)
        cands = interval_extract(ffmpeg, str(src), frames_dir, duration, target, start, end)
        strategy = "interval"

    # Pin evenly spaced frames so luminance-blind dedup cannot erase a flow.
    # This applies to BOTH strategies: dHash sees only brightness, so a screen
    # whose only change is colour hashes identically and would collapse to one
    # frame — and interval frames were deliberately chosen in the first place.
    pinned: set[int] = set()
    if len(cands) > 2:
        stride = max(1, len(cands) // PIN_COUNT)
        pinned = set(range(0, len(cands), stride))
    kept, dropped = dedupe(ffmpeg, cands, pinned)

    if len(kept) > target:
        stride = len(kept) / target
        kept = [kept[min(len(kept) - 1, round(i * stride))] for i in range(target)]
    # Budget trimming ALSO opens gaps, so fill after it, not before.
    kept = fill_gaps(ffmpeg, str(src), frames_dir, kept, start or 0.0, end or duration, target)
    keep_paths = {p for _, p, _ in kept}
    for f in dropped + [p for _, p in cands if p not in keep_paths]:
        f.unlink(missing_ok=True)

    ocr = {} if args.no_ocr else ocr_frames([p for _, p, _ in kept])

    segments = []
    if args.transcript and Path(args.transcript).is_file():
        segments = json.loads(Path(args.transcript).read_text()).get("segments", [])

    shots = []
    for i, (t, path, motion) in enumerate(kept):
        nxt = kept[i + 1][0] if i + 1 < len(kept) else (end or duration)
        shots.append(
            {
                "index": i,
                "start_ms": int(t * 1000),
                "end_ms": int(nxt * 1000),
                "start": fmt_time(t),
                "frame": str(path.relative_to(out)),
                "ocr": ocr.get(path.name, ""),
                "motion": motion,
                "speech": transcript_span(segments, int(t * 1000), int(nxt * 1000)),
            }
        )

    digest = {
        "source": str(src),
        "duration_s": duration,
        "strategy": strategy,
        "scene_threshold": thr,
        "calibration_confidence": round(confidence, 3),
        "frames_extracted": len(cands),
        "frames_kept": len(kept),
        "frame_budget": target,
        "ocr": bool(ocr),
        "shots": shots,
    }
    tl.write_text(json.dumps(digest, indent=2, ensure_ascii=False))
    print(
        json.dumps(
            {k: v for k, v in digest.items() if k != "shots"}
            | {"timeline": str(tl), "shots": len(shots)},
            indent=2,
        )
    )
    return 0


def cmd_frame_at(args) -> int:
    ffmpeg = need("ffmpeg", "Install it: `brew install ffmpeg`.")
    out: Path = args.out
    (out / "frames").mkdir(parents=True, exist_ok=True)
    results = []
    for raw in args.timestamps:
        t = parse_time(raw)
        dst = out / "frames" / f"at_{int(t * 1000):08d}.jpg"
        p = run(
            [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-ss",
                f"{t}",
                "-i",
                args.source,
                "-frames:v",
                "1",
                "-q:v",
                "2",
                "-y",
                str(dst),
            ]
        )
        if p.returncode == 0 and dst.exists():
            results.append({"t": fmt_time(t), "start_ms": int(t * 1000), "frame": str(dst)})
    if not results:
        die(
            "frame_extract_failed",
            "No frame could be extracted at those timestamps.",
            "Check the timestamps fall inside the video's duration.",
        )
    print(json.dumps(results, indent=2))
    return 0


def cmd_clip(args) -> int:
    ffmpeg = need("ffmpeg", "Install it: `brew install ffmpeg`.")
    t0, t1 = parse_time(args.t0), parse_time(args.t1)
    out: Path = args.out
    d = out / "frames"
    d.mkdir(parents=True, exist_ok=True)
    p = run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            f"{t0}",
            "-i",
            args.source,
            "-t",
            f"{max(0.1, t1 - t0)}",
            "-vf",
            f"fps={args.fps}",
            "-q:v",
            "3",
            "-y",
            str(d / f"clip_{int(t0)}_%03d.jpg"),
        ]
    )
    files = sorted(d.glob(f"clip_{int(t0)}_*.jpg"))
    if p.returncode != 0 or not files:
        die(
            "clip_failed",
            "Could not sample that range.",
            "Check t0 < t1 and both are inside the video.",
        )
    step = 1.0 / args.fps
    print(
        json.dumps(
            [
                {
                    "t": fmt_time(t0 + i * step),
                    "start_ms": int((t0 + i * step) * 1000),
                    "frame": str(f),
                }
                for i, f in enumerate(files)
            ],
            indent=2,
        )
    )
    return 0


def self_check() -> int:
    assert parse_time("90") == 90 and parse_time("1:30") == 90 and parse_time("1:00:00") == 3600
    assert fmt_time(3661) == "01:01:01"

    a = dhash_bits(bytes([0, 10] * 36))
    b = dhash_bits(bytes([0, 10] * 36))
    c = dhash_bits(bytes([10, 0] * 36))
    assert a == b and hamming(a, b) == 0
    assert hamming(a, c) > DEDUP_HAMMING, "inverted gradients must not dedupe together"

    # Real measured distribution from a 3-slide deck: two transitions at
    # ~0.02-0.03 against a noise floor of ~1e-5. A threshold above 0.03 finds
    # NOTHING here, which is why the inherited 0.05 floor had to go.
    slides = [0.023, 0.019] + [0.00001] * 8
    thr, _ = threshold_from_scores(slides, target=14)
    assert thr < 0.019, f"must sit below a real slide transition, got {thr}"
    assert sum(s > thr for s in slides) == 2, "must select exactly the two transitions"

    # A busy video must not blow the budget: the threshold rises to admit ~target.
    busy = [0.05 + i * 0.01 for i in range(60)]
    thr_busy, _ = threshold_from_scores(busy, target=10)
    assert 8 <= sum(s > thr_busy for s in busy) <= 20, sum(s > thr_busy for s in busy)

    # Degenerate input must not crash or return a nonsense threshold.
    assert threshold_from_scores([], 10)[0] == DEFAULT_THRESHOLD
    assert MIN_THRESHOLD <= threshold_from_scores([0.0] * 20, 5)[0] <= MAX_THRESHOLD

    assert frame_budget(20) == 20 and frame_budget(45) == 40
    assert frame_budget(4000) == FRAME_CAP
    assert frame_budget(3) == min(12, round(MAX_FPS * 3)), "must respect the 2 fps cap"
    assert all(frame_budget(d) <= FRAME_CAP for d in (1, 30, 60, 180, 600, 7200))

    # Coverage, not frame count, decides the interval fallback.
    short3 = [(0.0, Path("a")), (4.7, Path("b")), (9.4, Path("c"))]
    assert not scene_coverage_poor(short3, 0.0, 14.1), "3 frames fully cover a 14s clip"
    assert scene_coverage_poor([(0.0, Path("a")), (5.0, Path("b"))], 0.0, 7200.0), (
        "2 frames in a 2h lecture is a blind spot"
    )
    assert scene_coverage_poor([(0.0, Path("a"))], 0.0, 10.0), "one frame is never enough"

    # Gap-fill targets: a 280s hole under a 120s limit needs 2 interior samples.
    marks = [0.0, 207.0, 487.0, 816.0]
    interior = []
    for a, b in itertools.pairwise(marks):
        span = b - a
        if span > MAX_UNOBSERVED_S:
            n = int(span // MAX_UNOBSERVED_S)
            step = span / (n + 1)
            interior.extend(a + step * (i + 1) for i in range(n))
    assert len(interior) == 5, interior
    filled = sorted(marks + interior)
    assert max(b - a for a, b in itertools.pairwise(filled)) <= MAX_UNOBSERVED_S

    segs = [
        {"start_ms": 0, "end_ms": 2000, "text": "hello"},
        {"start_ms": 2000, "end_ms": 4000, "text": "world"},
        {"start_ms": 9000, "end_ms": 9500, "text": "later"},
    ]
    assert transcript_span(segs, 0, 3000) == "hello world"
    assert transcript_span(segs, 4000, 5000) == ""
    print("self-check OK")
    return 0


def main() -> int:
    # Explicit dispatch: argparse subparsers cannot coexist with a bare
    # positional for the default (extract) form.
    argv = sys.argv[1:]
    mode = argv[0] if argv and not argv[0].startswith("-") else None

    if "--self-check" in argv:
        return self_check()

    if mode == "frame-at":
        ap = argparse.ArgumentParser(prog="extract_frames.py frame-at")
        ap.add_argument("source")
        ap.add_argument("timestamps", nargs="+")
        ap.add_argument("--out", type=Path, required=True)
        return cmd_frame_at(ap.parse_args(argv[1:]))

    if mode == "clip":
        ap = argparse.ArgumentParser(prog="extract_frames.py clip")
        ap.add_argument("source")
        ap.add_argument("t0")
        ap.add_argument("t1")
        ap.add_argument("--out", type=Path, required=True)
        ap.add_argument("--fps", type=float, default=2.0)
        return cmd_clip(ap.parse_args(argv[1:]))

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("source", nargs="?", help="local media file")
    ap.add_argument("--out", type=Path, help="output directory")
    ap.add_argument("--transcript", help="transcript.json to align speech spans with")
    ap.add_argument("--max-frames", type=int, help="override the duration-derived budget")
    ap.add_argument("--start", help="window start (SS|MM:SS|HH:MM:SS)")
    ap.add_argument("--end", help="window end")
    ap.add_argument("--no-ocr", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--self-check", action="store_true")
    args = ap.parse_args(argv[1:] if mode == "extract" else argv)
    if not args.source or not args.out:
        ap.error("source and --out are required")
    return cmd_extract(args)


if __name__ == "__main__":
    sys.exit(main())
