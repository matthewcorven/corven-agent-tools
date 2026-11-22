# Archived execution helpers

These files preserve the original session's work, **not ready-to-run tooling**. Python helpers use `.py.txt` deliberately: they contain machine-specific paths and assumptions from the original checkout/session and must not be executed against the moved item without review.

| Archived file | Original purpose |
| --- | --- |
| [run-paldom-evaluation.py.txt](session-scripts/run-paldom-evaluation.py.txt) | Bound upstream acquisition, local ASR and scene/OCR runs; preserve stage outcomes |
| [verify-paldom-results.py.txt](session-scripts/verify-paldom-results.py.txt) | Validate the Paldom packets, selected frame bounds, source hashes, links and cleanup |
| [verify-video-research.py.txt](session-scripts/verify-video-research.py.txt) | Validate the earlier six-video baseline and its retained-image manifest |
| [youtube-key-prompt.py.txt](session-scripts/youtube-key-prompt.py.txt) | Hidden terminal input, read-only Google metadata verification, no credential persistence |
| [processing-state.json](processing-state.json) | Historical processing-location record; status is cleaned and its raw inputs no longer exist |

The scripts are preserved byte-for-byte. Future reuse needs explicit path/configuration design, tested input/output boundaries, retention decisions, and the timing/visual fixes listed in the [unfinished research item](../README.md). The key-input helper contains no API key. Do not infer that an old processing path or a successful historic verification means its source media/transcript still exists.
