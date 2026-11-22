# OpenRouter State Of Models: Jev, Open-Weights, and Tokenomics

**Status: blocked before local transcription or frame extraction.**

- Source: https://www.youtube.com/watch?v=8BD6w5wELRo
- Published: 2026-10-05T13:00:15Z
- API duration: `PT26M9S`
- Target: the same video selected in the earlier six-video collection.

## Actual result

The normal public media selection (up to 720p) returned **HTTP 403 Forbidden**. A single ordinary public muxed-format fallback (`18`) also returned **HTTP 403**. No browser cookies, credentials, alternate identities, proxies, challenge handling or repeated retries were used.

Paldom's frame extractor needs a local video file. Without one, neither the local ASR stage nor the scene/OCR stage could run. This folder therefore contains **no new transcript-based summary and no new screenshots**. The stage outcomes and durations are preserved in [run.json](run.json); [evidence.json](evidence.json) explicitly records an empty new-evidence set.

## Existing baseline, not a result of this run

The [earlier collection](../../../../../../indydevdan/2026-11-10/README.md) contains the previous caption-grounded research and its own image-quality limitations. Those outputs have not been copied into this run or relabeled as successful Paldom output.

## What would unblock the evaluation

An authorized local copy of this video would allow the already-tested local ASR and frame/OCR stages to run without YouTube media acquisition. A YouTube Data API key verifies metadata but does not resolve this media-download denial. No additional key is requested here.
