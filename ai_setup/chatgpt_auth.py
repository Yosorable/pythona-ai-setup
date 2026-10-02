"""Public Sign in with ChatGPT for a local, dynamically registered client."""

import base64
import hashlib
import json
import math
import re
import secrets
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .codex_auth import AuthError, OAuthLogin


ISSUER = "https://auth.openai.com"
AUTHORIZE_URL = ISSUER + "/api/accounts/authorize"
TOKEN_URL = ISSUER + "/api/accounts/oauth/token"
JWKS_URL = ISSUER + "/.well-known/jwks.json"
RESOURCE = "https://api.openai.com/v1"
USER_AGENT = "PythonaAISetup/1.0 (iOS)"
PLAN_SCOPE = "chatgpt.tokens.use.direct"
SCOPES = "openid profile email offline_access resource.invoke " + PLAN_SCOPE
AUTH_STORAGE_KEY = "chatgpt_auth"
AUTH_FIELDS = ("client_id", "ext_agent_host_id", "issuer", "subject", "email", "access_token", "refresh_token",
               "id_token", "scope", "expires_at", "earliest_refresh_at", "plan_notice_seen", "local_grant_id")


def plan_enabled(auth):
    return PLAN_SCOPE in auth.get("scope", "").split()


def read_json_response(request, limit=131072):
    with urlopen(request, timeout=15) as response:
        raw = response.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("Oversized response")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("Invalid response")
    return value


def token_request(values):
    request = Request(TOKEN_URL, data=urlencode(values).encode("ascii"),
                      headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": USER_AGENT})
    try:
        result = read_json_response(request)
        if not isinstance(result.get("access_token"), str) or not result["access_token"]:
            raise ValueError("Missing access token")
        return result
    except HTTPError as error:
        try:
            data = json.loads(error.read(131072))
            code = data.get("error") if isinstance(data, dict) else None
            if isinstance(code, dict):
                code = code.get("code")
        except (ValueError, OSError):
            code = None
        finally:
            error.close()
        if values.get("grant_type") == "refresh_token" and code in (
                "invalid_grant", "invalid_refresh_token", "token_expired", "refresh_token_expired",
                "refresh_token_invalidated", "refresh_token_invalid", "refresh_token_reused"):
            raise AuthError("chatgpt_refresh_expired") from None
        raise AuthError("auth_exchange_failed") from None
    except (URLError, OSError, ValueError) as error:
        raise AuthError("auth_exchange_failed") from error


def decode_part(part):
    return base64.b64decode(part + "=" * (-len(part) % 4), altchars=b"-_", validate=True)


def verify_identity(token, client_id, nonce, previous_subject=None, jwks=None, now=None):
    """Verify the published RS256 OIDC contract using Pythona's cryptography library."""
    try:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding, rsa
    except ImportError as error:
        raise AuthError("chatgpt_crypto_unavailable") from error
    try:
        if not isinstance(token, str) or len(token) > 65536:
            raise ValueError("Invalid ID token")
        header_part, claims_part, signature_part = token.split(".")
        header = json.loads(decode_part(header_part))
        claims = json.loads(decode_part(claims_part))
        if not isinstance(header, dict) or header.get("alg") != "RS256" or header.get("crit") \
                or not isinstance(header.get("kid"), str) or not header["kid"] or not isinstance(claims, dict):
            raise ValueError("Invalid ID token")
        if jwks is None:
            jwks = read_json_response(Request(JWKS_URL, headers={"User-Agent": USER_AGENT}))
        keys = [key for key in jwks["keys"] if isinstance(key, dict) and key.get("kid") == header["kid"]
                and key.get("kty") == "RSA" and key.get("use", "sig") == "sig"
                and key.get("alg", "RS256") == "RS256" and "verify" in key.get("key_ops", ["verify"])]
        if len(keys) != 1:
            raise ValueError("Unknown signing key")
        key = keys[0]
        public = rsa.RSAPublicNumbers(int.from_bytes(decode_part(key["e"]), "big"),
                                     int.from_bytes(decode_part(key["n"]), "big")).public_key()
        public.verify(decode_part(signature_part), (header_part + "." + claims_part).encode("ascii"),
                      padding.PKCS1v15(), hashes.SHA256())
        now = time.time() if now is None else now
        audience = claims.get("aud")
        if isinstance(audience, str):
            audience = [audience]
        if not isinstance(audience, list) or client_id not in audience:
            raise ValueError("Wrong audience")
        if (len(audience) > 1 or "azp" in claims) and claims.get("azp") != client_id:
            raise ValueError("Wrong authorized party")
        for field in ("exp", "iat"):
            if type(claims.get(field)) not in (int, float) or not math.isfinite(claims[field]):
                raise ValueError("Invalid token time")
        if claims["exp"] <= now or claims["iat"] > now + 60:
            raise ValueError("Expired or future token")
        if "nbf" in claims and (type(claims["nbf"]) not in (int, float)
                                or not math.isfinite(claims["nbf"]) or claims["nbf"] > now + 60):
            raise ValueError("Token not yet valid")
        if claims.get("iss") != ISSUER or not isinstance(claims.get("sub"), str) or not claims["sub"]:
            raise ValueError("Wrong identity")
        if not isinstance(claims.get("nonce"), str) or not secrets.compare_digest(claims["nonce"].encode(), nonce.encode()):
            raise ValueError("Wrong nonce")
        if previous_subject and claims["sub"] != previous_subject:
            raise ValueError("Account changed")
        return claims
    except Exception as error:
        # Never copy a token, authorization URL, or token response into a UI error.
        raise AuthError("chatgpt_identity_invalid") from error


def token_credentials(value, previous):
    result = dict(previous)
    for key in ("access_token", "refresh_token"):
        if not isinstance(value.get(key), str) or not value[key]:
            raise AuthError("auth_exchange_failed")
        result[key] = value[key]
    expires = value.get("expires_in")
    if type(expires) not in (int, float) or not math.isfinite(expires) or not 0 < expires <= 86400:
        raise AuthError("auth_exchange_failed")
    if str(value.get("token_type", "Bearer")).lower() != "bearer":
        raise AuthError("auth_exchange_failed")
    result["expires_at"] = str(time.time() + expires)
    if isinstance(value.get("scope"), str):
        result["scope"] = value["scope"]
    result.setdefault("scope", "")
    earliest = value.get("earliest_refresh_at")
    result["earliest_refresh_at"] = str(earliest) if isinstance(earliest, (str, int, float)) else ""
    return result


def needs_refresh(auth):
    try:
        expires = float(auth["expires_at"])
        return not auth.get("access_token") or not math.isfinite(expires) or expires <= time.time() + 60
    except (KeyError, ValueError):
        return True


def without_tokens(auth):
    return {key: value for key, value in auth.items()
            if key not in ("access_token", "refresh_token", "id_token", "expires_at", "earliest_refresh_at", "scope")}


def refresh_credentials(auth):
    if not auth.get("client_id") or not auth.get("refresh_token"):
        raise AuthError("chatgpt_auth_required")
    value = token_request({"grant_type": "refresh_token", "client_id": auth["client_id"],
                           "refresh_token": auth["refresh_token"], "resource": RESOURCE})
    # Keep the identity verified at sign-in, including the retained ID-token hint.
    return token_credentials(value, auth)


class ChatGPTLogin(OAuthLogin):
    def __init__(self, tr, host, previous=None, **kwargs):
        kwargs.setdefault("port", 0)
        super().__init__(tr, **kwargs)
        self.previous = dict(previous or {})
        self.host = host
        self.client_id = self.previous.get("client_id")
        self.nonce = secrets.token_urlsafe(32)
        self.exchange = self.exchange_code

    @property
    def registration(self):
        return {"client_id": self.client_id, "ext_agent_host_id": self.host} if self.client_id else {}

    def authorization_url(self):
        challenge = base64.urlsafe_b64encode(hashlib.sha256(self.verifier.encode()).digest()).rstrip(b"=").decode()
        params = {"response_type": "code", "client_id": self.client_id or "dynamic_agent_client",
                  "ext_agent_host_id": self.host, "redirect_uri": self.redirect_uri, "scope": SCOPES,
                  "resource": RESOURCE, "state": self.state, "nonce": self.nonce,
                  "code_challenge": challenge, "code_challenge_method": "S256"}
        if self.client_id:
            if self.previous.get("id_token"):
                params["id_token_hint"] = self.previous["id_token"]
            if self.previous.get("email"):
                params["login_hint"] = self.previous["email"]
            if self.previous.get("access_token") and not plan_enabled(self.previous):
                params["prompt"] = "consent"
        else:
            params["agent_name_hint"] = "Pythona AI Setup"
        return AUTHORIZE_URL + "?" + urlencode(params)

    def accept_callback(self, query):
        returned = query.get("client_id", [])
        if returned:
            if len(returned) != 1 or not re.fullmatch(r"oaiapp_[A-Za-z0-9_-]{1,200}", returned[0]):
                raise AuthError("auth_invalid_callback")
            if self.client_id and returned[0] != self.client_id:
                raise AuthError("auth_invalid_callback")
            self.client_id = returned[0]
        if not self.client_id:
            raise AuthError("auth_invalid_callback")

    def exchange_code(self, code, verifier, redirect_uri):
        value = token_request({"grant_type": "authorization_code", "client_id": self.client_id,
                               "code": code, "code_verifier": verifier, "redirect_uri": redirect_uri,
                               "resource": RESOURCE})
        claims = verify_identity(value.get("id_token"), self.client_id, self.nonce, self.previous.get("subject"))
        result = token_credentials(value, {**self.previous, **self.registration, "scope": ""})
        result.update(issuer=ISSUER, subject=claims["sub"], id_token=value["id_token"],
                      email=claims.get("email", "") if isinstance(claims.get("email", ""), str) else "",
                      local_grant_id=secrets.token_hex(16))
        return result

    def success_message(self, value):
        return self.tr("auth_success" if plan_enabled(value) else "chatgpt_plan_required")
