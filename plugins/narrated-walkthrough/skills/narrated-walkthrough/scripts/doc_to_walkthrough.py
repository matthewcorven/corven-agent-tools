#!/usr/bin/env python3
"""Convert a Markdown architecture/design document with Mermaid diagrams into a
ready-to-view narrated-walkthrough artifact pack in under 1 second.

Usage:
  python3 doc_to_walkthrough.py --doc <path.md> --out-dir <planning-dir>
  python3 doc_to_walkthrough.py --doc <path.md> --out-dir <planning-dir> --with-audio --endpoint http://127.0.0.1:18001/v1/audio/speech --model Ming-3.0-tts-tiny-4bit --voice US_James

Zero third-party dependencies (Python stdlib only). Produces a schema-valid
``timeline.json`` (with ``hud.pack_kind = "architecture_doc"`` and semantic
scene ``kind`` annotations) and builds ``slides.html`` + ``deck.json`` via
``build_narration.py``.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import subprocess
import sys

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent
BUILDER = SCRIPT_DIR / "build_narration.py"
VENDORED_MERMAID = SKILL_DIR / "assets" / "mermaid.min.js.gz"


def _slugify(text: str, fallback: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    slug = re.sub(r"-+", "-", slug)[:36].strip("-")
    return slug or fallback


def _clean_plain(md: str) -> str:
    """Strip Markdown formatting and unsafe punctuation for crisp spoken narration."""
    text = re.sub(r"```.*?```", " ", md, flags=re.S)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"[*_>#]+", " ", text)
    text = re.sub(r"^\s*[-+]\s+", "", text, flags=re.M)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _sentences(text: str, limit: int = 2) -> list[str]:
    clean = _clean_plain(text)
    if not clean:
        return []
    parts = [p.strip() for p in re.split(r"(?<=[.!?])\s+", clean) if p.strip()]
    picked: list[str] = []
    for p in parts:
        # Keep spoken captions crisp and readable
        if len(p) > 220:
            p = p[:217].rsplit(" ", 1)[0].rstrip(",;:") + "."
        if not p.endswith((".", "!", "?")):
            p += "."
        picked.append(p)
        if len(picked) >= limit:
            break
    return picked


def _estimate_duration(text: str) -> float:
    words = len(re.findall(r"[A-Za-z0-9]+", text))
    # Natural executive reading/speaking pace (~155 WPM = 2.58 words/sec), min 4.5s
    return round(max(4.5, words / 2.58 + 0.6), 2)


def _infer_scene_kind(index: int, total: int, heading: str, mermaid: str) -> str:
    if index == 0:
        return "opening"
    if total > 1 and index == total - 1:
        return "closing"
    if re.search(r"subgraph\s+Before\b", mermaid, re.I) and re.search(r"subgraph\s+After\b", mermaid, re.I):
        return "comparison"
    h = heading.lower()
    if any(k in h for k in ("trust", "security", "boundary", "proof", "evidence", "guard", "budget")):
        return "evidence"
    if any(k in h for k in ("dual-lane", "compare", "comparison", "vs", "before")):
        return "comparison"
    return "narrative"


def parse_markdown_doc(md_text: str, doc_path: pathlib.Path) -> tuple[str, str, list[dict]]:
    """Extract document title, intro summary, and sections with Mermaid diagrams."""
    h1_m = re.search(r"^#\s+(.+)$", md_text, flags=re.M)
    title = h1_m.group(1).strip() if h1_m else doc_path.stem.replace("-", " ").title()

    # Extract optional blockquote / intro paragraph before first ##
    first_h2 = re.search(r"^##\s+", md_text, flags=re.M)
    preamble = md_text[: first_h2.start()] if first_h2 else md_text
    if h1_m:
        preamble = preamble[h1_m.end() :]
    summary_sents = _sentences(preamble, limit=2)
    summary = " ".join(summary_sents) if summary_sents else f"Architecture & flow walkthrough generated from {doc_path.name}."

    # Split into ## sections
    matches = list(re.finditer(r"^##\s+(.+)$", md_text, flags=re.M))
    raw_sections: list[tuple[str, str]] = []
    for i, m in enumerate(matches):
        heading = re.sub(r"^\d+\.\s*", "", m.group(1).strip())
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(md_text)
        raw_sections.append((heading, md_text[start:end].strip()))

    if not raw_sections:
        raw_sections = [(title, md_text)]

    sections: list[dict] = []
    for idx, (heading, body) in enumerate(raw_sections):
        mermaid_m = re.search(r"```mermaid\s*\n(.*?)\n```", body, flags=re.S)
        if mermaid_m:
            mermaid = mermaid_m.group(1).strip()
        else:
            # Synthesize a compact fallback diagram from bullet points when a section has no Mermaid block
            bullets = re.findall(r"^\s*[-*]\s+\*\*([^*]+)\*\*:?\s*(.*)$", body, flags=re.M)
            if bullets:
                lines = ["flowchart LR"]
                for bi, (lbl, desc) in enumerate(bullets[:4], start=1):
                    short_lbl = re.sub(r'["<>]', "", lbl)[:36]
                    short_desc = re.sub(r'["<>]', "", _clean_plain(desc))[:44]
                    node_txt = f"{short_lbl}<br>{short_desc}" if short_desc else short_lbl
                    lines.append(f'  N{bi}["{node_txt}"]')
                if len(bullets) > 1:
                    lines.append("  " + " --> ".join(f"N{bi}" for bi in range(1, min(len(bullets), 4) + 1)))
                mermaid = "\n".join(lines)
            else:
                safe_h = re.sub(r'["<>]', "", heading)[:52]
                mermaid = f'flowchart LR\n  S["{safe_h}"]'

        prose = re.sub(r"```mermaid.*?```", " ", body, flags=re.S)
        captions = _sentences(prose, limit=2)
        if not captions:
            captions = [f"This section covers {heading}."]
        narration = " ".join(captions)
        sections.append(
            {
                "heading": heading,
                "mermaid": mermaid,
                "captions": captions,
                "narration": narration,
            }
        )
    return title, summary, sections


def build_doc_plan(
    doc_path: pathlib.Path,
    *,
    title_override: str | None = None,
    producer: str = "Architecture Doc Brief",
    repo_slug: str | None = None,
    tts_cfg: dict | None = None,
) -> dict:
    md_text = doc_path.read_text(encoding="utf-8")
    doc_title, summary, sections = parse_markdown_doc(md_text, doc_path)
    title = title_override or doc_title
    total = len(sections)

    scenes: list[dict] = []
    for idx, sec in enumerate(sections):
        sid = f"{idx + 1:02d}-{_slugify(sec['heading'], f'section-{idx + 1}')}"
        kind = _infer_scene_kind(idx, total, sec["heading"], sec["mermaid"])
        captions = list(sec["captions"])
        narration = sec["narration"]
        if idx == total - 1:
            invite = "Tell me which part of this architecture you want to drill into, revise, or prototype next."
            if not re.search(r"\b(tell me|let me know|which|your call|reply)\b", narration, re.I):
                narration = narration.rstrip() + " " + invite
                captions.append(invite)

        scene_obj: dict = {
            "id": sid,
            "title": sec["heading"],
            "mermaid": sec["mermaid"],
            "narration": narration,
            "captions": captions,
            "audio": f"audio/{sid}.wav",
            "duration": _estimate_duration(narration),
            "kind": kind,
            "meta": {"sourceDoc": str(doc_path.name), "sectionIndex": idx + 1},
        }
        if idx == 0:
            scene_obj["choices"] = [
                {
                    "label": f"Open source doc ({doc_path.name})",
                    "action": "open_source_doc",
                    "variant": "secondary",
                    "context": {"docPath": str(doc_path.resolve())},
                }
            ]
        scenes.append(scene_obj)

    surface_id = f"doc-discussion-{_slugify(doc_path.stem, 'arch')}"
    plan: dict = {
        "title": title,
        **({"repo": repo_slug} if repo_slug else {}),
        "mermaid_source": str(VENDORED_MERMAID),
        "hud": {
            "pack_kind": "architecture_doc",
            "producer": producer,
            "summary": summary,
            "oversight_default": "lead",
            "artifacts": [
                {
                    "label": f"Source Doc: {doc_path.name}",
                    "url": doc_path.resolve().as_uri(),
                    "kind": "doc",
                }
            ],
        },
        "a2ui": [
            {
                "version": "v1.0",
                "createSurface": {
                    "surfaceId": surface_id,
                    "catalogId": "https://a2ui.org/specification/v1_0/basic_catalog.json",
                },
            },
            {
                "version": "v1.0",
                "updateComponents": {
                    "surfaceId": surface_id,
                    "components": [
                        {"id": "root", "component": "Column", "children": ["prompt", "note", "actions"]},
                        {"id": "prompt", "component": "Text", "variant": "h3", "text": f"Architecture review ready for {doc_path.name} — where should we dive deeper?"},
                        {"id": "note", "component": "Text", "variant": "body", "text": "Generated deterministically from Markdown + Mermaid in under a second. Click any option or type a question to wake the agent."},
                        {"id": "actions", "component": "Row", "children": ["act-drill", "act-revise", "act-voice"]},
                        {"id": "lbl-drill", "component": "Text", "text": "Walk through control flow & edge cases"},
                        {"id": "act-drill", "component": "Button", "variant": "primary", "child": "lbl-drill", "action": {"name": "discuss_doc_section", "context": {"docPath": str(doc_path.resolve()), "focus": "control_flow"}}},
                        {"id": "lbl-revise", "component": "Text", "text": "Propose edits or ADR follow-up"},
                        {"id": "act-revise", "component": "Button", "variant": "secondary", "child": "lbl-revise", "action": {"name": "propose_doc_revision", "context": {"docPath": str(doc_path.resolve())}}},
                        {"id": "lbl-voice", "component": "Text", "text": "Synthesize US_James audio for this doc"},
                        {"id": "act-voice", "component": "Button", "variant": "secondary", "child": "lbl-voice", "action": {"name": "synthesize_doc_audio", "context": {"docPath": str(doc_path.resolve())}}},
                    ],
                },
            },
        ],
        "scenes": scenes,
        "disposition": {
            "kind": "architecture_doc_discussion",
            "prompt": f"Architecture review ready for {doc_path.name} — where should we dive deeper?",
            "options": [
                "Walk through control flow & edge cases",
                "Propose edits or ADR follow-up",
                "Synthesize US_James audio for this doc",
            ],
            "note": "Generated deterministically from Markdown + Mermaid. No answer is also valid.",
        },
    }
    if tts_cfg:
        plan["tts"] = dict(tts_cfg)
        plan["tts"].setdefault("anchor_scene", scenes[0]["id"])
    return plan


def main() -> int:
    ap = argparse.ArgumentParser(description="Fast Markdown + Mermaid -> narrated-walkthrough pack generator.")
    ap.add_argument("--doc", required=True, help="Path to source Markdown architecture/design document")
    ap.add_argument("--out-dir", required=True, help="Output directory where timeline.json and slides.html are written")
    ap.add_argument("--title", default=None, help="Optional deck title override")
    ap.add_argument("--producer", default="Architecture Doc Brief", help="HUD producer/agent label")
    ap.add_argument("--repo", default=None, help="Optional GitHub owner/repo slug")
    ap.add_argument("--with-audio", action="store_true", help="Also synthesize TTS WAVs + gapless master.wav")
    ap.add_argument("--endpoint", default=None, help="TTS endpoint when --with-audio is set")
    ap.add_argument("--model", default=None, help="TTS model when --with-audio is set")
    ap.add_argument("--voice", default=None, help="TTS voice when --with-audio is set")
    ap.add_argument("--target-sps", type=float, default=6.5, help="Target voiced syllables/sec when --with-audio is set")
    args = ap.parse_args()

    doc_path = pathlib.Path(args.doc).resolve()
    if not doc_path.is_file():
        print(f"FAIL doc not found: {doc_path}", file=sys.stderr)
        return 2

    out_dir = pathlib.Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    tts_cfg = None
    if args.with_audio:
        if not (args.endpoint and args.model and args.voice):
            print("FAIL --with-audio requires --endpoint, --model, and --voice", file=sys.stderr)
            return 2
        tts_cfg = {
            "endpoint": args.endpoint,
            "model": args.model,
            "voice": args.voice,
            "language": "en",
            "temperature": 0.0,
            "pace": {"enabled": True, "target_sps": args.target_sps, "lead_ms": 180, "tail_ms": 420},
        }

    plan = build_doc_plan(
        doc_path,
        title_override=args.title,
        producer=args.producer,
        repo_slug=args.repo,
        tts_cfg=tts_cfg,
    )
    plan_path = out_dir / "timeline.json"
    plan_path.write_text(json.dumps(plan, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    cmd = [sys.executable, str(BUILDER), "--plan", str(plan_path)]
    if not args.with_audio:
        cmd.append("--no-audio")
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        print(proc.stdout + proc.stderr, file=sys.stderr)
        return proc.returncode

    print(
        json.dumps(
            {
                "ok": True,
                "doc": str(doc_path),
                "outDir": str(out_dir),
                "slides": str(out_dir / "slides.html"),
                "scenes": len(plan["scenes"]),
                "packKind": "architecture_doc",
                "audioSynthesized": bool(args.with_audio),
            }
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
