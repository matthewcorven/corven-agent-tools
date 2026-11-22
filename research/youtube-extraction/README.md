# YouTube extraction — unfinished research

**Status: UNFINISHED / not ready for implementation or plugin publication.**

Moved from `matthewcorven/corven-agents` on **2026-11-10 (America/New_York)** at the user's request. This item consolidates the discovery work, two research collections, selected images, upstream source, execution evidence, and session helpers. It is not an installed skill, accepted design, or authorization to continue implementation.

## Start here

| Material | Location |
| --- | --- |
| Latest local Paldom evaluation and adoption assessment | [Evaluation results](artifacts/youtube/indydevdan/runs/2026-11-10-paldom/README.md) |
| Six-video baseline, preserved unchanged | [Earlier collection](artifacts/indydevdan/2026-11-10/README.md) |
| Community skills, MCPs and applications comparison | [Tooling landscape](tooling-landscape.md) |
| Exact upstream code, instructions, license and hashes | [Pinned Paldom snapshot](artifacts/youtube/tooling/paldom-video-to-skill/576f5ea880f965091be26320065dd732ea483e50/PROVENANCE.json) |
| Public metadata verified through Google's API | [Metadata verification](provenance/youtube-api-verification.json) |
| Archived evaluation/verification and hidden-key-input helpers | [Session-script archive](archive/README.md) |
| Relocation integrity and provenance | [Migration manifest](MIGRATION.json) |
| Machine-readable unfinished status | [Research status](status.json) |

## What was actually achieved

- The initial collection covers the six selected IndyDevDan uploads with original summaries and **18 stills**: six direct 720p frames and twelve low-resolution storyboard samples.
- A pinned, unmodified `Paldom/video-to-skill` snapshot was subsequently run locally. **Two videos completed local media acquisition, Whisper ASR, scene selection and OCR; four remained blocked by HTTP 403** after a single ordinary public-format fallback.
- The successful Paldom packets retain **10 native 1280 × 720 images**, original notes, structured evidence and quality metrics. The blocked packets explicitly contain no new extraction evidence.
- Local ASR was generated for `GRlcm3wmOqs` and `1v7ldJiyH-Y`, but **no entire transcripts are retained**. Full media, ASR/caption intermediates, unselected frames and downloaded model weights were removed after verification. No raw transcripts can be recovered from this archive.
- Tesseract and whisper.cpp were installed on the original workstation. Their installation is not portable repository state; versions are recorded in the evaluation environment file.
- The API key was entered through hidden terminal input and used only in process memory. **No credential value was saved or moved.** The archived helper is source code; the verification JSON contains public metadata only.

## Why this remains unfinished

1. **Input coverage:** four target videos could not be downloaded. Authorized local files or another permitted acquisition route are needed; a YouTube metadata API key does not resolve the media denial.
2. **Timestamp correctness:** upstream whisper.cpp invocation includes `-nt`, producing 30-second chunks. Main-run final intervals exceed actual video duration by **25.286 and 24.086 seconds**, yet pass the upstream quality gate. A separate one-minute test without `-nt` improved granularity but still overran the clip by 300 ms. No fix was adopted.
3. **Visual access:** image tools did not expose usable pixel payloads to the assistant. OCR/context selection and image decoding succeeded, but complete visual review did not.
4. **Semantic alignment:** temporal speech/frame overlap is not verified meaning or intent. A stable evidence contract must distinguish spoken claims, visible facts and inference.
5. **Frame quality:** transition frames, animated overlays, small code/color changes, sampling fallbacks and privacy screening need explicit validation.
6. **Retention and rights:** decide the authorized input and retained-output contract before collecting a new corpus. Software licensing does not grant rights to source videos.
7. **Portability:** session helpers contain historical machine/workspace paths. They are archived as `.py.txt`, not promoted as runnable project utilities.

## Next bounded research step

Use one authorized local screencast with known speech and visible changes to verify a real image-delivery path, finer bounded timestamps, and an uncertainty-aware speech/frame evidence record. Then evaluate the missing four inputs if authorized copies become available. Only after those results should an internal skill/script design be proposed and approved.

The existing findings support **evaluating selected Paldom components**, not adopting the snapshot unchanged. No plugin manifest, marketplace registration, runtime authority or protected project configuration was changed by this relocation.

## Preservation notes

The `artifacts/` subtree preserves the old research directory's relative structure and byte content so its existing links and checksums continue to work. Historical wording such as “this repository” or old absolute paths refers to the original execution context; this README and `status.json` are the current item-level status.

`MIGRATION.json` records all **91 moved files** and their hashes. Runtime-owned chat attachment snapshots and renderer image caches remain in the original conversation history; they are not the canonical working copy. The former source repository's `research/` directory and the seven authored session artifacts were moved, not duplicated. No commit or push was made.
