# Existing tooling for synchronized video research

**Research date:** 2026-11-10 (America/New_York). **Status:** public documentation and implementation inspected; nothing installed or runtime-tested. This is a tool-selection report, not approval to adopt software or change repository policy.

## Bottom line

There are unusually close community matches. **Paldom/video-to-skill is the strongest inspectable starting point for a local extraction pipeline**, while **mrzhangguoguo/gemini-video-analysis-skill describes the closest semantic, multimodal output contract**. The latter is instructions, not a finished application. No surveyed package was verified to deliver the entire requested workflow unattended, including accurate semantic alignment, readable retained evidence, actual image delivery to the model, and confidence/coverage checks.

The central distinction is **temporal association versus semantic alignment**. A frame and a sentence occurring together does not establish that the sentence describes that frame. Most implemented tools solve the first; a vision-capable model, explicit evidence schema and validation still have to solve the second.

## 1. Closest community skills

| Candidate | What exists | Important gap | Practical fit |
| --- | --- | --- | --- |
| [Paldom/video-to-skill](https://github.com/Paldom/video-to-skill) | Three coordinated skills: caption/local-ASR retrieval, calibrated scene extraction and OCR, then Markdown notes with screenshots. Retains `transcript.json`, `timeline.json` and selected images. | Implemented speech/frame association is interval overlap. Selection can fall back to interval sampling; grayscale dedup can miss color-only changes. Semantic interpretation and final visual checking belong to the calling agent. | **First candidate to evaluate.** MIT; Python, FFmpeg, optional Tesseract, whisper.cpp/faster-whisper. Local extraction; model privacy depends on the calling agent. |
| [Gemini video-analysis skill](https://github.com/mrzhangguoguo/gemini-video-analysis-skill) | Explicitly asks for timestamped transcription, semantic visual changes, transcript-to-shot alignment, representative frames, `aligned_storyboard.json` and an HTML report. | Repository contains reusable instructions, not an implemented/tested pipeline or strict output schema. Requires a functioning Gemini video client and cloud processing. The instructions also mix preserving Whisper output with Gemini-derived transcript export; raw versus corrected provenance needs clarification. | **Closest design to the requested semantic output.** MIT; local Whisper/FFmpeg plus separate Gemini access. |
| [bgevorkian/video-to-notes](https://github.com/bgevorkian/agent-skills/tree/main/skills/video-to-notes) | Bundled Python scripts prepare captions, optional faster-whisper ASR, timestamped frames and a manifest; the agent reads speech and images and writes Markdown/searchable PDF. | Frames are sampled periodically, then adjacent dHash duplicates are removed and the remaining set is capped. This is not continuous semantic change detection. Final notes omit images by default; retention and embedding must be requested. | **Good model-independent notes workflow**, but less close to a retained, verbose multimodal evidence archive. MIT. |
| [bsisduck/video-analyzer-skill](https://github.com/bsisduck/video-analyzer-skill) | Bundled shell scripts for Whisper, frame grids, FFmpeg scene changes and parallel audio/visual analysis; produces a Markdown timeline. | Source uses a fixed scene threshold of `0.3`, takes at most 20 change frames, and caps ordinary sampling. Default cleanup removes the working evidence; no durable joined evidence schema. | Useful starter for bounded video analysis, not the preferred archive pipeline. GPL-3.0. |

### Why Paldom is the best first trial

The implementation is more than a prompt. `extract_frames.py` measures the video's scene-score distribution, calibrates a threshold, computes perceptual hashes, optionally OCRs retained frames, and produces shot records with:

- `start_ms` / `end_ms`;
- frame file path;
- OCR text and a perceptual-difference value named `motion`;
- speech segments overlapping that interval.

It also offers exact-time `frame-at` and bounded `clip` extraction for looking again when a frame is mid-animation or fails to show what the speech describes. Its published worked example documents both deduplication losing an important slide and re-sampling a chart caught mid-animation. That is useful evidence of the intended workflow, **not our reproduction of the result**.

Important qualifications:

1. `motion` is not an understanding of the speaker's intent.
2. The implementation's `transcript_span` joins by temporal overlap, not embedding similarity or verified semantic correspondence.
3. When scene coverage is poor, it replaces candidates with interval samples; it also pins samples and fills gaps. Therefore it does **not** deliver “only meaningful changes.” Those fallback samples should remain labeled.
4. A one-line edit, color-only status change or region hidden behind a presenter overlay can still be missed. A real test must include those cases.
5. Some upstream acquisition suggestions involve browser cookies or responding to site challenges. They are not authorization to use credentials or bypass barriers here. Prefer authorized local media for the first trial.
6. Secret redaction in prose is not proof that retained screenshot pixels contain no secrets. The ingestion boundary needs its own image/privacy check.

### Skill maturity and pinned evidence

All four repositories were non-archived when checked. Small adoption signals are a reason to test, not proof of quality or failure.

| Candidate | License | Verified code/history observation |
| --- | --- | --- |
| Paldom/video-to-skill | MIT | Default-branch commit `576f5ea880f965091be26320065dd732ea483e50`, 2026-08-31; that commit updates CI. Repository `pushed_at` is 2026-11-01, which is not proof of a recent feature change. One star at inspection. |
| Gemini video-analysis skill | MIT | `14a55d2e823c3cef8683d78848849aa64a250336`, 2026-06-26, initial publication. Seven stars. |
| bgevorkian/video-to-notes | MIT | Latest inspected skill-path commit `7a7ef0a00b3249e357cb7a61febd83febf2f0d51`, 2026-07-26. Repository last push 2026-09-17; zero stars. |
| bsisduck/video-analyzer-skill | GPL-3.0 | `1dd7e8172ba6ebb216e46df5cfdc3bdc10062600`, 2026-05-04. Fifty-six stars. |

Primary implementation evidence:

- [Paldom notes contract](https://github.com/Paldom/video-to-skill/blob/576f5ea880f965091be26320065dd732ea483e50/skills/video-notes/SKILL.md), [frame implementation](https://github.com/Paldom/video-to-skill/blob/576f5ea880f965091be26320065dd732ea483e50/skills/video-keyframes/scripts/extract_frames.py), [transcription contract](https://github.com/Paldom/video-to-skill/blob/576f5ea880f965091be26320065dd732ea483e50/skills/video-transcribe/SKILL.md), [worked example](https://github.com/Paldom/video-to-skill/blob/576f5ea880f965091be26320065dd732ea483e50/docs/example.md).
- [Gemini skill instructions](https://github.com/mrzhangguoguo/gemini-video-analysis-skill/blob/14a55d2e823c3cef8683d78848849aa64a250336/SKILL.md).
- [bgevorkian preparation implementation](https://github.com/bgevorkian/agent-skills/blob/7a7ef0a00b3249e357cb7a61febd83febf2f0d51/skills/video-to-notes/scripts/prepare_video.py).
- [bsisduck frame implementation](https://github.com/bsisduck/video-analyzer-skill/blob/1dd7e8172ba6ebb216e46df5cfdc3bdc10062600/scripts/extract-frames.sh).

## 2. MCP options

**An MCP server returning a frame path is not equivalent to delivering an image to a model.** That distinction directly addresses our previous image-viewing failure.

| Candidate | Verified capabilities | Missing capability / constraint |
| --- | --- | --- |
| [GetVideoFrames / GVF](https://github.com/GetVideoFrames/gvf) | Local probing, extraction, scene/quality/duplicate analysis, selection with `bestPerScene` and deduplication, then export at selected timestamps. Eight MCP tools include `gvf_analyze`, `gvf_select`, `gvf_export` and `gvf_run`. | No ASR or audio alignment. MCP emits text/structured JSON with image paths, **not image-content blocks**. MIT core; Node 22+, FFmpeg; `v1alpha1` contract is explicitly unstable. |
| [eequaled/frames-mcp](https://github.com/eequaled/frames-mcp) | Extracts a frame at a requested time, multiple frames and clips, with OCR. **Actually emits base64 MCP `image` blocks**, not merely image URLs. | No ASR or change detection; its automatic multi-frame mode is evenly spaced. Use its exact-time tool with timestamps chosen elsewhere. GPL-3.0; Node/FFmpeg; small project with limited activity. |
| [TwelveLabs MCP](https://www.npmjs.com/package/twelvelabs-mcp) | Cloud video indexing, transcript retrieval via `get-indexed-asset` with `transcription:true`, semantic segment search, image queries and composed image+text queries using Marengo 3.0, plus video analysis. | Strongest verified semantic-search backend, but **semantic retrieval is not a complete per-utterance evidence-alignment/export pipeline**. Shared output helper emits text only; search thumbnails are expiring URLs, not image blocks. Requires separate provider access and cloud processing. |
| [VideoDB Director MCP](https://github.com/video-db/agent-toolkit/tree/main/modelcontextprotocol) | Source exposes documentation helpers, video playback and `call_director`, a conversational relay to VideoDB's hosted service. Its description advertises transcripts, scene indexing, semantic search and timestamped frames. | Those richer features are backend claims mediated through one relay—not separately typed tools with a locally verifiable result schema. Image payload and complete export behavior remain unverified. Requires provider access/cloud processing. |

### The most useful MCP combinations

- **Local extraction:** GVF for scene-aware/quality-aware selection, a timed-ASR component, then `frames-mcp` for selected timestamps if the host needs explicit image blocks. An orchestrator must still join and semantically evaluate the evidence. This is more integration work than the Paldom skill route.
- **Cloud semantic retrieval:** TwelveLabs is a credible candidate when external processing is acceptable. It provides real multimodal search, not just adjacent-timestamp matching. Persisting permitted selected images while their URLs are valid, exporting a stable evidence package, and verifying speech-to-visual claims are additional work.
- Merely replacing one text-only MCP tool with another would not fix the previous inability to inspect pixels. Even correct image blocks require a client and selected model that support them; test that path explicitly.

### MCP provenance, maintenance and caveats

- **GVF:** MIT core; inspected `0.1.0` / unstable `v1alpha1` documentation. Repository created 2026-07-14, last repository push 2026-10-03, two stars and no releases/tags at inspection. A repository push is not a feature-maintenance guarantee. Its hosted/desktop offerings are separate from the local core.
- **frames-mcp:** GPL-3.0; created 2026-02-24, last repository push 2026-02-28, three stars/no releases at inspection. Local tools require no provider key, though model inference through the host may still be remote.
- **TwelveLabs:** npm `twelvelabs-mcp` **1.0.0**, published **2026-05-12T22:56:17Z**. No repository link or license field was declared in the inspected npm metadata/package. **This does not mean the distribution is uninspectable or necessarily closed-source:** the published JavaScript and source maps were inspected read-only, without installation or execution. Clarify licensing before adopting or redistributing it. `dist/tools/videoIndexing.js` supplies transcript retrieval; `dist/tools/search.js` supplies semantic/image queries; `dist/utils.js` constructs text-only MCP results. Search thumbnail URLs expire after about one hour per the package's own tool description. Transcript timestamp granularity was not established from the wrapper alone.
- **VideoDB toolkit:** no repository-root license was detected; resolve that before reuse. The inspected MCP source is a Socket.IO relay. Do not assume it returns stable image artifacts or that its natural-language result has a fixed schema.

Primary sources:

- [GVF MCP implementation](https://github.com/GetVideoFrames/gvf/blob/main/packages/mcp/src/index.ts), [MCP documentation](https://github.com/GetVideoFrames/gvf/blob/main/docs/MCP.md), [readiness contract](https://github.com/GetVideoFrames/gvf/blob/main/docs/V1_READINESS.md).
- [frames-mcp implementation](https://github.com/eequaled/frames-mcp/blob/main/src/index.ts): image blocks in the single and multiple frame handlers; fixed interval/even-division selection in the multiple-frame handler.
- [TwelveLabs published package](https://www.npmjs.com/package/twelvelabs-mcp), [registry metadata](https://registry.npmjs.org/twelvelabs-mcp), [vendor announcement](https://www.twelvelabs.io/blog/twelve-labs-mcp-server). The distributed package, not an assumed GitHub implementation, is the source for the verified tool behavior above.
- [VideoDB relay implementation](https://github.com/video-db/agent-toolkit/blob/main/modelcontextprotocol/videodb_director_mcp/main.py), [backend capability description](https://github.com/video-db/agent-toolkit/blob/main/modelcontextprotocol/videodb_director_mcp/constants.py).

Transcript-only servers such as [shawnwei512/video-transcript-mcp](https://github.com/shawnwei512/video-transcript-mcp) can fill an ASR/caption role, but do not provide the visual selection or semantic matching required here.

## 3. Standalone applications and scripts

No surveyed standalone application was verified to be a better turnkey match than the leading skills. The most relevant implementations still associate speech and images temporally; none of these four establishes semantic-intent alignment.

| Candidate | What is genuinely implemented | Why it is not the first choice |
| --- | --- | --- |
| [HHousen/lecture2notes](https://github.com/HHousen/lecture2notes) | Periodic frame capture followed by CNN slide classification, clustering/deduplication, slide OCR/structure analysis, local ASR and summarization. Programmatic `final_data` contains transcript and summaries. AGPL-3.0. | Slide-focused and dependency-heavy. It is **not change-triggered capture**: FFmpeg uses `fps=1/N` before downstream filtering. Speech/slide integration is temporal/text combination, not semantic matching. Ready-to-consume image+speech JSON file export and Apple Silicon compatibility were not verified. |
| [SelinaMangaroo/video-extraction-pipeline](https://github.com/SelinaMangaroo/video-extraction-pipeline) | Local Whisper, PySceneDetect content detection, transcript/scene overlap, and per-scene JSON with start/end times, frame paths, transcript text, caption and keywords. | Image/text captioning calls OpenAI; not fully local. No declared license found, so do not copy/adapt without permission. Main branch last changed 2025-08-21. Useful output-schema reference, not a recommended dependency. |
| [lalit-dev/transcriptor](https://github.com/lalit-dev/transcriptor) | Local faster-whisper, adaptive scene-score thresholding, screenshots, SRT and Markdown interleaving speech with frame intervals. macOS setup is documented. | No JSON export, no semantic matching, no declared license found, and minimal commit history. Study the approach; do not treat unlicensed code as freely reusable. |
| [harrytormey/video-analyzer](https://github.com/harrytormey/video-analyzer) | FFmpeg scene detection, local BLIP image captions, optional scene-local Whisper transcription, JSON/Markdown outputs; optional local/cloud LLM processing. | Falls back to fixed 30-second chunks when no cuts are detected. Re-transcribing each scene loses cross-scene context. Minimal history and no declared license found. |

**Maintenance correction that matters:** `lecture2notes` has a repository push in July 2026, but its inspected default-branch HEAD is **2025-05-26**, commit prefix `10210930` (`remove pyaudio`). The newer push belonged to an unmerged dependency-update branch. It should not be presented as recently feature-maintained based on `pushed_at` alone. Its older ML stack and ASR documentation drift make installation a separate investigation.

Implementation evidence:

- [lecture2notes frame sampler](https://github.com/HHousen/lecture2notes/blob/master/lecture2notes/end_to_end/frames_extractor.py), [orchestrator](https://github.com/HHousen/lecture2notes/blob/master/lecture2notes/end_to_end/summarizer_class.py), [text combination and summarization](https://github.com/HHousen/lecture2notes/blob/master/lecture2notes/end_to_end/summarization_approaches.py), [default-branch history](https://github.com/HHousen/lecture2notes/commits/master).
- [video-extraction-pipeline scene selection, overlap and JSON output](https://github.com/SelinaMangaroo/video-extraction-pipeline/blob/main/captioning.py), [Whisper implementation](https://github.com/SelinaMangaroo/video-extraction-pipeline/blob/main/utils/whisper_utils.py).
- [transcriptor implementation](https://github.com/lalit-dev/transcriptor/blob/main/transcribe_videos.py).
- [video-analyzer repository](https://github.com/harrytormey/video-analyzer), including `analyze.py`, `whisper_utils.py` and `vision_model.py`.

Heavier research systems such as [Video-RAG](https://github.com/Leon1207/Video-RAG-master) and [Multimodal-Video-RAG](https://github.com/iisc-kc/Multimodal-Video-RAG) are interesting for retrieval research, but their large vision models/services and unresolved licensing/setup details make them poor first candidates for this Mac-based collection task.

## 4. Recommendation and acceptance gate

Evaluate one short, authorized local screencast before reprocessing the six videos. A useful test contains a slide transition, a tiny code change, a color-only state change, scrolling, a presenter overlay and a spoken explanation that refers backward to an earlier screen.

A successful trial must demonstrate:

- actual decoded image pixels reaching the chosen vision model—not just paths or thumbnail URLs;
- correct speech time ranges and frame timestamps, with timing errors measured;
- separate fields for spoken claims, visible observations and inferred intent;
- retained images, normalized speech/summary records, and many-to-many speech/frame links;
- labels distinguishing scene-selected frames, fallback samples and model-selected frames;
- uncertainty and coverage reporting, including regions or moments not inspected;
- privacy/secret screening before persistence or external upload, and bounded storage/model cost;
- honest treatment of unavailable media and copyright/access permissions.

A YouTube Data API key improves metadata access; it does not provide arbitrary caption downloads, media-download rights, Gemini/TwelveLabs access, or vision support in the client. Upstream examples that place credentials in environment variables, command-line arguments or configuration files should not be copied into this repository's workflow. Use a separately reviewed, in-memory secret path.

## Installation status

**Nothing installed or executed from the candidate repositories.** `npx --no skills find ...` refused because the Skills CLI was not installed; discovery continued through public web sources and GitHub APIs rather than installing it.

For discoverability, the repositories advertise commands such as `npx skills add Paldom/video-to-skill` and `npx skills add bsisduck/video-analyzer-skill`. These were **not run** and are not approval to install unpinned code. Review and pin the selected revision before any approved installation.
