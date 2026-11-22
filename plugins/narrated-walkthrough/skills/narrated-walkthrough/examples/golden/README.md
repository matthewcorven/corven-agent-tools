# Golden example

A three-scene walkthrough that demonstrates every contract in the skill without
being about anything real.

```sh
cd examples/golden
python3 ../../scripts/build_narration.py --validate      # plan contract, no model needed
python3 ../../scripts/build_narration.py --no-audio --serve   # render from recorded durations
open http://127.0.0.1:8765/slides.html
python3 ../../scripts/build_narration.py --stop

# full path, with a local TTS endpoint configured in timeline.json:
python3 ../../scripts/build_narration.py --serve
```

`--no-audio` renders the deck from the durations already recorded in
`timeline.json`, so a reviewer sees the layout, the diagrams, the captions and the
ask panel without running a model. Playback needs clips: run the full path.

Audio and `slides.html` are gitignored build output, on the same reasoning as
`visual-proposal`'s `evidence/`. Committing a 27-second lossless clip to prove a
build happened is not worth the repository. The measured durations stay in
`timeline.json` because they are the example's actual content.

Expected: three clips measured, a `slides.html` with three diagrams, total
duration around 40 s depending on the voice, and an ask panel with three options.

## What to look at

| Check | Where |
|---|---|
| The clock is derived | `timeline.json` after a build has `start` values you never wrote |
| One claim per scene | three scenes, three titles, three diagrams |
| The invitation | scene 3 narration asks; the ask panel repeats it |
| Re-voicing | `--only 02-clock --voice af_heart` changes one clip and re-derives every later offset |

## Deliberate omissions

- No CDN lockfile, no PDF export, no video. Those belong to other skills.
- No screenshot evidence. This example is about timing, not visual regression.
