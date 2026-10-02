"""A WebKit container; settings, layout, and forms live in the local web page."""

import json
import queue
import weakref

from rubicon.objc import NSObject, ObjCClass, SEL, objc_method
from rubicon.objc.runtime import load_library

from .app import SetupApp
from .l10n import app_language
from .page import render_page
from .settings import SettingsStore


_WEBKIT = load_library("WebKit")


def objc_value(receiver, name):
    value = getattr(receiver, name)
    return value() if callable(value) else value


def presenter():
    app = objc_value(ObjCClass("UIApplication"), "sharedApplication")
    for scene in objc_value(app.connectedScenes, "allObjects"):
        if not scene.isKindOfClass_(ObjCClass("UIWindowScene")) or scene.activationState != 0:
            continue
        for window in scene.windows:
            if objc_value(window, "isKeyWindow"):
                controller = window.rootViewController
                while controller.presentedViewController is not None:
                    controller = controller.presentedViewController
                return controller
    raise RuntimeError("No active window is available to present settings")


class PageHandler(NSObject, auto_rename=True):
    @objc_method
    def closeTap(self):
        host = self.host_ref()
        if host is not None and not host.app.closed.is_set():
            host.app.close()

    @objc_method
    def resumeLogin_(self, notification):
        host = self.host_ref()
        if host is not None and not host.app.closed.is_set():
            # Wake the listener even while Safari covers the setup WKWebView.
            login = host.app.login
            if login is not None:
                login.resume()

    @objc_method
    def safariViewControllerDidFinish_(self, controller):
        host = self.host_ref()
        if host is not None and host.safari is not None and controller == host.safari:
            # Cancel immediately, before a queued UI poll can consume a late token response.
            host.safari_login.cancel()
            host.safari = host.safari_login = None

    @objc_method
    def userContentController_didReceiveScriptMessage_(self, controller, message):
        host = self.host_ref()
        if host is not None and not host.app.closed.is_set() and objc_value(message.frameInfo, "isMainFrame"):
            # JSON preserves booleans across Foundation's NSNumber bridge.
            try:
                body = json.loads(str(message.body))
            except (ValueError, TypeError):
                return
            if isinstance(body, dict) and type(body.get("id")) is int:
                if body.get("action") == "cancel_login":
                    payload, login = body.get("payload"), host.app.login
                    if (isinstance(payload, dict) and payload.get("editor_id") == host.app.login_editor
                            and login is not None):
                        login.cancel()
                host.requests.put(body)


class WebSettings:
    def __init__(self, app):
        self.app = app
        self.requests = queue.Queue()
        self.controller = self.navigation = self.webview = self.handler = None
        self.safari = self.safari_login = self.shown_login = None
        self.browser_sync_pending = False
        self.safari_library = None

    def open(self):
        self.controller = ObjCClass("UIViewController").alloc().init()
        self.controller.title = "Pythona AI Setup"
        configuration = ObjCClass("WKWebViewConfiguration").alloc().init()
        configuration.websiteDataStore = objc_value(ObjCClass("WKWebsiteDataStore"), "nonPersistentDataStore")
        self.handler = PageHandler.alloc().init()
        self.handler.host_ref = weakref.ref(self)
        notifications = objc_value(ObjCClass("NSNotificationCenter"), "defaultCenter")
        for name in ("UIApplicationWillEnterForegroundNotification", "UIApplicationDidBecomeActiveNotification"):
            notifications.addObserver_selector_name_object_(self.handler, SEL("resumeLogin:"), name, None)
        close = ObjCClass("UIBarButtonItem").alloc().initWithImage_style_target_action_(
            ObjCClass("UIImage").systemImageNamed_("xmark"), 0, self.handler, SEL("closeTap"))
        close.tintColor = objc_value(ObjCClass("UIColor"), "labelColor")
        self.controller.navigationItem.rightBarButtonItem = close
        configuration.userContentController.addScriptMessageHandler_name_(self.handler, "setup")
        webview = ObjCClass("WKWebView").alloc().initWithFrame_configuration_(self.controller.view.bounds, configuration)
        webview.autoresizingMask = 2 | 16
        # UIKit accounts for the navigation bar and safe area; WebKit handles the keyboard.
        webview.scrollView.contentInsetAdjustmentBehavior = 0
        self.webview = webview
        self.controller.view.addSubview_(webview)
        self.controller.setContentScrollView_forEdge_(webview.scrollView, 1)
        self.navigation = ObjCClass("UINavigationController").alloc().initWithRootViewController_(self.controller)
        self.navigation.modalPresentationStyle = 0
        webview.loadHTMLString_baseURL_(render_page(self.app.tr.language), None)
        presenter().presentViewController_animated_completion_(self.navigation, True, None)

    def process_next(self, timeout=0.1):
        try:
            request = self.requests.get(timeout=timeout)
        except queue.Empty:
            self.app.poll_login()
            self.schedule_browser_sync()
            return False
        try:
            result = self.app.dispatch(request.get("action"), request.get("payload"))
            if request.get("action") == "manage_usage":
                def open_usage():
                    application = objc_value(ObjCClass("UIApplication"), "sharedApplication")
                    url = ObjCClass("NSURL").URLWithString_("https://chatgpt.com/settings/usage")
                    application.openURL_options_completionHandler_(url, {}, None)
                run_on_ui(open_usage)
            response = {"id": request["id"], "result": result}
        except Exception as error:
            response = {"id": request["id"], "error": self.app.tr.error(error)}
        self.schedule_browser_sync()
        code = "window.setupBridge.receive(" + json.dumps(response, ensure_ascii=True) + ")"

        def reply():
            if self.webview is not None:
                self.webview.evaluateJavaScript_completionHandler_(code, None)
        run_on_ui(reply)
        return True

    def schedule_browser_sync(self):
        if self.browser_sync_pending:
            return
        self.browser_sync_pending = True

        def sync():
            try:
                self.sync_browser()
            finally:
                self.browser_sync_pending = False
        run_on_ui(sync)

    def sync_browser(self):
        login = self.app.login
        if self.safari is not None:
            if self.app.closed.is_set() or login is not self.safari_login or login.snapshot()["phase"] in ("success", "error", "cancelled"):
                self.safari.dismissViewControllerAnimated_completion_(False, None)
                self.safari = self.safari_login = None
            return
        if self.app.closed.is_set() or login is None or login is self.shown_login:
            return
        url = login.browser_url()
        if url is None:
            return
        try:
            application = objc_value(ObjCClass("UIApplication"), "sharedApplication")
            if objc_value(application, "applicationState") != 0:
                return
            self.safari_library = self.safari_library or load_library("SafariServices")
            controller = ObjCClass("SFSafariViewController").alloc().initWithURL_(ObjCClass("NSURL").URLWithString_(url))
            controller.delegate = self.handler
            controller.modalPresentationStyle = 0
            self.safari, self.safari_login, self.shown_login = controller, login, login
            presenter().presentViewController_animated_completion_(controller, True, None)
        except Exception:
            self.safari = self.safari_login = None
            login.fail("auth_browser_failed")

    def close(self):
        self.app.close()
        if self.handler is not None:
            objc_value(ObjCClass("NSNotificationCenter"), "defaultCenter").removeObserver_(self.handler)
        if self.safari is not None:
            self.safari.dismissViewControllerAnimated_completion_(False, None)
            self.safari = self.safari_login = None
        if self.webview is not None:
            self.webview.stopLoading()
            self.webview.configuration.userContentController.removeScriptMessageHandlerForName_("setup")
        if self.navigation is not None and self.navigation.presentingViewController is not None:
            self.navigation.dismissViewControllerAnimated_completion_(False, None)
        self.controller = self.navigation = self.webview = self.handler = None


def main():
    store = SettingsStore()
    store.save()
    app = SetupApp(store, app_language())
    host = WebSettings(app)
    try:
        run_on_ui(host.open).wait()
        app.refresh_installations()
        while not app.closed.is_set():
            host.process_next()
    finally:
        run_on_ui(host.close).wait()
        for job in app.jobs.values():
            job["worker"].join(timeout=0.1)
