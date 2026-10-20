from shiny import App
from src.ui import app_ui
from src.server import server
import os
from src.paths import pictures_thumbnails_dir, pictures_original_dir
from src.api import ApiMiddleware
from src.webauth import WebAuthMiddleware
from src.webpush import WebPushMiddleware
from src.fcm_push import FCMMiddleware

# 07.10, Sid's Shiny -> FastAPI/React migration: fastapi is a brand new
# dependency, only present once the Docker image has actually been rebuilt
# (deps are baked into the image at build time; this source file is bind-
# mounted and takes effect immediately on the NEXT container restart - a
# window where the image may still be the old one). Importing it bare would
# crash-loop the ENTIRE app (Shiny included) on any restart that lands
# before the rebuilt image is swapped in. Falling back to an empty
# FASTAPI_TAB_PATHS below keeps every tab on Shiny, exactly as before this
# migration started, until the dependency is really there.
try:
    from src.webui_api import webui_app
except ImportError:
    webui_app = None

path_www = os.path.join(os.path.dirname(__file__), "www")
path_doc_diagrams = os.path.join(os.path.dirname(__file__), "doc", "diagrams")

# Serve event thumbnails/originals directly from disk to reduce server RAM/CPU load
# (no base64 embedding for the event modal).
path_thumbs = pictures_thumbnails_dir()
path_originals = pictures_original_dir()

# Latest-frame previews from the multi-camera watchdog (Reolink terrace/catio
# cameras) - written by watchdog.py onto the shared /data volume, served
# here so the "Presence" page can show them without a second HTTP server.
path_watchdog_frames = "/data/watchdog_frames"
os.makedirs(path_watchdog_frames, exist_ok=True)

# 09.09, Sid: same idea for the detection-triggered outdoor-camera training
# samples (watchdog.py's TRAINING_SAMPLES_DIR) - served straight from disk
# so the new Journey tab can show them without base64-embedding.
path_watchdog_samples = "/data/watchdog_training_samples"
os.makedirs(path_watchdog_samples, exist_ok=True)

# ---------------------------------------------------------------------------
# Tab routing middleware
# ---------------------------------------------------------------------------
# Each UI tab has a dedicated URL path (e.g. /pictures/, /system/). The Shiny
# SPA always runs at "/", so this ASGI middleware transparently rewrites any
# request whose first path segment is a known Shiny tab slug back to "/" (or
# strips the prefix for sub-resources like /pictures/shared/shiny.js ->
# /shared/shiny.js). This works for both HTTP and WebSocket upgrade requests.
#
# 07.10, Sid's Shiny -> FastAPI/React migration (twinkly-percolating-wombat.md):
# this is now also the dispatcher between the two UIs. SHINY_TAB_PATHS
# shrinks over time as tabs migrate; FASTAPI_TAB_PATHS grows. A migrated
# slug's full, unstripped path is forwarded to the new FastAPI app (webui_app)
# instead, so it can serve its own React static assets and /app-api/v1/*
# JSON routes under that same prefix.
# ---------------------------------------------------------------------------

# "chronology" stays listed here too, even though it's also claimed by
# FASTAPI_TAB_PATHS below when webui_app is loaded - the dispatcher checks
# FASTAPI_TAB_PATHS first, so that takes priority whenever it's available.
# This is the fallback for the window where webui_app failed to import
# (old Docker image, no fastapi yet): without it, a migrated slug that's
# absent from FASTAPI_TAB_PATHS *and* SHINY_TAB_PATHS matches neither check
# below and falls through to Shiny unmodified, which 404s on it ("Not
# Found") - caught 07.10, build 16 testing.
SHINY_TAB_PATHS = frozenset({
    "presence", "live-view", "journey", "pictures", "manage-cats",
    "ai-training", "configuration", "wlan-configuration", "chronology",
    # 08.10, Sid ("je vois plus accès" after the configuration cutover):
    # temporary stopgap so the self-update flow and the remote-mode doc
    # (the 2 sections NOT ported to React - see FASTAPI_TAB_PATHS comment
    # below) stay reachable via a direct link, even though "configuration"
    # itself now goes to React. Shiny's own client JS (www/app.js's
    # PATH_TO_TAB) maps this path to the same "configuration" nav_panel.
    # Remove once the React port of those 2 sections lands.
    "configuration-legacy",
})

# Slugs already migrated to the FastAPI/React app (webui_api.py). Empty
# (not {"chronology", "presence"}) if webui_app failed to import above -
# see that try/except's comment. SHINY_TAB_PATHS above still carries both
# slugs as the fallback for exactly that case.
FASTAPI_TAB_PATHS = (
    frozenset(
        {
            "chronology", "presence", "journey", "live-view", "appearance", "pictures",
            "configuration",
            # 08.10, Sid ("tu peux créer une page et la conserver?"): React
            # port of doc/remote-mode.md - new slug, was never a Shiny tab.
            "remote-mode-doc",
            # 08.10, Sid ("entrainement terminé, on peut lancer la page
            # react"): AI Training cutover, held back until her own
            # Label-Studio-export + model-training round trip on the
            # pre-cutover Shiny page finished cleanly - see the 5 sections
            # (Label Studio, Model Training, Corrections humaines, Gestion
            # des modèles, Révision rétroactive) individually curl-tested
            # earlier. "ai-training" stays in SHINY_TAB_PATHS too as the
            # webui_app-import-failed fallback, same pattern as the rest.
            "ai-training",
        }
    )
    if webui_app is not None
    else frozenset()
)
# 08.10, Sid: cut over after 11 of 13 sections were individually built and
# tested (round-tripped GET/POST with no side effects) overnight. Two
# sections deliberately NOT replicated and still only reachable via a
# direct link to Shiny's own (now unlisted) config page if ever needed:
# the self-update flow (git pull/pip install/version-mismatch gating -
# info.py's ui_info(), the single riskiest piece of the whole page) and the
# static remote-mode markdown doc.


class TabRoutingMiddleware:
    """ASGI middleware: dispatches each tab slug to Shiny (legacy, stripped
    back to "/") or to the new FastAPI app (full path kept), per the two
    frozensets above."""

    def __init__(self, shiny_asgi_app, fastapi_asgi_app):
        self.shiny_app = shiny_asgi_app
        self.fastapi_app = fastapi_asgi_app

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            path = scope.get("path", "/")

            # 07.10, Sid ("quand j'ouvre l'app, chats est en shiny... si je
            # retourne sur chats [via navigation] il passe en react"): the
            # Android app's capacitor.config.json server.url has no path at
            # all, so a cold app start always requests bare "/" first -
            # which matched neither frozenset below and fell straight
            # through to Shiny (the only thing that still legitimately owns
            # "/" itself). A real redirect to "/presence/" (the same home
            # tab every other bit of this app already treats as default -
            # BottomNav, the back-button fallback, TAB_ORDER) makes cold
            # start land on the same page a normal in-app navigation to
            # "Chats" does, instead of silently diverging only on first load.
            if scope["type"] == "http" and path == "/" and self.fastapi_app is not None:
                await send(
                    {
                        "type": "http.response.start",
                        "status": 307,
                        "headers": [(b"location", b"/presence/")],
                    }
                )
                await send({"type": "http.response.body", "body": b""})
                return

            # Split off the first segment: "/pictures/foo" -> ("pictures", "foo")
            stripped = path.lstrip("/")
            first_seg, _, remainder = stripped.partition("/")
            # 08.10, Sid ("pourquoi tout est vide?"): watchdog-sample-thumb
            # is a dynamic FastAPI route (lazy-generated, disk-cached), not
            # a page tab - grouped with app-api/web-assets rather than
            # FASTAPI_TAB_PATHS (that frozenset is conceptually "page
            # slugs").
            # 10.10, Sid ("l'image en live date d'hier" / "toujours pas
            # d'images de la reolink dans parcours"): watchdog-frame was
            # supposedly "left on Shiny's own static_assets mount,
            # unchanged" per the comment above - tested live tonight and
            # it's actually serving the SPA's index.html (text/html) for
            # every request, not the image, root cause not chased further
            # (Shiny internals). Moved to a real FastAPI route instead (see
            # get_watchdog_frame below) rather than debugging Shiny's static
            # dispatch - same fix already proven for watchdog-sample-thumb
            # and watchdog-event-frame (the latter was simply never added
            # here at all - a plain miss when that route was written).
            if self.fastapi_app is not None and (
                first_seg in FASTAPI_TAB_PATHS
                or first_seg
                in ("app-api", "web-assets", "watchdog-sample-thumb", "watchdog-frame", "watchdog-event-frame")
            ):
                await self.fastapi_app(scope, receive, send)
                return
            if first_seg in SHINY_TAB_PATHS:
                new_path = "/" + remainder  # e.g. "/foo" or just "/"
                scope = dict(scope, path=new_path)
                if "raw_path" in scope:
                    scope["raw_path"] = new_path.encode("latin-1")
        await self.shiny_app(scope, receive, send)


shiny_app = App(
	app_ui,
	server,
	static_assets={
		"/thumb": path_thumbs,
		"/orig": path_originals,
		"/diagrams": path_doc_diagrams,
		"/watchdog-frame": path_watchdog_frames,
		"/watchdog-sample": path_watchdog_samples,
		"/": path_www,
	},
)

# Middleware chain (outermost first, evaluated top-down on each request):
#   WebAuthMiddleware    -> username/password session gate for the human UI
#                           (passes /api/v1/* straight through, untouched)
#   ApiMiddleware        -> captures /api/v1/* and serves the REST API
#   TabRoutingMiddleware -> dispatches migrated tabs to webui_app (FastAPI/
#                           React), everything else to Shiny as before
#   shiny_app            -> the SPA + static asset mounts
#
# 07.09, Sid: Kittyhack ships with no UI login at all — anyone who can reach
# the URL can view the camera and control the flap. Temporary passwords
# ("admin") are seeded on first run only (users_auth.json, gitignored) —
# change them from day one via src.webauth.set_password().
app = WebAuthMiddleware(
    FCMMiddleware(WebPushMiddleware(ApiMiddleware(TabRoutingMiddleware(shiny_app, webui_app)))),
    seed_users={"sidounette": "admin", "xacarr": "admin"},
)