"""ChatGPT plan OAuth and protected credentials, shared by all delivery paths.

Protocol follows OpenAI's sign-in-with-chatgpt-devkit and pi-ai openai provider.
Only registration metadata is stored in SQLite; all grants remain in SecretStore.
"""
import base64
import hashlib
import json
import math
import re
import secrets as random
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlsplit
from uuid import uuid4

import httpx
import jwt


ISSUER = "https://auth.openai.com"
RESOURCE = "https://api.openai.com/v1"
SCOPES = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"
PROFILES = "v2_subscription_profiles"
PROFILE = "chatgpt"
PROFILE_REFERENCE = "subscription-chatgpt-profile"


class SubscriptionError(ValueError):
    def __init__(self, code, status=502, *, uncertain=False):
        self.code, self.status, self.uncertain = code, status, uncertain
        super().__init__(code)


class ChatGPTSubscriptions:
    def __init__(self, records, secret_store, *, client=None):
        self.records, self.secrets = records, secret_store
        self.client = client or httpx.Client(timeout=15, follow_redirects=False, trust_env=False)
        self._owns_client = client is None
        self._lock = threading.RLock()
        self._attempts = {}
        self._discovery = None
        self._closed = False

    def _ensure_open(self):
        if self._closed:
            raise SubscriptionError("subscription_closed", 409)

    def _json(self, method, url, **kwargs):
        self._ensure_open()
        response = None
        try:
            response = self.client.request(method, url, timeout=15, follow_redirects=False, **kwargs)
            if len(response.content) > 1_000_000:
                raise SubscriptionError("subscription_invalid_response", uncertain=method == "POST" and response.status_code == 200)
            if response.status_code != 200:
                if response.status_code in (400, 401, 403):
                    value = response.json()
                    error = value.get("error") if isinstance(value, dict) else None
                    code = error.get("code") if isinstance(error, dict) else error
                    if code in ("invalid_grant", "invalid_refresh_token", "token_expired",
                            "refresh_token_expired", "refresh_token_invalidated", "refresh_token_reused"):
                        raise SubscriptionError("subscription_reauthentication_required", 401)
                    if code == "invalid_client":
                        raise SubscriptionError("subscription_client_invalid", 400)
                raise SubscriptionError("subscription_request_failed")
            return response.json()
        except SubscriptionError:
            raise
        except Exception:
            raise SubscriptionError("subscription_request_failed",
                uncertain=method == "POST" and (response is None or response.status_code == 200)) from None

    def discovery(self):
        with self._lock:
            self._ensure_open()
            if self._discovery is None:
                value = self._json("GET", ISSUER + "/.well-known/openid-configuration")
                if not isinstance(value, dict) or value.get("issuer") != ISSUER:
                    raise SubscriptionError("subscription_discovery_invalid")
                for field in ("authorization_endpoint", "token_endpoint", "jwks_uri", "revocation_endpoint"):
                    endpoint = value.get(field)
                    if field == "revocation_endpoint" and endpoint is None:
                        continue
                    parsed = urlsplit(endpoint) if isinstance(endpoint, str) else None
                    if not parsed or parsed.scheme != "https" or parsed.netloc != "auth.openai.com" or parsed.fragment:
                        raise SubscriptionError("subscription_discovery_invalid")
                self._discovery = value
            return self._discovery

    def host_id(self):
        with self.records.begin() as tx:
            row = tx.read("v2_subscription_installation", "default")
            if row:
                return row.payload["host_id"]
            host = "urn:uuid:" + str(uuid4())
            tx.put("v2_subscription_installation", "default", {"host_id": host}, expected_revision=0)
            tx.commit()
            return host

    def _read_profile(self):
        raw = self.secrets.get_snapshot(PROFILE_REFERENCE).value
        try:
            return json.loads(raw) if raw else {}
        except ValueError:
            raise SubscriptionError("subscription_credentials_unavailable", 401) from None

    def _write_profile(self, value, *, expected_revision=None, state=None):
        expected = self._revision() if expected_revision is None else expected_revision
        with self.records.begin() as tx:
            row = tx.read(PROFILES, PROFILE)
            if (row.revision if row else 0) != expected:
                raise SubscriptionError("subscription_revision_conflict", 409)
            self.secrets.set(PROFILE_REFERENCE, json.dumps(value, ensure_ascii=False))
            if state is not None:
                tx.put(PROFILES, PROFILE, {"provider": PROFILE, "state": state, "secret_ref": PROFILE_REFERENCE}, expected_revision=expected)
            tx.commit()

    def _revision(self):
        row = self.records.read(PROFILES, PROFILE)
        return row.revision if row else 0

    def _check_revision(self, expected):
        if type(expected) is not int or expected < 0:
            raise SubscriptionError("invalid_subscription_revision", 400)
        if self._revision() != expected:
            raise SubscriptionError("subscription_revision_conflict", 409)

    def _metadata(self, state, expected):
        with self.records.begin() as tx:
            row = tx.read(PROFILES, PROFILE)
            if (row.revision if row else 0) != expected:
                raise SubscriptionError("subscription_revision_conflict", 409)
            tx.put(PROFILES, PROFILE, {"provider": PROFILE, "state": state, "secret_ref": PROFILE_REFERENCE}, expected_revision=expected)
            tx.commit()

    def status(self):
        with self._lock:
            row = self.records.read(PROFILES, PROFILE)
            profile = self._read_profile()
            connected = bool(row and row.payload.get("state") == "connected" and profile.get("verified"))
            return {"provider": PROFILE, "connected": connected, "sharing": connected and "chatgpt.tokens.use.direct" in profile.get("scopes", []),
                "identity": {"email": profile.get("email", ""), "name": profile.get("name", "")} if connected else None,
                "revision": row.revision if row else 0, "state": row.payload["state"] if row else "disconnected",
                "login": next(({"attempt_id": key, "expires_at": value["expires_at"]} for key, value in self._attempts.items()
                               if value["state"] == "pending"), None)}

    def _verify(self, token, client_id, *, nonce=None, received_at=None):
        config = self.discovery()
        keys = self._json("GET", config["jwks_uri"])
        try:
            header = jwt.get_unverified_header(token)
            jwk = next(key for key in keys["keys"] if key.get("kid") == header.get("kid") and key.get("kty") == "RSA")
            claims = jwt.decode(token, jwt.algorithms.RSAAlgorithm.from_jwk(json.dumps(jwk)), algorithms=["RS256"],
                issuer=ISSUER, audience=client_id, leeway=5,
                options={"require": ["iss", "aud", "sub", "iat", "exp"], "verify_exp": False, "verify_iat": False})
            now = received_at if received_at is not None else time.time()
            if (not isinstance(claims["sub"], str) or not claims["sub"]
                    or type(claims["exp"]) not in (int, float) or type(claims["iat"]) not in (int, float)
                    or not math.isfinite(claims["exp"]) or not math.isfinite(claims["iat"])
                    or claims["exp"] < now - 5 or claims["iat"] > now + 5
                    or nonce is not None and claims.get("nonce") != nonce
                    or claims.get("azp", client_id) != client_id
                    or isinstance(claims["aud"], list) and len(claims["aud"]) > 1 and claims.get("azp") != client_id):
                raise ValueError()
            return {key: claims[key] for key in ("sub", "email", "name") if isinstance(claims.get(key), str)}
        except Exception:
            raise SubscriptionError("subscription_identity_invalid", 401) from None

    def _token_fields(self, value, *, previous_scopes=None):
        if not isinstance(value, dict):
            raise SubscriptionError("subscription_token_invalid", 401)
        scopes = value.get("scope", previous_scopes)
        expiry = value.get("expires_in")
        if (not isinstance(scopes, str) or not isinstance(value.get("access_token"), str) or not value["access_token"]
                or not isinstance(value.get("token_type"), str) or value["token_type"].lower() != "bearer" or type(expiry) not in (int, float)
                or not math.isfinite(expiry) or expiry <= 0 or not isinstance(value.get("refresh_token"), str) or not value["refresh_token"]):
            raise SubscriptionError("subscription_token_invalid", 401)
        return {"access_token": value["access_token"], "refresh_token": value["refresh_token"],
            "expires_at": time.time() + expiry, "scopes": scopes.split()}

    def login(self, *, expected_revision):
        with self._lock:
            self._ensure_open()
            self._check_revision(expected_revision)
            if any(value["state"] == "pending" for value in self._attempts.values()):
                raise SubscriptionError("subscription_login_in_progress", 409)
            if len(self._attempts) >= 64:
                self._attempts.pop(next(iter(self._attempts)))
            config = self.discovery()
            state, nonce, verifier = (random.token_urlsafe(32) for _ in range(3))
            attempt_id = uuid4().hex
            entry = {"state": "pending", "expires_at": time.time() + 600, "revision": expected_revision}
            previous = self._read_profile()
            service = self
            accepted = threading.Lock()
            class Callback(BaseHTTPRequestHandler):
                def log_message(self, *args):
                    return  # Authorization codes must never enter request logs.

                def do_GET(self):
                    self.connection.settimeout(10)
                    parsed = urlsplit(self.path)
                    params = parse_qs(parsed.query, keep_blank_values=True)
                    valid = (self.headers.get("Host") == f"127.0.0.1:{self.server.server_port}"
                        and parsed.path == "/auth/callback" and len(self.path) <= 16000
                        and params.get("state") == [state] and entry["state"] == "pending"
                        and time.time() < entry["expires_at"])
                    if not valid or not accepted.acquire(blocking=False):
                        self.send_response(400); self.end_headers(); return
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Referrer-Policy", "no-referrer")
                    page_nonce = random.token_urlsafe(20)
                    self.send_header("Content-Security-Policy", f"default-src 'none'; script-src 'nonce-{page_nonce}'; frame-ancestors 'none'; base-uri 'none'")
                    self.end_headers()
                    self.wfile.write(("<!doctype html><meta charset=utf-8><title>ChatGPT</title><p>请返回应用查看登录结果。</p>"
                        + f"<script nonce='{page_nonce}'>history.replaceState(null,'','/auth/complete')</script>").encode())
                    threading.Thread(target=service._finish_login, args=(entry, params, previous, nonce, verifier, redirect), daemon=True).start()

            try:
                server = ThreadingHTTPServer(("127.0.0.1", 0), Callback)
            except OSError:
                # Never fall back to a redirect that another process could receive.
                raise SubscriptionError("subscription_callback_unavailable", 503) from None
            server.daemon_threads = True
            redirect = f"http://127.0.0.1:{server.server_port}/auth/callback"
            entry["server"] = server
            self._attempts[attempt_id] = entry
            threading.Thread(target=server.serve_forever, daemon=True).start()
            def expired():
                if entry["state"] == "pending":
                    entry.update(state="failed", error="subscription_login_expired")
                self._stop(entry)
            timer = threading.Timer(600, expired)
            timer.daemon = True
            entry["timer"] = timer
            timer.start()
            # PKCE is a protocol challenge, not a file/content fingerprint.
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
            params = {"client_id": previous.get("client_id", "dynamic_agent_client"), "response_type": "code",
                "redirect_uri": redirect, "scope": SCOPES, "resource": RESOURCE, "state": state, "nonce": nonce,
                "code_challenge_method": "S256", "code_challenge": challenge, "ext_agent_host_id": self.host_id()}
            if not previous.get("client_id"):
                params["agent_name_hint"] = "ChriptmasAgent"
            return {"attempt_id": attempt_id, "authorization_url": config["authorization_endpoint"] + "?" + urlencode(params), "expires_at": entry["expires_at"]}

    def _finish_login(self, entry, params, previous, nonce, verifier, redirect):
        try:
            client_id = params.get("client_id", [previous.get("client_id")])
            code = params.get("code", [])
            if (params.get("error") or len(code) != 1 or not code[0] or len(client_id) != 1
                    or not isinstance(client_id[0], str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", client_id[0])
                    or client_id[0] == "dynamic_agent_client" or previous.get("client_id") not in (None, client_id[0])):
                raise SubscriptionError("subscription_registration_invalid", 401)
            with self._lock:
                if entry["state"] != "pending":
                    raise SubscriptionError("subscription_login_cancelled", 409)
                self._check_revision(entry["revision"])
                # Preserve dynamically issued registration even if the code fails.
                self._write_profile({**previous, "client_id": client_id[0]}, expected_revision=entry["revision"])
            value = self._json("POST", self.discovery()["token_endpoint"], data={"grant_type": "authorization_code",
                "client_id": client_id[0], "code": code[0], "code_verifier": verifier, "redirect_uri": redirect, "resource": RESOURCE})
            fields = self._token_fields(value)
            identity = self._verify(value.get("id_token"), client_id[0], nonce=nonce)
            if previous.get("sub") not in (None, identity["sub"]):
                raise SubscriptionError("subscription_account_mismatch", 401)
            with self._lock:
                if entry["state"] != "pending":
                    raise SubscriptionError("subscription_login_cancelled", 409)
                self._check_revision(entry["revision"])
                self._write_profile({**fields, **identity, "client_id": client_id[0], "verified": True},
                    expected_revision=entry["revision"], state="connected")
                entry["state"] = "completed"
        except SubscriptionError as error:
            entry.update(state="failed", error=error.code)
        except Exception:
            entry.update(state="failed", error="subscription_login_failed")
        finally:
            self._stop(entry)

    def attempt(self, attempt_id):
        value = self._attempts.get(attempt_id)
        if not value:
            raise SubscriptionError("subscription_login_not_found", 404)
        return {key: value[key] for key in ("state", "error", "expires_at") if key in value}

    def _stop(self, entry):
        timer, server = entry.pop("timer", None), entry.pop("server", None)
        if timer:
            timer.cancel()
        if server:
            server.shutdown(); server.server_close()

    def cancel(self, attempt_id):
        with self._lock:
            entry = self._attempts.get(attempt_id)
            if not entry:
                raise SubscriptionError("subscription_login_not_found", 404)
            if entry["state"] == "pending":
                entry.update(state="failed", error="subscription_login_cancelled")
        self._stop(entry)
        return self.attempt(attempt_id)

    def _acquire_refresh(self):
        owner = uuid4().hex
        deadline = time.monotonic() + 20
        while True:
            with self.records.begin() as tx:
                lease = tx.read("v2_subscription_refresh", PROFILE)
                if not lease or not lease.payload.get("owner"):
                    tx.put("v2_subscription_refresh", PROFILE, {"owner": owner, "expires_at": time.time() + 60},
                        expected_revision=lease.revision if lease else 0)
                    tx.commit()
                    return owner
                if lease.payload["expires_at"] < time.time():
                    if not self._read_profile().get("pending_rotation"):
                        raise SubscriptionError("subscription_reauthentication_required", 401)
                    tx.put("v2_subscription_refresh", PROFILE, {"owner": owner, "expires_at": time.time() + 60}, expected_revision=lease.revision)
                    tx.commit()
                    return owner
            if time.monotonic() > deadline:
                raise SubscriptionError("subscription_refresh_in_progress", 409)
            time.sleep(.05)

    def _release_refresh(self, owner):
        with self.records.begin() as tx:
            lease = tx.read("v2_subscription_refresh", PROFILE)
            if lease and lease.payload.get("owner") == owner:
                tx.put("v2_subscription_refresh", PROFILE, {"owner": None}, expected_revision=lease.revision)
                tx.commit()

    def token(self):
        with self._lock:
            self._ensure_open()
            if not self.status()["sharing"]:
                raise SubscriptionError("subscription_permission_required", 401)
            profile = self._read_profile()
            if not profile.get("pending_rotation") and profile.get("expires_at", 0) > time.time() + 120:
                return profile["access_token"]
            revision = self._revision()
            try:
                owner = self._acquire_refresh()
            except SubscriptionError as error:
                if error.status == 401 and self._revision() == revision:
                    registration = {"client_id": profile["client_id"]} if profile.get("client_id") else {}
                    self._write_profile(registration, expected_revision=revision, state="reauthentication_required")
                raise
            try:
                if not self.status()["sharing"]:
                    raise SubscriptionError("subscription_permission_required", 401)
                profile = self._read_profile()
                if not profile.get("pending_rotation") and profile.get("expires_at", 0) > time.time() + 120:
                    return profile["access_token"]
                rotation = profile.get("pending_rotation")
                if not rotation:
                    try:
                        value = self._json("POST", self.discovery()["token_endpoint"], data={"grant_type": "refresh_token",
                            "client_id": profile["client_id"], "refresh_token": profile["refresh_token"], "resource": RESOURCE})
                    except SubscriptionError as error:
                        # A lost reply may already have consumed this grant.
                        # Reauthentication is safer than repeating the POST.
                        if error.uncertain:
                            raise SubscriptionError("subscription_refresh_uncertain", 401) from None
                        raise
                    rotation = {**self._token_fields(value, previous_scopes=" ".join(profile["scopes"])),
                        "id_token": value.get("id_token"), "received_at": time.time()}
                    self._write_profile({**profile, "pending_rotation": rotation}, expected_revision=revision)
                identity = self._verify(rotation["id_token"], profile["client_id"], received_at=rotation["received_at"])
                if identity["sub"] != profile["sub"]:
                    raise SubscriptionError("subscription_account_mismatch", 401)
                if "chatgpt.tokens.use.direct" not in rotation["scopes"]:
                    raise SubscriptionError("subscription_permission_required", 401)
                self._check_revision(revision)
                updated = {**profile, **{k: rotation[k] for k in ("access_token", "refresh_token", "expires_at", "scopes")}, **identity}
                updated.pop("pending_rotation", None)
                self._write_profile(updated, expected_revision=revision)
                return updated["access_token"]
            except SubscriptionError as error:
                if error.status == 401 and self._revision() == revision:
                    registration = {"client_id": profile["client_id"]} if profile.get("client_id") else {}
                    self._write_profile(registration, expected_revision=revision, state="reauthentication_required")
                raise
            finally:
                self._release_refresh(owner)

    def models(self):
        value = self._json("GET", RESOURCE + "/models", headers={"Authorization": "Bearer " + self.token()})
        if not isinstance(value, dict) or not isinstance(value.get("models"), list):
            raise SubscriptionError("subscription_models_invalid")
        result = []
        for row in value["models"]:
            if not isinstance(row, dict) or row.get("visibility") != "list":
                continue
            if any(not isinstance(row.get(key), str) or not row[key].strip() or len(row[key]) > 200
                   for key in ("slug", "display_name")):
                raise SubscriptionError("subscription_models_invalid")
            result.append({"id": row["slug"], "name": row["display_name"]})
        return result

    def logout(self, *, expected_revision):
        with self._lock:
            self._check_revision(expected_revision)
            profile = self._read_profile()
            with self.records.begin() as tx:
                row = tx.read(PROFILES, PROFILE)
                if (row.revision if row else 0) != expected_revision:
                    raise SubscriptionError("subscription_revision_conflict", 409)
                registration = {"client_id": profile["client_id"]} if profile.get("client_id") else {}
                if registration:
                    self.secrets.set(PROFILE_REFERENCE, json.dumps(registration))
                else:
                    self.secrets.delete(PROFILE_REFERENCE)
                tx.put(PROFILES, PROFILE, {"provider": PROFILE, "state": "disconnected", "secret_ref": PROFILE_REFERENCE}, expected_revision=expected_revision)
                tx.commit()
            for entry in self._attempts.values():
                if entry["state"] == "pending":
                    entry.update(state="failed", error="subscription_login_cancelled")
                    self._stop(entry)
        token = profile.get("pending_rotation", profile).get("refresh_token")
        if token:
            try:
                endpoint = self.discovery().get("revocation_endpoint")
                if not endpoint:
                    raise SubscriptionError("subscription_revocation_unconfirmed")
                self._json("POST", endpoint, data={"token": token, "token_type_hint": "refresh_token", "client_id": profile["client_id"]})
            except SubscriptionError:
                return {"remote_revoked": False}
        return {"remote_revoked": True}

    def close(self):
        # Started refreshes hold this lock until the rotated token and checkpoint
        # are saved. Caller cancellation or shutdown must not close their client.
        with self._lock:
            if self._closed:
                return
            self._closed = True
            for entry in self._attempts.values():
                if entry["state"] == "pending":
                    entry.update(state="failed", error="subscription_login_interrupted")
                self._stop(entry)
            if self._owns_client:
                self.client.close()
