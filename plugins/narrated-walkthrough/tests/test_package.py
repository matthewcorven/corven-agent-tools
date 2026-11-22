"""Contract tests for the narrated-walkthrough skill package (stdlib only)."""

import hashlib
import json
import pathlib
import re
import subprocess
import sys
import tempfile
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
    pytypes = {"object": dict, "array": list, "string": str, "number": (int, float)}
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
        allowed = {"__future__", "argparse", "json", "pathlib", "re", "subprocess", "sys",
                   "urllib", "shutil", "struct", "gzip"}
        self.assertTrue(roots <= allowed, roots - allowed)

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


if __name__ == "__main__":
    unittest.main()
