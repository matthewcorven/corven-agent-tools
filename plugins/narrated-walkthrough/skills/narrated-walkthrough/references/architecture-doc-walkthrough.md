# Architecture & Document Discussion Walkthrough (Fast-Path)

Use this built-in capability whenever the Architect asks for a **logical
architecture, data flow, control flow, or user flow walkthrough** from a
Markdown design document — with **seconds of wait time**, just like PR Reviews
and Daily Standups.

## Instant Visual Pack (< 1 second)

`scripts/doc_to_walkthrough.py` deterministically parses any Markdown document
with `##` headings and ```` ```mermaid ```` blocks into a schema-valid
`narrated-timeline/v1` pack (`hud.pack_kind = "architecture_doc"`) with semantic
Web Components (`<nw-opening-scene>`, `<nw-comparison-scene>`,
`<nw-evidence-scene>`, `<nw-closing-scene>`), offline inlined Mermaid, and an
A2UI v1.0 discussion surface:

```sh
python3 scripts/doc_to_walkthrough.py \
  --doc /path/to/design-doc.md \
  --out-dir <artifact-run-dir>/planning \
  --repo owner/repo
```

Because it runs `--no-audio` by default (deriving natural reading durations from
each section's word count), the pack builds and appears in the A-HUD message
queue pane in **under 1 second**.

## Optional Spoken Narration (`--with-audio`)

When spoken narration is requested (or triggered later from the A2UI card's
*"Synthesize US_James audio for this doc"* action), pass `--with-audio` with the
local TTS server's `--endpoint`, `--model`, and `--voice` to synthesize anchored
clips and compile a gapless `audio/master.wav`.

## Canvas Action (`generate_doc_walkthrough`)

From an open `narrated-walkthrough` Extension Canvas, call:

```json
{
  "actionName": "generate_doc_walkthrough",
  "input": {
    "docPath": "/absolute/path/to/architecture.md",
    "load": true
  }
}
```

The extension allocates a collision-free pack directory under the active HUD's
artifact root, runs `doc_to_walkthrough.py` in < 1 s, records the pack in
`runs.jsonl`, and loads it directly into the Architect HUD workspace.
