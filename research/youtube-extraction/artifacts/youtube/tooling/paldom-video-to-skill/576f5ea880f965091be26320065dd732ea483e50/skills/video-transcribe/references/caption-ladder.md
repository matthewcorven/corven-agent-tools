# Caption ladder and quality gate

Reference for `video-transcript`. Read when a transcript came out wrong, when
choosing a language, or when tuning the quality thresholds.

**Contents:** [Track selection](#track-selection) · [Why not translations](#why-auto-translations-are-refused) ·
[Parsing](#parsing) · [ASR](#asr-backends) · [Quality gate](#the-quality-gate) ·
[yt-dlp failures](#yt-dlp-failure-modes)

## Track selection

Order, best first:

1. **Manual / creator captions** (`--write-subs`). Human punctuation, casing,
   speaker labels. Always preferred.
2. **Auto-captions in the video's own language** (`--write-auto-subs`).
   Unpunctuated and uppercase-ish, but genuinely time-aligned.
3. **Local ASR.** Used for local files and for videos with no caption track.

### Determining the video's language

`yt-dlp --dump-json` exposes `language`, but it is frequently `null` — the
classic `jNQXAC9IVRw` reports `NA`. The fallback is structural: YouTube names
auto-translations `<target>-<source>`, so the source subtag shared across the
translation list *is* the original language. A video whose auto-caption list is
`en`, `ab-en`, `aa-en`, `af-en`… is an English video.

### Code shapes

| Code | Meaning | Accepted |
| --- | --- | --- |
| `en` | plain track in that language | yes |
| `en-orig` | untranslated auto track | yes |
| `en-US`, `zh-Hans`, `pt-BR` | regional / script variant | yes |
| `en-uYU-mmqFLq8` | a real CC track with an opaque id | yes (manual only) |
| `ab-en` | Abkhazian **from** English — a machine translation | **no** |

The opaque-id form is why the translation filter must not run on manual tracks:
`en-uYU-mmqFLq8` splits into `en` + `uYU-mmqFLq8`, which looks like a
translation but is a creator-uploaded CC1 track. Manual tracks are never
auto-translations, so they are filtered on language prefix only.

## Why auto-translations are refused

An auto-translation is a machine translation *of a machine transcription*. Two
error rates compound, and the result is confidently wrong in a way that reads
fluently — the worst failure shape for a knowledge note. If the user genuinely
wants another language, transcribe in the original and translate as a separate,
labelled step.

## Parsing

`json3` is preferred when offered: `events[].tStartMs` / `dDurationMs` /
`segs[].utf8` give clean per-event timing. It is not always available — plenty
of original tracks offer only `vtt` while their translations offer json3.

VTT needs two clean-ups:

- **inline word-timing tags** (`<00:00:01.500>`, `<c>`) are stripped;
- **rolling duplication** — each cue repeats the tail of the previous one, which
  inflates a transcript two to three times and wrecks any repetition metric.
  Only newly added text is kept.

## ASR backends

| Backend | Best on | Notes |
| --- | --- | --- |
| whisper.cpp (`whisper-cli`) | Apple Silicon | Metal + Core ML; set `WHISPER_CPP_MODEL` to a `ggml-*.bin` |
| faster-whisper | NVIDIA GPU, or any CPU | CTranslate2; **CPU-only on Mac** — no Metal backend |

Both use identical Whisper weights, so accuracy is the same; only runtime
differs. `large-v3-turbo` is roughly 5x faster than `large-v3` with near-equal
accuracy, but was not trained for translation.

**VAD is not optional.** Whisper's decoder keeps emitting tokens over silence
and loops phrases ("thanks for watching", "Subtitles by the Amara.org
community"). `vad_filter=True` removes most of it. Silence hallucination is the
single largest cause of bad ASR transcripts.

Segment timestamps drift on long audio. Caption timings are already aligned and
should be preferred; word-accurate ASR timing needs forced alignment (WhisperX),
which is out of scope here.

## The quality gate

Deterministic, no model. Thresholds scale with duration and with how the
transcript was produced (`kind_factor`: manual 0.7, auto 0.85, asr 1.0).

```
expected_min_chars = clamp(round(duration_min * 20 * kind_factor), 20, 400)
hard_min_chars     = max(3, round(expected_min * 0.2))
warning_min_cpm    = 12.0 * kind_factor
repetition         fail >= 0.93   warn >= 0.80   (>= 400 effective chars)
timed coverage     fail <  0.10   warn <  0.35
large_segment_gap  max_gap > max(30s, 0.4 * duration)
```

### Repetition is measured per window, not per transcript

A whole-transcript distinct-4-gram ratio **saturates with length**. Measured on
synthetic prose: 0.48 at one minute, 0.87 at fifteen, 0.96 at sixty. A global
0.85 threshold therefore rejects every video longer than about fifteen minutes —
the metric is measuring duration, not quality.

Measured per 1000-character window it is flat in length:

| Content | Windowed ratio |
| --- | --- |
| healthy prose, 1 min to 2 h | 0.48 – 0.53 |
| filler-heavy, small-vocabulary speech | 0.71 |
| legitimate enumerated procedure ("step 1… step 2…") | 0.86 |
| un-deduplicated rolling captions | 0.97 |
| Whisper looping over silence | 0.98 |

The fail line sits at **0.93**, not the 0.85 this was ported from: an enumerated
tutorial — exactly the content this repo targets — measures 0.86 and must not be
rejected. It warns instead.

## yt-dlp failure modes

**Bot check** (`Sign in to confirm you're not a bot`). Fix order: `yt-dlp -U`
first, since distro packages lag and trigger it on their own; then
`--cookies-from-browser chrome`, exporting from a private window and closing it
without logging out (logout kills the session server-side). Cookies say *who*
you are, not *where* the request came from, which is why they often do not help:
since 2024 the web client needs a proof-of-origin token generated by YouTube's
own JavaScript, which yt-dlp cannot manufacture. Datacenter and VPN ranges get
the strictest treatment and no flag fixes that.

**HTTP 429 on captions.** Rate limiting, easily triggered by running several
extractions back to back. Note that YouTube throttles the caption (`timedtext`)
endpoint **independently of media download** — captions can return 429 while the
audio stream downloads fine, which is why a caption 429 is reported and recorded
rather than treated as fatal.

Once an IP is blocked it stays blocked, and it is not a rate you can back off
from. Measured on one throttled connection, all of these still returned 429:

| Attempted remedy | Result |
| --- | --- |
| plain retry | 429 |
| `--sleep-subtitles 3 --retries 5 --retry-sleep 10` | 429 |
| `youtube-transcript-api` (different HTTP path) | `IpBlocked` |
| `player_client=android` / `ios` / `tv` / `web_safari` / `mweb` | 429 / 403 / no captions |
| **`--cookies-from-browser chrome`** (99 cookies extracted and sent) | **429** |

The cookie result is worth stating plainly because it contradicts the common
advice: cookies are widely recommended for 429, and on a caption-throttled IP
they did not help. Cookies establish *who* you are, not *where* the request came
from, and this block is on the origin. (Caveat: it was not confirmed whether that
browser profile held a logged-in YouTube session, so an authenticated cookie jar
may still behave differently. Try it, but do not count on it.)

What is left, in order: a different network (mobile hotspot, or off the VPN —
datacenter ranges are treated worst); or wait hours. The ASR rung keeps working
throughout, so a transcript is still obtainable meanwhile — which is exactly why
a caption 429 is recorded rather than treated as fatal.

### The YouTube Data API is not a way around this

A Data API **key** cannot fetch caption text for a video you do not own:

- `captions.download` "requires the user to have permission to edit the video"
  and needs OAuth scope `youtube.force-ssl` — an API key alone is rejected.
- `captions.list` also requires OAuth, and returns track metadata only: "the API
  response does not contain the actual captions".

So an API key buys nothing for third-party transcripts. It can serve *metadata*
(`videos.list`, 1 quota unit, returns title/channel/duration/`contentDetails.caption`),
but metadata was never the bottleneck — `--dump-json` keeps working while
captions are throttled, and if extraction is blocked outright there is no media
to transcribe either. Not worth the credential.

The one case where the Data API genuinely helps: **your own uploads.** With OAuth
as the channel owner, `captions.download` is a supported, ToS-clean path that
never touches `timedtext`. Worth using for your own content; useless for
everyone else's.

**Missing JS runtime.** Recent yt-dlp warns that YouTube extraction without a JS
runtime is deprecated and some formats may be missing. Install `deno` if
extraction degrades.

**Diagnostic:** `yt-dlp -F <url>`. A populated format table means the challenge
cleared; an empty or low-resolution-only table means extraction worked but
formats are gated — a proof-of-origin problem, not a bot-check one.
