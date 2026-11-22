"""Contract tests for the narrated-walkthrough skill package (stdlib only)."""

import base64
import hashlib
import http.server
import json
import pathlib
import re
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1]
SCHEMA_PATH = PLUGIN / "skills" / "narrated-walkthrough" / "schemas" / "narrated-timeline.v1.json"
SKILL = PLUGIN / "skills" / "narrated-walkthrough"
BUILDER = SKILL / "scripts" / "build_narration.py"
GOLDEN = SKILL / "examples" / "golden"

sys.path.insert(0, str(BUILDER.parent))
import build_narration as bn  # noqa: E402


def plan(**overrides):
    base = {
        "title": "t",
        "tts": {"model": "m", "voice": "v"},
        "scenes": [
            {
                "id": "a",
                "title": "A",
                "mermaid": "flowchart LR\n    x",
                "narration": "One claim.",
                "captions": ["One claim."],
                "audio": "audio/a.wav",
            }
        ],
    }
    base.update(overrides)
    return base


class PlanContractTests(unittest.TestCase):
    def test_golden_plan_validates(self):
        tl = json.loads((GOLDEN / "timeline.json").read_text())
        self.assertEqual(bn.validate(tl), [])

    def test_required_fields_are_enforced(self):
        tl = plan()
        del tl["scenes"][0]["mermaid"]
        self.assertTrue(any("mermaid" in p for p in bn.validate(tl)))

    def test_duplicate_ids_are_rejected(self):
        tl = plan()
        tl["scenes"].append(dict(tl["scenes"][0]))
        self.assertTrue(any("duplicate" in p for p in bn.validate(tl)))

    def test_empty_plan_is_rejected(self):
        self.assertEqual(bn.validate({"scenes": []}), ["plan has no scenes"])


class DispositionContractTests(unittest.TestCase):
    """The rule the user asked for: open work must invite a reply."""

    def test_open_disposition_without_invitation_fails(self):
        tl = plan(
            disposition={"prompt": "Which way?", "options": ["A", "B"], "note": "n"}
        )
        problems = bn.validate(tl)
        self.assertTrue(any("invites a reply" in p for p in problems))

    def test_invitation_in_narration_passes(self):
        tl = plan(
            scenes=[
                {
                    "id": "z",
                    "title": "Z",
                    "mermaid": "flowchart LR\n    x",
                    "narration": "That leaves two options. Tell me which one you want.",
                    "captions": ["That leaves two options."],
                    "audio": "audio/z.wav",
                }
            ],
            disposition={"prompt": "Which way?", "options": ["A", "B"], "note": "n"},
        )
        self.assertEqual(bn.validate(tl), [])

    def test_invitation_in_last_caption_passes(self):
        tl = plan(
            disposition={"prompt": "Which way?", "options": ["A", "B"], "note": "n"}
        )
        tl["scenes"][0]["captions"] = ["Pick one and reply in the chat."]
        self.assertEqual(bn.validate(tl), [])

    def test_broadcast_signoff_is_rejected(self):
        tl = plan()
        tl["scenes"][0]["narration"] = "That's the whole story. Thanks for watching."
        self.assertTrue(any("sign-off" in p for p in bn.validate(tl)))

    def test_disposition_requires_prompt_and_options(self):
        tl = plan(disposition={"note": "n"})
        tl["scenes"][0]["narration"] = "Tell me which one you want."
        problems = bn.validate(tl)
        self.assertTrue(any("'prompt'" in p for p in problems))
        self.assertTrue(any("'options'" in p for p in problems))


class NoDefaultsTests(unittest.TestCase):
    """A missing endpoint must never silently target someone's local port."""

    def test_missing_tts_config_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan_file = pathlib.Path(tmp) / "timeline.json"
            tl = plan()
            tl["tts"] = {}
            plan_file.write_text(json.dumps(tl))
            result = subprocess.run(
                [sys.executable, str(BUILDER), "--plan", str(plan_file)],
                capture_output=True, text=True,
            )
        self.assertEqual(result.returncode, 2)
        self.assertIn("not configured", result.stdout)
        self.assertIn("curl -s <host>/v1/models", result.stdout)

    def test_builder_has_no_endpoint_or_model_defaults(self):
        source = BUILDER.read_text()
        for literal in ("18001", "Ming-3.0", "setdefault(\"endpoint\"", "setdefault(\"model\""):
            self.assertNotIn(literal, source, f"builder must not ship a default: {literal}")


class GoldenExampleTests(unittest.TestCase):
    def test_build_output_is_not_committed(self):
        ignore = (GOLDEN / ".gitignore").read_text()
        self.assertIn("audio/", ignore)
        self.assertIn("slides.html", ignore)
        self.assertFalse((GOLDEN / "audio").exists(), "golden audio must not be committed")

    def test_no_audio_path_renders_from_recorded_durations(self):
        result = subprocess.run(
            [sys.executable, str(BUILDER), "--no-audio", "--plan", str(GOLDEN / "timeline.json")],
            capture_output=True, text=True, cwd=str(GOLDEN),
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        deck = (GOLDEN / "slides.html").read_text()
        self.assertIn("disposition", deck)
        (GOLDEN / "slides.html").unlink()


class SchemaTests(unittest.TestCase):
    """The schema is the executable contract; these checks replace prose review."""

    @classmethod
    def setUpClass(cls):
        cls.schema = json.loads(SCHEMA_PATH.read_text())

    def test_schema_declares_itself_without_a_dead_url(self):
        self.assertEqual(self.schema["$id"], "narrated-timeline/v1")
        self.assertFalse(
            str(self.schema["$id"]).startswith("http"),
            "use a repo-relative $id: a dead URL is worse than none, because consumers fetch it",
        )

    def test_every_level_is_closed(self):
        self.assertIs(self.schema.get("additionalProperties"), False)
        for name, sub in self.schema["properties"].items():
            if sub.get("type") == "object":
                self.assertIs(sub.get("additionalProperties"), False, name)
        self.assertIs(self.schema["$defs"]["scene"].get("additionalProperties"), False)

    def test_derived_fields_are_permitted_but_not_required(self):
        required = set(self.schema["$defs"]["scene"]["required"])
        self.assertNotIn("start", required)
        self.assertNotIn("total_duration", set(self.schema["required"]))
        self.assertIn("start", self.schema["$defs"]["scene"]["properties"])

    def test_required_scene_fields_match_the_validator(self):
        required = set(self.schema["$defs"]["scene"]["required"])
        self.assertEqual(
            required, {"id", "title", "mermaid", "narration", "audio", "captions", "duration"}
        )

    def test_tts_is_optional_but_closed_when_present(self):
        self.assertNotIn("tts", self.schema["required"])
        tts = self.schema["properties"]["tts"]
        self.assertEqual(set(tts["required"]), {"endpoint", "model", "voice"})
        self.assertIs(tts["additionalProperties"], False)

    def test_disposition_requires_prompt_and_options_only(self):
        disp = self.schema["properties"]["disposition"]
        self.assertEqual(set(disp["required"]), {"prompt", "options"})
        self.assertNotIn("kind", disp["required"])

    def test_audio_is_a_reference_not_an_artifact_condition(self):
        audio = self.schema["$defs"]["scene"]["properties"]["audio"]
        self.assertEqual(audio["type"], "string")
        self.assertIn("REFERENCE", audio["description"])
        self.assertNotIn("format", audio)


def _shape_errors(instance, schema, path="$", root=None):
    """Minimal draft-2020-12 subset: $ref, type, required, additionalProperties,
    minItems, minLength, exclusiveMinimum, minimum. Enough to prove the contract
    without adding a dependency."""
    root = root if root is not None else schema
    if "$ref" in schema:
        ref = schema["$ref"]
        target = root
        for part in ref.lstrip("#/").split("/"):
            target = target[part]
        return _shape_errors(instance, target, path, root)
    errors = []
    t = schema.get("type")
    if t == "boolean":
        return [] if isinstance(instance, bool) else [f"{path}: expected boolean"]
    if t == "number":
        if isinstance(instance, bool) or not isinstance(instance, (int, float)):
            return [f"{path}: expected number"]
    else:
        pytypes = {"object": dict, "array": list, "string": str}
        if t and not isinstance(instance, pytypes[t]):
            return [f"{path}: expected {t}"]
    if t == "object":
        for key in schema.get("required", []):
            if key not in instance:
                errors.append(f"{path}.{key}: required")
        if schema.get("additionalProperties") is False:
            for key in instance:
                if key not in schema.get("properties", {}):
                    errors.append(f"{path}.{key}: not allowed")
        for key, sub in schema.get("properties", {}).items():
            if key in instance:
                errors += _shape_errors(instance[key], sub, f"{path}.{key}", root)
    elif t == "array":
        if len(instance) < schema.get("minItems", 0):
            errors.append(f"{path}: fewer than {schema['minItems']} items")
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for i, item in enumerate(instance):
                errors += _shape_errors(item, item_schema, f"{path}[{i}]", root)
    elif t == "string":
        if len(instance) < schema.get("minLength", 0):
            errors.append(f"{path}: shorter than {schema['minLength']}")
    elif t == "number":
        if "exclusiveMinimum" in schema and not instance > schema["exclusiveMinimum"]:
            errors.append(f"{path}: must exceed {schema['exclusiveMinimum']}")
        if "minimum" in schema and instance < schema["minimum"]:
            errors.append(f"{path}: must be >= {schema['minimum']}")
    return errors


class SchemaConformanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schema = json.loads(SCHEMA_PATH.read_text())
        cls.valid_scene = {
            "id": "a", "title": "A", "mermaid": "flowchart LR\n    x",
            "narration": "One claim.", "audio": "audio/a.wav",
            "captions": ["One claim."], "duration": 4.0,
        }

    def plan_with(self, **overrides):
        base = {"title": "t", "scenes": [dict(self.valid_scene)]}
        base.update(overrides)
        return base

    def test_golden_plan_conforms(self):
        golden = json.loads((GOLDEN / "timeline.json").read_text())
        self.assertEqual(_shape_errors(golden, self.schema), [])

    def test_minimal_plan_conforms(self):
        self.assertEqual(_shape_errors(self.plan_with(), self.schema), [])

    def test_derived_fields_are_accepted_when_present(self):
        tl = self.plan_with(total_duration=4.0)
        tl["scenes"][0].update({"start": 0, "duration": 4.0})
        self.assertEqual(_shape_errors(tl, self.schema), [])

    def test_each_required_scene_field_is_enforced(self):
        for field in self.schema["$defs"]["scene"]["required"]:
            scene = dict(self.valid_scene)
            del scene[field]
            errors = _shape_errors({"title": "t", "scenes": [scene]}, self.schema)
            self.assertTrue(any(field in e and "required" in e for e in errors), field)

    def test_speculative_fields_are_rejected(self):
        tl = self.plan_with()
        tl["scenes"][0]["chapters"] = []
        self.assertTrue(any("chapters" in e for e in _shape_errors(tl, self.schema)))
        tl = self.plan_with(language_tracks=[])
        self.assertTrue(any("language_tracks" in e for e in _shape_errors(tl, self.schema)))

    def test_tts_requires_all_three_when_present(self):
        tl = self.plan_with(tts={"model": "m", "voice": "v"})
        errors = _shape_errors(tl, self.schema)
        self.assertTrue(any("tts.endpoint" in e for e in errors))

    def test_disposition_requires_prompt_and_options(self):
        tl = self.plan_with(disposition={"note": "n"})
        errors = _shape_errors(tl, self.schema)
        self.assertTrue(any("disposition.prompt" in e for e in errors))
        self.assertTrue(any("disposition.options" in e for e in errors))

    def test_duration_must_be_positive(self):
        tl = self.plan_with()
        tl["scenes"][0]["duration"] = 0
        self.assertTrue(any("duration" in e for e in _shape_errors(tl, self.schema)))

    def test_captions_need_at_least_one(self):
        tl = self.plan_with()
        tl["scenes"][0]["captions"] = []
        self.assertTrue(any("captions" in e for e in _shape_errors(tl, self.schema)))


class NoNetworkDefaultTests(unittest.TestCase):
    """A deck that silently fetches from a CDN cannot be verified as offline."""

    def test_builder_has_no_network_literal_outside_the_cdn_branch(self):
        source = BUILDER.read_text()
        urls = [line for line in source.splitlines() if "https://" in line]
        self.assertEqual(len(urls), 1, f"expected exactly one documented URL, got {urls}")
        self.assertIn("CDN_MERMAID", urls[0])
        self.assertIn("mermaid@12.3.0", urls[0], "the CDN fallback must pin an exact version")

    def test_missing_mermaid_source_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan_file = pathlib.Path(tmp) / "timeline.json"
            tl = plan()
            plan_file.write_text(json.dumps(tl))
            result = subprocess.run(
                [sys.executable, str(BUILDER), "--no-audio", "--plan", str(plan_file)],
                capture_output=True, text=True,
            )
        self.assertEqual(result.returncode, 2)
        self.assertIn("mermaid_source not set", result.stdout)
        self.assertIn("--cdn", result.stdout)

    def test_cdn_flag_uses_the_pinned_build(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan_file = pathlib.Path(tmp) / "timeline.json"
            tl = plan()
            tl["scenes"][0]["duration"] = 1.0
            tl["scenes"][0]["start"] = 0.0
            tl["total_duration"] = 1.0
            plan_file.write_text(json.dumps(tl))
            result = subprocess.run(
                [sys.executable, str(BUILDER), "--no-audio", "--cdn", "--plan", str(plan_file)],
                capture_output=True, text=True,
            )
            deck = (pathlib.Path(tmp) / "slides.html").read_text() if result.returncode == 0 else ""
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("mermaid@12.3.0", deck)

    def test_no_audio_tolerates_a_configured_tts_block(self):
        """A reviewer who keeps their TTS config must not be punished for it."""
        with tempfile.TemporaryDirectory() as tmp:
            plan_file = pathlib.Path(tmp) / "timeline.json"
            tl = plan()
            tl["tts"] = {"endpoint": "http://127.0.0.1:1/v1/audio/speech", "model": "m", "voice": "v"}
            tl["mermaid_source"] = str(PLUGIN / "skills" / "narrated-walkthrough" / "assets" / "mermaid.min.js.gz")
            tl["scenes"][0].update({"duration": 1.0, "start": 0.0})
            tl["total_duration"] = 1.0
            plan_file.write_text(json.dumps(tl))
            result = subprocess.run(
                [sys.executable, str(BUILDER), "--no-audio", "--plan", str(plan_file)],
                capture_output=True, text=True,
            )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_golden_deck_fetches_nothing(self):
        """No <script src>, <link href> or @import may reach the network. Note the
        inlined Mermaid bundle contains URLs in its own error messages, so the
        assertion is on fetchable references, not on the substring 'https://'."""
        result = subprocess.run(
            [sys.executable, str(BUILDER), "--no-audio", "--plan", str(GOLDEN / "timeline.json")],
            capture_output=True, text=True, cwd=str(GOLDEN),
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        deck = (GOLDEN / "slides.html").read_text()
        (GOLDEN / "slides.html").unlink()
        external = re.findall(
            r'<script[^>]+src=["\'](https?://[^"\']+)|<link[^>]+href=["\'](https?://[^"\']+)'
            r'|@import\s+url\(["\']?(https?://[^)"\']+)',
            deck,
        )
        self.assertEqual([u for group in external for u in group if u], [])
        self.assertIn("mermaid", deck, "the vendored bundle should be inlined")

    def test_vendored_license_travels_with_the_bundle(self):
        """MIT requires the notice to accompany the binary; a naive include list
        that filters on *.js would drop both the license and the provenance note."""
        assets = PLUGIN / "skills" / "narrated-walkthrough" / "assets"
        license_text = (assets / "mermaid.LICENSE").read_text()
        self.assertIn("MIT License", license_text)
        self.assertIn("Knut Sveidqvist", license_text)
        note = (assets / "mermaid.VENDORED.md").read_text()
        self.assertIn("mermaid.LICENSE", note, "the provenance note must point at the license")
        self.assertIn("integrity", note.lower(), "the bump path must verify registry integrity first")

    def test_vendored_asset_is_pinned_and_hashed(self):
        assets = PLUGIN / "skills" / "narrated-walkthrough" / "assets"
        self.assertTrue((assets / "mermaid.min.js.gz").exists())
        self.assertTrue((assets / "mermaid.LICENSE").exists())
        note = (assets / "mermaid.VENDORED.md").read_text()
        self.assertIn("12.3.0", note)
        digest = hashlib.sha256((assets / "mermaid.min.js.gz").read_bytes()).hexdigest()
        self.assertIn(digest, note, "the recorded hash must match the shipped asset")


class ClockTests(unittest.TestCase):
    """Offsets are sums of measured durations, never authored."""

    def test_offsets_are_cumulative(self):
        tl = json.loads((GOLDEN / "timeline.json").read_text())
        scenes = tl["scenes"]
        if any("start" not in s for s in scenes):
            self.skipTest("golden plan has not been built yet")
        total = 0.0
        for scene in scenes:
            self.assertAlmostEqual(scene["start"], total, delta=1e-6)
            total += scene["duration"]
        self.assertAlmostEqual(tl["total_duration"], total, delta=1e-6)

    def test_cues_cover_every_scene_in_order(self):
        tl = json.loads((GOLDEN / "timeline.json").read_text())
        if any("duration" not in s or s["duration"] is None for s in tl["scenes"]):
            self.skipTest("golden plan has not been built yet")
        cues = []
        for scene in tl["scenes"]:
            step = scene["duration"] / len(scene["captions"])
            cues += [scene["start"] + i * step for i in range(len(scene["captions"]))]
        self.assertEqual(cues, sorted(cues))
        self.assertEqual(len(cues), sum(len(s["captions"]) for s in tl["scenes"]))


class PackageTests(unittest.TestCase):
    def test_skill_front_matter(self):
        text = (SKILL / "SKILL.md").read_text()
        self.assertTrue(text.startswith("---\n"))
        block = text.split("---")[1]
        self.assertIn("name: narrated-walkthrough", block)
        self.assertIn("USE FOR:", block)
        self.assertIn("DO NOT USE FOR:", block)

    def test_relative_links_resolve(self):
        documents = [PLUGIN / "README.md", SKILL / "SKILL.md"]
        documents += list((SKILL / "references").glob("*.md"))
        documents += [GOLDEN / "README.md"]
        for document in documents:
            for link in re.findall(r"\[[^\]]+\]\(([^)#]+)\)", document.read_text()):
                if link.startswith(("http", "mailto")):
                    continue
                self.assertTrue((document.parent / link).exists(), f"{document.name} -> {link}")

    def test_builder_has_no_third_party_imports(self):
        source = BUILDER.read_text()
        imports = re.findall(r"^(?:import|from)\s+([a-zA-Z0-9_.]+)", source, re.M)
        roots = {name.split(".")[0] for name in imports}
        allowed = {"__future__", "argparse", "base64", "json", "math", "pathlib", "re", "statistics",
                   "subprocess", "sys", "tempfile", "urllib", "shutil", "struct", "gzip"}
        self.assertTrue(roots <= allowed, roots - allowed)

    def test_pr_review_walkthrough_reference_ships_and_guards_merge_authority(self):
        ref = SKILL / "references" / "pr-review-walkthrough.md"
        self.assertTrue(ref.is_file(), "pr-review-walkthrough.md reference must exist")
        text = ref.read_text(encoding="utf-8")
        self.assertIn("change-learn", text)
        self.assertIn("playwright-report-retrieval", text)
        self.assertIn("anchor_scene", text)
        self.assertIn("A2UI", text)
        self.assertIn("Never merge", text)

    def test_player_ships_sticky_compact_controls_hud_and_a2ui_surface(self):
        html = (SKILL / "player" / "player.html").read_text(encoding="utf-8")
        for token in (
            'id="mini-media"',
            'id="mini-play"',
            'id="mini-fill"',
            'syncStickyMedia',
            'stickyCompact:',
            'id="hud"',
            'renderHud',
            'a2ui-json-drawer',
            'system voice fallback',
            '__WALKTHROUGH_BRIDGE__',
            'sendBridgeCallback',
            'connectBridgeEvents',
            'data-color-mode="dark"',
            'detectDarkMode',
            'hud-tabstrip',
            'loadTelemetry',
            'pauseForLiveChat',
            'briefing audio is pre-generated for exact slide sync; pause or click any agent node anytime for live chat',
        ):
            self.assertIn(token, html, f"player.html missing expected UI hook: {token}")

    def test_plugin_ships_copilot_extension_canvas_with_a2ui_bridge(self):
        ext_dir = PLUGIN / "com.github.copilot" / "extensions" / "narrated-walkthrough"
        ext_entry = ext_dir / "extension.mjs"
        bridge_mod = ext_dir / "a2ui-bridge.mjs"
        telemetry_mod = ext_dir / "live-telemetry.mjs"
        self.assertTrue(ext_entry.is_file(), "extension.mjs must ship with the plugin")
        self.assertTrue(bridge_mod.is_file(), "a2ui-bridge.mjs must ship with the plugin")
        self.assertTrue(telemetry_mod.is_file(), "live-telemetry.mjs must ship with the plugin")
        ext_src = ext_entry.read_text(encoding="utf-8")
        bridge_src = bridge_mod.read_text(encoding="utf-8")
        self.assertIn('id: "narrated-walkthrough"', ext_src)
        self.assertIn("createCanvas", ext_src)
        self.assertIn("push_a2ui", ext_src)
        self.assertIn("get_telemetry", ext_src)
        self.assertIn("session.send", bridge_src)
        self.assertIn("x-canvas-token", bridge_src)

    def test_hud_and_a2ui_fields_conform_to_closed_schema(self):
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        tl = {
            "title": "HUD + A2UI test",
            "scenes": [
                {
                    "id": "s1", "title": "S1", "mermaid": "flowchart LR\n    A",
                    "narration": "Tell me where to steer next.", "captions": ["Tell me where to steer next."],
                    "audio": "audio/s1.wav", "duration": 4.0,
                }
            ],
            "hud": {
                "pack_kind": "pr_review",
                "producer": "PR Review Agent",
                "summary": "Ahead on rollout.",
                "oversight_default": "lead",
                "stats": [{"label": "CI", "value": "9/9", "trend": "green", "tone": "ok"}],
                "nodes": [{
                    "id": "lead", "name": "Eric", "role": "Lead", "level": "lead", "summary": "Review owner",
                    "state": "waiting_merge", "state_label": "Waiting on PR merge · free for next item", "queue_depth": 0,
                    "quick_actions": [{"label": "Assign next item", "action": "assign_next_item", "context": {"nodeId": "lead"}}],
                }],
                "artifacts": [{"label": "PR #353", "url": "https://github.com/matthewcorven/venom-dog-sports/pull/353"}],
            },
            "a2ui": [
                {"version": "v1.0", "createSurface": {"surfaceId": "s", "catalogId": "urn:test:v1"}},
                {"version": "v1.0", "updateComponents": {"surfaceId": "s", "components": [{"id": "root", "component": "Column", "children": []}]}},
            ],
        }
        self.assertEqual(_shape_errors(tl, schema), [])

    def test_cli_validate_runs(self):
        result = subprocess.run(
            [sys.executable, str(BUILDER), "--validate", "--plan", str(GOLDEN / "timeline.json")],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("plan ok", result.stdout)

    def test_invalid_plan_exits_two(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "timeline.json"
            bad.write_text(json.dumps(plan(disposition={"prompt": "p", "options": ["o"]})))
            result = subprocess.run(
                [sys.executable, str(BUILDER), "--validate", "--plan", str(bad)],
                capture_output=True, text=True,
            )
        self.assertEqual(result.returncode, 2)
        self.assertIn("FAIL", result.stdout)


def _fake_wav(seconds: float, sample_rate: int = 8000, marker: bytes = b"\x00\x00") -> bytes:
    """Build a minimal valid 16-bit mono RIFF/WAVE payload of the requested length."""
    n_samples = max(1, int(round(seconds * sample_rate)))
    raw = (marker * ((n_samples * 2 // len(marker)) + 1))[: n_samples * 2]
    byte_rate = sample_rate * 2
    fmt = struct.pack("<HHIIHH", 1, 1, sample_rate, byte_rate, 2, 16)
    riff_size = 4 + (8 + len(fmt)) + (8 + len(raw))
    return b"RIFF" + struct.pack("<I", riff_size) + b"WAVE" + b"fmt " + struct.pack("<I", len(fmt)) + fmt + b"data" + struct.pack("<I", len(raw)) + raw


class VoiceConsistencyTests(unittest.TestCase):
    """Multi-scene voice anchoring via anchor_scene / ref_audio + language/temperature."""

    @classmethod
    def setUpClass(cls):
        cls.schema = json.loads(SCHEMA_PATH.read_text())

    def test_schema_accepts_anchor_and_ref_audio_options_and_rejects_unknown(self):
        base_scene = {
            "id": "a", "title": "A", "mermaid": "flowchart LR\n    x",
            "narration": "One claim.", "audio": "audio/a.wav",
            "captions": ["One claim."], "duration": 2.0,
        }
        tl_anchor = {
            "title": "t",
            "tts": {
                "endpoint": "http://127.0.0.1:1/v1/audio/speech",
                "model": "m",
                "voice": "US_James",
                "anchor_scene": "a",
                "language": "en",
                "temperature": 0.0,
            },
            "scenes": [base_scene],
        }
        self.assertEqual(_shape_errors(tl_anchor, self.schema), [])

        tl_ref = {
            "title": "t",
            "tts": {
                "endpoint": "http://127.0.0.1:1/v1/audio/speech",
                "model": "m",
                "voice": "US_James",
                "ref_audio": "ref.wav",
                "ref_text": "Reference.",
            },
            "scenes": [base_scene],
        }
        self.assertEqual(_shape_errors(tl_ref, self.schema), [])

        tl_bad = dict(tl_anchor)
        tl_bad["tts"] = dict(tl_anchor["tts"], speculative_flag=True)
        self.assertTrue(any("speculative_flag" in e for e in _shape_errors(tl_bad, self.schema)))

    def test_validator_rejects_invalid_anchor_and_ref_combinations(self):
        tl = plan(tts={"endpoint": "http://127.0.0.1:1/v1/audio/speech", "model": "m", "voice": "v", "anchor_scene": "missing"})
        self.assertTrue(any("anchor_scene" in p for p in bn.validate(tl)))

        tl_both = plan(tts={"endpoint": "http://127.0.0.1:1/v1/audio/speech", "model": "m", "voice": "v", "anchor_scene": "a", "ref_audio": "r.wav"})
        self.assertTrue(any("mutually exclusive" in p for p in bn.validate(tl_both)))

        tl_orphan_text = plan(tts={"endpoint": "http://127.0.0.1:1/v1/audio/speech", "model": "m", "voice": "v", "ref_text": "hello"})
        self.assertTrue(any("ref_text" in p for p in bn.validate(tl_orphan_text)))

        tl_neg_temp = plan(tts={"endpoint": "http://127.0.0.1:1/v1/audio/speech", "model": "m", "voice": "v", "temperature": -0.1})
        self.assertTrue(any("temperature" in p for p in bn.validate(tl_neg_temp)))

    def _run_with_mock_tts(self, responses_by_input):
        recorded = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                recorded.append(payload)
                wav = responses_by_input(payload)
                self.send_response(200)
                self.send_header("Content-Type", "audio/wav")
                self.send_header("Content-Length", str(len(wav)))
                self.end_headers()
                self.wfile.write(wav)

            def log_message(self, format, *args):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server, f"http://127.0.0.1:{port}/v1/audio/speech", recorded

    def test_anchor_scene_conditions_subsequent_scenes_and_only_revoices_in_place(self):
        anchor_wav = _fake_wav(2.0, marker=b"\x11\x22")
        second_wav = _fake_wav(3.0, marker=b"\x33\x44")
        revoiced_wav = _fake_wav(4.0, marker=b"\x55\x66")
        call_count = {"n": 0}

        def respond(payload):
            call_count["n"] += 1
            if "First scene." in payload["input"]:
                return anchor_wav
            return revoiced_wav if call_count["n"] >= 3 else second_wav

        server, endpoint, recorded = self._run_with_mock_tts(respond)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                tmp_path = Path(tmp)
                plan_file = tmp_path / "timeline.json"
                tl = {
                    "title": "Anchored test",
                    "mermaid_source": str(SKILL / "assets" / "mermaid.min.js.gz"),
                    "tts": {
                        "endpoint": endpoint,
                        "model": "test-tts",
                        "voice": "US_James",
                        "anchor_scene": "s1",
                        "language": "en",
                        "temperature": 0.0,
                    },
                    "scenes": [
                        {
                            "id": "s1", "title": "One", "mermaid": "flowchart LR\n    A",
                            "narration": "First scene.", "captions": ["First scene."],
                            "audio": "audio/s1.wav",
                        },
                        {
                            "id": "s2", "title": "Two", "mermaid": "flowchart LR\n    B",
                            "narration": "Second scene.", "captions": ["Second scene."],
                            "audio": "audio/s2.wav",
                        },
                    ],
                }
                plan_file.write_text(json.dumps(tl))

                res = subprocess.run(
                    [sys.executable, str(BUILDER), "--plan", str(plan_file)],
                    capture_output=True, text=True,
                )
                self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
                self.assertEqual(len(recorded), 2)

                # Anchor scene has no ref_audio/ref_text; second scene gets base64(anchor_wav) + anchor narration
                self.assertEqual(recorded[0]["input"], "First scene.")
                self.assertNotIn("ref_audio", recorded[0])
                self.assertNotIn("ref_text", recorded[0])
                self.assertEqual(recorded[0]["language"], "en")
                self.assertEqual(recorded[0]["temperature"], 0.0)

                self.assertEqual(recorded[1]["input"], "Second scene.")
                self.assertEqual(recorded[1]["ref_audio"], base64.b64encode(anchor_wav).decode("ascii"))
                self.assertEqual(recorded[1]["ref_text"], "First scene.")
                self.assertEqual(recorded[1]["language"], "en")
                self.assertEqual(recorded[1]["temperature"], 0.0)

                updated = json.loads(plan_file.read_text())
                self.assertEqual(updated["scenes"][0]["start"], 0.0)
                self.assertEqual(updated["scenes"][0]["duration"], 2.0)
                self.assertEqual(updated["scenes"][1]["start"], 2.0)
                self.assertEqual(updated["scenes"][1]["duration"], 3.0)
                self.assertEqual(updated["total_duration"], 5.0)

                # --only s2 overwrites s2 in place, keeping s1 intact and still anchored to s1
                res2 = subprocess.run(
                    [sys.executable, str(BUILDER), "--plan", str(plan_file), "--only", "s2"],
                    capture_output=True, text=True,
                )
                self.assertEqual(res2.returncode, 0, res2.stdout + res2.stderr)
                self.assertEqual(len(recorded), 3)
                self.assertEqual(recorded[2]["input"], "Second scene.")
                self.assertEqual(recorded[2]["ref_audio"], base64.b64encode(anchor_wav).decode("ascii"))
                updated2 = json.loads(plan_file.read_text())
                self.assertEqual(updated2["scenes"][1]["duration"], 4.0)
                self.assertEqual(updated2["total_duration"], 6.0)
        finally:
            server.shutdown()
            server.server_close()

    def test_explicit_ref_audio_conditions_all_scenes(self):
        ext_ref = _fake_wav(1.5, marker=b"\x77\x88")
        clip_wav = _fake_wav(2.5, marker=b"\x99\xaa")
        server, endpoint, recorded = self._run_with_mock_tts(lambda _p: clip_wav)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                tmp_path = Path(tmp)
                (tmp_path / "voice-sample.wav").write_bytes(ext_ref)
                plan_file = tmp_path / "timeline.json"
                tl = {
                    "title": "External ref test",
                    "mermaid_source": str(SKILL / "assets" / "mermaid.min.js.gz"),
                    "tts": {
                        "endpoint": endpoint,
                        "model": "test-tts",
                        "voice": "US_James",
                        "ref_audio": "voice-sample.wav",
                        "ref_text": "Sample voice transcript.",
                    },
                    "scenes": [
                        {
                            "id": "s1", "title": "One", "mermaid": "flowchart LR\n    A",
                            "narration": "Only scene.", "captions": ["Only scene."],
                            "audio": "audio/s1.wav",
                        }
                    ],
                }
                plan_file.write_text(json.dumps(tl))
                res = subprocess.run(
                    [sys.executable, str(BUILDER), "--plan", str(plan_file)],
                    capture_output=True, text=True,
                )
                self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
                self.assertEqual(len(recorded), 1)
                self.assertEqual(recorded[0]["ref_audio"], base64.b64encode(ext_ref).decode("ascii"))
                self.assertEqual(recorded[0]["ref_text"], "Sample voice transcript.")
        finally:
            server.shutdown()
            server.server_close()


def _tone_wav(active_seconds: float, lead_seconds: float = 0.05, tail_seconds: float = 0.05, sample_rate: int = 16000) -> bytes:
    """Build a valid 16-bit mono PCM WAV containing a 440 Hz tone surrounded by silence."""
    import math as _m
    lead = [0] * int(round(lead_seconds * sample_rate))
    tail = [0] * int(round(tail_seconds * sample_rate))
    n_act = max(1, int(round(active_seconds * sample_rate)))
    tone = [int(round(9000 * _m.sin(2 * _m.pi * 440.0 * i / sample_rate))) for i in range(n_act)]
    samples = lead + tone + tail
    raw = struct.pack(f"<{len(samples)}h", *samples)
    byte_rate = sample_rate * 2
    fmt = struct.pack("<HHIIHH", 1, 1, sample_rate, byte_rate, 2, 16)
    riff_size = 4 + (8 + len(fmt)) + (8 + len(raw))
    return b"RIFF" + struct.pack("<I", riff_size) + b"WAVE" + b"fmt " + struct.pack("<I", len(fmt)) + fmt + b"data" + struct.pack("<I", len(raw)) + raw


class PaceBalancingTests(unittest.TestCase):
    """Pace balancing normalizes speaking cadence without breaking audio/scene sync."""

    @classmethod
    def setUpClass(cls):
        cls.schema = json.loads(SCHEMA_PATH.read_text())

    def test_schema_accepts_pace_config_and_rejects_unknown(self):
        tl = {
            "title": "Pace schema test",
            "tts": {
                "endpoint": "http://127.0.0.1:1/v1/audio/speech",
                "model": "m",
                "voice": "US_James",
                "pace": {
                    "enabled": True,
                    "target_sps": 6.3,
                    "min_tempo": 0.76,
                    "max_tempo": 1.25,
                    "tolerance": 0.03,
                    "lead_ms": 100,
                    "tail_ms": 280,
                    "target_dbfs": -20.0,
                },
            },
            "scenes": [
                {
                    "id": "a", "title": "A", "mermaid": "flowchart LR\n    x",
                    "narration": "One claim.", "audio": "audio/a.wav",
                    "captions": ["One claim."], "duration": 2.0,
                }
            ],
        }
        self.assertEqual(_shape_errors(tl, self.schema), [])
        tl_bad = json.loads(json.dumps(tl))
        tl_bad["tts"]["pace"]["unknown_knob"] = 1
        self.assertTrue(any("unknown_knob" in e for e in _shape_errors(tl_bad, self.schema)))

    def test_validator_rejects_invalid_pace_bounds(self):
        tl = plan(tts={
            "endpoint": "http://127.0.0.1:1/v1/audio/speech", "model": "m", "voice": "v",
            "pace": {"target_sps": -1, "min_tempo": 1.2, "max_tempo": 0.8, "tolerance": 1.5, "lead_ms": -5},
        })
        problems = bn.validate(tl)
        for token in ("target_sps", "min_tempo", "max_tempo", "tolerance", "lead_ms"):
            self.assertTrue(any(token in p for p in problems), f"expected error for {token}, got {problems}")

    def test_pace_balancing_converges_rate_preserves_sync_and_is_idempotent(self):
        import shutil as _shutil
        if not _shutil.which("ffmpeg"):
            self.skipTest("ffmpeg not installed in test environment")

        # Scene 1: 24 syllables spoken in 3.0s (~8.0 syl/s -> rushed)
        # Scene 2: 24 syllables spoken in 4.8s (~5.0 syl/s -> slow)
        text_24 = "Validation verifies every artifact signature continuously before publication."
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            (tmp_path / "audio").mkdir()
            (tmp_path / "audio" / "fast.wav").write_bytes(_tone_wav(3.0, lead_seconds=0.40, tail_seconds=0.50))
            (tmp_path / "audio" / "slow.wav").write_bytes(_tone_wav(4.8, lead_seconds=0.02, tail_seconds=0.05))
            plan_file = tmp_path / "timeline.json"
            tl = {
                "title": "Pace balance sync test",
                "mermaid_source": str(SKILL / "assets" / "mermaid.min.js.gz"),
                "tts": {
                    "endpoint": "http://127.0.0.1:1/v1/audio/speech",
                    "model": "test-tts",
                    "voice": "US_James",
                    "pace": {"enabled": True, "target_sps": 6.2, "min_tempo": 0.72, "max_tempo": 1.30},
                },
                "scenes": [
                    {
                        "id": "fast", "title": "Fast", "mermaid": "flowchart LR\n    A",
                        "narration": text_24,
                        "captions": ["Validation verifies every artifact.", "Signature continuously before publication."],
                        "audio": "audio/fast.wav",
                        "duration": 1.0,
                    },
                    {
                        "id": "slow", "title": "Slow", "mermaid": "flowchart LR\n    B",
                        "narration": text_24,
                        "captions": ["Validation verifies every artifact.", "Signature continuously before publication."],
                        "audio": "audio/slow.wav",
                        "duration": 1.0,
                    },
                ],
            }
            plan_file.write_text(json.dumps(tl))

            res1 = subprocess.run([sys.executable, str(BUILDER), "--plan", str(plan_file)], capture_output=True, text=True)
            self.assertEqual(res1.returncode, 0, res1.stdout + res1.stderr)
            self.assertIn("pace balanced:", res1.stdout)

            # Raw masters preserved untouched
            self.assertTrue((tmp_path / "audio" / ".raw" / "fast.wav").exists())
            self.assertTrue((tmp_path / "audio" / ".raw" / "slow.wav").exists())

            updated = json.loads(plan_file.read_text())
            manifest = json.loads((tmp_path / "deck.json").read_text())
            d0 = updated["scenes"][0]["duration"]
            d1 = updated["scenes"][1]["duration"]
            # Fast clip lengthened, slow clip shortened -> converged to near-identical duration!
            self.assertAlmostEqual(d0, d1, delta=0.25)
            # Exact audio-to-timeline sync invariant: durations equal actual WAV headers & starts are cumulative
            self.assertAlmostEqual(d0, bn.duration(tmp_path / "audio" / "fast.wav"), delta=0.002)
            self.assertAlmostEqual(d1, bn.duration(tmp_path / "audio" / "slow.wav"), delta=0.002)
            self.assertEqual(updated["scenes"][0]["start"], 0.0)
            self.assertAlmostEqual(updated["scenes"][1]["start"], d0, delta=0.002)
            self.assertAlmostEqual(updated["total_duration"], d0 + d1, delta=0.002)
            # Balanced rates reported cleanly in deck.json
            rates = [s["balanced_sps"] for s in manifest["pace"]["scenes"]]
            self.assertLess(max(rates) / min(rates), 1.08)

            # Second build is idempotent (reads from audio/.raw/, never double-stretches!)
            res2 = subprocess.run([sys.executable, str(BUILDER), "--plan", str(plan_file)], capture_output=True, text=True)
            self.assertEqual(res2.returncode, 0, res2.stdout + res2.stderr)
            updated2 = json.loads(plan_file.read_text())
            self.assertEqual(updated2["scenes"][0]["duration"], d0)
            self.assertEqual(updated2["scenes"][1]["duration"], d1)
            self.assertEqual(updated2["total_duration"], updated["total_duration"])


if __name__ == "__main__":
    unittest.main()
