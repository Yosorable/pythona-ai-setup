"""List/detail navigation, explicit saves, and native installation checks."""

import asyncio
import copy
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
        self.store.save()
        self.ai = FakeAI()
        self.app = SetupApp(self.store, ai=self.ai, status=lambda settings: {'available': False, 'reason': 'DEVICE_NOT_ELIGIBLE'})
        self.addCleanup(self.app.close)

    def form(self, name=None):
        state = self.app.snapshot()
        value = {key: copy.deepcopy(state['settings'][key]) for key in ('name', 'backend', 'model_id', 'groups')}
        if name is not None:
            value['name'] = name
        return {'editor_id': state['editor_id'], 'settings': value}

    def new(self, backend='apple_fm'):
        self.app.dispatch('new')
        if backend != 'apple_fm':
            form = self.form()
            form['settings']['backend'] = backend
            self.app.dispatch('backend', form)
        return self.app.snapshot()

    def add(self, name='Local Model', backend='apple_fm'):
        self.new(backend)
        state = self.app.dispatch('install', self.form(name))
        self.assertEqual(state['page'], 'home')
        return state['profiles'][-1]

    def test_new_forms_and_model_tests_stay_in_memory_until_add(self):
        before = self.store.path.read_bytes()
        self.assertEqual(self.app.snapshot()['page'], 'home')
        self.assertEqual(self.app.snapshot()['profiles'], [])
        draft = self.new('mlx_lm')
        self.assertEqual(draft['settings']['model_id'], DEFAULT_MLX_MODEL)
        self.app.probe = lambda settings, prompt, cancelled: {'text': 'Test reply', 'seconds': 0.1}
        self.app.dispatch('test', {**self.form('Draft only'), 'prompt': 'Hi'})
        self.app.jobs[draft['editor_id']]['worker'].join(2)
        self.assertEqual(self.ai.created, 0)
        self.assertEqual(self.store.path.read_bytes(), before)
        self.app.dispatch('back')
        self.assertEqual(self.app.snapshot()['profiles'], [])
        self.assertIsNone(self.app.snapshot()['settings'])
        self.assertEqual(self.new()['settings']['name'], 'Apple On-Device Model')

    def test_add_and_save_update_one_provider_and_hide_service_credentials(self):
        apple = self.add()
        provider_id = apple['provider_id']
        self.assertNotIn('service_token', repr(self.app.snapshot()))
        self.app.dispatch('edit', {'profile_id': apple['id']})
        changed = self.app.dispatch('install', self.form('Updated'))
        self.assertEqual(changed['profiles'][0]['provider_id'], provider_id)
        self.assertEqual(self.ai.created, 1)
        self.assertEqual(self.ai.get_custom_provider(provider_id)['name'], 'Updated')
        self.assertEqual(SettingsStore(self.store.path).get(apple['id'])['name'], 'Updated')
        self.app.dispatch('edit', {'profile_id': apple['id']})
        self.app.probe = lambda settings, prompt, cancelled: {'text': 'OK', 'seconds': 0.1}
        editor = self.app.editor_id
        self.app.dispatch('test', {**self.form('Unsaved change'), 'prompt': 'Hi'})
        self.app.jobs[editor]['worker'].join(2)
        self.app.dispatch('back')
        self.app.dispatch('close')
        self.assertTrue(self.app.closed.is_set())
        self.assertEqual(SettingsStore(self.store.path).get(apple['id'])['name'], 'Updated')

    def test_profiles_and_editor_sessions_are_independent(self):
        apple = self.add('Apple')
        mlx = self.add('MLX', 'mlx_lm')
        self.assertNotEqual(apple['provider_id'], mlx['provider_id'])
        self.app.dispatch('edit', {'profile_id': apple['id']})
        outdated = self.form('Stale edit')
        self.app.dispatch('back')
        self.app.dispatch('edit', {'profile_id': mlx['id']})
        with self.assertRaisesRegex(ValueError, 'form has closed'):
            self.app.dispatch('install', outdated)
        self.app.dispatch('install', self.form('MLX changed'))
        self.assertEqual(self.store.get(apple['id'])['name'], 'Apple')
        self.assertEqual(self.store.get(mlx['id'])['name'], 'MLX changed')
        self.assertEqual(self.ai.created, 2)
        self.assertEqual(set(SettingsStore(self.store.path).data), {'service', 'profiles'})

    def test_refresh_removes_deleted_providers_from_home_and_disk(self):
        apple = self.add('Apple')
        mlx = self.add('MLX', 'mlx_lm')
        reopened = SetupApp(SettingsStore(self.store.path), ai=self.ai, status=self.app.status)
        self.assertEqual(reopened.snapshot()['page'], 'home')
        self.assertEqual(reopened.snapshot()['profiles'][0]['installation_status'], 'checking')
        del self.ai.providers[apple['provider_id']]
        state = reopened.dispatch('refresh')
        self.assertEqual([p['provider_id'] for p in state['profiles']], [mlx['provider_id']])
        self.assertEqual(state['profiles'][0]['installation_status'], 'installed')
        self.assertEqual(len(SettingsStore(self.store.path).data['profiles']), 1)
        reopened.close()

    def test_deleted_provider_can_be_recreated_from_an_open_editor(self):
        profile = self.add()
        self.app.dispatch('edit', {'profile_id': profile['id']})
        del self.ai.providers[profile['provider_id']]
        refreshed = self.app.dispatch('refresh')
        self.assertEqual(refreshed['page'], 'new')
        self.assertEqual(refreshed['profiles'], [])
        saved = self.app.dispatch('install', self.form('Restored'))
        self.assertNotEqual(saved['profiles'][0]['provider_id'], profile['provider_id'])
        self.assertEqual(self.ai.created, 2)

    def test_native_failures_do_not_create_drafts_or_overwrite_saved_settings(self):
        self.new()
        with patch.object(self.ai, 'create_custom_provider', side_effect=RuntimeError('Native write failed')):
            with self.assertRaisesRegex(RuntimeError, 'Native write failed'):
                self.app.dispatch('install', self.form())
        self.assertEqual(SettingsStore(self.store.path).data['profiles'], [])
        self.assertEqual(self.app.snapshot()['page'], 'new')
        saved = self.app.dispatch('install', self.form())['profiles'][0]
        with patch.object(self.ai, 'get_custom_provider', side_effect=RuntimeError('Temporary native failure')):
            state = self.app.dispatch('refresh')
            self.assertEqual(state['profiles'][0]['provider_id'], saved['provider_id'])
            self.assertEqual(state['profiles'][0]['installation_status'], 'unknown')
            self.app.dispatch('edit', {'profile_id': saved['id']})
            with self.assertRaisesRegex(RuntimeError, 'Temporary native failure'):
                self.app.dispatch('install', self.form('Not saved'))
        self.assertEqual(self.ai.created, 1)
        self.assertEqual(SettingsStore(self.store.path).get(saved['id'])['name'], saved['name'])

    def test_adding_while_probe_runs_returns_home_and_cancels_the_probe(self):
        started = threading.Event()
        def probe(settings, prompt, cancelled):
            started.set()
            cancelled.wait(3)
            raise asyncio.CancelledError
        self.app.probe = probe
        self.new()
        editor = self.app.editor_id
        self.app.dispatch('test', {**self.form(), 'prompt': 'Hello'})
        self.assertTrue(started.wait(1))
        saved = self.app.dispatch('install', self.form())
        self.assertEqual(saved['page'], 'home')
        self.app.jobs[editor]['worker'].join(2)
        self.assertTrue(self.app.jobs[editor]['cancelled'].is_set())
        self.assertEqual(self.ai.created, 1)

    def test_validation_keeps_form_open_and_rejects_private_fields(self):
        self.new()
        with self.assertRaises(ValueError):
            self.app.dispatch('install', self.form(''))
        payload = self.form()
        payload['settings']['port'] = 1234
        with self.assertRaises(ValueError):
            self.app.dispatch('install', payload)
        self.assertEqual(self.app.snapshot()['page'], 'new')
        self.assertEqual(self.store.service['port'], 8768)
        self.assertEqual(self.ai.created, 0)
        self.app.dispatch('back')
        self.assertEqual(self.app.snapshot()['page'], 'home')

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
