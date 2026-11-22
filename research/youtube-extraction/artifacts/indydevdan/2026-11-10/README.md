# IndyDevDan — six newest video research packets

Snapshot: **2026-11-10**, from [IndyDevDan's Videos tab](https://www.youtube.com/@indydevdan/videos).

## Scope and limitations

The request was for full transcripts and illustrative screenshots. **Full third-party copyrighted transcripts are not reproduced here.** The substitute deliverables are original research notes, timestamped source links, and a small selection of illustrative stills with commentary. They are not a verbatim record or a replacement for watching the videos.

`GOOGLE_API_YOUTUBE_API_KEY` was **not available in the executing session**. No credential files were read and no API key was used. Public YouTube metadata was retrieved with the installed `yt-dlp`, ignoring local configuration. Google's [official caption-download endpoint](https://developers.google.com/youtube/data/reference/rest/v3/captions/download) requires OAuth authorization and permission to edit the video; an API key alone does not grant that access.

Each packet identifies its actual evidence and any access or transcription limitations. Automatically generated captions can misrecognize technical names; summaries should be checked against linked playback before relying on an exact claim. Captions, full videos/audio, and signed media URLs are not included in this collection.

## Reading order — newest first

| # | Video / research notes | Duration | Original |
| --- | --- | --- | --- |
| 1 | [ZERO to RUNNING Software Factory In 16 Minutes (LIQUID Software)](01-GRlcm3wmOqs/notes.md) | 16:35 | [YouTube](https://www.youtube.com/watch?v=GRlcm3wmOqs) |
| 2 | [Tiny Software Factories: Agentic Engineering DONE WRONG](02-1v7ldJiyH-Y/notes.md) | 18:36 | [YouTube](https://www.youtube.com/watch?v=1v7ldJiyH-Y) |
| 3 | [ELITE Agentic Engineers Are LEAVING THE LOOP... Here’s why](03-52JX83rGPLc/notes.md) | 25:46 | [YouTube](https://www.youtube.com/watch?v=52JX83rGPLc) |
| 4 | [My Jev Pi Durable Agent CURATES News: HYPE, FACT, or SIGNAL](04-OLqP8jxJ0Wc/notes.md) | 22:34 | [YouTube](https://www.youtube.com/watch?v=OLqP8jxJ0Wc) |
| 5 | [Pi Durable PROACTIVE Agents: Jev Triggers My ON-CALL Pi Agent](05-DNDYYHsypZg/notes.md) | 35:16 | [YouTube](https://www.youtube.com/watch?v=DNDYYHsypZg) |
| 6 | [OpenRouter State Of Models: Jev, Open-Weights, and Tokenomics](06-8BD6w5wELRo/notes.md) | 26:08 | [YouTube](https://www.youtube.com/watch?v=8BD6w5wELRo) |

Total source duration: **2h 24m 55s** using per-video metadata. The sixth video's channel-listing duration is one second longer; both observations are preserved rather than silently made identical.

## Capture quality and review status

There are **six original research notes and 18 PNG stills**, three per video, covering uploads published from **2026-10-05 through 2026-11-09**.

| Videos | Retained images | Timestamp quality |
| --- | --- | --- |
| 2 and 3 | Six 1280 × 720 frames from direct public-stream seeks | Requested video seek positions |
| 1, 4, 5 and 6 | Twelve 320 × 180 crops of public YouTube storyboards after direct media requests failed | Approximate sampled positions; see individual notes |

The storyboard stills are sampled video frames, not cover thumbnails, but fine text is often unreadable. Image decoding and local links were checked. **Full visual review could not be completed:** image-view tools did not expose pixels to the analysis context. Local OCR supported several frame selections; others are aligned to the spoken topic and explicitly labeled as such. Do not treat the images as independently verified evidence of exact screen contents.

All summaries use public English automatic captions. Videos 1–4 were analyzed across the full caption tracks; videos 5–6 use minute samples across the videos, with additional focused passages for video 5. Later supplemental requests failed. The notes distinguish those evidence limits and identify mock demonstrations and untested claims. For exact wording, open the original video and use YouTube's **Show transcript** option when available.

## Provenance and organization

- [selection.json](selection.json) records the retrieval time, channel identity, exact selection order, and the seventh upload excluded at the cutoff. Selection was checked twice; it is a dated snapshot, not a continuously updated feed. Shorts and the separate Live tab were outside the requested `/videos` scope.
- Each numbered folder contains `notes.md`, `source.json`, and selected images in `frames/`. The notes explain why the selected frames matter; `source.json` records publication metadata, evidence status, and frame timestamps.
- [manifest.json](manifest.json) records the artifact checks, image dimensions, and SHA-256 inventory. Its checks establish file integrity and internal consistency, not the truth of the presenter's claims or a completed visual review.
- Frame timestamps refer to the original video timeline. Follow the corresponding time-linked source when detail is unclear; a still does not establish everything said around it.
- Video titles and selected stills are attributed to **IndyDevDan** and their respective rights holders. No ownership or reuse license is claimed. The written research analysis is newly composed.

This is an external research collection, not accepted architecture, an implementation plan, or new runtime-validation evidence for this repository. No repository policy, product behavior, or deployment was changed.
