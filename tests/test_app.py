"""Profile identity, native installation checks, and independent background tests."""

import asyncio
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from ai_setup.app import SetupApp
from ai_setup.l10n import STRINGS
from ai_setup.page import render_page
from ai_setup.settings import SettingsStore, DEFAULT_MLX_MODEL
from test_setup import FakeAI


class AppTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = SettingsStore(Path(self.directory.name) / 'settings.local.json')
        self.ai = FakeAI()
        self.app = SetupApp(self.store, ai=self.ai, status=lambda settings: {'available': False, 'reason': 'DEVICE_NOT_ELIGIBLE'})
        self.addCleanup(self.app.close)

    def form(self, name='Local Model', profile_id=None):
        profile_id = profile_id or self.store.value['id']
        return {'profile_id': profile_id, 'settings': {'name': name, 'model_id': self.store.get(profile_id)['model_id'],
                'groups': {'files': True, 'browser': False, 'python': False}}}

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

    def test_profiles_have_independent_ids_and_late_edits_target_their_original_profile(self):
        apple = self.app.dispatch('install', self.form('Apple'))['settings']
        mlx = self.app.dispatch('new', {'backend': 'mlx_lm'})['settings']
        self.assertEqual(mlx['model_id'], DEFAULT_MLX_MODEL)
        self.app.dispatch('install', self.form('MLX'))
        self.app.dispatch('save', self.form('Apple edited', apple['id']))
        self.assertEqual(self.store.value['id'], mlx['id'])
        self.assertEqual(self.store.value['name'], 'MLX')
        self.assertEqual(self.store.get(apple['id'])['name'], 'Apple edited')
        self.app.dispatch('install', self.form('Apple updated', apple['id']))
        self.assertEqual(self.store.get(apple['id'])['provider_id'], apple['provider_id'])
        self.assertEqual(self.ai.created, 2)
        self.assertNotEqual(self.store.value['provider_id'], apple['provider_id'])
        restored = SettingsStore(self.store.path)
        self.assertEqual(len(restored.data['profiles']), 2)
        self.assertEqual(restored.data['selected_id'], mlx['id'])

    def test_refresh_verifies_ids_and_only_recreates_a_deleted_provider(self):
        apple = self.app.dispatch('install', self.form('Apple'))['settings']
        self.app.dispatch('new', {'backend': 'mlx_lm'})
        mlx = self.app.dispatch('install', self.form('MLX'))['settings']
        # Startup treats the JSON IDs as unverified until the App confirms them.
        reopened = SetupApp(SettingsStore(self.store.path), ai=self.ai, status=self.app.status)
        self.assertEqual(reopened.snapshot()['profiles'][0]['installation_status'], 'checking')
        del self.ai.providers[apple['provider_id']]
        state = reopened.dispatch('refresh')
        self.assertIsNone(state['profiles'][0]['provider_id'])
        self.assertEqual(state['profiles'][1]['provider_id'], mlx['provider_id'])
        self.assertEqual(state['profiles'][1]['installation_status'], 'installed')
        reopened.dispatch('select', {'profile_id': apple['id']})
        replacement = reopened.dispatch('install', self.form('Apple again', apple['id']))['settings']['provider_id']
        self.assertNotEqual(replacement, apple['provider_id'])
        self.assertEqual(self.ai.created, 3)
        self.assertEqual(SettingsStore(self.store.path).get(apple['id'])['provider_id'], replacement)
        reopened.close()

    def test_read_failure_preserves_provider_id_and_does_not_create_duplicates(self):
        installed = self.app.dispatch('install', self.form())['settings']['provider_id']
        with patch.object(self.ai, 'get_custom_provider', side_effect=RuntimeError('Temporary native failure')):
            state = self.app.dispatch('refresh')
            self.assertEqual(state['settings']['provider_id'], installed)
            self.assertEqual(state['profiles'][0]['installation_status'], 'unknown')
            with self.assertRaisesRegex(RuntimeError, 'Temporary native failure'):
                self.app.dispatch('install', self.form())
            self.assertEqual(self.ai.created, 1)
        self.assertEqual(self.app.dispatch('refresh')['profiles'][0]['installation_status'], 'installed')

    def test_pending_probe_stays_with_its_profile_and_can_be_cancelled(self):
        started = threading.Event()
        def probe(settings, prompt, cancelled):
            started.set()
            cancelled.wait(3)
            raise asyncio.CancelledError
        self.app.probe = probe
        apple = self.store.value['id']
        state = self.app.dispatch('test', {**self.form(), 'prompt': 'Hello'})
        self.assertTrue(state['test']['running'])
        self.assertTrue(started.wait(1))
        other = self.app.dispatch('new', {'backend': 'mlx_lm'})
        self.assertFalse(other['test']['running'])
        self.app.dispatch('install', self.form())
        self.app.dispatch('cancel_test', {'profile_id': apple})
        self.app.jobs[apple]['worker'].join(2)
        self.assertFalse(self.app.jobs[apple]['worker'].is_alive())
        self.app.dispatch('select', {'profile_id': apple})
        self.assertEqual(self.app.snapshot()['test']['message'], 'Test cancelled')
        self.assertEqual(self.ai.created, 1)

    def test_failed_save_keeps_window_open_and_rejects_private_fields(self):
        with self.assertRaises(ValueError):
            self.app.dispatch('close', self.form(''))
        payload = self.form()
        payload['settings']['port'] = 1234
        with self.assertRaises(ValueError):
            self.app.dispatch('save', payload)
        self.assertEqual(self.store.service['port'], 8768)
        self.assertFalse(self.app.closed.is_set())

    def test_remove_rechecks_native_provider_and_empty_library_can_close(self):
        installed = self.app.dispatch('install', self.form())['settings']['provider_id']
        payload = {'profile_id': self.store.value['id']}
        with self.assertRaises(ValueError):
            self.app.dispatch('remove', payload)
        del self.ai.providers[installed]
        state = self.app.dispatch('remove', payload)
        self.assertEqual(state['profiles'], [])
        self.assertIsNone(state['settings'])
        self.app.dispatch('close')
        self.assertTrue(self.app.closed.is_set())

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
