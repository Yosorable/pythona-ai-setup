"""A WebKit container; settings, layout, and forms live in the local web page."""

import json
import queue
import weakref

from rubicon.objc import NSObject, ObjCClass, objc_method
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
        self.controller = self.webview = self.handler = None

    def open(self):
        self.controller = ObjCClass("UIViewController").alloc().init()
        self.controller.modalPresentationStyle = 0  # Full screen; the page owns its header.
        configuration = ObjCClass("WKWebViewConfiguration").alloc().init()
        configuration.websiteDataStore = objc_value(ObjCClass("WKWebsiteDataStore"), "nonPersistentDataStore")
        self.handler = PageHandler.alloc().init()
        self.handler.host_ref = weakref.ref(self)
        configuration.userContentController.addScriptMessageHandler_name_(self.handler, "setup")
        webview = ObjCClass("WKWebView").alloc().initWithFrame_configuration_(self.controller.view.bounds, configuration)
        webview.autoresizingMask = 2 | 16
        # WebKit handles keyboard scrolling; CSS accounts for the device safe area.
        webview.scrollView.contentInsetAdjustmentBehavior = 2
        self.webview = webview
        self.controller.view.addSubview_(webview)
        webview.loadHTMLString_baseURL_(render_page(self.app.tr.language), None)
        presenter().presentViewController_animated_completion_(self.controller, True, None)

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
                self.webview.evaluateJavaScript_completionHandler_(code, None)
        run_on_ui(reply)
        return True

    def close(self):
        self.app.close()
        if self.webview is not None:
            self.webview.stopLoading()
            self.webview.configuration.userContentController.removeScriptMessageHandlerForName_("setup")
        if self.controller is not None and self.controller.presentingViewController is not None:
            self.controller.dismissViewControllerAnimated_completion_(False, None)
        self.controller = self.webview = self.handler = None


def main():
    store = SettingsStore()
    store.save()
    app = SetupApp(store, app_language())
    host = WebSettings(app)
    try:
        run_on_ui(host.open).wait()
        app.check_availability()
        while not app.closed.is_set():
            host.process_next()
    finally:
        run_on_ui(host.close).wait()
        if app.worker is not None:
            app.worker.join(timeout=1)
