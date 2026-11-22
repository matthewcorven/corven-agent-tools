# Golden visual proposal

A fictional release-note review workflow demonstrates the method without importing the source
project's architecture or policy. **All product decisions are illustrative and unaccepted.**
Read `brief.md`, then `deck.md`: the latter is real Marp Markdown with both Mermaid and PlantUML fences,
not pseudo-code.

## Run

Requires Node 22+, npm and Java 21+. These are build tools only; recipients need just the generated HTML.

```shell
npm ci
npx playwright install chromium
npm run setup:plantuml
npm run build
npm test
npm run serve
```

Open `http://127.0.0.1:4178/`, or open `index.html` directly without a server.
Set `PORT` for another preview. Stop the foreground server with Ctrl+C.
If starting it through an agent harness, use that harness's service lifecycle tools.

`setup:plantuml` downloads the pinned official jar, validates its SHA-256, then saves it under ignored
`.tools/`. To reuse a previously verified jar, set `PLANTUML_JAR` to its path. Every build checks the
hash. Set `JAVA_BIN` if Java is not on PATH; common Homebrew installations are detected.
Diagrams are rendered locally. No content is sent to a network rendering endpoint.

## Customize

Copy this folder, excluding `node_modules`, `.tools`, `.cache`, `evidence` and generated output.
Replace the fictional brief, all subject-specific slides, `sources.json`, and the title/download name
in `focus.json`. Keep IDs stable while iterating. Each slide needs exactly one level-one or level-two
heading and one unique `<!-- slide: id -->`; annotate diagram titles with `%% title:` or `' title:`.

The current diagram inventory is derived from source, not a hard-coded count. Source provenance is
embedded as supplied; the author must verify its claims. `npm test` compares that embedded inventory
to the source, not to an unrelated checkout.

Edit Markdown/CSS/viewer files to trigger a rebuild. `focus.json` changes update the live focus without
rebuilding; rebuild for a new standalone edition or metadata changes. Restart for builder/server changes.
Manual navigation pauses following, including across reloads. Follow latest restores the current focus.
Read all, arrow keys, source download and diagram zoom work offline.

## Evidence

`npm test` visits every slide at 1440x1000, 390x844 and 216x713, tests the offline artifact and live viewer,
and launches an isolated real server with `PORT=0` to exercise routing, watching and failure retention.
Negative checks alter actual emitted diagrams, requiring the same inventory/decode assertions to fail.
The test copies its fixture into an OS temporary directory; it never edits this deck or a user's live preview.

`evidence/report.json` records revision, HTML hash, inventories and results. Desktop and phone screenshots
are saved beside it. They must also be inspected for label readability: no-overflow is not proof of good
visual communication. Generated evidence is ephemeral and ignored; preserve the report with a real
proposal when appropriate to its repository's evidence policy.

The HTML contains Marp/Mermaid MIT notices. PlantUML is a build-time tool, not bundled in the HTML.
These checks do not prove the fictional product exists. PDF export is not part of this example.

System fonts are intentional: offline availability and consistent architectural-label metrics take
priority over a distinctive display face. A generic-font design warning is contextual, not a reason
to introduce remote fonts or weaken readability.
