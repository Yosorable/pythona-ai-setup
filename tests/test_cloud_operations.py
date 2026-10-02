"""Regression checks for setup operations spanning more than one editor."""

import asyncio
from pathlib import Path
import tempfile
import threading
import unittest

from ai_setup.app import SetupApp
from ai_setup.chatgpt_auth import without_tokens
from ai_setup.settings import SettingsStore, defaults, install, read_credentials, write_credentials
from test_chatgpt_setup import AUTH as CHATGPT_AUTH
from test_codex_setup import AUTH as CODEX_AUTH
from test_setup import FakeAI


class CloudOperationTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.store = SettingsStore(Path(folder.name) / "settings.local.json")
        self.ai = FakeAI()
        self.app = SetupApp(self.store, ai=self.ai, models_loader=lambda *_: [])
        self.addCleanup(self.app.close)

    def installed(self, backend):
        auth = {**CHATGPT_AUTH, "local_grant_id": "first-login"} if backend == "chatgpt" else CODEX_AUTH
        provider_id = install(self.store, defaults(backend), self.ai, auth=auth)
        profile = next(item for item in self.store.data["profiles"] if item["provider_id"] == provider_id)
        self.app.dispatch("edit", {"profile_id": profile["id"]})
        return profile

    def form(self):
        settings = self.app.snapshot()["settings"]
        return {"editor_id": self.app.editor_id, "settings": {key: settings[key] for key in
                ("name", "backend", "model_id", "reasoning_effort")}}

    def test_reopening_an_installed_profile_does_not_start_another_refresh(self):
        for backend in ("codex", "chatgpt"):
            with self.subTest(backend=backend):
                profile = self.installed(backend)
                entered, release = threading.Event(), threading.Event()
                self.addCleanup(release.set)
                received = []

                def probe(settings, prompt, cancelled, auth, save):
                    received.append(auth["refresh_token"])
                    entered.set()
                    release.wait(2)
                    save(auth, {**auth, "refresh_token": "rotated"})
                    if cancelled.is_set():
                        raise asyncio.CancelledError
                    return {"text": "OK", "seconds": 0.1}

                setattr(self.app, backend + "_probe", probe)
                editor = self.app.editor_id
                self.app.dispatch("test", {**self.form(), "prompt": "Hi"})
                self.assertTrue(entered.wait(1))
                self.app.dispatch("back")
                state = self.app.dispatch("edit", {"profile_id": profile["id"]})
                self.assertNotEqual(editor, self.app.editor_id)
                self.assertTrue(state["test"]["running"])
                with self.assertRaisesRegex(RuntimeError, "already running"):
                    self.app.dispatch("test", {**self.form(), "prompt": "Again"})
                with self.assertRaisesRegex(RuntimeError, "auth_test_running"):
                    self.app.dispatch("install", self.form())
                self.assertEqual(len(received), 1)
                release.set()
                self.app.jobs[editor]["worker"].join(2)
                self.assertFalse(self.app.snapshot()["test"]["running"])
                self.app.dispatch("test", {**self.form(), "prompt": "Again"})
                self.app.jobs[self.app.editor_id]["worker"].join(2)
                self.assertEqual(received[-1], "rotated")
                self.app.dispatch("back")

    def test_model_catalog_and_test_share_latest_credentials(self):
        profile = self.installed("chatgpt")
        self.app.dispatch("back")
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        seen = []

        def catalog(auth, save):
            entered.set()
            release.wait(2)
            save(auth, {**auth, "refresh_token": "catalog-rotation"})
            return []

        def probe(settings, prompt, cancelled, auth, save):
            seen.append(auth["refresh_token"])
            return {"text": "OK", "seconds": 0.1}

        self.app.models_loader, self.app.chatgpt_probe = catalog, probe
        self.app.dispatch("edit", {"profile_id": profile["id"]})
        self.assertTrue(entered.wait(1))
        self.app.dispatch("test", {**self.form(), "prompt": "Hi"})
        self.assertEqual(seen, [])
        release.set()
        self.app.jobs[self.app.editor_id]["worker"].join(2)
        self.assertEqual(seen, ["catalog-rotation"])

    def test_late_refresh_restores_cleared_tokens_but_not_a_replaced_login(self):
        for replaced in (False, True):
            with self.subTest(replaced=replaced):
                profile = self.installed("chatgpt")
                storage = self.ai.providers[profile["provider_id"]]["local_storage"]

                def probe(settings, prompt, cancelled, auth, save):
                    current = without_tokens(auth)
                    if replaced:
                        current["local_grant_id"] = "new-login"
                    write_credentials("chatgpt", storage, current)
                    save(auth, {**auth, "refresh_token": "late-success"})
                    return {"text": "OK", "seconds": 0.1}

                self.app.chatgpt_probe = probe
                self.app.dispatch("test", {**self.form(), "prompt": "Hi"})
                self.app.jobs[self.app.editor_id]["worker"].join(2)
                current = read_credentials("chatgpt", self.ai.providers[profile["provider_id"]]["local_storage"])
                self.assertEqual(current.get("refresh_token"), None if replaced else "late-success")
                if replaced:
                    self.assertIn("changed", self.app.snapshot()["test"]["message"])
                self.app.dispatch("back")

    def test_test_can_be_cancelled_while_waiting_for_catalog_loading(self):
        profile = self.installed("chatgpt")
        self.app.dispatch("back")
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        requests = []

        def catalog(auth, save):
            entered.set()
            release.wait(2)
            return []

        def probe(*args):
            requests.append(args)
            return {"text": "Unexpected", "seconds": 0.1}

        self.app.models_loader, self.app.chatgpt_probe = catalog, probe
        self.app.dispatch("edit", {"profile_id": profile["id"]})
        self.assertTrue(entered.wait(1))
        self.app.dispatch("test", {**self.form(), "prompt": "Hi"})
        editor = self.app.editor_id
        self.app.dispatch("cancel_test", {"editor_id": editor})
        self.app.jobs[editor]["worker"].join(0.5)
        self.assertFalse(self.app.jobs[editor]["worker"].is_alive())
        self.assertFalse(self.app.snapshot()["test"]["running"])
        self.assertFalse(release.is_set())
        self.assertEqual(requests, [])
