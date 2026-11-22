# IndyDevDan — local Paldom evaluation

**Local date:** 2026-11-10. **Verdict: promising extraction components, not ready for project-internal adoption.**

The exact six videos from the [earlier collection](../../../../indydevdan/2026-11-10/README.md) were attempted. **Two completed local media acquisition, ASR, scene selection and OCR; four were blocked by HTTP 403 on both ordinary public media formats tested.** Those four have explicit failure packets, not copied baseline notes presented as new results.

## Results by video

| # | Video / packet | Result | Local ASR | Upstream retained candidates |
| --- | --- | --- | --- | --- |
| 1 | [ZERO to RUNNING Software Factory In 16 Minutes](videos/GRlcm3wmOqs/notes.md) | Processed | 34 segments; 21.24 s | 35 of 78 extracted |
| 2 | [Tiny Software Factories: Agentic Engineering DONE WRONG](videos/1v7ldJiyH-Y/notes.md) | Processed | 38 segments; 17.42 s | 32 of 88 extracted |
| 3 | [ELITE Agentic Engineers Are LEAVING THE LOOP](videos/52JX83rGPLc/notes.md) | **Blocked: HTTP 403 twice** | Not run | Not run |
| 4 | [My Jev Pi Durable Agent CURATES News](videos/OLqP8jxJ0Wc/notes.md) | **Blocked: HTTP 403 twice** | Not run | Not run |
| 5 | [Pi Durable PROACTIVE Agents](videos/DNDYYHsypZg/notes.md) | **Blocked: HTTP 403 twice** | Not run | Not run |
| 6 | [OpenRouter State Of Models](videos/8BD6w5wELRo/notes.md) | **Blocked: HTTP 403 twice** | Not run | Not run |

The successful packets retain **10 native 1280 × 720 illustrative JPEGs**, five per video, plus original summaries and structured evidence links. The successful inputs total approximately **35 minutes**. Their local ASR stages totaled **38.65 seconds**; the final scene/OCR stages totaled **55.41 seconds**. These are measured subprocess durations on this machine, not a general performance benchmark. They exclude dependency/model setup, an earlier pilot scene pass, manual selection, written analysis and blocked attempts.

## What ran

- A [pinned upstream snapshot](../../../tooling/paldom-video-to-skill/576f5ea880f965091be26320065dd732ea483e50/PROVENANCE.json) of [Paldom/video-to-skill](https://github.com/Paldom/video-to-skill), commit `576f5ea880f965091be26320065dd732ea483e50`, with its [MIT license](../../../tooling/paldom-video-to-skill/576f5ea880f965091be26320065dd732ea483e50/LICENSE). Both upstream `--self-check` commands passed. **Upstream files were not modified; no global skill or hooks were installed.**
- Ordinary public YouTube download at up to 720p. For each failed video, one normal muxed-format `18` fallback also returned HTTP 403. No cookies, alternate identities, proxies or challenge bypass were used.
- The initial caption request returned **HTTP 429**. Instead of repeatedly querying the caption endpoint, subsequent processing used **local whisper.cpp with a checksum-verified English `small.en` model**. No audio was sent to an external ASR service and no API key was used for this run.
- Paldom's scene/OCR extractor with a **60-frame budget per video**, followed by its exact-time `frame-at` tool for a small number of better-settled illustrative moments. Full raw output was used only as processing input; final packets retain original summaries, bounded illustrative images and traceable metrics.
- Tesseract and whisper.cpp were missing and installed through Homebrew, with their dependencies. [environment.json](environment.json) records versions and boundaries; [model.json](model.json) records model provenance. Written synthesis uses the existing assistant session, not a claim that all reasoning was offline.

## Findings that matter before reuse

### 1. The local extraction path is real and useful

For the two obtainable videos, the pipeline produced **67 scene candidates with OCR and overlapping speech**, rather than depending on low-resolution YouTube storyboard previews. This is a meaningful improvement for the newest video, whose earlier images were only 320 × 180. The second video already had 720p baseline images; the new benefit there is a broader scene-selection trail and an independently generated local transcript, not an automatic resolution improvement.

The two lessons support a progression from a configurable empty factory to a specialized running instance: recurring triggers, adjustable agent capabilities, bounded delegation, and human review near the workflow. They do not independently establish production reliability or safe self-modification. See the individual notes for source-grounded interpretation and limitations; the four blocked videos were not re-summarized from nonexistent new evidence.

### 2. An upstream flag substantially weakens the timestamp result

The unmodified whisper.cpp command includes **`-nt`**. Both successful main runs produced a median segment length of **30 seconds**, even though the quality gate reported `pass` and timed coverage `1.0`. Short visual shots therefore inherit the same long spoken passage. **This is coarse temporal association, not precise semantic alignment.**

Worse, the final ASR intervals extend **25.286 seconds beyond the newest video's measured duration** and **24.086 seconds beyond the introduction video's duration**. The upstream gate accepted both. These range defects are preserved in each `run.json`; they were not silently corrected or presented as valid source-time evidence. The selected final frames and their referenced speech windows remain within the actual media bounds.

A bounded, separate [one-minute diagnostic](timing-experiment.json), using the same audio and model, showed:

| Invocation | Segments in 60 s | Median segment length | Maximum |
| --- | --- | --- | --- |
| Upstream flags, including `-nt` | 2 | 30.00 s | 30.00 s |
| Same command without `-nt` | 23 | 2.72 s | 3.84 s |

This strongly identifies a granularity problem worth addressing before adoption. It is **not a shipped fix or an accuracy proof**: the alternative's final segment extends 300 ms beyond the clip, so range validation and alignment checks are still required. The main-run results and vendored source remain unchanged.

### 3. Change detection does not equal meaningful-state selection

The two runs used scene strategy, but each also added one coverage-gap sample. Six combined shot intervals were shorter than two seconds. Some selected frames land on transitions, presentation overlays or incomplete animations. A screenshot must sometimes be re-extracted after the screen settles; the existence of OCR text does not prove the frame is useful or readable.

The retained candidate traces are text-free [scene catalogs](videos/GRlcm3wmOqs/scene-catalog.json) ([second video](videos/1v7ldJiyH-Y/scene-catalog.json)). They preserve timing, sampling flags and OCR/speech character counts without retaining complete transcripts, raw OCR or all candidate screenshots.

### 4. Full visual-semantic validation remains blocked

The image-view tool continued to return success without a usable pixel payload in the assistant context. Final selections are therefore supported by **OCR, source-time context and image decoding**, not a falsely claimed full visual review. Future adoption must prove that actual image bytes reach the selected vision-capable model. Merely passing image paths or declaring a model multimodal is insufficient.

A valid evidence record must keep three things separate: what was said, what is visibly established, and what intent is inferred. Timing overlap alone does not establish their equivalence. OCR and small-model ASR can misread technical names and on-screen syntax.

### 5. URL acquisition is still the principal coverage blocker

Only **2 of 6** target videos could be fully obtained in this evaluation. The four failure packets preserve both attempts and make the absent stages explicit. Authorized local copies would allow those videos to use the already-working local stages without YouTube download requests. The previously verified YouTube API key would not resolve these media 403s, so no new credential prompt was needed.

## Retained deliverables and reproducibility

- [collection.json](collection.json): ordered source IDs, publication metadata from the earlier authenticated metadata check, source scope and pinned tool identity.
- [environment.json](environment.json), [model.json](model.json): local dependencies, model checksum and execution boundaries.
- `videos/<id>/run.json`: actual stage outcomes, timings, sanitized diagnostics and quality measurements.
- `videos/<id>/notes.md`, `evidence.json`, and selected `frames/`: original commentary and limited visual evidence for successful videos; explicit empty evidence records for blocked videos.
- `videos/<id>/scene-catalog.json`: text-free candidate audit trail for successful videos.
- [timing-experiment.json](timing-experiment.json): bounded diagnostic evidence, separate from unmodified main runs.
- [manifest.json](manifest.json): final retained-file checksums and verification results.
- [cleanup.json](cleanup.json): confirmation that 120 task-owned processing files (683,521,486 bytes), including full media, full transcripts and model weights, were removed after output verification. Installed Tesseract/whisper.cpp dependencies remain available.

The [parent research index](../../../README.md) describes the reusable channel/run/video-ID layout. The earlier baseline remains intact. This evaluation introduces **no internal project skill**, no runtime behavior, and no commit/push.

Full third-party transcripts, raw media/audio, unselected images and model weights are not retained deliverables. Source videos and selected stills remain attributed to IndyDevDan and their respective rights holders. Research notes are newly composed. An upstream software license does not grant rights to the videos.

## Recommendation

**Do not promote this snapshot unchanged.** Its local ASR, OCR, configurable scene selection and evidence files are useful building blocks. A subsequent, explicitly authorized adaptation should first resolve:

1. Reliable authorized local input and clear blocked-state handling.
2. Timestamp granularity and bounds, with real alignment tests rather than coverage alone.
3. Settled-frame selection, small code/color changes, and bounded fallback sampling.
4. Actual image delivery, visible-evidence validation and uncertainty-aware semantic matching.
5. Secret/privacy screening before retaining screenshots or sending inputs to any external service.
6. A stable reduced-evidence export contract and retention policy.

The first two videos demonstrate the extraction foundation; they do **not** yet satisfy the full semantic audio-plus-visual accounting goal.
