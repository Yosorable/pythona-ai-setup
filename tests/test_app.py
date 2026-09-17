"""Settings actions remain usable independently of WebKit and model availability."""

import asyncio
from pathlib import Path
import tempfile
import threading
import unittest

from ai_setup.app import SetupApp
from ai_setup.l10n import STRINGS
from ai_setup.page import render_page
from ai_setup.settings import SettingsStore
from test_setup import FakeAI


class AppTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = SettingsStore(Path(self.directory.name) / 'settings.local.json')
        self.ai = FakeAI()
        self.app = SetupApp(self.store, ai=self.ai, status=lambda: {'available': False, 'reason': 'DEVICE_NOT_ELIGIBLE'})
        self.addCleanup(self.app.close)

    def form(self, name='Local Model'):
        return {'name': name, 'groups': {'files': True, 'browser': False, 'python': False}}

    def test_save_install_and_update_keep_id_and_hide_service_credentials(self):
        self.app.check_availability()
        first = self.app.dispatch('install', self.form())
        provider_id = first['settings']['provider_id']
        updated = self.app.dispatch('install', self.form('Updated'))
        self.assertEqual(updated['settings']['provider_id'], provider_id)
        self.assertEqual(self.ai.created, 1)
        self.assertEqual(SettingsStore(self.store.path).value['provider_id'], provider_id)
        self.assertNotIn('service_token', repr(updated))
        self.app.dispatch('close', self.form('Saved on close'))
        self.assertTrue(self.app.closed.is_set())
        self.assertEqual(SettingsStore(self.store.path).value['name'], 'Saved on close')

    def test_pending_probe_does_not_block_install_and_can_be_cancelled(self):
        started = threading.Event()
        def probe(settings, prompt, cancelled):
            started.set()
            cancelled.wait(3)
            raise asyncio.CancelledError
        self.app.probe = probe
        state = self.app.dispatch('test', {'settings': self.form(), 'prompt': 'Hello'})
        self.assertTrue(state['test']['running'])
        self.assertTrue(started.wait(1))
        self.app.dispatch('install', self.form())
        self.app.dispatch('cancel_test')
        self.app.worker.join(2)
        self.assertFalse(self.app.worker.is_alive())
        self.assertEqual(self.app.snapshot()['test']['message'], 'Test cancelled')
        self.assertEqual(self.ai.created, 1)

    def test_failed_save_keeps_window_open_and_rejects_private_fields(self):
        with self.assertRaises(ValueError):
            self.app.dispatch('close', self.form(''))
        self.assertFalse(self.app.closed.is_set())
        with self.assertRaises(ValueError):
            self.app.dispatch('save', {**self.form(), 'port': 1234})
        self.assertEqual(self.store.value['port'], 8768)

    def test_all_languages_render_without_external_assets(self):
        keys = set(STRINGS['en'])
        for language, strings in STRINGS.items():
            self.assertEqual(set(strings), keys, language)
            page = render_page(language, preview=True)
            self.assertNotIn('/* SCRIPT */', page)
            self.assertNotIn('/* STYLES */', page)
            self.assertIn('window.SETUP', page)
            self.assertIn('"preview": true', page)
            self.assertNotIn('<script src=', page)
