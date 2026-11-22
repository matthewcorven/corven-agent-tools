---
name: narrated-walkthrough
description: >-
  Turn findings, a change set, a proposal or a session outcome into a
  narrated, diagram-driven walkthrough whose spoken audio, slides, captions
  and scrubber all follow one pre-planned timeline, with narration
  synthesized by a local text-to-speech model. USE FOR: narrate this
  visually, walk me through it with audio, synchronized narration and
  slides, voice-over deck, briefing I can listen to, present changes out
  loud, explain a session with diagrams and speech, local TTS presentation.
  DO NOT USE FOR: authoring a Marp/PDF proposal deck (use visual-proposal),
  explaining a git diff as slides (use change-learn), owning browser or
  Playwright verification standards (use playwright-report-retrieval),
  performing the code review itself (use code-review or branch-diff), or
  producing a video file for external publishing.
---

# Narrated walkthrough

**One plan, one clock.** Every visual, caption and spoken sentence is positioned
on a single timeline that is computed *after* the audio exists. Nothing is
estimated; nothing is re-timed at playback. That is the whole trick, and every
rule below exists to protect it.

## The plan contract

[`schemas/narrated-timeline.v1.json`](schemas/narrated-timeline.v1.json) is the
executable shape: `title`, `scenes[]`, optional `tts`, optional `disposition`,
and two **derived** fields (`start`, `total_duration`) that are permitted but
never required. Closed at every level, so a speculative field fails immediately.

The `$id` is the repo-relative `narrated-timeline/v1`, deliberately not a URL — a
dead `$id` is worse than none, because consumers try to fetch it.

`start` and `total_duration` are written by the builder and asserted by the test
suite (`start[n] == Σ duration[0..n-1]`, tolerance 1e-6). They are absent from
`required` on purpose: requiring them would invite an author to supply them, and
the derived-clock invariant would die quietly.

## The five parts

| Part | Artifact | Owner |
|---|---|---|
| 1. Plan | `timeline.json` — scenes, narration, captions, diagrams, disposition | the author |
| 2. Voice | `audio/*.wav`, one clip per scene, local TTS | `scripts/build_narration.py` |
| 3. Clock | measured durations → `start` offsets → `total_duration` | `scripts/build_narration.py` |
| 4. Deck | `slides.html` — diagrams, captions, scrubber, one audio element | `scripts/build_narration.py` |
| 5. Invitation | `disposition` block + closing ask panel | the author, enforced by the validator |

Run `scripts/build_narration.py --validate` before every build. It refuses a plan
that leaves work open without asking the listener to respond, or that ends on a
broadcast sign-off.

## Why the clock is derived, not declared

A hand-written `start` for scene 5 is wrong the moment scene 4's sentence is
re-recorded. The builder instead:

1. synthesizes any missing clip,
2. measures it from the clip's WAV header (portable; no external audio tool),
3. sets `start = Σ previous durations`,
4. writes the offsets back into the plan.

Because each clip then fills its slot exactly, the deck can jump anywhere —
a scene boundary, the middle of the deck, a restart — and land on the right
clip without drift. Verified behaviour: seeking to 70.2 s lands mid-scene with
the matching caption; playback crosses a boundary and advances on time.

## Delivery contract

- **Audio is pre-generated.** Playback is exact because nothing is synthesized
  live. Never substitute live speech for the recorded clips except through the
  explicit **Use system voice** fallback.
- **One audio element follows the timeline**, not the reverse. The timeline is
  authoritative; the element is slaved to it and re-seeked when it drifts more
  than ~0.45 s.
- **One claim per scene.** A scene has a title, one dominant diagram, and
  captions that read as sentences. Details go in the chat reply, not the slide.
- **Captions are evenly distributed** across each scene's measured audio, so
  they stay aligned when a clip is re-recorded.
- **The deck fetches nothing.** Mermaid is a vendored, version-pinned, hashed
  asset that the builder inlines into the HTML, so there is no sibling file to
  lose and no request to make. The builder refuses to run without a
  `mermaid_source`; `--cdn` is the only way to reach the network and it pins an
  exact version rather than floating a major, because a walkthrough recorded
  today must render the same in six months.

## Disposition: when the walkthrough ends on open work

If the outcome leaves choices, unknowns, or a decision with the listener, the
presentation is *paused*, not finished. Then all three surfaces must invite a
reply, consistently:

1. **Narration** names the invitation out loud ("tell me which one you want —
   type it, or use the microphone").
2. **Slide text** carries it in the final caption, and the `disposition` block
   renders an ask panel with the options.
3. **The chat reply that accompanies the deck** asks the same question in the
   same voice, offers the same options, and says explicitly that no answer is a
   valid answer.

Never end on "thanks for watching" when something is open — it closes a door
that is actually still ajar. The validator fails the plan for it.

`disposition` shape:

```json
"disposition": {
  "kind": "choices_and_unknowns",
  "prompt": "Which way do you want to go?",
  "options": ["Fix the retry bug", "Ship the fast tool calls", "Park it for now"],
  "note": "Any answer works, including none. Type it in the chat, or use the microphone."
}
```

## Workflow

1. **Decide the arc before writing prose.** Six to nine scenes: what the
   question was, what the evidence showed, what changed, what shipped, what is
   still open, what the listener can choose. A walkthrough that ends on a
   decision needs the last two.
2. **Write narration for the ear.** Short sentences, contractions, no
   semicolons, no file paths, no identifiers. Read it aloud; if a clause needs a
   breath, split it.
3. **Write captions as the same sentences, chunked.** They are not a summary of
   the narration — they are the narration, split for reading.
4. **Give every scene one diagram.** Mermaid flowchart or timeline; label the
   nodes with the claim, not the mechanism. Colour by verdict (green = keep,
   amber = open, red = retired) and keep the palette consistent across scenes.
5. **Choose the voice, then measure.** Synthesize, listen, and only then let the
   builder fix the clock. Changing a voice changes every duration — that is
   expected, and it is why the plan stores no hand-written offsets.
6. **Verify with the page, not with the plan.** Use the browser surface's script
   hook (`window.state()`, `window.seek(t)`) to prove playback advances, a
   boundary is crossed, and the ask panel is visible. A plan that validates is
   not a deck that plays.
7. **Leave the deck running** through a supported detached service or the
   harness's own browser surface; report the URL.

## Local speech

**The builder ships no defaults.** `endpoint`, `model` and `voice` are required
configuration; a missing one fails the build with the field named and the
`curl -s <host>/v1/models` hint, because a silent fallback would aim requests at
whatever happens to be listening on a port that belongs to someone else.

Any OpenAI-compatible endpoint works, so a local server is a configuration
detail rather than a code path:

```json
"tts": {
  "endpoint": "http://127.0.0.1:18001/v1/audio/speech",
  "model": "Ming-3.0-tts-tiny-4bit",
  "voice": "default"
}
```

- Discover models with `GET <host>/v1/models` and look for `audio_tts`.
- Voices are server-defined. Probe a few and keep the one you like; they produce
  genuinely different audio and different lengths.
- Measured throughput on an MLX host: ~3.7× realtime (127 s of narration in
  33 s), so a two-minute briefing costs well under a minute to produce.
- `--only <scene> --voice <name>` re-voices one scene and re-derives the clock.
- `--no-audio` renders from the durations already recorded in the plan, so a
  reviewer can see the deck without running a model.
- If the endpoint is down, fail loudly with the URL and model in the message;
  never silently fall back to a different voice.

Requests pin `response_format: "wav"`, and the builder measures length from the
RIFF header. A non-WAV payload means the server ignored that field — report it as
a server contract breach instead of adding a platform-specific probe.

## Verification protocol

| Check | How | Pass condition |
|---|---|---|
| Plan contract | `--validate` | exit 0 |
| No shipped defaults | omit `tts.endpoint` | exit 2, field named |
| Clips exist and measure | build output | one duration per scene |
| Clock is consistent | `start[n] == Σ durations[0..n-1]` | exact, to 3 dp |
| Deck renders | browser `state()` | `scenes == N`, diagrams present |
| Playback advances | `seek(t)`, wait, `state()` | `t` increases, scene and caption track it |
| Boundary crossing | seek 1 s before a boundary | next scene active, correct caption |
| Invitation present | `state().askShown` | `true` when `disposition` is set |
| Audio audible | user confirms | never claim this from code |

## Known traps

| Symptom | Cause | Correction |
|---|---|---|
| Only the first scene is audible | one `Audio` element whose `src` was swapped while a stale `duration`/`currentTime` was assumed | slave the element to the timeline; re-seek on `loadedmetadata` |
| `play()` rejects with `NotAllowedError` | autoplay policy | require a click; keep a visible Play button |
| Captions lag one scene | caption updated only inside the scene-change branch | update the caption every frame from the cue list |
| Everything shifts after a re-record | hand-written offsets | never hand-write `start`; let the builder derive it |
| Diagram renders blank | mermaid `htmlLabels` in an SVG-as-image path | render inline in the DOM, or set `htmlLabels: false` when rasterising |
| Long narration sounds rushed | a single clip carrying three ideas | split into scenes; one claim per clip |

## Guardrails

- The walkthrough **reports**; it does not authorize. A narrated deck is not
  approval to start the work it describes.
- Do not narrate secrets, credentials, tokens, customer data, or file paths that
  leak a private layout.
- Do not claim audio quality, audibility or "it sounds good" — you cannot hear
  it. Verify timing and rendering; ask the human about the voice.
- Keep the plan in the target repository's own documentation output, not in a
  temp directory, so the deck can be rebuilt.
