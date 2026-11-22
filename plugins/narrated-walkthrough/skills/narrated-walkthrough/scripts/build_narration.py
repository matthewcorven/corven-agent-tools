#!/usr/bin/env python3
"""Build a narrated, synchronized walkthrough deck from a timeline plan.

Synthesizes narration locally (OpenAI-compatible /v1/audio/speech), measures each
clip, derives exact start offsets, and renders a single-file deck whose audio,
diagrams, captions and scrubber are all driven by that one timeline.

  python3 build_narration.py                 # build (only missing clips)
  python3 build_narration.py --validate      # check the plan, synthesize nothing
  python3 build_narration.py --serve         # build + serve on :8765
  python3 build_narration.py --stop          # stop the server
  python3 build_narration.py --only 04-x --voice af_heart   # re-voice one scene

Exit codes: 0 ok, 1 build/runtime failure, 2 invalid plan (validation).
"""
from __future__ import annotations

import argparse
import base64
import gzip
import json
import pathlib
import re
import shutil
import struct
import subprocess
import sys
import urllib.error
import urllib.request

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
ROOT = SCRIPT_DIR  # replaced with the plan's directory once the plan is located

# The ONLY network reference in this builder, reachable solely through --cdn.
# Pinned to an exact version: a floating major would let a walkthrough recorded
# today render differently six months from now, which is the wrong shape for an
# artifact whose whole point is a frozen timeline.
CDN_MERMAID = "https://cdn.jsdelivr.net/npm/mermaid@12.3.0/dist/mermaid.min.js"

# Phrases that make an invitation to respond unmistakable in spoken narration.
# Deliberately broad: the contract is "the listener is asked", not a fixed script.
INVITATION = re.compile(
    r"\b(tell me|let me know|which (one|way)|your call|your move|"
    r"answer|respond|reply|say (it|which)|pick one|your choice|"
    r"what would you like|where do you want)\b",
    re.IGNORECASE,
)
FAREWELL = re.compile(r"\b(thanks for watching|thank you for watching|that'?s all)\b", re.IGNORECASE)


class PlanError(Exception):
    """The plan is internally inconsistent or breaks the disposition contract."""


def synth(
    text: str,
    out: pathlib.Path,
    endpoint: str,
    model: str,
    voice: str,
    *,
    ref_audio_b64: str | None = None,
    ref_text: str | None = None,
    language: str | None = None,
    temperature: float | None = None,
) -> None:
    payload: dict = {"model": model, "input": text, "voice": voice, "response_format": "wav"}
    if ref_audio_b64 is not None:
        payload["ref_audio"] = ref_audio_b64
    if ref_text is not None:
        payload["ref_text"] = ref_text
    if language is not None:
        payload["language"] = language
    if temperature is not None:
        payload["temperature"] = temperature
    body = json.dumps(payload).encode()
    req = urllib.request.Request(endpoint, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            out.write_bytes(resp.read())
    except urllib.error.URLError as exc:
        raise SystemExit(
            f"TTS request failed ({endpoint}, model={model}): {exc}\n"
            "Is the local server running? Try: curl -s <host>/v1/models"
        ) from exc


def duration(path: pathlib.Path) -> float:
    """Clip length in seconds, read from the RIFF header.

    The synth request pins ``response_format: "wav"``, so a non-WAV payload means the
    server ignored that field. That is a server contract breach, not something to paper
    over with a platform-specific probe: fail with the fix in the message.
    """
    data = path.read_bytes()
    if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise SystemExit(
            f"{path.name} is not RIFF/WAVE (first bytes {data[:4]!r}). The TTS server "
            "ignored response_format=wav; fix the server or pin a compatible model."
        )
    pos, byte_rate, data_bytes = 12, None, None
    while pos + 8 <= len(data):
        chunk_id = data[pos:pos + 4]
        (size,) = struct.unpack("<I", data[pos + 4:pos + 8])
        body = pos + 8
        if chunk_id == b"fmt ":
            _, _channels, _rate, byte_rate, _, _bits = struct.unpack("<HHIIHH", data[body:body + 16])
        elif chunk_id == b"data":
            data_bytes = size if size not in (0, 0xFFFFFFFF) else len(data) - body
        pos = body + size + (size & 1)
    if not byte_rate or data_bytes is None:
        raise SystemExit(f"{path.name}: WAV header has no fmt or data chunk")
    return data_bytes / byte_rate


def validate(tl: dict) -> list[str]:
    """Return contract violations. An empty list means the plan may ship."""
    problems: list[str] = []
    scenes = tl.get("scenes") or []
    if not scenes:
        return ["plan has no scenes"]

    seen: set[str] = set()
    for i, sc in enumerate(scenes):
        sid = sc.get("id") or f"<scene {i}>"
        for field in ("id", "title", "mermaid", "narration", "audio"):
            if not sc.get(field):
                problems.append(f"{sid}: missing '{field}'")
        if sid in seen:
            problems.append(f"{sid}: duplicate id")
        seen.add(sid)
        if not (sc.get("captions") or []):
            problems.append(f"{sid}: no captions")
        if sc.get("narration") and FAREWELL.search(sc["narration"]):
            problems.append(
                f"{sid}: narration uses a broadcast sign-off; a walkthrough that leaves work "
                "open must invite a reply instead"
            )

    tts = tl.get("tts") or {}
    if isinstance(tts, dict):
        anchor_scene = tts.get("anchor_scene")
        ref_audio = tts.get("ref_audio")
        ref_text = tts.get("ref_text")
        if anchor_scene and anchor_scene not in seen:
            problems.append(f"tts.anchor_scene '{anchor_scene}' does not match any scene id")
        if anchor_scene and ref_audio:
            problems.append("tts: 'anchor_scene' and 'ref_audio' are mutually exclusive")
        if ref_text and not ref_audio:
            problems.append("tts: 'ref_text' requires 'ref_audio'")
        temp = tts.get("temperature")
        if temp is not None and (isinstance(temp, bool) or not isinstance(temp, (int, float)) or temp < 0):
            problems.append("tts.temperature must be a non-negative number")

    disp = tl.get("disposition")
    if disp:
        if not disp.get("prompt"):
            problems.append("disposition: missing 'prompt'")
        if not disp.get("options"):
            problems.append("disposition: missing 'options'")
        last = scenes[-1]
        spoken = " ".join([last.get("narration", "")] + list(last.get("captions") or []))
        if not INVITATION.search(spoken):
            problems.append(
                f"disposition is set but the final scene ({last.get('id')}) never invites a "
                "reply — name the invitation in its narration and last caption"
            )
    return problems


MERMAID_GZIP = "mermaid.min.js.gz"


def resolve_mermaid(tl: dict, base: pathlib.Path, use_cdn: bool) -> str:
    """Resolve the Mermaid script to something the page can load.

    Offline is the only default. A vendored `.js` is embedded directly into the
    deck (no sibling file to lose); a vendored `.js.gz` is decompressed into the
    bundle; anything else is treated as a path or URL and passed through.
    """
    if use_cdn:
        return CDN_MERMAID
    src = tl.get("mermaid_source")
    if not src:
        raise PlanError(
            "mermaid_source not set. This builder ships no CDN default: name a local "
            "Mermaid path in the plan (offline, reproducible), or pass --cdn to opt into "
            f"the pinned build at {CDN_MERMAID}"
        )
    path = (base / src).resolve()
    if path.suffix == ".gz" and path.exists():
        return _inline_script(gzip.decompress(path.read_bytes()).decode("utf-8"))
    if path.exists() and path.suffix == ".js":
        return _inline_script(path.read_text(encoding="utf-8"))
    return src  # a URL, or a path the page resolves itself


def _inline_script(body: str) -> str:
    return "<script>\n" + body.replace("</script", "<\\/script") + "\n</script>"


def render(tl: dict) -> str:
    scenes = tl["scenes"]
    cues = []
    for sc in scenes:
        n = len(sc["captions"])
        step = sc["duration"] / n
        for i, text in enumerate(sc["captions"]):
            cues.append(
                {
                    "t": round(sc["start"] + i * step, 3),
                    "end": round(sc["start"] + (i + 1) * step, 3),
                    "text": text,
                    "scene": sc["id"],
                }
            )

    data = json.dumps(
        {
            "scenes": [
                {
                    "id": s["id"],
                    "title": s["title"],
                    "mermaid": s["mermaid"],
                    "start": s["start"],
                    "duration": s["duration"],
                    "audio": s["audio"],
                    "narration": s["narration"],
                }
                for s in scenes
            ],
            "cues": cues,
            "total": tl["total_duration"],
            "disposition": tl.get("disposition"),
        },
        ensure_ascii=False,
    )
    mermaid = tl["_mermaid_resolved"]
    tts_cfg = tl.get("tts") or {}
    return (
        template().replace("__DATA__", data)
        .replace("__TITLE__", tl["title"])
        .replace("__VOICE__", tts_cfg.get("voice") or "none")
        .replace("__MODEL__", tts_cfg.get("model") or "none")
        .replace("__MERMAID__", mermaid)
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", default="timeline.json")
    ap.add_argument("--voice", default=None)
    ap.add_argument("--model", default=None)
    ap.add_argument("--endpoint", default=None)
    ap.add_argument("--anchor-scene", default=None, help="scene id used as ref_audio/ref_text anchor for other scenes")
    ap.add_argument("--language", default=None, help="optional language hint forwarded to /v1/audio/speech")
    ap.add_argument("--temperature", type=float, default=None, help="optional sampling temperature forwarded to /v1/audio/speech")
    ap.add_argument("--only", default=None, help="re-synthesize a single scene id (overwriting its clip)")
    ap.add_argument("--force", action="store_true", help="re-synthesize all clips even when WAV files already exist")
    ap.add_argument("--validate", action="store_true", help="check the plan and exit")
    ap.add_argument(
        "--cdn",
        action="store_true",
        help="use the pinned Mermaid CDN build instead of a local mermaid_source",
    )
    ap.add_argument(
        "--no-audio",
        action="store_true",
        help="render from the durations already recorded in the plan; synthesize nothing",
    )
    ap.add_argument("--serve", action="store_true", help="serve the deck after building")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--stop", action="store_true", help="stop the deck server")
    args = ap.parse_args()

    if args.stop:
        subprocess.run(["pkill", "-f", f"http.server {args.port}"], check=False)
        print("stopped deck server")
        return 0

    global ROOT
    plan_path = pathlib.Path(args.plan)
    if not plan_path.is_absolute():
        plan_path = pathlib.Path.cwd() / plan_path
    plan_path = plan_path.resolve()
    if not plan_path.exists():
        print(f"FAIL no plan at {plan_path}")
        return 1
    ROOT = plan_path.parent
    tl = json.loads(plan_path.read_text())
    has_tts_input = bool(tl.get("tts")) or any(
        v is not None
        for v in (args.model, args.voice, args.endpoint, args.anchor_scene, args.language, args.temperature)
    )
    tts = tl.setdefault("tts", {}) if (has_tts_input or not args.no_audio) else {}
    if args.model is not None:
        tts["model"] = args.model
    if args.voice is not None:
        tts["voice"] = args.voice
    if args.endpoint is not None:
        tts["endpoint"] = args.endpoint
    if args.anchor_scene is not None:
        tts["anchor_scene"] = args.anchor_scene
    if args.language is not None:
        tts["language"] = args.language
    if args.temperature is not None:
        tts["temperature"] = args.temperature
    missing_cfg = [] if args.no_audio else [
        k for k in ("endpoint", "model", "voice") if not tts.get(k)
    ]
    if missing_cfg:
        print(
            f"FAIL tts.{', tts.'.join(missing_cfg)} not configured. This builder ships no "
            "defaults: name the endpoint, model and voice in the plan (or pass --endpoint "
            "--model --voice). Discover what a local server offers with: "
            "curl -s <host>/v1/models"
        )
        return 2

    try:
        tl["_mermaid_resolved"] = resolve_mermaid(tl, plan_path.parent, args.cdn)
    except PlanError as exc:
        print(f"FAIL {exc}")
        return 2

    problems = validate(tl)
    if args.validate:
        for p in problems:
            print(f"FAIL {p}")
        print("plan ok" if not problems else f"{len(problems)} problem(s)")
        return 0 if not problems else 2

    if problems:
        for p in problems:
            print(f"FAIL {p}")
        print("\nrefusing to build: fix the plan, or drop 'disposition' if nothing is open")
        return 2

    if args.no_audio:
        stale = [s["id"] for s in tl["scenes"] if not s.get("duration")]
        if stale:
            print(f"FAIL --no-audio needs recorded durations; missing for {stale}")
            return 2
    if not args.no_audio:
        (ROOT / "audio").mkdir(exist_ok=True)
        anchor_id = tts.get("anchor_scene")
        ext_ref_path = tts.get("ref_audio")
        ext_ref_b64: str | None = None
        ext_ref_text: str | None = tts.get("ref_text")
        if ext_ref_path:
            resolved_ref = (ROOT / ext_ref_path).resolve()
            if not resolved_ref.exists():
                raise PlanError(f"tts.ref_audio not found: {resolved_ref}")
            ext_ref_b64 = base64.b64encode(resolved_ref.read_bytes()).decode("ascii")

        scenes_by_id = {s["id"]: s for s in tl["scenes"]}
        if args.only and args.only not in scenes_by_id:
            raise PlanError(f"--only '{args.only}' does not match any scene id")

        # Synthesize the anchor scene first so every subsequent clip can condition on it.
        ordered_scenes = list(tl["scenes"])
        if anchor_id and anchor_id in scenes_by_id:
            anchor_sc = scenes_by_id[anchor_id]
            ordered_scenes = [anchor_sc] + [s for s in ordered_scenes if s["id"] != anchor_id]

        anchor_b64: str | None = None
        anchor_text: str | None = None
        for scene in ordered_scenes:
            is_anchor = bool(anchor_id and scene["id"] == anchor_id)
            wav = ROOT / scene["audio"]
            targeted = args.only is None or scene["id"] == args.only
            need_synth = targeted and (args.force or args.only == scene["id"] or not wav.exists())

            if is_anchor:
                if need_synth:
                    print(f"  synth {scene['id']} (anchor) ...", flush=True)
                    synth(
                        scene["narration"],
                        wav,
                        tts["endpoint"],
                        tts["model"],
                        tts["voice"],
                        language=tts.get("language"),
                        temperature=tts.get("temperature"),
                    )
                if not wav.exists():
                    raise PlanError(
                        f"anchor scene '{anchor_id}' has no audio at {wav}; run without --only first"
                    )
                anchor_b64 = base64.b64encode(wav.read_bytes()).decode("ascii")
                anchor_text = scene["narration"]
                if targeted:
                    scene["duration"] = round(duration(wav), 3)
            elif targeted:
                if need_synth:
                    ref_b64 = anchor_b64 if anchor_id else ext_ref_b64
                    ref_txt = anchor_text if anchor_id else ext_ref_text
                    suffix = f" (anchored to {anchor_id})" if anchor_id else ""
                    print(f"  synth {scene['id']}{suffix} ...", flush=True)
                    synth(
                        scene["narration"],
                        wav,
                        tts["endpoint"],
                        tts["model"],
                        tts["voice"],
                        ref_audio_b64=ref_b64,
                        ref_text=ref_txt,
                        language=tts.get("language"),
                        temperature=tts.get("temperature"),
                    )
                scene["duration"] = round(duration(wav), 3)

    missing = [s["id"] for s in tl["scenes"] if s.get("duration") is None]
    if missing:
        raise PlanError(f"no duration for {missing}; run without --only first")

    t0 = 0.0
    for scene in tl["scenes"]:
        scene["start"] = round(t0, 3)
        t0 += scene["duration"]
    tl["total_duration"] = round(t0, 3)

    (ROOT / "slides.html").write_text(render(tl))
    resolved_mermaid = tl.pop("_mermaid_resolved", "")
    plan_path.write_text(json.dumps(tl, indent=2, ensure_ascii=False) + "\n")
    manifest = {
        "plugin": "narrated-walkthrough",
        "player": "player/player.html",
        "schema": "narrated-timeline/v1",
        "mermaid": "12.3.0" if "12.3.0" in resolved_mermaid else "local",
        "scenes": len(tl["scenes"]),
        "total_duration": tl["total_duration"],
        "generated_by": "build_narration.py",
    }
    (ROOT / "deck.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"total {tl['total_duration']}s across {len(tl['scenes'])} scenes")

    if args.serve:
        subprocess.run(["pkill", "-f", f"http.server {args.port}"], check=False)
        subprocess.Popen(
            [sys.executable, "-m", "http.server", str(args.port), "--bind", "127.0.0.1"],
            cwd=ROOT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        print(f"serving http://127.0.0.1:{args.port}/slides.html")
    return 0


PLAYER = pathlib.Path(__file__).resolve().parent.parent / "player" / "player.html"


def template() -> str:
    """The player is a shipped artifact, not a Python string literal.

    It is the same file every repo's deck is built from, so the UI of a narrated
    walkthrough is versioned and diffable instead of being duplicated per repo.
    """
    return PLAYER.read_text(encoding="utf-8")


if __name__ == "__main__":
    try:
        sys.exit(main())
    except PlanError as exc:
        print(f"FAIL {exc}")
        sys.exit(2)
