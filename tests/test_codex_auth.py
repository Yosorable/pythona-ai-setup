"""PKCE callbacks, cancellation, and recovery after a suspended listener is lost."""

import base64
import hashlib
import http.client
import json
import socket
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode, urlsplit

from ai_setup.codex_auth import AuthError, CLIENT_ID, OAuthLogin, credentials, exchange_code
from ai_setup.l10n import Localizer


def wait_for(predicate, timeout=3):
    end = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= end:
            raise AssertionError("Timed out waiting for login state")
        time.sleep(0.01)


class OAuthTests(unittest.TestCase):
    def login(self, **kwargs):
        value = OAuthLogin(Localizer("en"), port=kwargs.pop("port", 0), **kwargs).start()
        self.addCleanup(self.stop, value)
        wait_for(lambda: value.snapshot()["phase"] != "starting")
        return value

    @staticmethod
    def stop(login):
        login.cancel()
        login.worker.join(3)

    @staticmethod
    def request(login, query=None, host="127.0.0.1"):
        connection = http.client.HTTPConnection(host, login.port, timeout=2)
        try:
            connection.request("GET", "/auth/callback?" + urlencode(query or {"state": login.state, "code": "one-code"}, doseq=True))
            response = connection.getresponse()
            return response.status, response.read().decode()
        finally:
            connection.close()

    def test_pkce_callback_exchanges_once_and_never_exposes_credentials(self):
        calls = []
        entered, release = threading.Event(), threading.Event()

        def exchange(*args):
            calls.append(args)
            entered.set()
            release.wait(2)
            return {"access_token": "secret-access", "refresh_token": "secret-refresh"}

        self.addCleanup(release.set)
        login = self.login(exchange=exchange)
        query = parse_qs(urlsplit(login.browser_url()).query)
        self.assertEqual(query["client_id"], [CLIENT_ID])
        self.assertEqual(query["redirect_uri"], [login.redirect_uri])
        self.assertEqual(urlsplit(login.redirect_uri).hostname, "127.0.0.1")
        verifier = login.verifier
        self.assertEqual(query["code_challenge"], [base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()])
        self.assertEqual(self.request(login, {"state": "wrong", "code": "attacker-code"})[0], 400)
        self.assertEqual(login.snapshot()["phase"], "waiting")
        status, body = self.request(login)
        self.assertEqual(status, 200)
        self.assertTrue(entered.wait(1))
        self.assertEqual(self.request(login)[0], 409)
        release.set()
        self.assertTrue(login.closed.wait(2))
        self.assertEqual(calls, [("one-code", verifier, login.redirect_uri)])
        self.assertNotIn("secret-", body + repr(login.snapshot()))
        self.assertEqual(login.take_result()["refresh_token"], "secret-refresh")
        self.assertIsNone(login.take_result())

    def test_lost_listener_rebinds_same_port_and_accepts_original_authorization(self):
        calls = []
        login = self.login(exchange=lambda *args: calls.append(args) or {"access_token": "a", "refresh_token": "r"})
        original = (login.state, login.verifier, login.browser_url(), login.deadline, login.port)
        previous = list(login.listeners)
        # Simulate iOS losing listening sockets while the Python owner survives.
        for server, _, _, _ in previous:
            server.server_close()
        login.resume()
        wait_for(lambda: login.recoveries == 1)
        self.assertEqual((login.state, login.verifier, login.browser_url(), login.deadline, login.port), original)
        self.assertTrue(all(not thread.is_alive() for _, thread, _, _ in previous))
        self.assertEqual(self.request(login)[0], 200)
        self.assertTrue(login.closed.wait(2))
        self.assertEqual(login.snapshot()["phase"], "success")
        self.assertEqual(calls[0][1], original[1])

    def test_resume_does_not_restart_healthy_listener_or_extend_expiry(self):
        login = self.login()
        original = list(login.listeners)
        deadline = login.deadline
        login.resume()
        wait_for(lambda: not login.wake.is_set())
        self.assertEqual(login.listeners, original)
        self.assertEqual(login.deadline, deadline)
        login.deadline = time.time() - 1
        login.resume()
        self.assertTrue(login.closed.wait(2))
        self.assertEqual(login.snapshot()["phase"], "error")
        self.assertIn("expired", login.snapshot()["message"])
        self.assertFalse(login.listeners)

    def test_recovery_tolerates_close_errors_from_an_invalidated_socket(self):
        login = self.login()
        server = login.listeners[0][0]
        original_close = server.server_close
        original_close()

        def failed_close():
            original_close()
            raise OSError("Socket was invalidated during suspension")

        with patch.object(server, "server_close", side_effect=failed_close):
            login.resume()
            wait_for(lambda: login.recoveries == 1)
        self.assertEqual(login.snapshot()["phase"], "waiting")
        self.assertTrue(login._healthy())

    def test_repeated_script_login_stops_previous_owner_before_rebinding(self):
        first = self.login()
        old_state, port = first.state, first.port
        second = self.login(port=port)
        self.assertTrue(first.closed.is_set())
        self.assertTrue(first.cancelled.is_set())
        self.assertFalse(first.listeners)
        self.assertEqual(second.snapshot()["phase"], "waiting")
        self.assertEqual(self.request(second, {"state": old_state, "code": "old-code"})[0], 400)

    def test_occupied_port_is_not_taken_over(self):
        with socket.socket() as unrelated:
            unrelated.bind(("127.0.0.1", 0))
            unrelated.listen()
            login = self.login(port=unrelated.getsockname()[1])
            self.assertTrue(login.closed.wait(2))
            self.assertEqual(login.snapshot()["phase"], "error")
            self.assertIsNone(login.browser_url())
            self.assertGreaterEqual(unrelated.fileno(), 0)

    def test_cancellation_during_exchange_discards_late_result_and_closes_listener(self):
        entered, release = threading.Event(), threading.Event()

        def exchange(*args):
            entered.set()
            release.wait(2)
            return {"access_token": "late-access", "refresh_token": "late-refresh"}

        self.addCleanup(release.set)
        login = self.login(exchange=exchange)
        self.assertEqual(self.request(login)[0], 200)
        self.assertTrue(entered.wait(1))
        login.cancel()
        release.set()
        self.assertTrue(login.closed.wait(2))
        self.assertEqual(login.snapshot()["phase"], "cancelled")
        self.assertIsNone(login.take_result())
        self.assertFalse(login.listeners)

    def test_denial_and_duplicate_state_are_handled_without_exchange(self):
        calls = []
        login = self.login(exchange=lambda *args: calls.append(args))
        self.assertEqual(self.request(login, {"state": [login.state, login.state], "code": "x"})[0], 400)
        self.assertEqual(self.request(login, {"state": login.state, "error": "access_denied"})[0], 200)
        self.assertTrue(login.closed.wait(2))
        self.assertEqual(login.snapshot()["phase"], "error")
        self.assertEqual(calls, [])

    def test_cancel_discards_an_unclaimed_success(self):
        login = self.login(exchange=lambda *_: {"access_token": "a", "refresh_token": "r"})
        self.assertEqual(self.request(login)[0], 200)
        self.assertTrue(login.closed.wait(2))
        login.cancel()
        self.assertEqual(login.snapshot()["phase"], "cancelled")
        self.assertIsNone(login.take_result())

    def test_code_exchange_matches_authorization_client_and_redirect(self):
        with patch("ai_setup.codex_auth.token_request", return_value={"access_token": "a", "refresh_token": "r"}) as token:
            result = exchange_code("c", "v", "http://127.0.0.1:1455/auth/callback")
        self.assertEqual(token.call_args.args[0], {"grant_type": "authorization_code", "client_id": CLIENT_ID,
                         "code": "c", "code_verifier": "v", "redirect_uri": "http://127.0.0.1:1455/auth/callback"})
        self.assertEqual(result["refresh_token"], "r")
        with self.assertRaises(AuthError):
            credentials({"access_token": "a"})

    def test_account_metadata_and_optional_refresh_fields_are_preserved(self):
        claims = {"email": "test@example.test", "https://api.openai.com/auth": {"chatgpt_account_id": "account", "chatgpt_plan_type": "plus"}}
        token = "x." + base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=") + ".x"
        initial = credentials({"access_token": "a", "refresh_token": "r", "id_token": token})
        renewed = credentials({"access_token": "new"}, initial)
        self.assertEqual(renewed["refresh_token"], "r")
        self.assertEqual(renewed["email"], "test@example.test")
        self.assertEqual(renewed["account_id"], "account")
