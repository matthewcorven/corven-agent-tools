# Frame selection

Reference for `video-keyframes`. Read when extraction returned too many frames,
too few, or the wrong ones.

**Contents:** [Scene scores](#what-ffmpegs-scene-score-actually-is) ·
[Threshold](#choosing-the-threshold) · [Coverage fallback](#the-coverage-fallback) ·
[Budget](#frame-budget) · [Dedup](#perceptual-dedup-and-its-blind-spot) ·
[ffmpeg 8](#ffmpeg-8-notes) · [OCR](#ocr)

## What ffmpeg's `scene` score actually is

`select='gt(scene,T)'` compares consecutive frames and emits a score in `[0,1]`.
It is widely documented as if it were "fraction of the image that changed". It
is not, and the difference matters.

Measured with `metadata=print` on a plain three-slide deck (light background,
dark text, hard cuts between wholly different slides):

| Moment | `lavfi.scene_score` |
| --- | --- |
| within a static slide | ~0.00001 |
| **full slide transition** | **0.019 – 0.028** |

A full slide change scores under 0.03. The folklore defaults — `gt(scene,0.4)`,
or the genre tables that suggest 0.2–0.3 for lectures — detect **nothing** on
the single most common slide-deck case. Even a 0.05 floor is too high.

Inspect any video's real distribution before blaming the extractor:

```bash
ffmpeg -hide_banner -i video.mp4 -vf "scale=160:-2,select='gt(scene,0)',metadata=print" \
  -an -f null - 2>&1 | grep -o "scene_score=[0-9.]*" | sort -t= -k2 -g | tail
```

## Choosing the threshold

Rather than assume a content type, the script measures the distribution and
picks the threshold that admits roughly the frame budget:

1. one cheap pass at `scale=160:-2` collects every scene score (a 14 s clip
   costs about 0.06 s; downscaling barely moves the scores);
2. sort descending, take the `target`-th largest, multiply by 0.9;
3. clamp to `[0.004, 1.0]`.

No separate "noise floor" is applied on top. A median-based floor was tried and
removed: on busy footage it exceeded the candidate, pinned the threshold to the
cap, and selected everything. When a video genuinely is static, the candidate
falls below the minimum, almost nothing is selected, and the coverage check
below takes over — which is the correct outcome.

## The coverage fallback

Frame *count* is the wrong test for "did scene detection work". Three frames is
complete coverage of a three-scene 14-second clip and near-nothing in a two-hour
lecture.

The test is whether any stretch went **unobserved**: if the largest gap between
consecutive selected frames (including the window edges) exceeds 120 seconds,
the script discards the scene result and switches to interval sampling. The
strategy that ran is recorded in `timeline.json` and printed on stderr, because
it tells the reader how complete the coverage is.

## Frame budget

Duration-tiered, with a universal 2 fps cap and a hard cap of 100 frames:

| Duration | Target |
| --- | --- |
| ≤ 30 s | `max(12, round(duration))` |
| ≤ 60 s | 40 |
| ≤ 180 s | 60 |
| ≤ 600 s | 80 |
| longer | 100 |

720p is the download ceiling: it answers essentially every question a video can
answer, while 4K costs minutes of transfer and gigabytes of disk to answer it no
better.

This is the *extraction* budget — how many frames land on disk. It is not the
*reading* budget: a consumer should still open only the few frames it needs.

## Perceptual dedup and its blind spot

Frames are hashed with a 64-bit dHash (9×8 grayscale, comparing adjacent
pixels), computed by ffmpeg — no Python imaging dependency. A frame is kept only
if its Hamming distance to every kept frame exceeds 6.

**The blind spot:** dHash is luminance-only. A blue button turning grey, a total
turning red, a green check appearing on a white background — all can be
invisible to it. Worst case, an entire UI flow collapses to one frame. Verified
here: three solid frames in navy, dark green and maroon all hash identically.

Mitigation: up to 8 evenly spaced frames are **pinned** and never dropped by
dedup, in both the scene and interval strategies. Watching a flow means keeping
the in-between. If a flow still lost its middle, re-sample it with `clip`.

## ffmpeg 8 notes

- **`-vsync` was removed.** On ffmpeg 8.0.1 `-vsync vfr` exits with
  `Invalid argument`; the help text lists it as "deprecated, use -fps_mode".
  The script probes `ffmpeg -h long` for `-fps_mode` and only falls back to
  `-vsync` on older builds. Do not hardcode either.
- **`select` never fires on frame 0**, so `eq(n\,0)` is added to the expression
  to keep the opening frame. Without it the title card is silently lost.
- **One decode, not two.** `select=...,showinfo` writes the JPEGs and prints
  each `pts_time` to stderr in the same pass; a second pass just to collect
  timestamps is wasted work.
- Some builds lack `drawtext` (no libfreetype). Do not rely on it.

## OCR

Tesseract runs only on kept frames, with `--psm 6`. It is best-effort: a missing
binary degrades to frames-without-text and never sinks the run.

Treat OCR output as a **search index, not a transcript of the screen**. Observed
on a clean 1280×720 synthetic slide: `--maxfail` came back as `—-maxfail`. Double
hyphens become em dashes, and `l`/`1`/`I` and `O`/`0` interchange freely. Any
command that will be reproduced must be read off the image itself.
