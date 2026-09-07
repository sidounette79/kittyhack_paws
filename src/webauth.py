"""Simple username/password login for the Kittyhack web UI.

Independent of the REST API's own token auth (src/api.py) — this protects
the human-facing Shiny UI itself, since Kittyhack ships with no UI login at
all. Deliberately minimal: PBKDF2 password hashes in a local JSON file,
server-side session tokens in memory, a self-contained login page (no
external assets, so it renders even before authentication).
"""

import hashlib
import hmac
import html
import json
import logging
import os
import secrets
import threading
import time
from datetime import datetime, timezone
from urllib.parse import parse_qs, quote, urlencode

from src.paths import kittyhack_root

USERS_FILE = os.path.join(kittyhack_root(), "users_auth.json")
SESSION_COOKIE = "kittyhack_session"
SESSION_TTL_S = 12 * 3600  # 12 hours

# Same shape as the REST API's own limiter (src/api.py) for consistency.
_AUTH_FAIL_WINDOW_S = 60.0
_AUTH_FAIL_MAX = 10
_auth_fail_log: dict[str, list[float]] = {}
_auth_fail_lock = threading.Lock()

_sessions: dict[str, dict] = {}
_sessions_lock = threading.Lock()

PBKDF2_ITERATIONS = 600_000


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    derived = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt.hex()}${derived.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algo, iterations, salt_hex, hash_hex = encoded.split("$")
        if algo != "pbkdf2_sha256":
            return False
        salt = bytes.fromhex(salt_hex)
        derived = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(iterations))
        return hmac.compare_digest(derived.hex(), hash_hex)
    except Exception:
        return False


def _load_users() -> dict[str, str]:
    if not os.path.exists(USERS_FILE):
        return {}
    try:
        with open(USERS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logging.warning(f"[WEBAUTH] Failed to read {USERS_FILE}: {e}")
        return {}


def _save_users(users: dict[str, str]) -> None:
    tmp = USERS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(users, f, indent=2)
    os.replace(tmp, USERS_FILE)
    try:
        os.chmod(USERS_FILE, 0o600)
    except Exception:
        pass


def ensure_seed_users(seed: dict[str, str]) -> None:
    """Create USERS_FILE with the given {username: plaintext_password} if it
    doesn't exist yet. Never overwrites an existing file (so re-deploys don't
    reset passwords someone already changed)."""
    if os.path.exists(USERS_FILE):
        return
    _save_users({u: hash_password(p) for u, p in seed.items()})
    logging.info(f"[WEBAUTH] Seeded {USERS_FILE} with {len(seed)} user(s).")


def set_password(username: str, password: str) -> None:
    users = _load_users()
    users[username] = hash_password(password)
    _save_users(users)


def _check_credentials(username: str, password: str) -> bool:
    users = _load_users()
    encoded = users.get(username)
    if not encoded:
        # Still run a hash to keep timing similar whether or not the user exists.
        hash_password(password)
        return False
    return verify_password(password, encoded)


def _client_ip(scope) -> str:
    client = scope.get("client")
    return client[0] if client else "?"


def _rate_limited(ip: str) -> bool:
    now = time.monotonic()
    with _auth_fail_lock:
        recent = [t for t in _auth_fail_log.get(ip, []) if now - t < _AUTH_FAIL_WINDOW_S]
        _auth_fail_log[ip] = recent
        return len(recent) >= _AUTH_FAIL_MAX


def _record_fail(ip: str) -> None:
    with _auth_fail_lock:
        _auth_fail_log.setdefault(ip, []).append(time.monotonic())


def _new_session(username: str) -> str:
    token = secrets.token_urlsafe(32)
    with _sessions_lock:
        _sessions[token] = {"username": username, "created_at": time.monotonic()}
    return token


def _session_username(token: str | None) -> str | None:
    if not token:
        return None
    with _sessions_lock:
        session = _sessions.get(token)
        if not session:
            return None
        if time.monotonic() - session["created_at"] > SESSION_TTL_S:
            del _sessions[token]
            return None
        return session["username"]


def _drop_session(token: str | None) -> None:
    if not token:
        return
    with _sessions_lock:
        _sessions.pop(token, None)


def _parse_cookies(scope) -> dict[str, str]:
    for key, value in scope.get("headers", []):
        if key == b"cookie":
            raw = value.decode("latin-1")
            out = {}
            for part in raw.split(";"):
                if "=" in part:
                    k, v = part.strip().split("=", 1)
                    out[k] = v
            return out
    return {}


LOGIN_PAGE = """<!doctype html>
<html lang="fr"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Kittyhack &mdash; Connexion</title>
<style>
  body {{ background:#111418; color:#e7e9ec; font-family:-apple-system,Segoe UI,Roboto,sans-serif;
         display:flex; align-items:center; justify-content:center; height:100vh; margin:0; }}
  form {{ background:#1b1f26; padding:2rem 2.5rem; border-radius:12px; width:280px;
          box-shadow:0 10px 30px rgba(0,0,0,.4); }}
  h1 {{ font-size:1.3rem; margin:0 0 1.2rem; text-align:center; }}
  label {{ font-size:.85rem; color:#a7adb6; display:block; margin:.8rem 0 .3rem; }}
  input {{ width:100%; box-sizing:border-box; padding:.55rem .7rem; border-radius:8px;
           border:1px solid #333a45; background:#0f1216; color:#e7e9ec; font-size:1rem; }}
  .pwd-row {{ position:relative; }}
  .pwd-row input {{ padding-right:2.4rem; }}
  .toggle-eye {{ position:absolute; right:.6rem; top:50%; transform:translateY(-50%);
                 cursor:pointer; user-select:none; opacity:.7; font-size:1.1rem; background:none; border:none; color:#e7e9ec; }}
  button.submit {{ width:100%; margin-top:1.4rem; padding:.6rem; border:none; border-radius:8px;
           background:#3a7bd5; color:white; font-size:1rem; cursor:pointer; }}
  button.submit:hover {{ background:#2f66b3; }}
  .error {{ color:#ff8080; font-size:.85rem; margin-top:.8rem; text-align:center; }}
</style></head>
<body>
<form method="post" action="/login">
  <h1>&#128062; Kittyhack</h1>
  <label for="username">Utilisateur</label>
  <input id="username" name="username" autocomplete="username" autofocus required/>
  <label for="password">Mot de passe</label>
  <div class="pwd-row">
    <input id="password" name="password" type="password" autocomplete="current-password" required/>
    <button type="button" class="toggle-eye" onclick="var p=document.getElementById('password'); if(p.type==='password'){{p.type='text'; this.textContent='Masquer';}} else {{p.type='password'; this.textContent='Afficher';}}">Afficher</button>
  </div>
  <input type="hidden" name="rd" value="{redirect}"/>
  <button type="submit" class="submit">Se connecter</button>
  {error_html}
</form>
</body></html>"""


async def _send_html(send, status: int, body: str, extra_headers: list[tuple[bytes, bytes]] | None = None):
    headers = [(b"content-type", b"text/html; charset=utf-8")]
    if extra_headers:
        headers.extend(extra_headers)
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body.encode("utf-8")})


async def _read_body(receive) -> bytes:
    body = b""
    more = True
    while more:
        message = await receive()
        body += message.get("body", b"")
        more = message.get("more_body", False)
    return body


class WebAuthMiddleware:
    """ASGI middleware gating the Shiny UI behind a username/password session.

    Placed OUTSIDE ApiMiddleware in the chain (see app.py) — `/api/v1/*`
    keeps using its own independent token auth, untouched by this.
    """

    def __init__(self, asgi_app, seed_users: dict[str, str] | None = None):
        self.app = asgi_app
        if seed_users:
            ensure_seed_users(seed_users)

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "/")

        # /api/v1/* keeps its own independent Bearer/token auth (src/api.py) —
        # never gate it behind a browser session cookie.
        if path.startswith("/api/"):
            await self.app(scope, receive, send)
            return

        cookies = _parse_cookies(scope)
        username = _session_username(cookies.get(SESSION_COOKIE))

        if scope["type"] == "websocket":
            if not username:
                await send({"type": "websocket.close", "code": 4401})
                return
            await self.app(scope, receive, send)
            return

        # --- http ---
        if path == "/logout":
            _drop_session(cookies.get(SESSION_COOKIE))
            await send({
                "type": "http.response.start", "status": 302,
                "headers": [
                    (b"location", b"/login"),
                    (b"set-cookie", f"{SESSION_COOKIE}=; Path=/; Max-Age=0".encode()),
                ],
            })
            await send({"type": "http.response.body", "body": b""})
            return

        if path == "/login":
            if scope["method"] == "GET":
                qs = parse_qs(scope.get("query_string", b"").decode("utf-8"))
                rd = (qs.get("rd", ["/"])[0]) or "/"
                await _send_html(send, 200, LOGIN_PAGE.format(redirect=html.escape(rd), error_html=""))
                return
            if scope["method"] == "POST":
                ip = _client_ip(scope)
                if _rate_limited(ip):
                    await _send_html(send, 429, LOGIN_PAGE.format(
                        redirect="/", error_html='<p class="error">Trop de tentatives, réessaie dans une minute.</p>'))
                    return
                body = await _read_body(receive)
                form = parse_qs(body.decode("utf-8"))
                form_username = (form.get("username", [""])[0]).strip()
                form_password = form.get("password", [""])[0]
                redirect_to = (form.get("rd", ["/"])[0]) or "/"
                if _check_credentials(form_username, form_password):
                    token = _new_session(form_username)
                    await send({
                        "type": "http.response.start", "status": 302,
                        "headers": [
                            (b"location", redirect_to.encode()),
                            (b"set-cookie",
                             f"{SESSION_COOKIE}={token}; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age={SESSION_TTL_S}".encode()),
                        ],
                    })
                    await send({"type": "http.response.body", "body": b""})
                    return
                _record_fail(ip)
                await _send_html(send, 401, LOGIN_PAGE.format(
                    redirect=html.escape(redirect_to), error_html='<p class="error">Identifiants incorrects.</p>'))
                return

        if not username:
            rd = quote(path, safe="/")
            await _send_html(send, 200, LOGIN_PAGE.format(redirect=html.escape(rd), error_html=""))
            return

        await self.app(scope, receive, send)
