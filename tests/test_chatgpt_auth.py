"""Dynamic OAuth registration, verified identities, rotation, and local recovery."""

import base64
import hashlib
import json
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from ai_setup.chatgpt_auth import (AuthError, ChatGPTLogin, ISSUER, PLAN_SCOPE, RESOURCE,
                                  plan_enabled, refresh_credentials, verify_identity)
from ai_setup.l10n import Localizer
import test_codex_auth as callbacks
from test_codex_auth import wait_for


def b64(value):
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


class ChatGPTAuthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        numbers = cls.key.public_key().public_numbers()
        cls.jwks = {"keys": [{"kid": "test-key", "kty": "RSA", "alg": "RS256", "use": "sig",
                              "n": b64(numbers.n.to_bytes(256, "big")), "e": b64(numbers.e.to_bytes(3, "big"))}]}

    def token(self, **changes):
        claims = {"iss": ISSUER, "sub": "user", "aud": "oaiapp_test", "nonce": "nonce",
                  "iat": 1000, "exp": 2000, "email": "one@example.test"}
        claims.update(changes)
        value = b64(json.dumps({"alg": "RS256", "kid": "test-key"}).encode()) + "." + b64(json.dumps(claims).encode())
        return value + "." + b64(self.key.sign(value.encode(), padding.PKCS1v15(), hashes.SHA256()))

    def login(self, **kwargs):
        login = ChatGPTLogin(Localizer("en"), "urn:uuid:host", port=0, **kwargs).start()
        self.addCleanup(callbacks.OAuthTests.stop, login)
        wait_for(lambda: login.snapshot()["phase"] != "starting")
        return login

    def test_signature_and_account_claims_are_required(self):
        good = self.token()
        self.assertEqual(verify_identity(good, "oaiapp_test", "nonce", jwks=self.jwks, now=1500)["sub"], "user")
        for changes in ({"iss": "https://other.example"}, {"aud": "other-client"}, {"nonce": "other"},
                        {"exp": 1500}, {"exp": "2000"}, {"iat": 1900}, {"sub": ""}, {"nbf": 1800},
                        {"aud": ["oaiapp_test", "other"], "azp": "other"}):
            with self.subTest(changes=changes), self.assertRaises(AuthError):
                verify_identity(self.token(**changes), "oaiapp_test", "nonce", jwks=self.jwks, now=1500)
        with self.assertRaises(AuthError):
            verify_identity(good, "oaiapp_test", "nonce", previous_subject="another-user", jwks=self.jwks, now=1500)
        parts = good.split(".")
        parts[1] = b64(json.dumps({"iss": ISSUER, "sub": "attacker", "aud": "oaiapp_test", "nonce": "nonce", "iat": 1000, "exp": 2000}).encode())
        with self.assertRaises(AuthError):
            verify_identity(".".join(parts), "oaiapp_test", "nonce", jwks=self.jwks, now=1500)
        parts[0] = b64(json.dumps({"alg": "none", "kid": "test-key"}).encode())
        with self.assertRaises(AuthError):
            verify_identity(".".join(parts), "oaiapp_test", "nonce", jwks=self.jwks, now=1500)

    def test_new_registration_uses_issued_client_and_verified_identity(self):
        login = self.login()
        query = parse_qs(urlsplit(login.browser_url()).query)
        self.assertEqual(urlsplit(login.browser_url()).path, "/api/accounts/authorize")
        self.assertEqual(query["client_id"], ["dynamic_agent_client"])
        self.assertEqual(query["agent_name_hint"], ["Pythona AI Setup"])
        self.assertEqual(query["resource"], [RESOURCE])
        self.assertIn(PLAN_SCOPE, query["scope"][0].split())
        self.assertEqual(query["code_challenge"], [b64(hashlib.sha256(login.verifier.encode()).digest())])
        self.assertEqual(callbacks.OAuthTests.request(login)[0], 400)
        identity = self.token(nonce=login.nonce, iat=time.time(), exp=time.time() + 3600)
        result = {"access_token": "secret-access", "refresh_token": "secret-refresh", "id_token": identity,
                  "expires_in": 3600, "scope": PLAN_SCOPE + " offline_access"}
        with patch("ai_setup.chatgpt_auth.token_request", return_value=result) as exchange, \
                patch("ai_setup.chatgpt_auth.read_json_response", return_value=self.jwks):
            self.assertEqual(callbacks.OAuthTests.request(login, {"state": login.state, "code": "code", "client_id": "oaiapp_test"})[0], 200)
            self.assertTrue(login.closed.wait(2))
        auth = login.take_result()
        self.assertIsNotNone(auth, login.snapshot())
        self.assertEqual(exchange.call_args.args[0]["client_id"], "oaiapp_test")
        self.assertEqual(exchange.call_args.args[0]["redirect_uri"], login.redirect_uri)
        self.assertEqual(auth["subject"], "user")
        self.assertEqual(auth["client_id"], "oaiapp_test")
        self.assertRegex(auth["local_grant_id"], r"^[a-f0-9]{32}$")
        self.assertTrue(plan_enabled(auth))
        self.assertNotIn("secret-", repr(login.snapshot()))

    def test_reauthorization_reuses_registration_and_rejects_another_client(self):
        previous = {"client_id": "oaiapp_test", "id_token": "retained-hint", "email": "one@example.test"}
        login = self.login(previous=previous)
        query = parse_qs(urlsplit(login.browser_url()).query)
        self.assertEqual(query["client_id"], ["oaiapp_test"])
        self.assertEqual(query["id_token_hint"], ["retained-hint"])
        self.assertNotIn("agent_name_hint", query)
        self.assertEqual(callbacks.OAuthTests.request(login, {"state": login.state, "code": "code", "client_id": "oaiapp_other"})[0], 400)
        self.assertEqual(callbacks.OAuthTests.request(login, {"state": login.state, "code": "code", "client_id": ["oaiapp_test", "oaiapp_test"]})[0], 400)
        self.assertEqual(login.client_id, "oaiapp_test")

    def test_recovery_keeps_nonce_registration_and_original_pkce(self):
        login = self.login()
        original = (login.nonce, login.state, login.verifier, login.browser_url(), login.deadline, login.port)
        for server, _, _, _ in list(login.listeners):
            server.server_close()
        login.resume()
        wait_for(lambda: login.recoveries == 1)
        self.assertEqual((login.nonce, login.state, login.verifier, login.browser_url(), login.deadline, login.port), original)

    def test_failed_exchange_retains_issued_id_for_an_explicit_retry(self):
        login = self.login()
        with patch("ai_setup.chatgpt_auth.token_request", side_effect=AuthError("auth_exchange_failed")):
            callbacks.OAuthTests.request(login, {"state": login.state, "code": "code", "client_id": "oaiapp_test"})
            self.assertTrue(login.closed.wait(2))
        self.assertIsNone(login.take_result())
        retry = self.login(previous=login.registration)
        self.assertEqual(parse_qs(urlsplit(retry.browser_url()).query)["client_id"], ["oaiapp_test"])

    def test_cancellation_during_verification_discards_credentials(self):
        entered, release = threading.Event(), threading.Event()
        login = self.login()

        def verify(*args):
            entered.set()
            release.wait(2)
            return {"sub": "user", "email": "one@example.test"}

        self.addCleanup(release.set)
        with patch("ai_setup.chatgpt_auth.token_request", return_value={"access_token": "a", "refresh_token": "r", "id_token": "i", "expires_in": 3600, "scope": PLAN_SCOPE}), \
                patch("ai_setup.chatgpt_auth.verify_identity", side_effect=verify):
            callbacks.OAuthTests.request(login, {"state": login.state, "code": "code", "client_id": "oaiapp_test"})
            self.assertTrue(entered.wait(1))
            login.cancel()
            release.set()
            self.assertTrue(login.closed.wait(2))
        self.assertIsNone(login.take_result())

    def test_identity_without_plan_permission_is_not_inference_authorization(self):
        login = ChatGPTLogin(Localizer("en"), "host")
        login.client_id = "oaiapp_test"
        with patch("ai_setup.chatgpt_auth.token_request", return_value={"access_token": "a", "refresh_token": "r", "id_token": "i", "expires_in": 3600, "scope": "openid email offline_access"}), \
                patch("ai_setup.chatgpt_auth.verify_identity", return_value={"sub": "user"}):
            auth = login.exchange_code("code", "verifier", login.redirect_uri)
        self.assertFalse(plan_enabled(auth))
        retry = ChatGPTLogin(Localizer("en"), "host", auth)
        self.assertEqual(parse_qs(urlsplit(retry.authorization_url()).query)["prompt"], ["consent"])

    def test_refresh_uses_issued_client_resource_and_replacement_token(self):
        previous = {"client_id": "oaiapp_test", "refresh_token": "old", "id_token": "verified", "scope": PLAN_SCOPE,
                    "subject": "user", "local_grant_id": "same-login"}
        with patch("ai_setup.chatgpt_auth.token_request", return_value={"access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 3600, "id_token": "unverified"}) as request:
            value = refresh_credentials(previous)
        self.assertEqual(request.call_args.args[0], {"grant_type": "refresh_token", "client_id": "oaiapp_test", "refresh_token": "old", "resource": RESOURCE})
        self.assertEqual(value["refresh_token"], "new-refresh")
        self.assertEqual(value["id_token"], "verified")
        self.assertEqual(value["local_grant_id"], "same-login")
        self.assertTrue(plan_enabled(value))
