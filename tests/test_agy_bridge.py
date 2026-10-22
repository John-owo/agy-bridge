import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


BRIDGE_PATH = Path(__file__).resolve().parents[1] / "agy-bridge" / "scripts" / "agy_bridge.py"
spec = importlib.util.spec_from_file_location("agy_bridge", BRIDGE_PATH)
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)


MODELS = [
    {"id": "gemini-3.8-flash-medium", "name": "Gemini 3.8 Flash Medium"},
    {"id": "gemini-3.8-flash-high", "name": "Gemini 3.8 Flash High"},
    {"id": "gemini-4-argon-medium", "name": "Gemini 4 Argon Medium"},
    {"id": "gemini-4-argon-high", "name": "Gemini 4 Argon High"},
    {"id": "claude-opus-5-5-high", "name": "Claude Opus 5.5 High"},
    {"id": "claude-sonnet-5-5-high", "name": "Claude Sonnet 5.5 High"},
]


def group(name, description, remaining):
    return {"name": name, "description": description, "buckets": [{"remaining_fraction": remaining}]}


GROUPS = [
    group("Gemini Models", "Models within this group: Gemini Flash, Gemini Pro", 0.4),
    group("Gemini 4 Argon", "Models within this group: Gemini 4 Argon", 0.3),
    group("Claude and GPT models", "Models within this group: Claude Opus, Claude Sonnet, GPT-OSS", 0.7),
]


class StageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.source = self.base / "source"
        self.source.mkdir()
        (self.source / "src").mkdir()
        (self.source / "src" / "good.py").write_text("print('safe')\n", encoding="utf-8")
        (self.source / "src" / ".env").write_text("SENTINEL_SECRET", encoding="utf-8")
        (self.source / ".git").mkdir()
        (self.source / ".git" / "config").write_text("hidden", encoding="utf-8")

    def test_stage_selected_directory_excludes_secrets_and_tracks_changes(self):
        item = bridge.prepare(self.source, ["src"], "codex", self.base / "runs")
        run = Path(item["run_dir"])
        self.assertEqual(item["file_count"], 1)
        self.assertTrue((run / "workspace" / "src" / "good.py").exists())
        self.assertFalse((run / "workspace" / "src" / ".env").exists())
        self.assertFalse((run / "workspace" / ".git").exists())
        manifest = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["files"]["src/good.py"]["source_sha256"], bridge.hash_file(self.source / "src" / "good.py"))
        (run / "workspace" / "src" / "good.py").write_text("changed", encoding="utf-8")
        (run / "workspace" / "new.txt").write_text("new", encoding="utf-8")
        changes = bridge.inspect(run)["changes"]
        self.assertIn("src/good.py", changes["modified"])
        self.assertIn("new.txt", changes["created"])
        self.assertEqual((self.source / "src" / "good.py").read_text(encoding="utf-8"), "print('safe')\n")

    def test_rejects_traversal_and_sensitive_explicit_paths(self):
        for include in ("..\\outside", "../outside", ".git/config", "src/.env"):
            with self.subTest(include=include), self.assertRaises(bridge.BridgeError):
                bridge.prepare(self.source, [include], "cc", self.base / "runs")
        with self.assertRaises(bridge.BridgeError):
            bridge.prepare(self.source, [], "cc", self.base / "runs")

    def test_link_is_excluded_if_platform_permits(self):
        external = self.base / "outside.txt"
        external.write_text("outside", encoding="utf-8")
        try:
            (self.source / "src" / "linked.txt").symlink_to(external)
        except (OSError, NotImplementedError):
            self.skipTest("Symlinks unavailable")
        item = bridge.prepare(self.source, ["src"], "codex", self.base / "runs")
        self.assertFalse((Path(item["workspace"]) / "src" / "linked.txt").exists())

    def test_lock_refuses_concurrent_run(self):
        item = bridge.prepare(self.source, ["src/good.py"], "codex", self.base / "runs")
        run = Path(item["run_dir"])
        (run / ".run.lock").write_text("held", encoding="utf-8")
        task = self.base / "task.txt"
        task.write_text("Do something", encoding="utf-8")
        with self.assertRaisesRegex(bridge.BridgeError, "locked"):
            bridge.run_child(run, task, "routine", None, 2)


class RoutingTests(unittest.TestCase):
    def test_tiers_choose_expected_quota_groups(self):
        self.assertEqual(bridge.choose_model(MODELS, GROUPS, "routine")["model"]["id"], "gemini-3.8-flash-medium")
        self.assertEqual(bridge.choose_model(MODELS, GROUPS, "standard")["model"]["id"], "gemini-3.8-flash-high")
        self.assertEqual(bridge.choose_model(MODELS, GROUPS, "complex")["model"]["id"], "gemini-4-argon-high")

    def test_exhausted_argon_falls_back_to_opus(self):
        groups = [GROUPS[0], group("Gemini 4 Argon", "Argon", 0), GROUPS[2]]
        self.assertEqual(bridge.choose_model(MODELS, groups, "complex")["model"]["id"], "claude-opus-5-5-high")

    def test_unknown_group_and_empty_buckets_fail_closed(self):
        groups = [group("Unknown", "Something novel", 1)]
        with self.assertRaises(bridge.BridgeError):
            bridge.choose_model(MODELS, groups, "routine")
        with self.assertRaises(bridge.BridgeError):
            bridge.choose_model(MODELS, [{"name": "Gemini Models", "description": "Gemini Flash", "buckets": []}], "routine")
        with self.assertRaises(bridge.BridgeError):
            bridge.choose_model(MODELS, GROUPS, "routine", "unlisted")

    def test_every_bucket_must_have_positive_quota(self):
        groups = [group("Gemini Models", "Gemini Flash", 0.5)]
        groups[0]["buckets"].append({"remaining_fraction": 0})
        with self.assertRaises(bridge.BridgeError):
            bridge.choose_model(MODELS, groups, "routine")

    def test_usage_envelope_parses_exact_group_location(self):
        obj = {"command": {"data": {"groups": GROUPS}}}
        self.assertEqual(bridge.usage_groups(obj), GROUPS)


class SafetyTests(unittest.TestCase):
    def test_billing_settings_fail_closed(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as tmp:
            home = Path(tmp)
            settings = home / ".gemini" / "antigravity-cli" / "settings.json"
            shared = home / ".gemini" / "config" / "config.json"
            settings.parent.mkdir(parents=True)
            shared.parent.mkdir(parents=True)
            shared.write_text('{"userSettings": {"artifactReviewMode": "manual"}}', encoding="utf-8")
            settings.write_text('{"useG1Credits": false}', encoding="utf-8")
            clean = {key: "" for key in bridge.FORBIDDEN_ENV}
            with patch.object(bridge.Path, "home", return_value=home), patch.dict(os.environ, clean):
                self.assertFalse(bridge.billing_guard()["useG1Credits"])
                settings.write_text('{}', encoding="utf-8")
                self.assertEqual(bridge.billing_guard()["credit_setting_source"], "omitted-default-false")
                shared.write_text('{"userSettings": {"useG1Credits": true}}', encoding="utf-8")
                with self.assertRaises(bridge.BridgeError):
                    bridge.billing_guard()
                shared.write_text('{"nested": [{"useG1Credits": null}]}', encoding="utf-8")
                with self.assertRaises(bridge.BridgeError):
                    bridge.billing_guard()
                shared.write_text('{broken', encoding="utf-8")
                with self.assertRaises(bridge.BridgeError):
                    bridge.billing_guard()
                shared.write_text('{}', encoding="utf-8")
                settings.write_text('{"useG1Credits": true}', encoding="utf-8")
                with self.assertRaises(bridge.BridgeError):
                    bridge.billing_guard()
                settings.write_text('{"useG1Credits": false, "modelProvider": "api"}', encoding="utf-8")
                with self.assertRaises(bridge.BridgeError):
                    bridge.billing_guard()
                settings.write_text('{"useG1Credits": false}', encoding="utf-8")
                with patch.dict(os.environ, {"GEMINI_API_KEY": "secret"}):
                    with self.assertRaises(bridge.BridgeError):
                        bridge.billing_guard()

    def test_child_result_denial_timeout_and_empty_response_fail(self):
        self.assertEqual(bridge.parse_child_result('{"status":"SUCCESS","response":"ok"}', "permission denied", 0, False)[0], "FAILED_DENIAL")
        self.assertEqual(bridge.parse_child_result('{"status":"SUCCESS","response":"ok"}', "", 0, True)[0], "FAILED_TIMEOUT")
        self.assertEqual(bridge.parse_child_result('{"status":"SUCCESS","response":""}', "", 0, False)[0], "FAILED_OUTPUT")
        self.assertEqual(bridge.parse_child_result('{"status":"SUCCESS","response":"ok","tool":{"denied_actions":["write"]}}', "", 0, False)[0], "FAILED_DENIAL")
        self.assertEqual(bridge.parse_child_result('{"status":"SUCCESS","response":"ok"}', "", 1, False)[0], "FAILED_PROCESS")
        self.assertEqual(bridge.parse_child_result('{"status":"WAITING","response":"ok"}', "", 0, False)[0], "FAILED_CHILD")
        self.assertEqual(bridge.parse_child_result('{"status":"SUCCESS","response":"ok"}', "a tool required the file permission", 0, False)[0], "FAILED_DENIAL")
        self.assertEqual(bridge.parse_child_result('{"status":"SUCCESS","response":"ok"}', "soft-denied", 0, False)[0], "FAILED_DENIAL")

    def test_failed_child_is_recorded_and_cannot_be_rerun(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as tmp:
            base = Path(tmp)
            source = base / "source"
            source.mkdir()
            (source / "file.txt").write_text("original", encoding="utf-8")
            prepared = bridge.prepare(source, ["file.txt"], "codex", base / "runs")
            run = Path(prepared["run_dir"])
            task = base / "task.txt"
            task.write_text("Edit file.txt", encoding="utf-8")

            class FakeProcess:
                def __init__(self, args, **kwargs):
                    self.returncode = 2
                    self.pid = 12345
                    kwargs["stdout"].write('{"status":"FAILED","response":"could not complete"}')
                    kwargs["stderr"].write("failure")

                def wait(self, timeout=None):
                    return self.returncode

            info = {"models": MODELS, "groups": GROUPS}
            with patch.object(bridge, "billing_guard", return_value={"useG1Credits": False}), patch.object(bridge.subprocess, "Popen", FakeProcess):
                result = bridge.run_child(run, task, "routine", None, 2, agy="agy", info=info)
                self.assertEqual(result["status"], "FAILED_PROCESS")
                saved = json.loads((run / "result.json").read_text(encoding="utf-8"))
                self.assertEqual(saved["state"], "FAILED_PROCESS")
                self.assertTrue((run / "stdout.txt").is_file())
                self.assertTrue((run / "stderr.txt").is_file())
                self.assertFalse((run / ".run.lock").exists())
                with self.assertRaisesRegex(bridge.BridgeError, "already been used"):
                    bridge.run_child(run, task, "routine", None, 2, agy="agy", info=info)


if __name__ == "__main__":
    unittest.main()
