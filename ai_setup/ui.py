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
    def doneTap(self):
        host = self.host_ref()
        if host is not None and not host.app.closed.is_set():
            host.request_close()

    @objc_method
    def backTap(self):
        host = self.host_ref()
        if host is not None and not host.app.closed.is_set():
            host.request_back()

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
                host.requests.put(body)


class WebSettings:
    def __init__(self, app):
        self.app = app
        self.requests = queue.Queue()
        self.controller = self.navigation = self.webview = self.handler = None
        self.page_ready = False
        self.navigation_page = None
        self.navigation_revision = 0

    def open(self):
        self.controller = ObjCClass("UIViewController").alloc().init()
        self.controller.title = "Pythona AI Setup"
        configuration = ObjCClass("WKWebViewConfiguration").alloc().init()
        configuration.websiteDataStore = objc_value(ObjCClass("WKWebsiteDataStore"), "nonPersistentDataStore")
        self.handler = PageHandler.alloc().init()
        self.handler.host_ref = weakref.ref(self)
        self.update_navigation({"page": "home", "revision": 0})
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

    def request_close(self):
        if not self.page_ready:
            self.app.close()
            return
        self.controller.view.endEditing_(True)
        self.webview.evaluateJavaScript_completionHandler_("void window.setupBridge.close()", None)

    def request_back(self):
        self.controller.view.endEditing_(True)
        self.webview.evaluateJavaScript_completionHandler_("void window.setupBridge.back()", None)

    def update_navigation(self, state):
        if state["revision"] < self.navigation_revision:
            return
        self.navigation_revision = state["revision"]
        page = state["page"]
        if page == self.navigation_page:
            return
        self.navigation_page = page
        self.controller.view.endEditing_(True)
        item = self.controller.navigationItem
        button = ObjCClass("UIBarButtonItem")
        if page == "home":
            self.controller.title = "Pythona AI Setup"
            item.leftBarButtonItem = None
            item.rightBarButtonItem = button.alloc().initWithTitle_style_target_action_(
                self.app.tr("done"), 2, self.handler, SEL("doneTap"))
        else:
            self.controller.title = self.app.tr("add_provider" if page == "new" else "edit_provider")
            title = self.app.tr("cancel") if page == "new" else "‹ " + self.app.tr("back")
            item.leftBarButtonItem = button.alloc().initWithTitle_style_target_action_(title, 0, self.handler, SEL("backTap"))
            item.rightBarButtonItem = None

    def process_next(self, timeout=0.1):
        try:
            request = self.requests.get(timeout=timeout)
        except queue.Empty:
            return False
        try:
            result = self.app.dispatch(request.get("action"), request.get("payload"))
            response = {"id": request["id"], "result": result}
        except Exception as error:
            response = {"id": request["id"], "error": self.app.tr.error(error)}
        code = "window.setupBridge.receive(" + json.dumps(response, ensure_ascii=True) + ")"

        def reply():
            if self.webview is not None:
                if request.get("action") == "state" and "result" in response:
                    self.page_ready = True
                if "result" in response:
                    self.update_navigation(response["result"])
                self.webview.evaluateJavaScript_completionHandler_(code, None)
        run_on_ui(reply)
        return True

    def close(self):
        self.app.close()
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
