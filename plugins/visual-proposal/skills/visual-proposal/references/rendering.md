# Rendering and live delivery

## Use the working example

Copy `examples/golden/` to a new documentation output directory without caches or dependencies.
Keep `build.mjs`, `app.js`, CSS, `serve.mjs` and `verify.mjs`; replace the fictional `deck.md`,
`sources.json` and presentation metadata in `focus.json`. Never overwrite an existing proposal blindly.
The HTML is generated; edit Markdown/styles and rebuild, not generated HTML.

Node/Marp build the deck; Mermaid renders in local headless Chromium; PlantUML renders through a
local Java process. Both become SVG data-URI images. No remote renderer receives document content.
The example README contains reproducible commands and prerequisite details.

## Live protocol

`focus.json` contains `title`, `downloadName`, `focus` (a stable `<!-- slide: id -->`) and `phase`.
The preview serves only `/`, `/index.html`, `/__status` and a favicon, on `127.0.0.1`.
It does not serve the source repository or accept commands/answers.

The browser polls for a new revision or focus. A successful build replaces the deck atomically;
build failures preserve the previous HTML with an explicit error. Manual navigation turns follow
off and preserves position across rebuilds. Re-enabling follow selects the most recent focus.
Standalone `file://` HTML does not poll and needs no network. Use a separate port for each preview.

Open the preview through the harness's browser tool when available, otherwise provide its URL.
Keep it running beyond the turn only through a supported detached-service mechanism and verify
readiness. Do not background it with shell tricks or stop another task's preview. A live deck does
not itself authorize any decision. Question-tool calls can block the agent while the viewer runs.

## Known renderer traps

| Symptom | Proven correction |
| --- | --- |
| Mermaid SVG fails as an image despite looking valid in the DOM | Set root `htmlLabels: false`; parse as SVG XML, reject parser errors, serialize and set numeric dimensions from `viewBox` |
| Tall/wrapped diagrams after a Mermaid upgrade | Pin dependencies and choose `dagre`, `classic`, explicit spacing; revalidate rather than trusting defaults |
| Mermaid sequence parse failure | Avoid unescaped semicolons in message labels; commas are safer |
| Marp slides appear double-scaled | Use `new Marp({ html: true, inlineSVG: false })` with the supplied viewer |
| Compact slides collapse or clip | Override Marp's size containment with `container-type: normal` for auto-height layouts |
| Small labels despite no overflow | Split diagrams; inspect screenshots at actual viewport size; provide zoom/pan for intrinsically large views |
| A renderer upgrade reuses old SVG | Cache keys must include renderer/config/dependency identity, not just diagram text |
| Partial output during a rebuild | Write a same-directory temporary file, then rename after a successful complete render |
| Java launcher exists but no runtime is found | Check an actual JVM; set `JAVA_BIN` or install Java explicitly; do not infer availability from `/usr/bin/java` alone |

The example pins its tested versions, including an official PlantUML jar hash. Future upgrades should
prefer stable releases and re-run all checks. Do not bump versions automatically while writing a proposal.
The builder rejects PlantUML preprocessing other than the local Smetana layout directive; do not add
remote includes, arbitrary source file reads or remote images.

## Validation is an output contract

Run a build and test from the copied example directory. Playwright checks every slide and image
at desktop, phone and narrow side-panel sizes; exact embedded Markdown; zero offline HTTP requests;
navigation/zoom/read-all; and focus/reload/error behavior. The test also starts the real loopback server
on an ephemeral port to check routes and watcher rebuild/failure behavior. It never uses the user's
active preview port.

Negative cases must exercise the same assertions as the positive path: remove an actual rendered
diagram and corrupt its SVG, then require inventory/decode checks to fail. Reject missing/duplicate
slide IDs and unknown focus instead of silently pointing elsewhere.

Retain a report bound to the generated revision and HTML hash. Keep screenshots/cache transient;
inspect representative dense, state, sequence and option slides visually. Fix clipping and
unreadable labels, not just console errors. If the result has not been inspected visually, say so.
Do not claim PDF/print, accessibility conformance or runtime behavior was validated unless tested.

## Primary references

- Marp Core: <https://github.com/marp-team/marp-core>
- Marp Markdown/directives: <https://marpit.marp.app/markdown>
- Mermaid configuration: <https://mermaid.js.org/config/schema-docs/config.html>
- PlantUML local execution: <https://plantuml.com/command-line>
- Playwright browser automation: <https://playwright.dev/docs/library>

Consult these for version-specific changes. Use Microsoft Learn only when a proposal's actual subject
is a Microsoft technology; this renderer itself does not need Azure or Learn tooling.
