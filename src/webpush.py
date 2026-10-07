"""Native Web Push notifications for cat entry/exit events.

Independent of the Home Assistant/MQTT path — this sends a real browser/OS
push notification straight from Kittyhack itself, so it works even without
Home Assistant. VAPID keys and subscriptions are local, gitignored JSON
files (same posture as users_auth.json/watchdog secrets): generated/stored
on first use, never committed.
"""

import base64
import json
import logging
import os
import secrets
import threading
import time

from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from py_vapid import Vapid
from pywebpush import webpush, WebPushException

from src.paths import kittyhack_root
from src.webauth import SESSION_COOKIE, _parse_cookies, _read_body, _session_username

VAPID_PRIVATE_KEY_FILE = os.path.join(kittyhack_root(), "vapid_private_key.pem")
SUBSCRIPTIONS_FILE = os.path.join(kittyhack_root(), "webpush_subscriptions.json")
VAPID_CLAIM_SUB = "mailto:admin@sidounette.ch"

_lock = threading.Lock()


def _atomic_write_json(path: str, data) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except Exception:
        pass


# Vapid.from_file() generates+saves a fresh PEM key on first call if the file
# doesn't exist yet, and loads it straight from that same PEM file every time
# after. Pass the FILE PATH (not the PEM content as a string) to webpush()'s
# vapid_private_key= — passing PEM text directly gets misparsed as a raw/DER
# base64url key by py_vapid's from_string(), which corrupts it.
_vapid = Vapid.from_file(VAPID_PRIVATE_KEY_FILE)
try:
    os.chmod(VAPID_PRIVATE_KEY_FILE, 0o600)
except Exception:
    pass

_vapid_public_b64url = base64.urlsafe_b64encode(
    _vapid.public_key.public_bytes(encoding=Encoding.X962, format=PublicFormat.UncompressedPoint)
).decode("utf-8").rstrip("=")


def get_vapid_public_key() -> str:
    """The urlsafe-base64 applicationServerKey for pushManager.subscribe()."""
    return _vapid_public_b64url


def _load_subscriptions() -> list[dict]:
    if not os.path.exists(SUBSCRIPTIONS_FILE):
        return []
    try:
        with open(SUBSCRIPTIONS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logging.warning(f"[WEBPUSH] Failed to read {SUBSCRIPTIONS_FILE}: {e}")
        return []


def _save_subscriptions(subs: list[dict]) -> None:
    _atomic_write_json(SUBSCRIPTIONS_FILE, subs)


def add_subscription(subscription_info: dict, username: str = "") -> None:
    """Store a new browser subscription (dedup by endpoint)."""
    endpoint = subscription_info.get("endpoint")
    if not endpoint:
        return
    with _lock:
        subs = _load_subscriptions()
        subs = [s for s in subs if s.get("subscription", {}).get("endpoint") != endpoint]
        subs.append({"subscription": subscription_info, "username": username})
        _save_subscriptions(subs)
    logging.info(f"[WEBPUSH] Stored subscription for user '{username}' ({len(subs)} total).")


def remove_subscription(endpoint: str) -> None:
    with _lock:
        subs = _load_subscriptions()
        new_subs = [s for s in subs if s.get("subscription", {}).get("endpoint") != endpoint]
        if len(new_subs) != len(subs):
            _save_subscriptions(new_subs)


def send_notification_to_all(
    title: str, body: str, url: str = "/", tag: str = "kittyhack", image: str | None = None
) -> None:
    """Push `title`/`body` to every stored subscription; prunes dead ones (404/410)."""
    with _lock:
        subs = _load_subscriptions()

    if not subs:
        return

    data = {"title": title, "body": body, "url": url, "tag": tag}
    if image:
        data["image"] = image
    payload = json.dumps(data)
    dead_endpoints = []

    for entry in subs:
        sub_info = entry.get("subscription")
        if not sub_info:
            continue
        try:
            webpush(
                subscription_info=sub_info,
                data=payload,
                vapid_private_key=VAPID_PRIVATE_KEY_FILE,
                vapid_claims={"sub": VAPID_CLAIM_SUB},
                # 07.10, Sid ("qu'une seule notif reçue entre 2h et 6h alors
                # qu'il y en a eu plein"): pywebpush defaults ttl=0, meaning
                # "drop this immediately if the device isn't reachable right
                # now, never retry" - during overnight Doze, her phone's push
                # connection is only intermittently up, so ttl=0 silently
                # discarded almost every notification instead of queuing it
                # for the next time the phone reconnects. A real TTL lets the
                # push service (FCM) hold and deliver it once she's back.
                # Urgency=high additionally asks Android to treat this as
                # important enough to wake the device promptly rather than
                # wait for a Doze maintenance window.
                ttl=12 * 3600,
                headers={"Urgency": "high"},
            )
        except WebPushException as e:
            status = getattr(e.response, "status_code", None)
            if status in (404, 410):
                dead_endpoints.append(sub_info.get("endpoint"))
            else:
                logging.warning(f"[WEBPUSH] Push failed: {e}")
        except Exception as e:
            logging.warning(f"[WEBPUSH] Unexpected push error: {e}")

    if dead_endpoints:
        with _lock:
            subs = _load_subscriptions()
            subs = [
                s for s in subs
                if s.get("subscription", {}).get("endpoint") not in dead_endpoints
            ]
            _save_subscriptions(subs)
        logging.info(f"[WEBPUSH] Pruned {len(dead_endpoints)} expired subscription(s).")


# In-memory, short-lived cache mapping an opaque token to one event photo's
# bytes - lets a push notification's `image` reference a specific captured
# photo (via a public URL the OS/browser can fetch outside the normal
# session-cookie context) without exposing the whole thumbnails archive
# (sequential, guessable IDs covering years of home photos) publicly.
_NOTIF_IMAGE_TTL_S = 3600
_notif_images: dict[str, tuple[bytes, float]] = {}
_notif_images_lock = threading.Lock()


def register_notification_image(image_bytes: bytes) -> str:
    """Store `image_bytes` under a fresh random token; returns that token."""
    token = secrets.token_urlsafe(16)
    now = time.time()
    with _notif_images_lock:
        _notif_images[token] = (image_bytes, now + _NOTIF_IMAGE_TTL_S)
        # Light cleanup on each registration so the dict can't grow unbounded.
        expired = [t for t, (_, exp) in _notif_images.items() if exp < now]
        for t in expired:
            del _notif_images[t]
    return token


def _get_notification_image(token: str) -> bytes | None:
    with _notif_images_lock:
        entry = _notif_images.get(token)
        if not entry:
            return None
        image_bytes, expires_at = entry
        if expires_at < time.time():
            del _notif_images[token]
            return None
        return image_bytes


async def _send_json(send, status: int, payload: dict) -> None:
    body = json.dumps(payload).encode("utf-8")
    await send({
        "type": "http.response.start", "status": status,
        "headers": [(b"content-type", b"application/json")],
    })
    await send({"type": "http.response.body", "body": body})


class WebPushMiddleware:
    """ASGI middleware: /webpush/* subscription endpoints + public /cat-photo/*.

    Placed inside WebAuthMiddleware (so /webpush/* still requires a logged-in
    session) but must run before TabRoutingMiddleware/shiny_app, which have
    no idea what these paths are.
    """

    def __init__(self, asgi_app):
        self.app = asgi_app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "/")

        if path == "/webpush/vapid-public-key" and scope["method"] == "GET":
            await _send_json(send, 200, {"publicKey": get_vapid_public_key()})
            return

        if path == "/webpush/subscribe" and scope["method"] == "POST":
            cookies = _parse_cookies(scope)
            username = _session_username(cookies.get(SESSION_COOKIE)) or ""
            try:
                body = await _read_body(receive)
                subscription_info = json.loads(body.decode("utf-8"))
                add_subscription(subscription_info, username)
                await _send_json(send, 200, {"ok": True})
            except Exception as e:
                logging.warning(f"[WEBPUSH] Bad subscribe request: {e}")
                await _send_json(send, 400, {"ok": False})
            return

        if path == "/webpush/unsubscribe" and scope["method"] == "POST":
            try:
                body = await _read_body(receive)
                data = json.loads(body.decode("utf-8"))
                remove_subscription(data.get("endpoint", ""))
                await _send_json(send, 200, {"ok": True})
            except Exception as e:
                logging.warning(f"[WEBPUSH] Bad unsubscribe request: {e}")
                await _send_json(send, 400, {"ok": False})
            return

        if path.startswith("/cat-photo/") and scope["method"] == "GET":
            from src.database import CatsRepo
            from src.baseconfig import CONFIG

            rfid = path[len("/cat-photo/"):].rsplit(".", 1)[0]
            image_bytes = CatsRepo.db_get_cat_image_by_rfid(CONFIG["KITTYHACK_DATABASE_PATH"], rfid)
            if not image_bytes:
                await send({"type": "http.response.start", "status": 404, "headers": []})
                await send({"type": "http.response.body", "body": b""})
                return
            await send({
                "type": "http.response.start", "status": 200,
                "headers": [(b"content-type", b"image/jpeg"), (b"cache-control", b"no-cache")],
            })
            await send({"type": "http.response.body", "body": image_bytes})
            return

        if path.startswith("/notif-image/") and scope["method"] == "GET":
            token = path[len("/notif-image/"):].rsplit(".", 1)[0]
            image_bytes = _get_notification_image(token)
            if not image_bytes:
                await send({"type": "http.response.start", "status": 404, "headers": []})
                await send({"type": "http.response.body", "body": b""})
                return
            await send({
                "type": "http.response.start", "status": 200,
                "headers": [(b"content-type", b"image/jpeg"), (b"cache-control", b"no-cache")],
            })
            await send({"type": "http.response.body", "body": image_bytes})
            return

        await self.app(scope, receive, send)
