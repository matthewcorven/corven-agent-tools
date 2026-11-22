# assets

Vendored third-party assets that ship with the plugin and must survive packaging.

| File | Why it must be included |
|---|---|
| `mermaid.min.js.gz` | The only Mermaid source; the builder inlines it into every deck |
| `mermaid.LICENSE` | MIT notice — required to accompany the binary |
| `mermaid.VENDORED.md` | Version, upstream, hash, bump procedure |

Do not filter this directory by extension. `mermaid.LICENSE` and
`mermaid.VENDORED.md` are the files a `**/*.js` include list silently drops, and
losing the license is a compliance defect rather than a cosmetic one.

If `plugin.json` ever gains a `files` array, list `mermaid.min.js.gz` explicitly:
gzip has no default-inclusion rule, and some `**/*` implementations skip
compressed binaries. The current marketplace copies the whole plugin directory,
so nothing is required today — but that is a property of this marketplace
implementation, not a guarantee the Agent Plugins spec makes, and a fork that
adds `files` would drop the license silently.
