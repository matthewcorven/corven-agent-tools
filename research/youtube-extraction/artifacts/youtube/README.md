# YouTube research collections

This directory separates source-tool provenance from dated collection runs. It is a research workspace, not an installed agent skill or a deployed capability.

## Layout

```text
youtube/
  tooling/<tool>/<commit>/       Exact reviewed upstream files, license, hashes
  <channel>/runs/<date>-<tool>/
    README.md                   Results, quality limits, and adoption verdict
    collection.json             Ordered video IDs and public source metadata
    environment.json            Tool versions and execution boundaries
    model.json                  Local model source and verified checksum
    manifest.json               Final retained-file hashes and validation
    videos/<youtube-id>/
      run.json                  Stage outcomes, timing, quality metrics
      notes.md                  Original source-grounded analysis
      evidence.json             Selected timestamps and summary-to-frame links
      frames/                   Small retained selection of illustrative stills
```

Stable YouTube IDs identify videos; ordered collection manifests preserve newest-first selection without making filenames depend on ranking. New collections belong in new run directories rather than overwriting earlier evidence. Source-tool snapshots are reusable across runs and identified by immutable upstream commits.

## Collections

- [IndyDevDan — Paldom evaluation, 2026-11-10](indydevdan/runs/2026-11-10-paldom/README.md).
- [Earlier six-video baseline](../indydevdan/2026-11-10/README.md), preserved for comparison.

## Retention and interpretation

Raw downloaded media, full third-party transcripts, unselected frames, and model weights are temporary processing inputs, not retained research deliverables. Retained notes are original summaries; screenshots are a limited illustrative selection with source attribution. Output quality and access limitations must be reported rather than hidden by fallback behavior.

A successful extraction or transcript-quality heuristic does not certify factual accuracy, exact timing, semantic alignment, or safe agent operation. Keep source observations, interpretation, and validation results distinct. An upstream license permits software reuse under its terms; it does not grant rights to source videos.
