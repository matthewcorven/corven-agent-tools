# narrated-walkthrough

A plugin for turning findings, a change set, a proposal or a session outcome into
a **narrated, diagram-driven walkthrough** whose spoken audio, slides, captions
and scrubber all follow one pre-planned timeline.

Narration is synthesized by a local text-to-speech model, so the deck is produced
without a cloud service and plays back exactly: nothing is synthesized at
viewing time, so there is nothing to drift.

## Why it exists

A prose summary compresses; a diagram shows; a walkthrough **explains**. The
difference is the clock: if the audio, the diagram and the caption are positioned
by three different mechanisms, they drift apart within a minute. Here one
derived clock drives all three.

It also carries a contract the other presentation skills do not: **when the work
is left open, the walkthrough asks for an answer** — in the narration, on the
slide, and in the chat reply that delivers it.

## What's inside

| Component | Path | Purpose |
| --- | --- | --- |
| `narrated-walkthrough` skill | `skills/narrated-walkthrough/SKILL.md` | The method, the clock model, the disposition contract, verification protocol and traps |
| Builder | `skills/narrated-walkthrough/scripts/build_narration.py` | Synthesizes missing clips, measures them, derives offsets, renders a single-file deck, serves it |
| Authoring method | `skills/narrated-walkthrough/references/authoring-method.md` | Scene budgeting, narration for the ear, diagram contracts, disposition honesty |
| Rendering + timing | `skills/narrated-walkthrough/references/rendering.md` | Why the clock is derived, seeking, captions, the ask panel, browser verification |
| Case study | `skills/narrated-walkthrough/references/case-study.md` | The session that produced it, the four defects found, the measurements it narrated |
| Golden example | `skills/narrated-walkthrough/examples/golden/` | Three scenes, runnable end to end |
| Schema | `skills/narrated-walkthrough/schemas/narrated-timeline.v1.json` | Executable plan contract, closed at every level |
| Vendored Mermaid | `skills/narrated-walkthrough/assets/` | Version-pinned, hashed, MIT; inlined into every deck |
| Tests | `tests/test_package.py` | Plan contract, disposition contract, clock invariants, schema conformance, package shape |

## Quick start

```sh
cd skills/narrated-walkthrough/examples/golden
python3 ../../scripts/build_narration.py --validate   # plan contract only
python3 ../../scripts/build_narration.py --serve      # synthesize, render, serve on :8765
```

Then open `http://127.0.0.1:8765/slides.html` and press Play.

To author a real one, copy the golden directory, replace `timeline.json`, and
delete any clip you want re-synthesized.

## The plan contract

`narrated-timeline/v1` is frozen to what the player actually consumes:
`title`, `scenes[]`, optional `tts`, optional `disposition`, plus the derived
`start` and `total_duration`. It is `additionalProperties: false` at every level,
so a speculative field fails immediately rather than lingering untested.

`start` and `total_duration` are permitted but never required — the builder
writes them and the tests assert the cumulative invariant. Requiring them would
invite someone to author them, which is exactly how a derived clock starts
drifting.

## The disposition contract

Set a `disposition` block when the walkthrough ends on choices or unknowns. The
validator then refuses to build unless the final scene actually invites a reply,
and refuses any narration that ends on a broadcast sign-off.

```json
"disposition": {
  "kind": "choices_and_unknowns",
  "prompt": "Which way do you want to go?",
  "options": ["Fix the bug", "Ship the feature", "Park it"],
  "note": "Any answer works, including none. Type it in the chat, or use the microphone."
}
```

The chat reply that accompanies the deck must ask the same question, offer the
same options, and say that no answer is acceptable.

## Local speech

**No defaults are shipped.** `endpoint`, `model` and `voice` are required plan
configuration; omit one and the build stops with the field named and the
`curl -s <host>/v1/models` hint. A silent fallback would aim requests at whatever
is listening on a port that belongs to someone else.

Any OpenAI-compatible `/v1/audio/speech` endpoint works. Requests pin
`response_format: "wav"` and length is measured from the RIFF header, so no
platform-specific audio tool is involved. Voices are server-defined; `--voice`
re-records a scene and re-derives every later offset.

## Offline by default

Mermaid is **vendored and inlined**, not fetched. `assets/mermaid.min.js.gz` is
version-pinned (12.3.0), MIT-licensed, hash-recorded in `assets/mermaid.VENDORED.md`,
and embedded directly into every deck — so a rendered walkthrough has no sibling
file to lose and makes no network requests. The builder refuses to run without a
`mermaid_source`; `--cdn` is the only path to the network, and it pins the same
exact version. A deck that silently pulls from a CDN cannot be verified as offline,
and an unpinned major means a walkthrough recorded today may render differently
later — the wrong shape for a frozen timeline.

## Build output

`audio/` and `slides.html` are generated, not committed — the same reasoning as
`visual-proposal`'s `evidence/`. `--no-audio` renders the deck from the durations
already recorded in the plan, so a reviewer can inspect layout, diagrams,
captions and the ask panel without running a model.

## Boundaries

- **Not** a Marp/PDF proposal generator — that is `visual-proposal`.
- **Not** a diff explainer — that is `change-learn`.
- The deck reports; it does not authorize the work it describes.
- Audio quality and audibility are verified by the human, never by the agent.

## Requirements

`python3` (stdlib only) and a reachable TTS endpoint for the full path
(`--no-audio` needs neither). Mermaid is a vendored local asset, so the rendered
deck makes no network requests at all.

## Install

```shell
copilot plugin marketplace add matthewcorven/corven-agent-tools
copilot plugin install narrated-walkthrough@corven-agent-tools
```

## Tests

```sh
python3 -m unittest discover -s tests -v
```
