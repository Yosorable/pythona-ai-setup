"""Account drafts, provider storage, and tool-free test credential rotation."""

import asyncio
import copy
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from ai_setup.app import SetupApp
from ai_setup.codex_auth import AuthError
from ai_setup.codex_probe import test_codex
from ai_setup.responses_probe import read_test_response
from ai_setup.settings import CODEX_STORAGE_KEY, SettingsStore, defaults, install
from test_setup import FakeAI


AUTH = {"access_token": "secret-access", "refresh_token": "secret-refresh", "email": "test@example.test", "account_id": "account"}


class FakeLogin:
    def __init__(self, tr):
        self.phase = "starting"
        self.result = None
        self.cancelled = False
        self.resumes = 0

    def start(self):
        self.phase = "waiting"
        return self

    def cancel(self):
        self.cancelled = True
        self.result = None
        self.phase = "cancelled"

    def resume(self):
        self.resumes += 1

    def snapshot(self):
        return {"phase": self.phase, "message": self.phase}

    def take_result(self):
        value, self.result = self.result, None
        return value

    def complete(self):
        self.result, self.phase = dict(AUTH), "success"


class CodexSetupTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.store = SettingsStore(Path(folder.name) / "settings.json")
        self.store.save()
        self.ai = FakeAI()
        self.app = SetupApp(self.store, ai=self.ai, login_factory=FakeLogin, status=lambda _: {"available": True})
        self.addCleanup(self.app.close)
        self.app.dispatch("new")
        self.app.dispatch("select_provider", {"backend": "codex"})

    def form(self):
        settings = self.app.snapshot()["settings"]
        return {"editor_id": self.app.editor_id, "settings": {key: settings[key] for key in
                ("name", "backend", "model_id", "groups", "reasoning_effort") if key in settings}}

    def login(self):
        self.app.dispatch("login", self.form())
        login = self.app.login
        login.complete()
        self.app.poll_login()
        return login

    def add(self):
        self.login()
        profile = self.app.dispatch("install", self.form())["profiles"][0]
        self.app.dispatch("edit", {"profile_id": profile["id"]})
        return profile

    def test_login_drafts_and_cancellation_never_install_or_expose_tokens(self):
        before = self.store.path.read_bytes()
        self.login()
        self.assertTrue(self.app.snapshot()["auth"]["signed_in"])
        self.assertNotIn("secret-", repr(self.app.snapshot()))
        self.assertEqual(self.ai.created, 0)
        self.assertEqual(self.store.path.read_bytes(), before)
        self.app.dispatch("back")
        self.assertIsNone(self.app.draft_auth)
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_install_uses_repo_owned_js_and_stores_credentials_only_in_provider(self):
        self.login()
        state = self.app.dispatch("install", self.form())
        saved = self.ai.get_custom_provider(state["profiles"][0]["provider_id"])
        self.assertIn("async function runCodexRequest", saved["js_code"])
        self.assertNotIn("BACKEND_PAYLOAD", saved["js_code"])
        self.assertNotIn("NSBundle", saved["js_code"])
        self.assertEqual(saved["local_storage"]["refresh_token"], AUTH["refresh_token"])
        self.assertEqual(json.loads(saved["local_storage"][CODEX_STORAGE_KEY])["model_id"], "gpt-6.1-sol")
        self.assertNotIn("groups", json.loads(saved["local_storage"][CODEX_STORAGE_KEY]))
        self.assertNotIn("groups", self.store.data["profiles"][0])
        self.assertNotIn("secret-", self.store.path.read_text())
        self.assertNotIn("test@example.test", self.store.path.read_text())

    def test_edit_preserves_provider_refreshed_tokens_and_discarded_relogin(self):
        profile = self.add()
        provider = self.ai.providers[profile["provider_id"]]
        provider["local_storage"]["refresh_token"] = "rotated-in-assistant"
        provider["local_storage"]["unrelated"] = "keep"
        self.login()
        self.app.dispatch("back")
        self.app.dispatch("edit", {"profile_id": profile["id"]})
        payload = self.form()
        payload["settings"].update(name="Changed", model_id="custom-model", reasoning_effort="high")
        self.app.dispatch("install", payload)
        self.assertEqual(provider["local_storage"]["refresh_token"], "rotated-in-assistant")
        self.assertEqual(provider["local_storage"]["unrelated"], "keep")
        self.assertEqual(self.ai.created, 1)

    def test_back_and_new_login_ignore_stale_results_and_forward_resume(self):
        self.app.dispatch("login", self.form())
        old = self.app.login
        self.app.resume()
        self.assertEqual(old.resumes, 1)
        with self.assertRaises(AuthError):
            self.app.dispatch("install", self.form())
        self.app.dispatch("back")
        self.assertTrue(old.cancelled)
        old.complete()
        self.app.poll_login()
        self.assertIsNone(self.app.draft_auth)
        self.assertEqual(self.ai.created, 0)

    def test_missing_or_partial_credentials_block_adding_without_writes(self):
        before = self.store.path.read_bytes()
        self.assertFalse(self.app.snapshot()["auth"]["ready"])
        with self.assertRaisesRegex(AuthError, "auth_required"):
            self.app.dispatch("install", self.form())
        for auth in (None, {}, {"refresh_token": "refresh"}, {"access_token": "access"},
                     {"access_token": "access", "refresh_token": " "}):
            with self.subTest(auth=auth), self.assertRaisesRegex(AuthError, "auth_required"):
                install(self.store, defaults("codex"), self.ai, auth=auth)
        self.assertEqual(self.ai.created, 0)
        self.assertEqual(self.store.path.read_bytes(), before)
        self.login()
        self.assertTrue(self.app.snapshot()["auth"]["ready"])
        self.app.dispatch("install", self.form())
        self.assertEqual(self.ai.created, 1)

    def test_latest_missing_credentials_block_saving_and_recreating(self):
        profile = self.add()
        before = self.store.path.read_bytes()
        self.ai.providers[profile["provider_id"]]["local_storage"].pop("refresh_token")
        with self.assertRaisesRegex(AuthError, "auth_required"):
            self.app.dispatch("install", self.form())
        self.assertFalse(self.app.snapshot()["auth"]["ready"])
        self.ai.providers.pop(profile["provider_id"])
        with self.assertRaisesRegex(AuthError, "auth_required"):
            self.app.dispatch("install", self.form())
        self.assertEqual(self.ai.created, 1)
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_cancel_does_not_consume_a_completed_but_unclaimed_login(self):
        self.app.dispatch("login", self.form())
        self.app.login.complete()
        state = self.app.dispatch("cancel_login", {"editor_id": self.app.editor_id})
        self.assertFalse(state["auth"].get("signed_in"))
        self.assertIsNone(self.app.draft_auth)

    def test_failed_lookup_preserves_known_id_and_does_not_overwrite_account(self):
        profile = self.add()
        with patch.object(self.ai, "get_custom_provider", side_effect=OSError("unavailable")):
            self.app.dispatch("refresh")
            with self.assertRaises(OSError):
                self.app.dispatch("install", self.form())
        self.assertEqual(self.store.get(profile["id"])["provider_id"], profile["provider_id"])
        self.assertEqual(self.ai.created, 1)

    def test_draft_probe_rotation_stays_in_memory_until_save(self):
        self.login()
        def probe(settings, prompt, cancelled, auth, on_refresh):
            on_refresh(auth, {**auth, "refresh_token": "draft-renewed"})
            return {"text": "OK", "seconds": 0.1}
        self.app.codex_probe = probe
        editor = self.app.editor_id
        self.app.dispatch("test", {**self.form(), "prompt": "Hello"})
        self.app.jobs[editor]["worker"].join(2)
        self.assertEqual(self.ai.created, 0)
        self.assertEqual(self.app.draft_auth["refresh_token"], "draft-renewed")
        state = self.app.dispatch("install", self.form())
        self.assertEqual(self.ai.get_custom_provider(state["profiles"][0]["provider_id"])["local_storage"]["refresh_token"], "draft-renewed")

    def test_installed_probe_rotation_keeps_form_edits_unsaved_even_on_failure(self):
        profile = self.add()
        original = copy.deepcopy(self.ai.providers[profile["provider_id"]])
        def probe(settings, prompt, cancelled, auth, on_refresh):
            self.assertEqual(settings["model_id"], "unsaved-model")
            on_refresh(auth, {**auth, "refresh_token": "renewed"})
            raise RuntimeError("Inference failed")
        self.app.codex_probe = probe
        payload = self.form()
        payload["settings"].update(name="Unsaved", model_id="unsaved-model", reasoning_effort="max")
        editor = self.app.editor_id
        self.app.dispatch("test", {**payload, "prompt": "Hello"})
        self.app.jobs[editor]["worker"].join(2)
        saved = self.ai.get_custom_provider(profile["provider_id"])
        self.assertEqual(saved["name"], original["name"])
        self.assertEqual(saved["js_code"], original["js_code"])
        self.assertEqual(saved["local_storage"][CODEX_STORAGE_KEY], original["local_storage"][CODEX_STORAGE_KEY])
        self.assertEqual(saved["local_storage"]["refresh_token"], "renewed")
        self.assertEqual(self.store.get(profile["id"])["model_id"], "gpt-6.1-sol")

    def test_probe_never_overwrites_newer_account_credentials(self):
        profile = self.add()
        def probe(settings, prompt, cancelled, auth, on_refresh):
            self.ai.providers[profile["provider_id"]]["local_storage"]["refresh_token"] = "newer"
            latest = on_refresh(auth, {**auth, "refresh_token": "stale"})
            self.assertEqual(latest["refresh_token"], "newer")
            return {"text": "Recovered", "seconds": 0.1}
        self.app.codex_probe = probe
        editor = self.app.editor_id
        self.app.dispatch("test", {**self.form(), "prompt": "Hello"})
        self.app.jobs[editor]["worker"].join(2)
        self.assertEqual(self.ai.get_custom_provider(profile["provider_id"])["local_storage"]["refresh_token"], "newer")
        self.assertEqual(self.app.snapshot()["test"]["text"], "Recovered")

    def test_save_waits_for_codex_probe_to_finish_rotating_credentials(self):
        self.login()
        entered = threading.Event()
        def probe(settings, prompt, cancelled, auth, on_refresh):
            entered.set()
            cancelled.wait(2)
            raise asyncio.CancelledError
        self.app.codex_probe = probe
        editor = self.app.editor_id
        self.app.dispatch("test", {**self.form(), "prompt": "Hello"})
        self.assertTrue(entered.wait(1))
        with self.assertRaisesRegex(AuthError, "auth_test_running"):
            self.app.dispatch("install", self.form())
        self.app.dispatch("cancel_test", {"editor_id": editor})
        self.app.jobs[editor]["worker"].join(2)
        self.app.dispatch("install", self.form())
        self.assertEqual(self.ai.created, 1)


class CodexProbeTests(unittest.TestCase):
    def test_refresh_is_saved_before_inference_and_sse_is_completed(self):
        events = [{"type": "response.output_text.delta", "delta": "Hello 😀"}, {"type": "response.completed"}]
        stream = io.BytesIO(("\n".join("data: " + json.dumps(event) + "\n" for event in events) + "\n").encode())
        rotated = []
        def opened(request, check, deadline):
            self.assertTrue(rotated)
            self.assertEqual(request.get_header("Authorization"), "Bearer new-access")
            self.assertEqual(request.get_header("Version"), "0.160.0")
            self.assertEqual(request.get_header("User-agent"), "codex_cli_rs/0.160.0 (iOS; arm64)")
            self.assertEqual(request.get_header("Openai-beta"), "responses=experimental")
            self.assertEqual(json.loads(request.data)["model"], "gpt-6.1-sol")
            self.assertNotIn("tools", json.loads(request.data))
            return read_test_response(stream, check)
        with patch("ai_setup.codex_probe.token_request", return_value={"access_token": "new-access", "refresh_token": "new-refresh"}), \
                patch("ai_setup.codex_probe.request_test_response", side_effect=opened):
            result = test_codex(defaults("codex"), "Hi", threading.Event(), {"refresh_token": "old"},
                                lambda old, new: rotated.append(new))
        self.assertEqual(result["text"], "Hello 😀")
        self.assertEqual(rotated[0]["refresh_token"], "new-refresh")

    def test_truncated_response_and_pre_cancelled_test_fail(self):
        with patch("ai_setup.codex_probe.request_test_response", side_effect=lambda request, check, deadline:
                   read_test_response(io.BytesIO(b'data: {"type":"response.output_text.delta","delta":"partial"}\n\n'), check)):
            with self.assertRaisesRegex(RuntimeError, "No complete"):
                test_codex(defaults("codex"), "Hi", threading.Event(), AUTH, lambda *_: None)
        cancelled = threading.Event()
        cancelled.set()
        with patch("ai_setup.codex_probe.request_test_response") as request:
            with self.assertRaises(asyncio.CancelledError):
                test_codex(defaults("codex"), "Hi", cancelled, AUTH, lambda *_: None)
            request.assert_not_called()
