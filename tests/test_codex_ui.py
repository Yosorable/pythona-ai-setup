"""Host checks for the Python side of Safari presentation and foreground callbacks."""

import importlib.util
import json
from pathlib import Path
import threading
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch


class _NSObject:
    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__()


def load_ui():
    objc = ModuleType("rubicon.objc")
    objc.NSObject = _NSObject
    objc.ObjCClass = Mock()
    objc.SEL = lambda value: value
    objc.objc_method = lambda value: value
    runtime = ModuleType("rubicon.objc.runtime")
    runtime.load_library = Mock(return_value=object())
    spec = importlib.util.spec_from_file_location("ai_setup._tested_ui", Path(__file__).resolve().parents[1] / "ai_setup/ui.py")
    module = importlib.util.module_from_spec(spec)
    with patch.dict("sys.modules", {"rubicon": ModuleType("rubicon"), "rubicon.objc": objc, "rubicon.objc.runtime": runtime}):
        spec.loader.exec_module(module)
    return module


class SafariLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.ui = load_ui()
        self.login = SimpleNamespace(browser_url=lambda: "https://auth.openai.com/oauth/authorize?test",
                                     snapshot=lambda: {"phase": "waiting"}, resume=Mock(), cancel=Mock(), fail=Mock())
        self.app = SimpleNamespace(login=self.login, closed=threading.Event(), poll_login=Mock())
        self.host = self.ui.WebSettings(self.app)
        self.host.handler = self.ui.PageHandler()
        self.host.handler.host_ref = lambda: self.host
        self.application = SimpleNamespace(applicationState=0)
        self.controller = Mock()
        factory = Mock()
        factory.alloc.return_value.initWithURL_.return_value = self.controller
        classes = {"UIApplication": SimpleNamespace(sharedApplication=self.application),
                   "SFSafariViewController": factory,
                   "NSURL": SimpleNamespace(URLWithString_=lambda value: value)}
        self.ui.ObjCClass = classes.__getitem__
        self.ui.presenter = Mock(return_value=Mock())
        self.ui.run_on_ui = lambda action: action()

    def test_foreground_wakes_login_even_when_web_page_is_covered(self):
        self.host.handler.resumeLogin_(None)
        self.login.resume.assert_called_once()
        self.app.closed.set()
        self.host.handler.resumeLogin_(None)
        self.login.resume.assert_called_once()

    def test_background_defers_presentation_and_completion_dismisses_once(self):
        self.application.applicationState = 2
        self.host.sync_browser()
        self.ui.presenter.assert_not_called()
        self.login.fail.assert_not_called()
        self.application.applicationState = 0
        self.host.sync_browser()
        self.host.sync_browser()
        self.ui.presenter.return_value.presentViewController_animated_completion_.assert_called_once()
        self.assertIs(self.controller.delegate, self.host.handler)
        self.login.snapshot = lambda: {"phase": "exchanging"}
        self.host.sync_browser()
        self.controller.dismissViewControllerAnimated_completion_.assert_not_called()
        self.login.snapshot = lambda: {"phase": "success"}
        self.host.sync_browser()
        self.host.sync_browser()
        self.controller.dismissViewControllerAnimated_completion_.assert_called_once()

    def test_user_close_cancels_only_the_session_that_owned_the_window(self):
        self.host.sync_browser()
        self.host.handler.safariViewControllerDidFinish_(self.controller)
        self.login.cancel.assert_called_once()
        self.login.cancel.reset_mock()
        new_login = SimpleNamespace(browser_url=lambda: None, snapshot=lambda: {"phase": "starting"}, cancel=Mock())
        self.app.login = new_login
        self.host.handler.safariViewControllerDidFinish_(self.controller)
        self.host.process_next(timeout=0)
        self.login.cancel.assert_not_called()
        new_login.cancel.assert_not_called()

    def test_queued_cancel_precedes_consuming_a_completed_login(self):
        from test_codex_setup import CodexSetupTests
        case = CodexSetupTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        app = case.app
        app.dispatch("login", case.form())
        host = self.ui.WebSettings(app)
        host.schedule_browser_sync = Mock()
        host.requests.put({"id": 1, "action": "cancel_login", "payload": {"editor_id": app.editor_id}})
        app.login.complete()
        host.process_next(timeout=0)
        self.assertIsNone(app.draft_auth)
        self.assertFalse(app.snapshot()["auth"].get("ready"))
        self.assertEqual(case.ai.created, 0)

    def test_bridge_cancel_also_precedes_an_older_queued_state_poll(self):
        from test_codex_setup import CodexSetupTests
        case = CodexSetupTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        app = case.app
        app.dispatch("login", case.form())
        host = self.ui.WebSettings(app)
        host.schedule_browser_sync = Mock()
        handler = self.ui.PageHandler()
        handler.host_ref = lambda: host
        host.requests.put({"id": 1, "action": "state"})
        app.login.complete()
        message = SimpleNamespace(frameInfo=SimpleNamespace(isMainFrame=True), body=json.dumps(
            {"id": 2, "action": "cancel_login", "payload": {"editor_id": app.editor_id}}))
        handler.userContentController_didReceiveScriptMessage_(None, message)
        host.process_next(timeout=0)
        host.process_next(timeout=0)
        self.assertIsNone(app.draft_auth)
        self.assertFalse(app.snapshot()["auth"].get("ready"))
