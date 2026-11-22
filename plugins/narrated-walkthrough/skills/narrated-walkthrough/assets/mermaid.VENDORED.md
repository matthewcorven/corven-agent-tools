# Vendored Mermaid

| | |
|---|---|
| Package | `mermaid` |
| Version | **12.3.0** (exact, not a floating major) |
| Upstream | https://registry.npmjs.org/mermaid/-/mermaid-12.3.0.tgz |
| File | `dist/mermaid.min.js`, gzip -9 |
| SHA-256 of this file | `8193a8678f1329ce8676c7ef3ac66f415edd8b1bd05d994c5d023a1b8ce465ce` |
| License | MIT — see `mermaid.LICENSE` |

The builder inlines this file into every deck, so a rendered walkthrough makes no
network requests. Regenerate with:

```sh
curl -sL https://registry.npmjs.org/mermaid/-/mermaid-12.3.0.tgz \
  | tar xz -O package/dist/mermaid.min.js \
  | gzip -9 > mermaid.min.js.gz
```

## Bumping this version

Bumping is a deliberate change: it can alter diagram layout, so the golden example
must be rebuilt and re-reviewed in the same commit.

**Verify before you trust the download.** The command above pipes the registry
response straight into `tar`, so a hash computed afterwards only attests to
whatever the network returned. Check the tarball against the registry's published
integrity value *first*, then decompress, then update the SHA-256 above:

```sh
# 1. ask the registry what the tarball must hash to
curl -s https://registry.npmjs.org/mermaid/12.3.0 \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['dist']['integrity'])"

# 2. download, and refuse to continue unless it matches
curl -sL -o m.tgz https://registry.npmjs.org/mermaid/-/mermaid-12.3.0.tgz
python3 - <<'EOF'
import base64, hashlib, pathlib, sys
expected = "sha512-<paste from step 1>"
algo, _, b64 = expected.partition("-")
actual = base64.b64encode(hashlib.new(algo, pathlib.Path("m.tgz").read_bytes()).digest()).decode()
sys.exit(0 if actual == b64 else "registry integrity mismatch — do not vendor")
EOF

# 3. only now extract and record our own hash
tar xz -O -f m.tgz package/dist/mermaid.min.js | gzip -9 > mermaid.min.js.gz
shasum -a 256 mermaid.min.js.gz
```

Without step 1, a compromised registry mid-bump silently becomes the new pinned
truth, and the hash test would happily confirm it.

## Packaging note

`mermaid.LICENSE` must travel with the bundle — MIT requires the notice to
accompany the binary, and a naive `**/*.js` include list drops both the license
and this file. Losing it in packaging is a compliance defect, not a cosmetic one.
