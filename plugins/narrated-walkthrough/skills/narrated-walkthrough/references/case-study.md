# Case study — the briefing that produced this skill

## Origin

A user asked for an executive summary of a completed analysis session. A
compressed, jargon-heavy prose answer was rejected as "too compressed and
jargon-heavy"; the follow-up request was "guide me through using visuals". A
diagram-first briefing in an editor canvas worked. The next request was for
narration synchronized to computer-use on a pre-planned timeline, using local
audio models.

That is the shape of the demand this skill serves: **the same findings, delivered
as something you can watch and listen to, without losing the rigor of the
numbers.**

## What was built

| Artifact | Detail |
|---|---|
| `timeline.json` | 8 scenes, 25 caption cues, 1 disposition block |
| `audio/*.wav` | 8 clips, 44.1 kHz mono, 132 s total |
| `slides.html` | 8 inline mermaid diagrams, one audio element, scrubber, ask panel |
| Builder | ~460 lines, stdlib only, `--validate` / `--serve` / `--stop` / `--only` |

Narration: `Ming-3.0-tts-tiny-4bit` on a local MLX server, voice `default`.
Measured throughput **3.7× realtime** — 132 s of speech rendered in ~34 s.
Voice probes confirmed `default`, `af_heart` and `alloy` produce different bytes
and different durations.

## Defects found and fixed (in order)

1. **Only the first scene was audible.** One `Audio` element, `src` swapped, and
   `currentTime` read from it — every seek clamped to the first clip's length.
   Fixed by making the wall clock authoritative and slaving the element.
2. **`seek()` before metadata clamped to zero.** Fixed with a `pending` target
   applied on `loadedmetadata`.
3. **Captions lagged one scene** because they were updated only on scene change.
   Fixed by evaluating the cue list every frame.
4. **A broadcast sign-off on a deck that ended on open choices.** The user's
   correction: when the disposition is open, narration, slide text and the chat
   reply must all invite a response. Fixed by rewriting the closing scene and
   adding the validator rule that now prevents recurrence.

Defect 4 is the reason `disposition` and `--validate` exist. The others are the
reason the clock model is derived rather than declared.

## Measurements that made the narration honest

The deck narrated findings that were themselves measurements, which is why it
held up:

- 20 rounds × 2 tools: **371 → 171 events**, **2 465 → 695 ms** median.
- Upstream suite at 1.28.0: **398/398** green, one-line csproj change.
- A silent hazard: **400 s**, 134 polls, zero warnings — the run *succeeded*
  with an empty result.
- Unclassifiable provider error: **5 attempts in 30 s, no terminal state**.

Rule that follows: a narrated walkthrough may simplify language, never numbers.
If a figure appears on a slide it must be traceable to a command in the source
document.

## Timeline of the delivery

| Time (UTC) | Event |
|---|---|
| 00:41 | Prose summary delivered, judged too compressed |
| 00:47 | Visual briefing opened in an editor canvas |
| 00:53 | Local TTS endpoint discovered; throughput measured |
| 00:56 | First deck: 8 scenes, 126.8 s |
| 01:00 | Sync defects 1–3 found and fixed |
| 01:04 | Sign-off correction → disposition contract + validator |
| 01:05 | Deck rebuilt at 132.0 s, ask panel verified |

Roughly 25 minutes from "guide me through using visuals" to a synchronized,
locally narrated deck with an enforced invitation contract.

## Limits

- No video export; the deck is a live page, not a file for external publishing.
- The system-voice fallback cannot seek within a scene.
- Audio quality and audibility were never verified by the agent — only by the
  human. That boundary is permanent and is stated in the skill's guardrails.
- Computer-use automation of the host's own chat application is blocked by
  policy; the deck therefore drives itself and is opened through the harness's
  browser surface.
