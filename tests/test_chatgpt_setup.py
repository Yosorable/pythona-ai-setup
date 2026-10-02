"""Separate provider accounts, transient selection, and public Responses probes."""

import asyncio
import io
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from urllib.error import HTTPError
from unittest.mock import patch

from ai_setup.app import SetupApp
from ai_setup.chatgpt_auth import AUTH_STORAGE_KEY, AuthError, ISSUER, PLAN_SCOPE, RESOURCE
from ai_setup.chatgpt_probe import list_models, test_chatgpt
from ai_setup.responses_probe import read_test_response
from ai_setup.settings import CHATGPT_STORAGE_KEY, SettingsStore, defaults, install, read_credentials, write_credentials
from test_codex_setup import FakeLogin
from test_setup import FakeAI


AUTH = {"client_id": "oaiapp_one", "ext_agent_host_id": "urn:uuid:host", "issuer": ISSUER, "subject": "one",
        "email": "one@example.test", "access_token": "secret-access", "refresh_token": "secret-refresh",
        "id_token": "secret-identity", "expires_at": str(time.time() + 3600), "scope": PLAN_SCOPE}


class ChatGPTLoginStub(FakeLogin):
    def __init__(self, tr, host, previous):
        super().__init__(tr)
        self.host, self.previous = host, previous

    def complete(self, auth=None):
        self.result, self.phase = {**self.previous, **(AUTH if auth is None else auth)}, "success"


class ChatGPTSetupTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.store = SettingsStore(Path(folder.name) / "settings.local.json")
        self.store.save()
        self.store.ensure_chatgpt_host_id()
        self.ai = FakeAI()
        self.app = SetupApp(self.store, ai=self.ai, chatgpt_login_factory=ChatGPTLoginStub,
                            models_loader=lambda *_: [{"id": "account-model", "name": "Account model"}])
        self.addCleanup(self.app.close)

    def open(self):
        self.app.dispatch("new")
        return self.app.dispatch("select_provider", {"backend": "chatgpt"})

    def form(self):
        value = self.app.snapshot()["settings"]
        return {"editor_id": self.app.editor_id,
                "settings": {key: value[key] for key in ("name", "backend", "model_id", "reasoning_effort")}}

    def login(self, auth=None):
        self.app.dispatch("login", self.form())
        self.app.login.complete(auth)
        self.app.poll_login()

    def test_add_first_selects_a_provider_without_creating_a_draft_or_saving(self):
        before = self.store.path.read_bytes()
        state = self.app.dispatch("new")
        self.assertEqual(state["page"], "choose")
        self.assertIsNone(state["settings"])
        self.assertIsNone(state["editor_id"])
        self.app.dispatch("back")
        self.assertEqual(self.ai.created, 0)
        self.assertEqual(self.store.path.read_bytes(), before)
        state = self.open()
        self.assertEqual(state["settings"]["backend"], "chatgpt")
        self.assertNotIn("groups", state["settings"])
        payload = self.form()
        payload["settings"]["backend"] = "codex"
        with self.assertRaisesRegex(ValueError, "backend"):
            self.app.dispatch("install", payload)

    def test_first_login_saves_only_the_installation_id_and_keeps_drafts_in_memory(self):
        self.store.data.pop("chatgpt_host_id")
        install(self.store, defaults("codex"), self.ai,
                auth={"access_token": "codex-access", "refresh_token": "codex-refresh"})
        before = json.loads(self.store.path.read_text())
        self.open()
        form = self.form()
        form["settings"].update(name="Unsaved connection", model_id="unsaved-model")
        self.app.dispatch("login", form)
        saved = json.loads(self.store.path.read_text())
        host_id = saved.pop("chatgpt_host_id")
        self.assertEqual(saved, before)
        self.assertEqual(self.app.login.host, host_id)
        self.app.dispatch("cancel_login", {"editor_id": self.app.editor_id})
        self.app.dispatch("back")
        self.assertEqual(SettingsStore(self.store.path).ensure_chatgpt_host_id(), host_id)
        self.assertEqual(self.ai.created, 1)
        self.assertEqual(list(self.store.path.parent.iterdir()), [self.store.path])

    def test_draft_account_is_private_and_adding_keeps_codex_separate(self):
        codex_id = install(self.store, defaults("codex"), self.ai, auth={"access_token": "codex-access", "refresh_token": "codex-token"})
        before = self.store.path.read_bytes()
        self.open()
        self.login()
        self.assertNotIn("secret-", repr(self.app.snapshot()))
        self.assertTrue(self.app.snapshot()["plan_notice"])
        self.app.dispatch("acknowledge_plan")
        self.assertFalse(self.app.snapshot()["plan_notice"])
        self.assertEqual(self.store.path.read_bytes(), before)
        self.app.dispatch("back")
        self.assertEqual(self.ai.created, 1)
        self.open()
        self.assertFalse(self.app.snapshot()["auth"]["signed_in"])
        self.login()
        self.app.dispatch("acknowledge_plan")
        state = self.app.dispatch("install", self.form())
        chatgpt = next(profile for profile in state["profiles"] if profile["backend"] == "chatgpt")
        saved = self.ai.providers[chatgpt["provider_id"]]
        self.assertNotEqual(chatgpt["provider_id"], codex_id)
        self.assertEqual(read_credentials("chatgpt", saved["local_storage"])["client_id"], "oaiapp_one")
        self.assertNotIn("refresh_token", saved["local_storage"])
        self.assertIn(CHATGPT_STORAGE_KEY, saved["local_storage"])
        self.assertIn("const CHATGPT_PROVIDER", saved["js_code"])
        self.assertNotIn("BACKEND_PAYLOAD", saved["js_code"])
        self.assertEqual(self.ai.providers[codex_id]["local_storage"]["refresh_token"], "codex-token")
        self.assertNotIn("secret-", self.store.path.read_text())
        self.app.dispatch("edit", {"profile_id": chatgpt["id"]})
        self.login()
        self.assertEqual(self.app.login.previous["client_id"], "oaiapp_one")
        self.assertFalse(self.app.snapshot()["plan_notice"])

    def test_complete_signin_and_plan_permission_are_required_before_adding(self):
        self.open()
        before = self.store.path.read_bytes()
        with self.assertRaisesRegex(AuthError, "chatgpt_auth_required"):
            self.app.dispatch("install", self.form())
        self.login({**AUTH, "scope": "openid email offline_access"})
        self.assertFalse(self.app.snapshot()["auth"]["plan_enabled"])
        self.assertTrue(self.app.snapshot()["auth"]["signed_in"])
        self.assertFalse(self.app.snapshot()["auth"]["ready"])
        with self.assertRaisesRegex(AuthError, "chatgpt_plan_required"):
            self.app.dispatch("test", {**self.form(), "prompt": "Hello"})
        with self.assertRaisesRegex(AuthError, "chatgpt_plan_required"):
            self.app.dispatch("install", self.form())
        self.assertEqual(self.ai.created, 0)
        self.assertEqual(self.store.path.read_bytes(), before)
        self.login()
        self.assertTrue(self.app.snapshot()["auth"]["ready"])
        self.app.dispatch("install", self.form())
        self.assertEqual(self.ai.created, 1)

    def test_direct_install_rejects_incomplete_registration_and_missing_permission(self):
        before = self.store.path.read_bytes()
        for auth in (None, {}, {**AUTH, "refresh_token": ""}, {**AUTH, "access_token": ""},
                     {**AUTH, "client_id": "dynamic_agent_client"}, {**AUTH, "subject": ""},
                     {**AUTH, "issuer": "other"}, {**AUTH, "scope": "openid"}):
            with self.subTest(auth=auth), self.assertRaises(AuthError):
                install(self.store, defaults("chatgpt"), self.ai, auth=auth)
        self.assertEqual(self.ai.created, 0)
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_latest_revoked_permission_blocks_save_and_keeps_existing_record(self):
        self.open()
        self.login()
        profile = self.app.dispatch("install", self.form())["profiles"][0]
        self.app.dispatch("edit", {"profile_id": profile["id"]})
        self.assertTrue(self.app.snapshot()["auth"]["ready"])
        storage = self.ai.providers[profile["provider_id"]]["local_storage"]
        write_credentials("chatgpt", storage, {**AUTH, "scope": "openid"})
        before = self.store.path.read_bytes()
        with self.assertRaisesRegex(AuthError, "chatgpt_plan_required"):
            self.app.dispatch("install", self.form())
        self.assertFalse(self.app.snapshot()["auth"]["ready"])
        self.assertTrue(self.app.snapshot()["auth"]["signed_in"])
        self.assertEqual(self.ai.created, 1)
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_installed_probe_rotation_does_not_save_draft_fields_or_overwrite_newer_auth(self):
        self.open()
        self.login()
        profile = self.app.dispatch("install", self.form())["profiles"][0]
        self.app.dispatch("edit", {"profile_id": profile["id"]})
        payload = self.form()
        payload["settings"].update(model_id="unsaved-model", name="Unsaved")

        def probe(settings, prompt, cancelled, auth, save):
            self.assertEqual(settings["model_id"], "unsaved-model")
            save(auth, {**auth, "refresh_token": "rotated"})
            raise RuntimeError("Test inference failed")

        self.app.chatgpt_probe = probe
        editor = self.app.editor_id
        self.app.dispatch("test", {**payload, "prompt": "Hello"})
        self.app.jobs[editor]["worker"].join(2)
        storage = self.ai.providers[profile["provider_id"]]["local_storage"]
        self.assertEqual(read_credentials("chatgpt", storage)["refresh_token"], "rotated")
        self.assertEqual(json.loads(storage[CHATGPT_STORAGE_KEY])["model_id"], "gpt-6.1-sol")
        self.assertEqual(self.store.get(profile["id"])["name"], "ChatGPT")

        def racing(settings, prompt, cancelled, auth, save):
            write_credentials("chatgpt", storage, {**auth, "refresh_token": "newer"})
            save(auth, {**auth, "refresh_token": "stale"})

        self.app.chatgpt_probe = racing
        self.app.dispatch("test", {**payload, "prompt": "Hello"})
        self.app.jobs[editor]["worker"].join(2)
        self.assertEqual(read_credentials("chatgpt", storage)["refresh_token"], "newer")

    def test_late_catalog_results_and_cancelled_signins_cannot_update_another_form(self):
        entered, release = threading.Event(), threading.Event()

        def catalog(auth, on_refresh):
            entered.set()
            release.wait(2)
            return [{"id": "stale-model", "name": "Stale"}]

        self.addCleanup(release.set)
        self.app.models_loader = catalog
        self.open()
        self.login()
        self.assertTrue(entered.wait(1))
        self.app.dispatch("back")
        self.open()
        release.set()
        self.assertEqual(self.app.snapshot()["models"]["items"], [])
        self.app.dispatch("login", self.form())
        self.app.login.complete()
        self.app.dispatch("cancel_login", {"editor_id": self.app.editor_id})
        self.assertFalse(self.app.snapshot()["auth"].get("signed_in"))
        self.assertIsNone(self.app.draft_auth)


class ChatGPTProbeTests(unittest.TestCase):
    @staticmethod
    def stream(events):
        return io.BytesIO(("\n" + "".join("data: " + json.dumps(event) + "\n\n" for event in events)).encode())

    def test_discovery_preserves_visible_account_model_order(self):
        values = {"models": [{"slug": "second", "display_name": "Second", "visibility": "list"},
                              {"slug": "hidden", "visibility": "hidden"}, {"slug": "first", "visibility": "list"}]}
        with patch("ai_setup.chatgpt_probe.read_json_response", return_value=values) as request:
            self.assertEqual(list_models(AUTH), [{"id": "second", "name": "Second"}, {"id": "first", "name": "first"}])
        self.assertEqual(request.call_args.args[0].full_url, RESOURCE + "/models")

    def test_discovery_refreshes_and_saves_before_requesting_models(self):
        saved = []
        fresh = {**AUTH, "access_token": "new-access", "refresh_token": "new-refresh"}

        def catalog(request, **kwargs):
            self.assertEqual(saved, [fresh])
            self.assertEqual(request.get_header("Authorization"), "Bearer new-access")
            raise RuntimeError("Catalog unavailable")

        with patch("ai_setup.chatgpt_probe.refresh_credentials", return_value=fresh), \
                patch("ai_setup.chatgpt_probe.read_json_response", side_effect=catalog):
            with self.assertRaisesRegex(RuntimeError, "Catalog unavailable"):
                list_models({**AUTH, "expires_at": "0"}, lambda old, new: saved.append(new))
        self.assertEqual(saved[0]["refresh_token"], "new-refresh")

    def test_discovery_only_retries_one_401(self):
        def unauthorized(*args, **kwargs):
            raise HTTPError(RESOURCE + "/models", 401, "Unauthorized", {}, io.BytesIO(b"{}"))
        with patch("ai_setup.chatgpt_probe.refresh_credentials", return_value=AUTH) as refresh, \
                patch("ai_setup.chatgpt_probe.read_json_response", side_effect=unauthorized) as catalog:
            with self.assertRaisesRegex(RuntimeError, "HTTP 401"):
                list_models(AUTH)
        self.assertEqual(refresh.call_count, 1)
        self.assertEqual(catalog.call_count, 2)

    def test_public_inference_rotates_tokens_before_waiting_for_completed(self):
        events = [{"type": "response.output_text.delta", "delta": "Hello"}, {"type": "response.completed"}]
        rotations = []

        def opened(request, check, deadline, error_message):
            self.assertEqual(len(rotations), 1)
            self.assertEqual(request.full_url, RESOURCE + "/responses")
            self.assertNotIn("Originator", request.headers)
            self.assertEqual(request.get_header("Authorization"), "Bearer new-access")
            body = json.loads(request.data)
            self.assertFalse(body["store"])
            self.assertTrue(body["stream"])
            return read_test_response(self.stream(events), check, error_message)

        with patch("ai_setup.chatgpt_probe.refresh_credentials", return_value={**AUTH, "access_token": "new-access", "refresh_token": "rotated"}), \
                patch("ai_setup.chatgpt_probe.request_test_response", side_effect=opened):
            result = test_chatgpt(defaults("chatgpt"), "Hello", threading.Event(), {**AUTH, "expires_at": "0"}, lambda old, new: rotations.append(new))
        self.assertEqual(result["text"], "Hello")

    def test_failed_or_truncated_stream_is_not_a_successful_test(self):
        for terminal in ([], [{"type": "response.failed", "response": {"error": {"code": "subscription_sharing_usage_limit_exceeded"}}}], [{"type": "response.incomplete"}]):
            with patch("ai_setup.chatgpt_probe.request_test_response", side_effect=lambda request, check, deadline, error_message:
                       read_test_response(self.stream([{"type": "response.output_text.delta", "delta": "Partial"}] + terminal), check, error_message)):
                with self.assertRaises(RuntimeError):
                    test_chatgpt(defaults("chatgpt"), "Hello", threading.Event(), AUTH, lambda *_: None)

    def test_cancellation_after_rotation_keeps_credentials_and_stops_inference(self):
        cancelled, saved = threading.Event(), []

        def save(previous, auth):
            saved.append(auth)
            cancelled.set()

        with patch("ai_setup.chatgpt_probe.refresh_credentials", return_value={**AUTH, "refresh_token": "new"}), \
                patch("ai_setup.chatgpt_probe.request_test_response") as request:
            with self.assertRaises(asyncio.CancelledError):
                test_chatgpt(defaults("chatgpt"), "Hello", cancelled, {**AUTH, "expires_at": "0"}, save)
        self.assertEqual(saved[0]["refresh_token"], "new")
        request.assert_not_called()

    def test_expired_session_clears_tokens_but_retains_registration(self):
        saved = []
        with patch("ai_setup.chatgpt_probe.refresh_credentials", side_effect=AuthError("chatgpt_refresh_expired")):
            with self.assertRaisesRegex(AuthError, "chatgpt_refresh_expired"):
                test_chatgpt(defaults("chatgpt"), "Hello", threading.Event(), {**AUTH, "expires_at": "0"}, lambda old, new: saved.append(new))
        self.assertEqual(saved[0]["client_id"], "oaiapp_one")
        self.assertEqual(saved[0]["subject"], "one")
        for key in ("access_token", "refresh_token", "id_token"):
            self.assertNotIn(key, saved[0])
