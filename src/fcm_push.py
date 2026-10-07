"""Native Firebase Cloud Messaging push for the Kittyhack Android app.

07.10, Sid: Web Push via the PWA/Chrome was confirmed (real diagnostic, not
a guess) to silently drop most overnight notifications - Android kills
Chrome's background process after extended idle, something her real native
apps (Reolink, Sureflap) never suffer from because they talk to FCM
directly. This module is the server-side half of fixing that properly: the
Capacitor Android app (android-app/) registers a native FCM token via
@capacitor/push-notifications, and THIS sends to that token through FCM's
HTTP v1 API - not pywebpush/webpush.py, which stays in place for browser/PC
use (see "keep a browser access point" requirement).

Mirrors webpush.py's posture: local, gitignored JSON files for the
service-account key and registered tokens, generated/stored on first use,
never committed.

NOT YET WIRED IN: requires a real Firebase project + service account key
from Sid before send_fcm_notification_to_all() can do anything (see the
docstring on _get_access_token for exactly what's needed). Until then,
every call here is a safe, logged no-op - this must never be allowed to
break the existing webpush.py path, same as webpush.py's own "never let
tracking break real playback" posture elsewhere in this codebase.
"""

import base64
import json
import logging
import os
import threading
import time

import aiohttp
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from src.paths import kittyhack_root
from src.webauth import SESSION_COOKIE, _parse_cookies, _read_body, _session_username
from src.webpush import _send_json

# 07.10, Sid: downloaded from Firebase Console > Project Settings > Service
# Accounts > "Generate new private key" - a JSON file, NOT google-services.json
# (that one goes in android-app/android/app/, this one is server-side only).
SERVICE_ACCOUNT_FILE = os.path.join(kittyhack_root(), "fcm_service_account.json")
TOKENS_FILE = os.path.join(kittyhack_root(), "fcm_tokens.json")

_lock = threading.Lock()
_token_cache: dict = {"access_token": None, "expires_at": 0.0}


def _atomic_write_json(path: str, data) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except Exception:
        pass


def is_configured() -> bool:
    """False until Sid drops a real service-account key in place - callers
    should skip FCM entirely (not error) when this is False."""
    return os.path.exists(SERVICE_ACCOUNT_FILE)


def _load_tokens() -> list[dict]:
    if not os.path.exists(TOKENS_FILE):
        return []
    try:
        with open(TOKENS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logging.warning(f"[FCM] Failed to read {TOKENS_FILE}: {e}")
        return []


def _save_tokens(tokens: list[dict]) -> None:
    _atomic_write_json(TOKENS_FILE, tokens)


def register_fcm_token(token: str, username: str = "") -> None:
    """Store a new device's FCM registration token (dedup by token value).
    Call this from the same kind of /fcm/register endpoint webpush.py's
    /webpush/subscribe already models - the Capacitor app POSTs its token
    here after @capacitor/push-notifications hands it one."""
    if not token:
        return
    with _lock:
        tokens = _load_tokens()
        tokens = [t for t in tokens if t.get("token") != token]
        tokens.append({"token": token, "username": username})
        _save_tokens(tokens)
    logging.info(f"[FCM] Stored device token for user '{username}' ({len(tokens)} total).")


def remove_fcm_token(token: str) -> None:
    with _lock:
        tokens = _load_tokens()
        new_tokens = [t for t in tokens if t.get("token") != token]
        if len(new_tokens) != len(tokens):
            _save_tokens(new_tokens)


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


async def _get_access_token() -> str | None:
    """Exchanges the service-account key for a short-lived OAuth2 access
    token (self-signed JWT grant, RFC 7523) - no google-auth dependency,
    same "small and self-contained" posture as webpush.py's py_vapid use.
    Cached until ~5 min before real expiry (tokens are valid 1h)."""
    now = time.time()
    if _token_cache["access_token"] and now < _token_cache["expires_at"] - 300:
        return _token_cache["access_token"]

    if not is_configured():
        return None

    try:
        with open(SERVICE_ACCOUNT_FILE, "r", encoding="utf-8") as f:
            creds = json.load(f)
        private_key = serialization.load_pem_private_key(
            creds["private_key"].encode("utf-8"), password=None
        )

        header = _b64url(json.dumps({"alg": "RS256", "typ": "JWT"}).encode("utf-8"))
        claims = {
            "iss": creds["client_email"],
            "scope": "https://www.googleapis.com/auth/firebase.messaging",
            "aud": "https://oauth2.googleapis.com/token",
            "iat": int(now),
            "exp": int(now) + 3600,
        }
        payload = _b64url(json.dumps(claims).encode("utf-8"))
        signing_input = f"{header}.{payload}".encode("ascii")
        signature = private_key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
        assertion = f"{header}.{payload}.{_b64url(signature)}"

        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://oauth2.googleapis.com/token",
                data={
                    "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                    "assertion": assertion,
                },
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logging.warning(f"[FCM] Token exchange failed ({resp.status}): {body}")
                    return None
                data = await resp.json()

        _token_cache["access_token"] = data["access_token"]
        _token_cache["expires_at"] = now + int(data.get("expires_in", 3600))
        return _token_cache["access_token"]
    except Exception as e:
        logging.warning(f"[FCM] Failed to obtain access token: {e}")
        return None


async def send_fcm_notification_to_all(
    title: str, body: str, url: str = "/", image: str | None = None
) -> None:
    """Push `title`/`body` to every registered Android device via FCM HTTP
    v1. Mirrors webpush.send_notification_to_all()'s shape so loop.py can
    call both side by side. Prunes tokens FCM reports as UNREGISTERED,
    same "dead endpoint" cleanup webpush.py does for 404/410."""
    if not is_configured():
        return  # Not set up yet - silent no-op, see module docstring.

    with _lock:
        tokens = _load_tokens()
    if not tokens:
        return

    access_token = await _get_access_token()
    if not access_token:
        return

    with open(SERVICE_ACCOUNT_FILE, "r", encoding="utf-8") as f:
        project_id = json.load(f)["project_id"]
    endpoint = f"https://fcm.googleapis.com/v1/projects/{project_id}/messages:send"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json; UTF-8",
    }

    dead_tokens = []
    async with aiohttp.ClientSession() as session:
        for entry in tokens:
            device_token = entry.get("token")
            if not device_token:
                continue
            message = {
                "message": {
                    "token": device_token,
                    "notification": {"title": title, "body": body},
                    "data": {"url": url},
                    "android": {"priority": "high"},
                }
            }
            if image:
                message["message"]["notification"]["image"] = image
            try:
                async with session.post(
                    endpoint, headers=headers, json=message,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status == 404:
                        dead_tokens.append(device_token)
                    elif resp.status != 200:
                        body_text = await resp.text()
                        if "UNREGISTERED" in body_text:
                            dead_tokens.append(device_token)
                        else:
                            logging.warning(f"[FCM] Push failed ({resp.status}): {body_text}")
            except Exception as e:
                logging.warning(f"[FCM] Unexpected push error: {e}")

    if dead_tokens:
        with _lock:
            tokens = _load_tokens()
            tokens = [t for t in tokens if t.get("token") not in dead_tokens]
            _save_tokens(tokens)
        logging.info(f"[FCM] Pruned {len(dead_tokens)} unregistered token(s).")


class FCMMiddleware:
    """ASGI middleware: /fcm/* registration endpoints for the native Android
    app. Same shape as webpush.py's WebPushMiddleware - wire it in next to
    that one in app.py (same WebAuthMiddleware-protected layer, before
    TabRoutingMiddleware/shiny_app)."""

    def __init__(self, asgi_app):
        self.app = asgi_app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "/")

        if path == "/fcm/register" and scope["method"] == "POST":
            cookies = _parse_cookies(scope)
            username = _session_username(cookies.get(SESSION_COOKIE)) or ""
            try:
                body = await _read_body(receive)
                data = json.loads(body.decode("utf-8"))
                register_fcm_token(data.get("token", ""), username)
                await _send_json(send, 200, {"ok": True})
            except Exception as e:
                logging.warning(f"[FCM] Bad register request: {e}")
                await _send_json(send, 400, {"ok": False})
            return

        if path == "/fcm/unregister" and scope["method"] == "POST":
            try:
                body = await _read_body(receive)
                data = json.loads(body.decode("utf-8"))
                remove_fcm_token(data.get("token", ""))
                await _send_json(send, 200, {"ok": True})
            except Exception as e:
                logging.warning(f"[FCM] Bad unregister request: {e}")
                await _send_json(send, 400, {"ok": False})
            return

        await self.app(scope, receive, send)
