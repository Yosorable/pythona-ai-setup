"""Codex PKCE login with a recoverable, short-lived loopback callback listener."""

import base64
import contextlib
from datetime import datetime, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import http.client
import json
import secrets
import sys
import threading
import time
import types
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError


CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
CLIENT_VERSION = "0.160.0"
USER_AGENT = f"codex_cli_rs/{CLIENT_VERSION} (iOS; arm64)"
TOKEN_URL = "https://auth.openai.com/oauth/token"
AUTHORIZE_URL = "https://auth.openai.com/oauth/authorize"
CALLBACK_PATH = "/auth/callback"
LOGIN_SECONDS = 600


class AuthError(RuntimeError):
    """A localized error key without response bodies or credentials."""


def token_request(values):
    request = Request(TOKEN_URL, data=urlencode(values).encode("ascii"),
                      headers={"Content-Type": "application/x-www-form-urlencoded",
                               "User-Agent": USER_AGENT, "originator": "codex_cli_rs"})
    try:
        with urlopen(request, timeout=15) as response:
            body = response.read(131073)
        if len(body) > 131072:
            raise ValueError("Oversized token response")
        value = json.loads(body)
        if not isinstance(value, dict) or not isinstance(value.get("access_token"), str) or not value["access_token"]:
            raise ValueError("Missing access token")
        return value
    except (HTTPError, URLError, OSError, ValueError) as error:
        raise AuthError("auth_exchange_failed") from error


def token_claims(token):
    # These claims are display metadata from a TLS-authenticated token response,
    # never an authorization decision or a substitute for verifying OAuth state.
    if not isinstance(token, str):
        return {}
    try:
        part = token.split(".")[1]
        value = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
        return value if isinstance(value, dict) else {}
    except (ValueError, IndexError, TypeError):
        return {}


def credentials(value, previous=None):
    result = dict(previous or {})
    for key in ("access_token", "refresh_token", "id_token"):
        if isinstance(value.get(key), str) and value[key]:
            result[key] = value[key]
    if not result.get("access_token") or not result.get("refresh_token"):
        raise AuthError("auth_exchange_failed")
    claims = token_claims(result.get("id_token", ""))
    account = claims.get("https://api.openai.com/auth", {})
    if not isinstance(account, dict):
        account = {}
    for key, item in (("account_id", account.get("chatgpt_account_id") or claims.get("chatgpt_account_id")),
                      ("email", claims.get("email")),
                      ("plan_type", account.get("chatgpt_plan_type") or claims.get("chatgpt_plan_type"))):
        if isinstance(item, str) and item:
            result[key] = item
    result["last_refresh"] = datetime.now(timezone.utc).isoformat()
    return result


def exchange_code(code, verifier, redirect_uri):
    return credentials(token_request({"grant_type": "authorization_code", "client_id": CLIENT_ID,
                                      "code": code, "code_verifier": verifier, "redirect_uri": redirect_uri}))


def login_registry():
    # Like the model-service registry, retain the previous owner across script
    # runs even when its listener has disappeared while iOS was suspended.
    state = types.ModuleType("_pythona_ai_setup_oauth")
    state.lock = threading.Lock()
    state.active = None
    return sys.modules.setdefault(state.__name__, state)


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False
    allow_reuse_address = True

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(2)
        return connection, address

    def handle_error(self, request, client_address):
        # HTTP diagnostics must never print callback URLs or authorization codes.
        pass


class OAuthLogin:
    def __init__(self, tr, port=1455, timeout=LOGIN_SECONDS, exchange=exchange_code):
        self.tr, self.port, self.exchange = tr, port, exchange
        self.state = secrets.token_urlsafe(32)
        self.verifier = secrets.token_urlsafe(48)
        self.instance = secrets.token_hex(24)
        self.deadline = time.time() + timeout
        self.lock = threading.RLock()
        self.wake = threading.Event()
        self.cancelled = threading.Event()
        self.closed = threading.Event()
        self.worker = None
        self.listeners = []
        self.phase, self.message = "starting", tr("auth_starting")
        self.url = None
        self.result = None
        self.code = None
        self.recoveries = 0

    @property
    def redirect_uri(self):
        return f"http://127.0.0.1:{self.port}{CALLBACK_PATH}"

    def start(self):
        registry = login_registry()
        with registry.lock:
            previous = registry.active
            if previous is not None and previous is not self:
                previous.cancel()
                if previous.worker is not None:
                    previous.worker.join(2)
                if previous.worker is None or not previous.worker.is_alive():
                    previous._stop_listeners()
                    previous.closed.set()
                if not previous.closed.is_set():
                    raise AuthError("auth_still_stopping")
            registry.active = self
            self.worker = threading.Thread(target=self._run, name="Codex OAuth", daemon=True)
            self.worker.start()
        return self

    def snapshot(self):
        with self.lock:
            return {"phase": self.phase, "message": self.message}

    def browser_url(self):
        with self.lock:
            return self.url if self.phase == "waiting" and not self.cancelled.is_set() else None

    def take_result(self):
        with self.lock:
            result, self.result = self.result, None
            return result

    def resume(self):
        self.wake.set()

    def cancel(self):
        self.cancelled.set()
        self.wake.set()
        with self.lock:
            pending_result = self.result is not None
            self.result = None
            if self.phase not in ("success", "error") or pending_result:
                self.phase, self.message = "cancelled", self.tr("auth_cancelled")

    def fail(self, key):
        with self.lock:
            self.phase, self.message = "error", self.tr(key)
        self.cancelled.set()
        self.wake.set()

    def authorization_url(self):
        challenge = base64.urlsafe_b64encode(hashlib.sha256(self.verifier.encode()).digest()).rstrip(b"=").decode()
        return AUTHORIZE_URL + "?" + urlencode({
            "response_type": "code", "client_id": CLIENT_ID, "redirect_uri": self.redirect_uri,
            "scope": "openid profile email offline_access", "state": self.state,
            "code_challenge": challenge, "code_challenge_method": "S256",
            "id_token_add_organizations": "true", "codex_cli_simplified_flow": "true",
            "originator": "codex_cli_rs"})

    def accept_callback(self, query):
        """Allow a specialized OAuth flow to validate callback registration metadata."""

    def success_message(self, value):
        return self.tr("auth_success")

    def _handler(self):
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                self.connection.settimeout(2)
                host = self.headers.get("Host", "")
                if host not in (f"localhost:{owner.port}", f"127.0.0.1:{owner.port}", f"[::1]:{owner.port}"):
                    return self.reply(400, owner.tr("auth_invalid_callback"))
                if self.path == "/__oauth_health":
                    return self.reply(200, owner.instance)
                if len(self.path) > 8192:
                    return self.reply(400, owner.tr("auth_invalid_callback"))
                try:
                    url = urlsplit(self.path)
                    query = parse_qs(url.query, keep_blank_values=True, max_num_fields=16)
                    valid = (url.path == CALLBACK_PATH and len(query.get("state", [])) == 1
                             and secrets.compare_digest(query["state"][0].encode(), owner.state.encode()))
                except ValueError:
                    valid = False
                if not valid:
                    return self.reply(400, owner.tr("auth_invalid_callback"))
                with owner.lock:
                    if owner.cancelled.is_set() or time.time() >= owner.deadline:
                        return self.reply(410, owner.tr("auth_expired"))
                    if owner.phase != "waiting":
                        return self.reply(409, owner.tr("auth_callback_received"))
                    if "error" in query:
                        owner.phase, owner.message = "error", owner.tr("auth_denied")
                    elif len(query.get("code", [])) == 1 and query["code"][0]:
                        try:
                            owner.accept_callback(query)
                        except AuthError:
                            return self.reply(400, owner.tr("auth_invalid_callback"))
                        owner.code = query["code"][0]
                        owner.phase, owner.message = "exchanging", owner.tr("auth_exchanging")
                    else:
                        return self.reply(400, owner.tr("auth_invalid_callback"))
                self.reply(200, owner.tr("auth_callback_received"))
                owner.wake.set()

            def reply(self, status, text):
                body = text.encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                with contextlib.suppress(OSError):
                    self.wfile.write(body)

        return Handler

    def _listen(self):
        handler = self._handler()
        try:
            server = _Server(("127.0.0.1", self.port), handler)
        except OSError as error:
            raise AuthError("auth_port_busy") from error
        self.port = server.server_address[1]
        server.timeout = 0.2
        stopping = threading.Event()

        def serve():
            try:
                while not stopping.is_set():
                    server.handle_request()
            except (OSError, ValueError):
                pass
            finally:
                with contextlib.suppress(OSError):
                    server.server_close()

        thread = threading.Thread(target=serve, name="Codex OAuth callback", daemon=True)
        self.listeners.append((server, thread, stopping, "127.0.0.1"))
        thread.start()

    def _stop_listeners(self):
        for server, _, stopping, _ in self.listeners:
            stopping.set()
            # iOS may already have invalidated the underlying socket.
            with contextlib.suppress(OSError):
                server.server_close()
        for _, thread, _, _ in self.listeners:
            thread.join(1)
        if any(thread.is_alive() for _, thread, _, _ in self.listeners):
            raise AuthError("auth_still_stopping")
        self.listeners.clear()

    def _healthy(self):
        for _, thread, _, host in self.listeners:
            if not thread.is_alive():
                return False
            connection = http.client.HTTPConnection(host, self.port, timeout=0.5)
            try:
                connection.request("GET", "/__oauth_health")
                response = connection.getresponse()
                if response.status != 200 or response.read(128).decode() != self.instance:
                    return False
            except (OSError, ValueError, http.client.HTTPException):
                return False
            finally:
                connection.close()
        return bool(self.listeners)

    def _run(self):
        try:
            self._listen()
            with self.lock:
                if self.cancelled.is_set():
                    return
                self.url = self.authorization_url()
                self.phase, self.message = "waiting", self.tr("auth_waiting")
            while not self.cancelled.is_set():
                if time.time() >= self.deadline:
                    raise AuthError("auth_expired")
                with self.lock:
                    phase, code = self.phase, self.code
                if phase == "error":
                    return
                if phase == "exchanging":
                    value = self.exchange(code, self.verifier, self.redirect_uri)
                    if time.time() >= self.deadline:
                        raise AuthError("auth_expired")
                    with self.lock:
                        if not self.cancelled.is_set():
                            self.result = value
                            self.phase, self.message = "success", self.success_message(value)
                    return
                if not self._healthy():
                    # Rebind the SAME callback without regenerating state, verifier,
                    # browser URL, or deadline. A live Python thread is not a health check.
                    self._stop_listeners()
                    self._listen()
                    self.recoveries += 1
                    with self.lock:
                        if self.phase == "waiting":
                            self.message = self.tr("auth_recovered")
                self.wake.wait(2)
                self.wake.clear()
        except Exception as error:
            with self.lock:
                if not self.cancelled.is_set():
                    self.phase = "error"
                    self.message = self.tr(str(error) if isinstance(error, AuthError) else "auth_failed")
        finally:
            try:
                self._stop_listeners()
            except AuthError:
                with self.lock:
                    self.phase, self.message = "error", self.tr("auth_still_stopping")
            finally:
                self.code = self.verifier = None
                if not any(thread.is_alive() for _, thread, _, _ in self.listeners):
                    self.closed.set()
