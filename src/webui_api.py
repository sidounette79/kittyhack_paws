"""FastAPI app for the React-based pages migrated off Shiny (see the
migration plan, "chronology" is the first slice). Mounted by app.py's
TabRoutingMiddleware for the tab slugs listed there - never reached
directly, so it has no auth of its own responsibility at the ASGI level
(WebAuthMiddleware, outside this whole chain, already gates every path here
behind the session cookie) but every JSON route still re-checks the cookie
itself via require_session() as defense in depth and to get the username.

07.10, Sid's migration plan (twinkly-percolating-wombat.md): internal data
routes live under /app-api/v1/* and are guarded by webauth.py's session
cookie - never src/api.py's Bearer token, which is for external integrations
(Stream Deck, Shortcuts), not for Sid's own browser/app session.
"""

import asyncio
import glob
import json
import logging
import os
import re
import threading
import uuid
import zipfile as _zipfile

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import requests

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from src.api import _apply_mode, _current_mode, _door_state, _set_manual_override
from src.baseconfig import CONFIG, set_language, update_single_config_parameter
from src.database import (
    CatsRepo,
    DatabaseCore,
    DetectionFeedbackRepo,
    EventsRepo,
    ReturnDataCatDB,
    ReturnDataPhotosDB,
)
from src.helper import DateTimeUtil, EventType
from src.mode import is_remote_mode
from src.paths import pictures_original_dir, wallpapers_dir
from src.webauth import (
    SESSION_COOKIE,
    _check_credentials,
    _session_username,
    set_password,
    username_exists,
)
from src.server_ui.journeys import (
    _INSIDE_EVENT_TYPES,
    TRAINING_SAMPLES_DIR,
    _SAMPLE_RE,
    _cam_label,
    _correlate,
    _event_type_first,
    _group_solo_samples,
    _last_known_status_before,
    _list_flap_events_for_date,
    _list_training_samples_for_date,
    _local_day_utc_range,
    _presence_direction_text,
)
from src.server_ui.presence import (
    _day_glance_prey_counts,
    _day_stats_for_events,
    _day_timeline_for_events,
    _format_duration,
    _insert_manual_presence_event,
    _last_entry_for_rfid,
    _last_exit_for_rfid,
    _latest_presence_for_rfid,
    _monthly_prey_counts_for_rfid,
    _list_events_for_rfid_filtered,
    _presence_events_for_rfid,
    _presence_prey_blocks_for_rfid,
)

_ = set_language(CONFIG["LANGUAGE"])

_TAG_RE = re.compile(r"<[^>]+>")

_WEB_DIST_DIR = os.path.join(os.path.dirname(__file__), "..", "web", "dist")
_WEB_ASSETS_DIR = os.path.join(_WEB_DIST_DIR, "assets")
_WEB_INDEX_HTML = os.path.join(_WEB_DIST_DIR, "index.html")

webui_app = FastAPI(title="Kittyhack web UI (migrated pages)")

# 08.10: process-wide lock for the retroactive model-review scan, same
# reason as the Shiny original's _model_review_process_lock comment - a
# per-request guard alone would let several scans stack up if triggered
# from multiple tabs/devices at once.
_model_review_process_lock = threading.Lock()
_model_review_last_result: dict | None = None
# 08.10, Sid ("faudra faire un bouton du coup"): no previous way to stop a
# scan short of restarting the whole container - set from the new /cancel
# endpoint, checked inside run_retroactive_review_batch between blocks/photos.
_model_review_cancel_event = threading.Event()

# 09.10, Sid ("la revérification doit gérer automatiquement de réattribuer
# cet événement-là au bon chat tout du long"): separate lock/cancel/result
# state for the RFID-ground-truth reassignment (src.model.rfid_ground_truth)
# - a different feature from the YOLO review scan above (no model, no
# inference), kept independent so one can't block or clobber the other.
_rfid_ground_truth_process_lock = threading.Lock()
_rfid_ground_truth_last_result: dict | None = None
_rfid_ground_truth_cancel_event = threading.Event()

# 07.10, Sid: one shared static prefix for every migrated page's JS/CSS now
# (single multi-page bundle, see web/vite.config.ts and web/src/main.tsx) -
# app.py's TabRoutingMiddleware forwards "web-assets" to this app the same
# way it already does for "app-api".
#
# Mounted on the DIST ROOT, not _WEB_ASSETS_DIR - vite always nests its own
# built output under an "assets/" subfolder of whatever `base` is set to,
# so with base="/web-assets/" every built file's real URL is
# "/web-assets/assets/<file>". The previous version of this (when base was
# "/chronology/") mounted at "/chronology/assets" -> _WEB_ASSETS_DIR, which
# included that trailing "/assets" segment explicitly and so matched.
# Shortening the mount to just "/web-assets" -> _WEB_ASSETS_DIR here (this
# migration's first attempt) dropped that segment, so every asset request
# landed one directory too deep and 404'd - caught 07.10 after deploy
# ("j'ai encore l'ancienne version... page blanche"). Mounting the DIST
# ROOT instead keeps "/assets" in the URL matching the real "assets/"
# subfolder on disk, without hardcoding that path twice.
if os.path.isdir(_WEB_DIST_DIR):
    webui_app.mount("/web-assets", StaticFiles(directory=_WEB_DIST_DIR), name="web-assets")


def _serve_spa_index() -> FileResponse:
    if not os.path.isfile(_WEB_INDEX_HTML):
        raise HTTPException(
            status_code=503,
            detail="Web frontend not built yet - run `npm run build` in app/web/",
        )
    # 11.10, Sid: a real deploy (new TopBanner.tsx confirm-dialog fix) stayed
    # invisible on both her tablet and phone even after a full reload - not
    # a server-side bug this time (verified: the container's own /app/web/
    # dist/index.html already pointed at the new hashed bundle). A bare
    # FileResponse here only sets Last-Modified/ETag, which is conditional-
    # GET-friendly in theory but still left the Android WebView (the
    # Capacitor-wrapped "Paws Flap" app, same `server.url` as the plain
    # browser - see android-app/capacitor.config.json) serving a stale
    # cached copy of THIS document regardless. The hashed JS/CSS under
    # /web-assets/assets/* (StaticFiles mount above) are safe to cache
    # forever - they get a new URL on every real change - but this one
    # document (the only thing referencing which hash is current) must
    # never be served from a cache, or a new deploy can look like nothing
    # happened on any client until it independently decides to revalidate.
    return FileResponse(
        _WEB_INDEX_HTML,
        headers={"Cache-Control": "no-store, must-revalidate"},
    )


async def require_session(request: Request) -> str:
    """Re-validate the webauth.py session cookie. WebAuthMiddleware (outside
    this whole ASGI chain) already rejects an unauthenticated request before
    it ever reaches here - this is defense in depth, not the primary gate."""
    username = _session_username(request.cookies.get(SESSION_COOKIE))
    if not username:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return username


def _direction_code(event_type, rfid: str | None, event_dt) -> str | None:
    """Same classification _presence_direction_text() uses to pick a CSS
    class, exposed separately as a plain code for the React icon/color -
    the human-readable text itself still comes straight from
    _presence_direction_text() below so wording never drifts between the
    Shiny pages still live and this one."""
    real_type = _event_type_first(event_type)
    if real_type in _INSIDE_EVENT_TYPES:
        return "in"
    if real_type == EventType.CAT_WENT_OUTSIDE:
        return "out"
    if real_type == EventType.MOTION_OUTSIDE_ONLY:
        last_status = _last_known_status_before(rfid, event_dt) if (rfid and event_dt) else None
        return "glance_out" if last_status == "out" else "glance_in"
    return None


def _serialize_events(
    raw_events: list[dict], cat_name_dict: dict, timelines_by_block: dict | None = None
) -> list[dict]:
    """Shared by /app-api/v1/chronology and the Presence per-cat events
    endpoint below - same row shape (block_id/first_id/dt/event_type/rfid),
    same direction/text logic, just a different source query.

    09.10, Sid ("ca serait pas mal de rajouter une ligne, dans la
    chronologie, quand un événement comme ca se passe" - after a real
    incident where a cat got stuck outside because an entry decision was
    skipped while an exit was in progress for a different cat):
    ``timelines_by_block`` (optional, {block_id: timeline_entries} from
    EventsRepo.db_get_motion_timelines - same data the "Détails du
    mouvement" panel already reads) lets each event flag whether its own
    motion block contains an "exit_skipped_entry" timeline action, so the
    frontend can show a visible warning without opening the detail panel.
    Doesn't touch loop.py's actual decision logic at all - purely surfaces
    data that's already being recorded."""
    events = []
    for ev in raw_events:
        had_skipped_entry = False
        if timelines_by_block is not None:
            for entry in timelines_by_block.get(ev["block_id"], []):
                if entry.get("action") == "exit_skipped_entry":
                    had_skipped_entry = True
                    break
        resolved_name = cat_name_dict.get(ev["rfid"]) if ev["rfid"] else None
        rfid_is_real = ev.get("rfid_is_real")
        direction_html = _presence_direction_text(
            ev["event_type"], resolved_name, rfid=ev["rfid"], event_dt=ev["dt"],
            rfid_is_real=rfid_is_real,
        )
        if direction_html is not None:
            text = _TAG_RE.sub("", direction_html)
            code = _direction_code(ev["event_type"], ev["rfid"], ev["dt"])
        else:
            text = resolved_name or (
                f"RFID inconnu : {ev['rfid']}" if ev["rfid"] else "Aucun RFID trouvé"
            )
            if resolved_name and rfid_is_real is False:
                text += " (non confirmé)"
            code = None
        events.append(
            {
                "block_id": ev["block_id"],
                "photo_id": ev["first_id"],
                "time": ev["dt"].strftime("%H:%M:%S"),
                "date_label": ev["dt"].strftime("%d.%m"),
                "cat_name": resolved_name,
                "direction": code,
                "text": text,
                "thumb_url": f"/thumb/{ev['first_id']}.jpg",
                # 08.10, Sid: True = real chip read, False = video model's
                # guess only, None = unknown provenance (pre-migration row).
                "rfid_confirmed": rfid_is_real,
                # 09.10, Sid ("l'icône souris pour chronologie et parcours"):
                # absent (.get default) for callers whose raw_events rows
                # don't carry it (e.g. the Presence per-cat events endpoint),
                # not just a falsy 0 - same mouse_threshold Pictures already
                # exposes, so the frontend can colour this the same way.
                "mouse_probability": round(float(ev.get("mouse_probability", 0.0)), 1),
                "mouse_threshold": float(CONFIG["MOUSE_THRESHOLD"]),
                "had_skipped_entry": had_skipped_entry,
            }
        )
    return events


@webui_app.get("/app-api/v1/chronology")
async def get_chronology(date: str | None = None, _user: str = Depends(require_session)):
    try:
        if date:
            local_date = datetime.strptime(date, "%Y-%m-%d").date()
        else:
            local_date = datetime.now(ZoneInfo(CONFIG["TIMEZONE"])).date()
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid date, expected YYYY-MM-DD")

    date_start_utc, date_end_utc = _local_day_utc_range(local_date)
    raw_events = _list_flap_events_for_date(date_start_utc, date_end_utc)
    cat_name_dict = CatsRepo.get_cat_name_rfid_dict(CONFIG["KITTYHACK_DATABASE_PATH"])
    timelines_by_block = EventsRepo.db_get_motion_timelines(
        CONFIG["KITTYHACK_DATABASE_PATH"], [ev["block_id"] for ev in raw_events]
    )
    events = _serialize_events(raw_events, cat_name_dict, timelines_by_block)

    return JSONResponse({"date": local_date.isoformat(), "events": events})


@webui_app.get("/app-api/v1/chronology/recent")
async def get_chronology_recent(limit: int = 50, _user: str = Depends(require_session)):
    """09.10, Sid ("page chats, en dessous des 4 chips des chats, les 50
    derniers événements... et on ne garde pas la page chronologie"): same
    row shape/serialization as /app-api/v1/chronology, just the N most
    recent motion blocks across all days instead of one calendar day."""
    from src.server_ui.journeys import _list_recent_flap_events

    limit = max(1, min(int(limit), 200))
    raw_events = _list_recent_flap_events(limit)
    cat_name_dict = CatsRepo.get_cat_name_rfid_dict(CONFIG["KITTYHACK_DATABASE_PATH"])
    timelines_by_block = EventsRepo.db_get_motion_timelines(
        CONFIG["KITTYHACK_DATABASE_PATH"], [ev["block_id"] for ev in raw_events]
    )
    events = _serialize_events(raw_events, cat_name_dict, timelines_by_block)
    return JSONResponse({"events": events})


# ---------------------------------------------------------------------------
# Journey ("Parcours") - flap events correlated with nearby outdoor-camera
# watchdog captures, plus standalone solo-camera groups. Same correlation/
# grouping logic as journeys.py's ui_journey_timeline(), reused as-is - only
# the HTML rendering is replaced with JSON.
# ---------------------------------------------------------------------------

def _capture_json(sample: dict) -> dict:
    return {
        "filename": sample["filename"],
        "cam": _cam_label(sample["cam"]),
        "time": sample["dt"].strftime("%H:%M:%S"),
        # Full-res original (Label Studio quality, lightbox full view).
        "url": f"/watchdog-sample/{sample['filename']}",
        # 08.10, Sid ("pourquoi tout est vide?"): small, disk-cached
        # thumbnail for the list view - see get_watchdog_sample_thumb().
        "thumb_url": f"/watchdog-sample-thumb/{sample['filename']}",
    }


@webui_app.get("/app-api/v1/journey")
async def get_journey(date: str | None = None, _user: str = Depends(require_session)):
    try:
        local_date = (
            datetime.strptime(date, "%Y-%m-%d").date()
            if date
            else datetime.now(ZoneInfo(CONFIG["TIMEZONE"])).date()
        )
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid date, expected YYYY-MM-DD")

    date_start_utc, date_end_utc = _local_day_utc_range(local_date)
    events = _list_flap_events_for_date(date_start_utc, date_end_utc)
    samples = _list_training_samples_for_date(local_date.strftime("%Y-%m-%d"))
    # _correlate mutates `events` in place, attaching each one's "cluster"
    # of nearby samples, and returns whatever samples are left unclaimed.
    solo_samples = _correlate(events, samples)
    solo_groups = _group_solo_samples(solo_samples)
    cat_name_dict = CatsRepo.get_cat_name_rfid_dict(CONFIG["KITTYHACK_DATABASE_PATH"])
    timelines_by_block = EventsRepo.db_get_motion_timelines(
        CONFIG["KITTYHACK_DATABASE_PATH"], [ev["block_id"] for ev in events]
    )

    timeline = []
    for ev in events:
        serialized = _serialize_events([ev], cat_name_dict, timelines_by_block)[0]
        serialized["kind"] = "event"
        serialized["cluster"] = [_capture_json(s) for s in ev["cluster"]]
        timeline.append((ev["dt"], serialized))
    for group in solo_groups:
        timeline.append(
            (
                group[-1]["dt"],
                {
                    "kind": "solo_group",
                    "captures": [_capture_json(s) for s in group],
                },
            )
        )
    timeline.sort(key=lambda t: t[0], reverse=True)

    return JSONResponse({"date": local_date.isoformat(), "items": [item for __, item in timeline]})


def _safe_sample_path(filename: str) -> str | None:
    """Same guard as journeys.py's own _safe_sample_path - reject anything
    that isn't a plain filename matching the expected watchdog-sample
    pattern before touching the filesystem."""
    if not filename or not _SAMPLE_RE.match(filename):
        return None
    if os.path.basename(filename) != filename:
        return None
    path = os.path.join(TRAINING_SAMPLES_DIR, filename)
    if not os.path.abspath(path).startswith(os.path.abspath(TRAINING_SAMPLES_DIR) + os.sep):
        return None
    return path


# 10.10, Sid ("deux vidéos côte à côte... avec les incrustations aussi"):
# the dense per-event Reolink sequence watchdog.py's _save_event_frame
# writes - must match EVENT_FRAMES_DIR there (separate literal, same
# reasoning as TRAINING_SAMPLES_DIR's own duplication - watchdog.py has no
# dependency on this app's modules).
EVENT_FRAMES_DIR = "/data/watchdog_event_frames"
_EVENT_FRAME_RE = re.compile(r"^(?P<cam>[a-z_]+)_(?P<date>\d{8})_(?P<time>\d{6})_(?P<ms>\d{3})\.jpg$")
# Padding around the flap event's own [first photo, last photo] span - the
# PIR/Reolink trigger that opens chatiere's boost window typically fires a
# couple seconds before the flap camera itself logs its first frame, and
# the cat can still be in chatiere's frame briefly after the flap event's
# last logged photo.
_EVENT_FRAME_PAD_S = 15.0


def _safe_event_frame_path(filename: str) -> str | None:
    if not filename or not _EVENT_FRAME_RE.match(filename):
        return None
    if os.path.basename(filename) != filename:
        return None
    path = os.path.join(EVENT_FRAMES_DIR, filename)
    if not os.path.abspath(path).startswith(os.path.abspath(EVENT_FRAMES_DIR) + os.sep):
        return None
    return path


def _list_event_frames(local_start: datetime, local_end: datetime) -> list[dict]:
    """Dense "chatiere" (Reolink) frames within [local_start, local_end]
    (already padded by the caller), sorted oldest-first, each with its own
    detection boxes read back from the JSON sidecar _save_event_frame wrote
    alongside it."""
    results = []
    for filepath in glob.glob(os.path.join(EVENT_FRAMES_DIR, "chatiere_*.jpg")):
        filename = os.path.basename(filepath)
        m = _EVENT_FRAME_RE.match(filename)
        if not m:
            continue
        try:
            dt = datetime.strptime(m.group("date") + m.group("time"), "%Y%m%d%H%M%S")
        except ValueError:
            continue
        if not (local_start <= dt <= local_end):
            continue
        objects = []
        json_path = filepath[:-4] + ".json"
        try:
            with open(json_path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            for idx, d in enumerate(raw):
                obj = {
                    "x": d.get("x", 0),
                    "y": d.get("y", 0),
                    "w": d.get("width", 0),
                    "h": d.get("height", 0),
                    "name": d.get("name", ""),
                    "prob": d.get("probability", 0),
                    "idx": idx,
                }
                # 10.10, Sid ("il faut me donner les outils de
                # correction... pour reolink"): corrections are stored
                # in-place in the JSON sidecar itself (no DB table for
                # these file-backed frames) - see post_reolink_correction
                # below.
                if d.get("reviewed"):
                    obj["reviewed"] = True
                    obj["reviewedAs"] = d.get("reviewed_as")
                objects.append(obj)
        except Exception:
            pass
        results.append(
            {
                "ts": dt.strftime("%H:%M:%S.%f")[:-5],
                "url": f"/watchdog-event-frame/{filename}",
                "filename": filename,
                "objects": objects,
            }
        )
    results.sort(key=lambda r: r["ts"])
    return results


# 21.10, Sid ("j'ai que 8 images dans la video reolink, alors qu'il a duré
# autant du point de vue reolink que du point de vue HK") - un vrai
# passage avait des frames chatière denses (~1/s) bien après la fin du
# bloc HK (vérifié sur disque: capture continue jusqu'à +1min20 au-delà de
# la fenêtre ±15s), mais _EVENT_FRAME_PAD_S coupait net à un pad fixe,
# jetant du contenu pourtant réellement capturé. Remplace le pad fixe par
# une extension "tant que les frames restent proches les unes des autres"
# (coupe seulement sur un vrai trou de capture, pas sur un délai
# arbitraire) - _MAX_EVENT_FRAME_GAP_S borne l'écart maximal toléré entre
# deux frames consécutives pour continuer l'extension, _MAX_EVENT_FRAME_
# EXTRA_S plafonne l'extension totale par sécurité (évite une fenêtre qui
# s'étend sans fin sur du bruit occasionnel).
_MAX_EVENT_FRAME_GAP_S = 10.0
_MAX_EVENT_FRAME_EXTRA_S = 300.0


def _chatiere_frame_timestamps() -> list[datetime]:
    """All chatiere frame timestamps on disk, sorted ascending."""
    out = []
    for filepath in glob.glob(os.path.join(EVENT_FRAMES_DIR, "chatiere_*.jpg")):
        m = _EVENT_FRAME_RE.match(os.path.basename(filepath))
        if not m:
            continue
        try:
            out.append(datetime.strptime(m.group("date") + m.group("time"), "%Y%m%d%H%M%S"))
        except ValueError:
            continue
    out.sort()
    return out


def _extended_event_frame_window(base_start: datetime, base_end: datetime) -> tuple[datetime, datetime]:
    """Widen [base_start, base_end] (the flap event's own span + the small
    fixed _EVENT_FRAME_PAD_S) outward through any chatière frames that keep
    arriving close together, so real continued capture past that pad isn't
    silently dropped. Stops at the first real gap (or the safety ceiling)."""
    timestamps = _chatiere_frame_timestamps()
    if not timestamps:
        return base_start, base_end

    end = base_end
    for ts in timestamps:
        if ts <= end:
            continue
        if ts - end > timedelta(seconds=_MAX_EVENT_FRAME_GAP_S):
            break
        if ts - base_end > timedelta(seconds=_MAX_EVENT_FRAME_EXTRA_S):
            break
        end = ts

    start = base_start
    for ts in reversed(timestamps):
        if ts >= start:
            continue
        if start - ts > timedelta(seconds=_MAX_EVENT_FRAME_GAP_S):
            break
        if base_start - ts > timedelta(seconds=_MAX_EVENT_FRAME_EXTRA_S):
            break
        start = ts

    return start, end


@webui_app.get("/watchdog-event-frame/{filename}")
async def get_watchdog_event_frame(filename: str, _user: str = Depends(require_session)):
    path = _safe_event_frame_path(filename)
    if not path or not os.path.exists(path):
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(path, media_type="image/jpeg")


# 10.10, Sid ("il faut me donner les outils de correction/modification
# d'incrustation pour reolink"): same confirm/correct/false-positive +
# draw-a-missed-detection workflow as the flap's own photos
# (post_event_correction / add-detection below), but these frames have no
# DB photo id - the JSON sidecar _save_event_frame wrote IS the record,
# edited in place.
def _reolink_json_path(filename: str) -> str | None:
    path = _safe_event_frame_path(filename)
    if not path:
        return None
    return path[:-4] + ".json"


@webui_app.post("/app-api/v1/events/{block_id}/reolink-correction")
async def post_reolink_correction(block_id: int, request: Request, _user: str = Depends(require_session)):
    body = await request.json()
    filename = str(body.get("filename") or "")
    idx = int(body.get("idx", -1))
    action = str(body.get("action") or "")
    corrected_name = body.get("correctedName")

    json_path = _reolink_json_path(filename)
    if not json_path or not os.path.exists(json_path):
        raise HTTPException(status_code=404, detail="not found")
    with open(json_path, "r", encoding="utf-8") as f:
        detections = json.load(f)
    if idx < 0 or idx >= len(detections):
        raise HTTPException(status_code=404, detail="detection not found")

    det = detections[idx]
    det["reviewed"] = True
    final_label: str | None
    if action == "confirm":
        final_label = _canonical_cat_name(str(det.get("name") or ""))
        det["reviewed_as"] = final_label
    elif action == "false_positive":
        final_label = None
        det["reviewed_as"] = "none"
    elif action == "correct":
        final_label = _canonical_cat_name(str(corrected_name or ""))
        det["reviewed_as"] = final_label
    else:
        raise HTTPException(status_code=400, detail="invalid action")

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(detections, f)

    return JSONResponse({"ok": True})


@webui_app.post("/app-api/v1/events/{block_id}/reolink-add-detection")
async def post_reolink_add_detection(block_id: int, request: Request, _user: str = Depends(require_session)):
    body = await request.json()
    filename = str(body.get("filename") or "")
    label = _canonical_cat_name(str(body.get("label") or "").strip())
    x, y = float(body.get("x", 0)), float(body.get("y", 0))
    width, height = float(body.get("width", 0)), float(body.get("height", 0))
    if not label:
        raise HTTPException(status_code=400, detail="label required")

    json_path = _reolink_json_path(filename)
    if not json_path or not os.path.exists(json_path):
        raise HTTPException(status_code=404, detail="not found")
    with open(json_path, "r", encoding="utf-8") as f:
        detections = json.load(f)
    detections.append(
        {
            "name": label,
            "probability": 0,
            "x": x,
            "y": y,
            "width": width,
            "height": height,
            "reviewed": True,
            "reviewed_as": label,
        }
    )
    idx = len(detections) - 1
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(detections, f)

    return JSONResponse({"idx": idx})


# 10.10, Sid ("l'image en live date d'hier"): the live camera-preview
# snapshot watchdog.py's _save_frame_snapshot overwrites every poll (see
# FRAME_SNAPSHOT_DIR there) - real FastAPI route now, see the dispatch
# comment in app.py for why (Shiny's own static mount for this same path
# was silently serving the SPA's index.html instead of the image).
FRAME_SNAPSHOT_DIR = "/data/watchdog_frames"
_FRAME_SNAPSHOT_RE = re.compile(r"^[a-z_]+\.jpg$")


@webui_app.get("/watchdog-frame/{filename}")
async def get_watchdog_frame(filename: str, _user: str = Depends(require_session)):
    if not filename or not _FRAME_SNAPSHOT_RE.match(filename) or os.path.basename(filename) != filename:
        raise HTTPException(status_code=404, detail="not found")
    path = os.path.join(FRAME_SNAPSHOT_DIR, filename)
    if not os.path.abspath(path).startswith(os.path.abspath(FRAME_SNAPSHOT_DIR) + os.sep):
        raise HTTPException(status_code=404, detail="not found")
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


# 08.10, Sid ("pourquoi tout est vide?"): watchdog.py's training samples are
# saved at full camera resolution (2560x1920, ~2MB each - never resized,
# that's deliberate for labeling quality in Label Studio), but Parcours was
# requesting that same full-res original for a tiny 108px thumbnail - several
# at once in one group is several MB just to render small previews, slow
# enough on a weak connection to look permanently blank. Generated lazily
# here (not changed in watchdog.py itself) and cached to disk on first
# request, so it covers the ~17k samples already on disk too, not just new
# ones - no slow blanket backfill needed, only the ones actually viewed ever
# pay the resize cost.
_WATCHDOG_THUMB_DIR = os.path.join(TRAINING_SAMPLES_DIR, "..", "watchdog_training_samples_thumbs")
_WATCHDOG_THUMB_SIZE = 240


@webui_app.get("/watchdog-sample-thumb/{filename}")
async def get_watchdog_sample_thumb(filename: str, _user: str = Depends(require_session)):
    source_path = _safe_sample_path(filename)
    if not source_path or not os.path.exists(source_path):
        raise HTTPException(status_code=404, detail="not found")

    os.makedirs(_WATCHDOG_THUMB_DIR, exist_ok=True)
    thumb_path = os.path.join(os.path.abspath(_WATCHDOG_THUMB_DIR), filename)

    if not os.path.exists(thumb_path):
        import cv2

        def _generate():
            img = cv2.imread(source_path)
            if img is None:
                return False
            h, w = img.shape[:2]
            scale = _WATCHDOG_THUMB_SIZE / max(h, w)
            resized = cv2.resize(img, (max(1, int(w * scale)), max(1, int(h * scale))))
            # cv2.imwrite needs a recognized extension to pick an encoder -
            # a ".tmp" suffix breaks that (confirmed: "could not find a
            # writer for the specified extension"). imencode is format-
            # agnostic - same pattern watchdog.py's own _save_frame_snapshot
            # already uses for its own atomic write.
            ok, buf = cv2.imencode(".jpg", resized, [cv2.IMWRITE_JPEG_QUALITY, 80])
            if ok:
                tmp_path = thumb_path + ".tmp"
                with open(tmp_path, "wb") as f:
                    f.write(buf.tobytes())
                os.replace(tmp_path, thumb_path)
            return ok

        generated = await asyncio.to_thread(_generate)
        if not generated:
            # Fall back to the original rather than a broken image.
            return FileResponse(source_path, media_type="image/jpeg")

    return FileResponse(thumb_path, media_type="image/jpeg")


@webui_app.post("/app-api/v1/journey/capture")
async def post_journey_capture(request: Request, _user: str = Depends(require_session)):
    body = await request.json()
    filename = str(body.get("filename") or "")
    action = str(body.get("action") or "")
    path = _safe_sample_path(filename)
    if not path:
        raise HTTPException(status_code=400, detail="invalid or unsafe filename")

    if action == "delete":
        if os.path.exists(path):
            os.remove(path)
        return JSONResponse({"ok": True})

    raise HTTPException(status_code=400, detail=f"unknown action: {action}")


@webui_app.get("/journey")
@webui_app.get("/journey/")
@webui_app.get("/journey/{rest:path}")
async def serve_journey_page(_user: str = Depends(require_session)):
    return _serve_spa_index()


# ---------------------------------------------------------------------------
# Pictures - ungrouped photo grid (date + cats-only/mice-only/cat filters,
# reverse pagination), inline detection correction and manual box-add (05.10,
# Sid: "je puisse tagger directement depuis ces photos"), same
# detection_feedback table EventModal already reads/writes. The old Shiny
# page's "Group pictures to events" toggle is NOT replicated here - it's the
# same grouped data Chronology/Journey already show.
# ---------------------------------------------------------------------------


def _serialize_picture(row, cat_name_dict: dict, feedback: dict) -> dict:
    pid = int(row["id"])
    event_text = row.get("event_text")
    try:
        detected = EventsRepo.read_event_from_json(event_text) if event_text else []
    except Exception:
        detected = []

    objects = []
    for idx, dobj in enumerate(detected):
        name = (dobj.object_name or "").strip()
        if name.lower() == "false-accept":
            continue
        obj = {
            "x": round(dobj.x, 2),
            "y": round(dobj.y, 2),
            "w": round(dobj.width, 2),
            "h": round(dobj.height, 2),
            "name": name,
            "prob": round(dobj.probability, 1) if dobj.probability else 0,
            "idx": idx,
        }
        fb = feedback.get(idx)
        if fb is not None:
            obj["reviewed"] = True
            obj["reviewedAs"] = fb.get("original_name") if fb.get("confirmed") else fb.get("corrected_name")
        objects.append(obj)

    # 05.10, Sid: a manually-drawn box has no matching entry in `detected` -
    # just a detection_feedback row with an object_index beyond the model's
    # own list (see post_picture_add_detection below).
    for idx, fb in feedback.items():
        if idx < len(detected):
            continue
        objects.append(
            {
                "x": round(float(fb["x"]), 2),
                "y": round(float(fb["y"]), 2),
                "w": round(float(fb["width"]), 2),
                "h": round(float(fb["height"]), 2),
                "name": fb.get("corrected_name") or "?",
                "prob": 0,
                "idx": idx,
                "reviewed": True,
                "reviewedAs": fb.get("corrected_name"),
                "manual": True,
            }
        )

    rfid = row.get("rfid") or None
    # 07.10: a NULL mouse_probability column reads back as pandas NaN, which
    # `or 0` does NOT catch (NaN is truthy in Python) - float('nan') then
    # crashes JSON serialization outright ("Out of range float values are
    # not JSON compliant"), a real 500 caught testing this endpoint live.
    mouse_prob = row.get("mouse_probability")
    mouse_prob = 0.0 if mouse_prob is None or pd.isna(mouse_prob) else float(mouse_prob)
    return {
        "id": pid,
        "block_id": int(row["block_id"]),
        "time": _format_frame_ts(row["created_at"]),
        "thumb_url": f"/thumb/{pid}.jpg",
        "cat_name": cat_name_dict.get(rfid) if rfid else None,
        "rfid": rfid,
        "mouse_probability": mouse_prob,
        "mouse_threshold": float(CONFIG["MOUSE_THRESHOLD"]),
        "objects": objects,
    }


@webui_app.get("/app-api/v1/pictures")
async def get_pictures(
    date: str | None = None,
    page: int = 1,
    cats_only: bool = False,
    mice_only: bool = False,
    rfid: str = "",
    overlay: bool = True,
    _user: str = Depends(require_session)
):
    try:
        local_date = (
            datetime.strptime(date, "%Y-%m-%d").date()
            if date
            else datetime.now(ZoneInfo(CONFIG["TIMEZONE"])).date()
        )
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid date, expected YYYY-MM-DD")

    database = CONFIG["KITTYHACK_DATABASE_PATH"]
    date_start_utc, date_end_utc = _local_day_utc_range(local_date)

    total_count = EventsRepo.db_count_photos(
        database, date_start_utc, date_end_utc, cats_only, mice_only,
        CONFIG["MOUSE_THRESHOLD"], rfid_filter=rfid,
    )
    per_page = max(1, int(CONFIG["ELEMENTS_PER_PAGE"]))
    total_pages = max(1, -(-total_count // per_page))
    page_number = max(1, min(total_pages, page))
    # db_get_photos uses a reverse paging scheme; convert "newest page=1" into its index.
    page_index = max(0, total_pages - page_number)

    df = EventsRepo.db_get_photos(
        database, ReturnDataPhotosDB.all_except_photos, date_start_utc, date_end_utc,
        cats_only, mice_only, CONFIG["MOUSE_THRESHOLD"], page_index, per_page, rfid_filter=rfid,
    )

    cat_name_dict = CatsRepo.get_cat_name_rfid_dict(database)
    feedback_by_photo = (
        DetectionFeedbackRepo.get_for_photos(database, df["id"].tolist()) if overlay and not df.empty else {}
    )
    photos = [
        _serialize_picture(row, cat_name_dict, feedback_by_photo.get(int(row["id"]), {}))
        for __, row in df.iterrows()
    ]

    return JSONResponse(
        {
            "date": local_date.isoformat(),
            "page": page_number,
            "total_pages": total_pages,
            "total_count": total_count,
            "photos": photos,
            "correction_labels": CatsRepo.get_cat_names_list(database) + ["Prey"],
        }
    )


# 08.10, Sid ("tu as supprimé le mode groupé? il était assez utile"): the
# Shiny original's "Group pictures to events" switch (photos.py's
# button_events_view) swapped the flat grid for live_view.py's own
# ui_events_by_date table, filtered by the same date/cats/mice/cat-name
# filters as the flat grid - same underlying data as /app-api/v1/chronology,
# just day-scoped AND filterable the same way the flat grid already is.
from src.server_ui.journeys import _list_flap_events_for_date as _list_flap_events_for_date_filtered


@webui_app.get("/app-api/v1/pictures/events")
async def get_pictures_events(
    date: str | None = None,
    cats_only: bool = False,
    mice_only: bool = False,
    rfid: str = "",
    _user: str = Depends(require_session),
):
    try:
        local_date = (
            datetime.strptime(date, "%Y-%m-%d").date()
            if date
            else datetime.now(ZoneInfo(CONFIG["TIMEZONE"])).date()
        )
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid date, expected YYYY-MM-DD")

    date_start_utc, date_end_utc = _local_day_utc_range(local_date)
    raw_events = _list_flap_events_for_date_filtered(
        date_start_utc, date_end_utc,
        cats_only=cats_only, mouse_only=mice_only,
        mouse_probability=CONFIG["MOUSE_THRESHOLD"], rfid_filter=rfid,
    )
    cat_name_dict = CatsRepo.get_cat_name_rfid_dict(CONFIG["KITTYHACK_DATABASE_PATH"])
    events = _serialize_events(raw_events, cat_name_dict)

    return JSONResponse({"date": local_date.isoformat(), "events": events})


@webui_app.post("/app-api/v1/pictures/{pid}/correction")
async def post_picture_correction(pid: int, request: Request, _user: str = Depends(require_session)):
    database = CONFIG["KITTYHACK_DATABASE_PATH"]
    body = await request.json()
    try:
        obj_idx = int(body.get("idx"))
        action = str(body.get("action") or "")
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="invalid idx")
    corrected_name = body.get("correctedName")

    df = DatabaseCore.read_df_from_database(database, f"SELECT event_text FROM events WHERE id = {pid}")
    if df.empty:
        raise HTTPException(status_code=404, detail="unknown photo id")
    event_text = df.iloc[0]["event_text"]
    objs = EventsRepo.read_event_from_json(event_text) if event_text else []
    if obj_idx < 0 or obj_idx >= len(objs):
        raise HTTPException(status_code=400, detail="object index out of range")
    dobj = objs[obj_idx]

    if action == "confirm":
        confirmed, final_corrected_name = True, None
    elif action == "false_positive":
        confirmed, final_corrected_name = False, "none"
    elif action == "correct":
        if not corrected_name:
            raise HTTPException(status_code=400, detail="missing correctedName")
        confirmed, final_corrected_name = False, _canonical_cat_name(str(corrected_name))
    else:
        raise HTTPException(status_code=400, detail=f"unknown action: {action}")

    result = DetectionFeedbackRepo.upsert(
        database, photo_id=pid, object_index=obj_idx, original_name=dobj.object_name,
        original_probability=dobj.probability or 0, x=dobj.x, y=dobj.y, width=dobj.width,
        height=dobj.height, corrected_name=final_corrected_name, confirmed=confirmed,
    )
    if not result.success:
        raise HTTPException(status_code=500, detail=result.message)
    return JSONResponse({"ok": True})


@webui_app.post("/app-api/v1/pictures/{pid}/add-detection")
async def post_picture_add_detection(pid: int, request: Request, _user: str = Depends(require_session)):
    database = CONFIG["KITTYHACK_DATABASE_PATH"]
    body = await request.json()
    try:
        x, y, width, height = (float(body[k]) for k in ("x", "y", "width", "height"))
    except (KeyError, TypeError, ValueError):
        raise HTTPException(status_code=400, detail="invalid box")
    label = _canonical_cat_name(str(body.get("label") or "").strip())
    if not label:
        raise HTTPException(status_code=400, detail="missing label")

    df = DatabaseCore.read_df_from_database(database, f"SELECT event_text FROM events WHERE id = {pid}")
    if df.empty:
        raise HTTPException(status_code=404, detail="unknown photo id")
    event_text = df.iloc[0]["event_text"]
    objs = EventsRepo.read_event_from_json(event_text) if event_text else []

    existing_feedback = DetectionFeedbackRepo.get_for_photo(database, pid)
    new_idx = len(objs)
    while new_idx in existing_feedback:
        new_idx += 1

    result = DetectionFeedbackRepo.upsert(
        database, photo_id=pid, object_index=new_idx, original_name=None, original_probability=0,
        x=x, y=y, width=width, height=height, corrected_name=label, confirmed=False,
    )
    if not result.success:
        raise HTTPException(status_code=500, detail=result.message)
    return JSONResponse({"ok": True, "idx": new_idx})


# 25.10, Sid ("j'ai que deux photos qui sont venues de pookie en HK... faut
# que je corrige la forme de la boîte. Donc une fonction de transfert vers
# paws review") - l'ingestion automatique côté Paws Review (ingest.py) lit
# event_text en SÉQUENCE une seule fois (checkpoint jamais revisité) et ne
# relit jamais les corrections faites ici après coup - une boîte corrigée
# (fausse détection écartée + nouvelle boîte dessinée à la main) reste donc
# invisible côté Paws Review, ou y arrive déjà figée avec l'ancienne boîte
# fausse du modèle. Envoie explicitement l'état CORRIGÉ actuel (même
# reconstruction que _serialize_picture : reviewedAs appliqué, fausses
# détections exclues) - Paws Review n'a plus qu'à lire le fichier image lui-
# même (même volume partagé en lecture seule que son ingestion habituelle,
# voir compose.yaml) et upsert la capture avec ces boîtes.
@webui_app.post("/app-api/v1/pictures/{pid}/send-to-paws-review")
async def post_picture_send_to_paws_review(pid: int, _user: str = Depends(require_session)):
    database = CONFIG["KITTYHACK_DATABASE_PATH"]
    df = DatabaseCore.read_df_from_database(
        database,
        f"""SELECT id, block_id, created_at, mouse_probability, own_cat_probability, rfid, event_text
            FROM events WHERE id = {int(pid)}""",
    )
    if df.empty:
        raise HTTPException(status_code=404, detail="unknown photo id")
    row = df.iloc[0].to_dict()
    feedback = DetectionFeedbackRepo.get_for_photo(database, pid)
    cat_name_dict = CatsRepo.get_cat_name_rfid_dict(database)
    picture = _serialize_picture(row, cat_name_dict, feedback)

    boxes = []
    for obj in picture["objects"]:
        label = obj.get("reviewedAs") if obj.get("reviewed") else obj["name"]
        if not label or str(label).lower() == "none":
            continue  # fausse détection écartée, jamais envoyée
        boxes.append({
            "object_name": label,
            "probability": 0 if obj.get("manual") else obj["prob"],
            "x": obj["x"], "y": obj["y"], "width": obj["w"], "height": obj["h"],
        })

    payload = {
        "event_id": int(pid),
        "block_id": int(row["block_id"]),
        "created_at": str(row["created_at"]),
        "rfid": row.get("rfid") or None,
        # Déjà résolu ici (CatsRepo) - Paws Review n'a plus besoin de lire la
        # table cats de Kittyhack pour cette capture précise.
        "cat_name": cat_name_dict.get(row.get("rfid")) if row.get("rfid") else None,
        "mouse_probability": None if pd.isna(row.get("mouse_probability")) else float(row["mouse_probability"]),
        "own_cat_probability": None if pd.isna(row.get("own_cat_probability")) else float(row["own_cat_probability"]),
        "boxes": boxes,
    }
    try:
        # 25.10 : même constat que compose.yaml côté Paws Review
        # (KITTYHACK_API_URL) - deux projets docker compose séparés, donc
        # deux réseaux Docker distincts ; 127.0.0.1 ici ne route pas vers
        # l'hôte sur Linux, IP LAN de l'hôte nécessaire.
        resp = requests.post(
            "http://192.168.178.127:8082/app-api/ingest/kittyflap", json=payload, timeout=15,
            headers={"X-Upload-Token": os.environ.get("PAWS_REVIEW_UPLOAD_TOKEN", "")},
        )
        resp.raise_for_status()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Échec de l'envoi vers Paws Review : {e}")
    return JSONResponse(resp.json())


@webui_app.delete("/app-api/v1/pictures/{pid}")
async def delete_picture(pid: int, _user: str = Depends(require_session)):
    database = CONFIG["KITTYHACK_DATABASE_PATH"]
    result = EventsRepo.delete_photo_by_id(database, pid)
    if not result.success:
        raise HTTPException(status_code=500, detail=result.message)
    return JSONResponse({"ok": True})


@webui_app.get("/pictures")
@webui_app.get("/pictures/")
async def serve_pictures_page(_user: str = Depends(require_session)):
    return _serve_spa_index()


# ---------------------------------------------------------------------------
# Live view (camera snapshot + lock/motion status + manual override + quick
# settings + outdoor camera grid). The "last events"/"events by date" parts
# of the old Shiny page are deliberately NOT replicated here - they're a
# subset of what Chronology/Journey already show, see those instead.
# ---------------------------------------------------------------------------


@webui_app.get("/app-api/v1/live/frame.jpg")
async def get_live_frame(_user: str = Depends(require_session)):
    from src.backend import model_handler

    jpg = model_handler.get_camera_frame_jpg()
    if jpg is None:
        raise HTTPException(status_code=503, detail="no camera frame available")
    return Response(content=jpg, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


# 07.10, Sid ("y a plus le petit point de couleur qui dit si la chatière est
# en ligne ou pas" / "rouge = déconnecté et orange = en cours de connexion ?"
# / "pas oublier de reprendre... le statut connecté déconnecté qui est dans
# la barre de navigation"): same source of truth as the old navbar badge
# (session_setup.py's ui_remote_connection_badge / ctx._remote_connection_state).
# RemoteControlClient doesn't track an explicit "connecting" state, but
# is_manual_disconnect() tells apart a deliberate disconnect (red) from
# still-trying-to-reach-the-Pi (orange) - the client otherwise retries
# forever in the background whether this is the first attempt or a
# reconnect after a drop, so "not ready and not manually disconnected" is
# always a legitimate "connecting" state, never a dead end. Shared by
# /app-api/v1/live/status (polled on the Live page) and
# /app-api/v1/remote/status (polled globally by TopBanner).
def _remote_status() -> str:
    if not is_remote_mode():
        return "online"
    try:
        from src.remote.control_client import RemoteControlClient

        client = RemoteControlClient.instance()
        if bool(client.wait_until_ready(timeout=0)):
            return "online"
        if bool(getattr(client, "is_manual_disconnect", lambda: False)()):
            return "offline"
        return "connecting"
    except Exception:
        return "offline"


@webui_app.get("/app-api/v1/remote/status")
async def get_remote_status(_user: str = Depends(require_session)):
    return JSONResponse({"remote_status": _remote_status()})


@webui_app.post("/app-api/v1/remote/disconnect")
async def post_remote_disconnect(_user: str = Depends(require_session)):
    if not is_remote_mode():
        raise HTTPException(status_code=409, detail="not in remote mode")
    try:
        from src.remote.control_client import RemoteControlClient

        client = RemoteControlClient.instance()
        client.ensure_started()
        client.disconnect()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Échec de la déconnexion : {e}")
    return JSONResponse({"ok": True})


@webui_app.post("/app-api/v1/remote/reconnect")
async def post_remote_reconnect(_user: str = Depends(require_session)):
    if not is_remote_mode():
        raise HTTPException(status_code=409, detail="not in remote mode")
    try:
        from src.remote.control_client import RemoteControlClient

        client = RemoteControlClient.instance()
        client.reconnect()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Échec de la reconnexion : {e}")
    return JSONResponse({"ok": True})


@webui_app.get("/app-api/v1/live/status")
async def get_live_status(_user: str = Depends(require_session)):
    from src.backend import backend_main, motion_state, motion_state_lock

    remote_status = _remote_status()

    magnets = _magnets_class().instance
    if magnets is None:
        return JSONResponse({"ok": False, "remote_status": remote_status})

    with motion_state_lock:
        outside_motion = bool(motion_state["outside"])
        inside_motion = bool(motion_state["inside"])

    prey_detection_mono = float(getattr(backend_main, "prey_detection_mono", 0.0) or 0.0)
    time_until_release = 0.0
    forced_lock_due_prey = False
    if prey_detection_mono > 0.0:
        delta = _mono_time() - prey_detection_mono
        time_until_release = float(CONFIG["LOCK_DURATION_AFTER_PREY_DETECTION"]) - delta
        forced_lock_due_prey = time_until_release > 0

    return JSONResponse(
        {
            "ok": True,
            "inside_lock": bool(magnets.get_inside_state()),
            "outside_lock": bool(magnets.get_outside_state()),
            "inside_motion": inside_motion,
            "outside_motion": outside_motion,
            "forced_lock_due_prey": forced_lock_due_prey,
            "time_until_release": max(0.0, time_until_release),
            "remote_status": remote_status,
        }
    )


def _magnets_class():
    if is_remote_mode():
        from src.remote.hardware import Magnets  # type: ignore
    else:
        from src.magnets_rfid import Magnets
    return Magnets


def _mono_time() -> float:
    from src.clock import monotonic_time

    return monotonic_time()


@webui_app.post("/app-api/v1/live/toggle-inside")
async def post_live_toggle_inside(_user: str = Depends(require_session)):
    magnets = _magnets_class().instance
    if magnets is None:
        raise HTTPException(status_code=503, detail="hardware not initialized yet")
    _set_manual_override("lock_inside" if magnets.get_inside_state() else "unlock_inside")
    return JSONResponse({"ok": True})


@webui_app.post("/app-api/v1/live/reset-prey-cooldown")
async def post_reset_prey_cooldown(_user: str = Depends(require_session)):
    from src.backend import backend_main

    backend_main.prey_detection_tm = 0.0
    backend_main.prey_detection_mono = 0.0
    return JSONResponse({"ok": True})


@webui_app.get("/app-api/v1/live/mode")
async def get_live_mode(_user: str = Depends(require_session)):
    return JSONResponse(_current_mode())


@webui_app.post("/app-api/v1/live/mode")
async def post_live_mode(request: Request, _user: str = Depends(require_session)):
    body = await request.json()
    try:
        result = _apply_mode(body.get("entry"), body.get("exit"))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return JSONResponse(result)


def _list_latest_flap_events(limit: int) -> list[dict]:
    """Most recent `limit` motion blocks across all history, no date bound -
    same row shape as journeys.py's _list_flap_events_for_date(), just
    without the WHERE created_at BETWEEN clause (live_view.py's old
    get_events_table_html(block_count=N, ...) used the same unbounded
    default date range for exactly this "last events" teaser)."""
    database = CONFIG["KITTYHACK_DATABASE_PATH"]
    try:
        columns_info = DatabaseCore.read_column_info_from_database(database, "events")
        column_names = set(info[1] for info in (columns_info or []))
    except Exception:
        column_names = set()
    deleted_filter = " WHERE deleted != 1" if "deleted" in column_names else ""

    stmt = f"""
        SELECT block_id, MIN(id) AS first_id, MAX(created_at) AS created_at, event_type, rfid
        FROM events
        {deleted_filter}
        GROUP BY block_id
        ORDER BY block_id DESC
        LIMIT {int(limit)}
    """
    try:
        df = DatabaseCore.read_df_from_database(database, stmt)
    except Exception as e:
        logging.error(f"[LIVE] Failed to query latest flap events: {e}")
        return []

    events = []
    for __, row in df.iterrows():
        try:
            # 07.10: get_local_date_from_utc_date() can include fractional
            # seconds ("...18:26:02.1747"), which a plain "%H:%M:%S" strptime
            # format rejects outright - silently dropping every single row.
            # journeys.py's _list_flap_events_for_date() sidesteps this with
            # pd.to_datetime(), used here too for the same reason.
            local_str = DateTimeUtil.get_local_date_from_utc_date(row["created_at"])
            dt = pd.to_datetime(local_str).to_pydatetime().replace(tzinfo=None)
        except Exception:
            continue
        events.append(
            {
                "block_id": int(row["block_id"]),
                "first_id": int(row["first_id"]),
                "dt": dt,
                "event_type": row["event_type"],
                "rfid": row["rfid"],
            }
        )
    return events


@webui_app.get("/app-api/v1/live/last-event")
async def get_live_last_event(_user: str = Depends(require_session)):
    raw_events = _list_latest_flap_events(1)
    if not raw_events:
        return JSONResponse({"event": None})
    cat_name_dict = CatsRepo.get_cat_name_rfid_dict(CONFIG["KITTYHACK_DATABASE_PATH"])
    event = _serialize_events(raw_events, cat_name_dict)[0]
    return JSONResponse({"event": event})


@webui_app.get("/live-view")
@webui_app.get("/live-view/")
async def serve_live_view_page(_user: str = Depends(require_session)):
    return _serve_spa_index()


# ---------------------------------------------------------------------------
# Event modal (multi-frame playback + detection overlays + corrections) -
# 07.10, Sid ("avant j'avais la loupe et ça me donnait la vidéo et
# maintenant j'ai juste un instantané... c'est important ça"): real
# restoration of event_modal.py's player, not the plain single-photo
# Lightbox used elsewhere. Reuses the exact same DB helpers (EventsRepo,
# DetectionFeedbackRepo) show_event_server() does - only the rendering
# changes from a server-pushed Shiny modal to JSON + a React player.
# Deliberately skips event_modal.py's tar-bundle optimization (a perf-only
# shortcut batching every frame's JPEG into one tar fetch) - frames load
# individually from /thumb/<pid>.jpg here, same URLs Chronology/Journey
# already reference directly.
# ---------------------------------------------------------------------------


def _format_frame_ts(created_at) -> str:
    try:
        local_str = DateTimeUtil.get_local_date_from_utc_date(str(created_at))
        ts = pd.to_datetime(local_str)
        return ts.strftime("%H:%M:%S.%f")[:-5]  # HH:MM:SS.d (one decimal)
    except Exception:
        return ""


# 07.10, Sid ("oublie pas de reproduire les différents détails des
# mouvements quand on clique sur les petites icônes"): Chronology/Journey's
# rows never got the old events-table's click-to-expand sub-event timeline
# (motion detected -> cat identified -> entry allowed/denied -> door opened/
# closed, one line per backend decision) - live_view.py's get_events_table_html
# had it, the new React rows didn't. Same data source (motion_timeline table
# via EventsRepo.db_get_motion_timelines), same fallback for older rows with
# no stored timeline (a coarse one-line-per-comma-flag reconstruction from
# the raw event_type column).
@webui_app.get("/app-api/v1/events/{block_id}/timeline")
async def get_event_timeline(block_id: int, _user: str = Depends(require_session)):
    from src.event_timeline import (
        timeline_extract_latest_event,
        timeline_fallback_from_event_type,
        timeline_format_message,
        timeline_format_time,
    )

    database = CONFIG["KITTYHACK_DATABASE_PATH"]
    timelines_by_block = EventsRepo.db_get_motion_timelines(database, [block_id])
    entries = timelines_by_block.get(block_id)
    if entries:
        entries = timeline_extract_latest_event(entries)
    else:
        df = DatabaseCore.read_df_from_database(
            database,
            f"SELECT event_type, created_at FROM events WHERE block_id = {int(block_id)} ORDER BY created_at ASC LIMIT 1",
        )
        if df.empty:
            raise HTTPException(status_code=404, detail="unknown block_id")
        entries = timeline_fallback_from_event_type(
            df.iloc[0]["event_type"], CONFIG["TIMEZONE"], df.iloc[0]["created_at"]
        )

    tz = CONFIG["TIMEZONE"]
    formatted = [
        {"time": timeline_format_time(e.get("at", ""), tz), "message": timeline_format_message(e)}
        for e in entries
    ]
    return JSONResponse({"entries": formatted})


@webui_app.get("/app-api/v1/events/{block_id}")
async def get_event(block_id: int, _user: str = Depends(require_session)):
    database = CONFIG["KITTYHACK_DATABASE_PATH"]
    df = EventsRepo.db_get_photos_by_block_id(database, block_id, ReturnDataPhotosDB.all_except_photos)
    if df.empty:
        raise HTTPException(status_code=404, detail="event not found")

    df = df.sort_values("created_at")
    pids = [int(x) for x in df["id"].tolist()]
    fallback_mode = not any(bool(t) for t in df["event_text"].tolist())
    feedback_by_photo = DetectionFeedbackRepo.get_for_photos(database, pids)

    frames = []
    for __, row in df.iterrows():
        pid = int(row["id"])
        event_text = row.get("event_text")
        try:
            detected = EventsRepo.read_event_from_json(event_text) if event_text else []
        except Exception:
            detected = []
        photo_feedback = feedback_by_photo.get(pid, {})
        objects = []
        for idx, dobj in enumerate(detected):
            name = (dobj.object_name or "").strip()
            if name.lower() == "false-accept":
                continue
            obj = {
                "x": round(dobj.x, 2),
                "y": round(dobj.y, 2),
                "w": round(dobj.width, 2),
                "h": round(dobj.height, 2),
                "name": name,
                "prob": round(dobj.probability, 1) if dobj.probability else 0,
                "idx": idx,
            }
            fb = photo_feedback.get(idx)
            if fb is not None:
                obj["reviewed"] = True
                obj["reviewedAs"] = fb.get("original_name") if fb.get("confirmed") else fb.get("corrected_name")
            objects.append(obj)
        frames.append(
            {
                "pid": pid,
                "ts": _format_frame_ts(row["created_at"]),
                "thumb_url": f"/thumb/{pid}.jpg",
                "objects": objects,
            }
        )

    # FPS: stored effective_fps column first, else approximate from the
    # frames' own timestamp span - same fallback event_modal.py uses.
    fps = None
    if "effective_fps" in df.columns:
        for v in df["effective_fps"].tolist():
            if v is not None and not pd.isna(v) and float(v) > 0:
                fps = float(v)
                break
    if fps is None and len(frames) >= 2:
        try:
            ts = pd.to_datetime(df["created_at"], errors="coerce").dropna()
            span = (ts.max() - ts.min()).total_seconds()
            if span and span > 0:
                fps = max(0.1, min(30.0, (len(frames) - 1) / span))
        except Exception:
            fps = None

    aspect = None
    if "img_width" in df.columns and "img_height" in df.columns:
        for __, row in df.iterrows():
            w, h = row.get("img_width"), row.get("img_height")
            if w and h:
                try:
                    aspect = f"{int(w)} / {int(h)}"
                    break
                except Exception:
                    pass

    correction_labels = CatsRepo.get_cat_names_list(database) + ["Prey"]

    # 10.10, Sid ("deux vidéos côte à côte... avec les incrustations
    # aussi... que je voie comment ça détecte les différences de chats par
    # rapport à celle de la PI"): the dense chatiere (Reolink) sequence for
    # this same event, padded around the flap camera's own [first, last]
    # photo span - converted to local time since watchdog.py's filenames
    # are local (same assumption journeys.py's own correlation already
    # makes for the sparse training-sample thumbnails).
    reolink_frames = []
    try:
        created_ats = pd.to_datetime(df["created_at"], errors="coerce").dropna()
        if len(created_ats) > 0:
            first_local = pd.to_datetime(DateTimeUtil.get_local_date_from_utc_date(str(created_ats.min())))
            last_local = pd.to_datetime(DateTimeUtil.get_local_date_from_utc_date(str(created_ats.max())))
            window_start, window_end = _extended_event_frame_window(
                first_local.to_pydatetime() - timedelta(seconds=_EVENT_FRAME_PAD_S),
                last_local.to_pydatetime() + timedelta(seconds=_EVENT_FRAME_PAD_S),
            )
            reolink_frames = _list_event_frames(window_start, window_end)
    except Exception as e:
        logging.warning(f"[WEBUI] Could not list Reolink event frames for block {block_id}: {e}")

    # 10.10, Sid ("reolink dure 1 seconde"): chatiere's own capture cadence
    # (watchdog.py's boost window, ~1.5-8s between frames) is much sparser
    # than the flap camera's own, and its span often doesn't match the
    # flap event's span either (padding, network gaps) - sharing the main
    # panel's `fps` made the far-fewer Reolink frames blip past almost
    # instantly. Computed the same way as the main `fps` above, just from
    # the Reolink frames' own timestamps instead.
    reolink_fps = 2.0
    if len(reolink_frames) >= 2:
        try:
            r_ts = [datetime.strptime(f["ts"], "%H:%M:%S.%f") for f in reolink_frames]
            span = (r_ts[-1] - r_ts[0]).total_seconds()
            if span and span > 0:
                reolink_fps = max(0.2, min(10.0, (len(reolink_frames) - 1) / span))
        except Exception:
            reolink_fps = 2.0

    return JSONResponse(
        {
            "block_id": block_id,
            "fps": fps or 4.0,
            "reolink_fps": reolink_fps,
            "fallback_mode": fallback_mode,
            "overlay_initial": bool(int(CONFIG.get("SHOW_IMAGES_WITH_OVERLAY", True))) and not fallback_mode,
            "mouse_threshold": float(CONFIG.get("MOUSE_THRESHOLD", 50)),
            "correction_labels": correction_labels,
            "aspect": aspect,
            "frames": frames,
            "reolink_frames": reolink_frames,
        }
    )


@webui_app.post("/app-api/v1/events/{block_id}/correction")
async def post_event_correction(block_id: int, request: Request, _user: str = Depends(require_session)):
    body = await request.json()
    pid = int(body.get("pid", 0))
    idx = int(body.get("idx", -1))
    action = str(body.get("action") or "")
    corrected_name = body.get("correctedName")

    database = CONFIG["KITTYHACK_DATABASE_PATH"]
    df = DatabaseCore.read_df_from_database(
        database, f"SELECT event_text FROM events WHERE id = {pid} AND block_id = {block_id}"
    )
    if df.empty:
        raise HTTPException(status_code=404, detail="photo not found in this event")
    event_text = df.iloc[0]["event_text"]
    try:
        objs = EventsRepo.read_event_from_json(event_text) if event_text else []
    except Exception:
        objs = []
    if idx < 0 or idx >= len(objs):
        raise HTTPException(status_code=400, detail="object index out of range")
    dobj = objs[idx]

    if action == "confirm":
        confirmed, final_corrected_name = True, None
    elif action == "false_positive":
        confirmed, final_corrected_name = False, "none"
    elif action == "correct":
        if not corrected_name:
            raise HTTPException(status_code=400, detail="missing correctedName")
        confirmed, final_corrected_name = False, _canonical_cat_name(str(corrected_name))
    else:
        raise HTTPException(status_code=400, detail=f"unknown action: {action}")

    result = DetectionFeedbackRepo.upsert(
        database,
        photo_id=pid,
        object_index=idx,
        original_name=dobj.object_name,
        original_probability=dobj.probability or 0,
        x=dobj.x, y=dobj.y, width=dobj.width, height=dobj.height,
        corrected_name=final_corrected_name,
        confirmed=confirmed,
    )
    if not result.success:
        raise HTTPException(status_code=500, detail=result.message)
    return JSONResponse({"ok": True})


@webui_app.post("/app-api/v1/events/{block_id}/correction/bulk")
async def post_event_correction_bulk(block_id: int, request: Request, _user: str = Depends(require_session)):
    """11.10, Sid ("une correction globale en plus par bloc... c'est avec
    ca que je peux entrainer le modele"): same confirm/correct/false-
    positive actions as post_event_correction above, but applied to every
    cat-identity detection across every photo in the block in one call -
    never Prey (a different kind of detection, not an identity mislabel).
    Pairs with the loop.py majority-vote fix (same day) that already
    stabilizes the block-level identity used for door decisions - this
    does the equivalent for the training-review workflow, which until now
    still required one correction per noisy frame."""
    body = await request.json()
    action = str(body.get("action") or "")
    corrected_name = body.get("correctedName")

    database = CONFIG["KITTYHACK_DATABASE_PATH"]
    df = EventsRepo.db_get_photos_by_block_id(database, block_id, ReturnDataPhotosDB.all_except_photos)
    if df.empty:
        raise HTTPException(status_code=404, detail="event not found")

    if action == "correct":
        if not corrected_name:
            raise HTTPException(status_code=400, detail="missing correctedName")
        corrected_name = _canonical_cat_name(str(corrected_name))
    elif action not in ("confirm", "false_positive"):
        raise HTTPException(status_code=400, detail=f"unknown action: {action}")

    applied = 0
    for __, row in df.iterrows():
        pid = int(row["id"])
        event_text = row.get("event_text")
        try:
            objs = EventsRepo.read_event_from_json(event_text) if event_text else []
        except Exception:
            objs = []
        for idx, dobj in enumerate(objs):
            name = (dobj.object_name or "").strip().lower()
            if not name or name in ("prey", "beute", "false-accept"):
                continue
            if action == "confirm":
                confirmed, final_corrected_name = True, None
            elif action == "false_positive":
                confirmed, final_corrected_name = False, "none"
            else:
                confirmed, final_corrected_name = False, corrected_name
            result = DetectionFeedbackRepo.upsert(
                database,
                photo_id=pid,
                object_index=idx,
                original_name=dobj.object_name,
                original_probability=dobj.probability or 0,
                x=dobj.x, y=dobj.y, width=dobj.width, height=dobj.height,
                corrected_name=final_corrected_name,
                confirmed=confirmed,
            )
            if result.success:
                applied += 1

    return JSONResponse({"ok": True, "applied": applied})


@webui_app.post("/app-api/v1/events/{block_id}/reolink-correction/bulk")
async def post_reolink_correction_bulk(block_id: int, request: Request, _user: str = Depends(require_session)):
    """Same bulk-correction idea as post_event_correction_bulk above, for
    the dense Reolink frame sequence (file-backed JSON sidecars, no DB
    table - see post_reolink_correction above for why)."""
    body = await request.json()
    action = str(body.get("action") or "")
    corrected_name = body.get("correctedName")

    database = CONFIG["KITTYHACK_DATABASE_PATH"]
    df = EventsRepo.db_get_photos_by_block_id(database, block_id, ReturnDataPhotosDB.all_except_photos)
    if df.empty:
        raise HTTPException(status_code=404, detail="event not found")

    if action == "correct":
        if not corrected_name:
            raise HTTPException(status_code=400, detail="missing correctedName")
        corrected_name = _canonical_cat_name(str(corrected_name))
    elif action not in ("confirm", "false_positive"):
        raise HTTPException(status_code=400, detail=f"unknown action: {action}")

    try:
        created_ats = pd.to_datetime(df["created_at"], errors="coerce").dropna()
        if created_ats.empty:
            raise HTTPException(status_code=404, detail="event not found")
        first_local = pd.to_datetime(DateTimeUtil.get_local_date_from_utc_date(str(created_ats.min())))
        last_local = pd.to_datetime(DateTimeUtil.get_local_date_from_utc_date(str(created_ats.max())))
        window_start, window_end = _extended_event_frame_window(
            first_local.to_pydatetime() - timedelta(seconds=_EVENT_FRAME_PAD_S),
            last_local.to_pydatetime() + timedelta(seconds=_EVENT_FRAME_PAD_S),
        )
        reolink_frames = _list_event_frames(window_start, window_end)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"could not list reolink frames: {e}")

    applied = 0
    for frame in reolink_frames:
        json_path = _reolink_json_path(frame["filename"])
        if not json_path or not os.path.exists(json_path):
            continue
        try:
            with open(json_path, "r", encoding="utf-8") as f:
                detections = json.load(f)
        except Exception:
            continue
        changed = False
        for det in detections:
            name = str(det.get("name") or "").strip().lower()
            if not name or name in ("prey", "beute"):
                continue
            det["reviewed"] = True
            if action == "confirm":
                final_label = _canonical_cat_name(str(det.get("name") or ""))
            elif action == "false_positive":
                final_label = None
            else:
                final_label = corrected_name
            det["reviewed_as"] = final_label if final_label else "none"
            changed = True
            applied += 1
        if changed:
            with open(json_path, "w", encoding="utf-8") as f:
                json.dump(detections, f)

    return JSONResponse({"ok": True, "applied": applied})


@webui_app.delete("/app-api/v1/events/{block_id}")
async def delete_event(block_id: int, _user: str = Depends(require_session)):
    database = CONFIG["KITTYHACK_DATABASE_PATH"]
    result = EventsRepo.delete_photos_by_block_id(database, block_id)
    if not result.success:
        raise HTTPException(status_code=500, detail=result.message)
    return JSONResponse({"ok": True})


def _load_original_image_bytes(pid: int) -> bytes | None:
    try:
        fp = os.path.join(pictures_original_dir(), f"{pid}.jpg")
        if os.path.exists(fp):
            with open(fp, "rb") as f:
                return f.read()
    except Exception:
        pass
    try:
        database = CONFIG["KITTYHACK_DATABASE_PATH"]
        df = DatabaseCore.read_df_from_database(database, f"SELECT original_image FROM events WHERE id = {pid}")
        if not df.empty:
            ob = df.iloc[0].get("original_image")
            if isinstance(ob, (bytes, bytearray)) and len(ob) > 0:
                return bytes(ob)
    except Exception:
        pass
    return None


@webui_app.get("/app-api/v1/events/{block_id}/download/{pid}")
async def download_event_photo(block_id: int, pid: int, _user: str = Depends(require_session)):
    img_bytes = _load_original_image_bytes(pid)
    if img_bytes is None:
        raise HTTPException(status_code=404, detail="no image bytes")
    return Response(
        content=img_bytes,
        media_type="image/jpeg",
        headers={"Content-Disposition": f'attachment; filename="kittyhack_event_{block_id}_{pid}.jpg"'},
    )


@webui_app.get("/app-api/v1/events/{block_id}/download-zip")
async def download_event_zip(block_id: int, _user: str = Depends(require_session)):
    database = CONFIG["KITTYHACK_DATABASE_PATH"]
    df = EventsRepo.db_get_photos_by_block_id(database, block_id, ReturnDataPhotosDB.all)
    files: list[tuple[str, bytes]] = []
    if not df.empty:
        for __, row in df.iterrows():
            pid = int(row["id"])
            try:
                local_dt_str = DateTimeUtil.get_local_date_from_utc_date(str(row["created_at"]))
                ts = pd.to_datetime(local_dt_str, errors="coerce")
                ts_str = ts.strftime("%Y%m%d_%H%M%S") if isinstance(ts, pd.Timestamp) else "unknown"
            except Exception:
                ts_str = "unknown"
            img_bytes = _load_original_image_bytes(pid)
            if img_bytes is None:
                ob = row.get("original_image")
                if isinstance(ob, (bytes, bytearray)) and len(ob) > 0:
                    img_bytes = bytes(ob)
            if img_bytes:
                files.append((f"{pid}_{ts_str}.jpg", img_bytes))
    if not files:
        files.append(("README.txt", b"No images are available for this event.\n"))

    import io

    buf = io.BytesIO()
    with _zipfile.ZipFile(buf, "w", compression=_zipfile.ZIP_DEFLATED) as zf:
        for name, data in files:
            zf.writestr(name, data)
    return Response(
        content=buf.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="kittyhack_event_{block_id}.zip"'},
    )


# ---------------------------------------------------------------------------
# Presence (door controls + per-cat cards/detail/stats)
# ---------------------------------------------------------------------------

def _cat_location(rfid: str | None) -> dict:
    """(location, since_iso) for one cat - "in"/"out"/"unknown", mirrors
    presence.py's ui_presence_cats()/ui_presence_detail() badge logic."""
    if not rfid:
        return {"location": "unknown", "since": None}
    event_type, created_at = _latest_presence_for_rfid(CONFIG["KITTYHACK_DATABASE_PATH"], rfid)
    if event_type is None:
        return {"location": "unknown", "since": None}
    location = "in" if _event_type_first(event_type) in _INSIDE_EVENT_TYPES else "out"
    try:
        since = pd.to_datetime(created_at, utc=True).isoformat()
    except Exception:
        since = None
    return {"location": location, "since": since}


@webui_app.get("/app-api/v1/presence/doors")
async def get_presence_doors(_user: str = Depends(require_session)):
    return JSONResponse(_door_state())


_DOOR_OVERRIDE_KEYS = {"unlock_inside", "lock_inside", "unlock_outside", "lock_outside"}


@webui_app.post("/app-api/v1/presence/doors/{key}")
async def post_presence_door(key: str, _user: str = Depends(require_session)):
    if key not in _DOOR_OVERRIDE_KEYS:
        raise HTTPException(status_code=400, detail=f"unknown override key: {key}")
    try:
        _set_manual_override(key)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return JSONResponse(_door_state())


@webui_app.get("/app-api/v1/presence/cats")
async def get_presence_cats(_user: str = Depends(require_session)):
    database = CONFIG["KITTYHACK_DATABASE_PATH"]
    df_cats = CatsRepo.db_get_cats(database, ReturnDataCatDB.all)
    cats = []
    for __, row in df_cats.iterrows():
        rfid = row["rfid"]
        loc = _cat_location(rfid)
        cats.append(
            {
                "rfid": rfid,
                "name": row["name"] or "Inconnu",
                "photo_url": f"/cat-photo/{rfid}.jpg" if rfid and row["cat_image"] else None,
                **loc,
            }
        )
    return JSONResponse({"cats": cats})


@webui_app.get("/app-api/v1/presence/cats/{rfid}")
async def get_presence_cat_detail(
    rfid: str, date: str | None = None, _user: str = Depends(require_session)
):
    database = CONFIG["KITTYHACK_DATABASE_PATH"]
    df_cats = CatsRepo.db_get_cats(database, ReturnDataCatDB.all)
    match = df_cats[df_cats["rfid"] == rfid]
    if match.empty:
        raise HTTPException(status_code=404, detail="unknown cat")
    row = match.iloc[0]

    try:
        local_date = (
            datetime.strptime(date, "%Y-%m-%d").date()
            if date
            else datetime.now(ZoneInfo(CONFIG["TIMEZONE"])).date()
        )
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid date, expected YYYY-MM-DD")

    loc = _cat_location(rfid)

    last_entry_utc, __ = _last_entry_for_rfid(database, rfid)
    last_entry_iso = None
    if last_entry_utc is not None:
        try:
            last_entry_iso = pd.to_datetime(last_entry_utc, utc=True).isoformat()
        except Exception:
            last_entry_iso = None

    last_exit_utc, __ = _last_exit_for_rfid(database, rfid)
    last_exit_iso = None
    if last_exit_utc is not None:
        try:
            last_exit_iso = pd.to_datetime(last_exit_utc, utc=True).isoformat()
        except Exception:
            last_exit_iso = None

    events_df = _presence_events_for_rfid(database, rfid)
    entries_count, seconds_outside, exits_count = _day_stats_for_events(events_df, local_date)
    glances_count, prey_blocked_count = _day_glance_prey_counts(database, rfid, local_date)

    # Click-to-correct (house/tree badge): the opposite of the current
    # known state, same reasoning as presence.py's status_icon_attrs.
    correction_target = None
    if loc["location"] == "in":
        correction_target = str(EventType.CAT_WENT_OUTSIDE)
    elif loc["location"] == "out":
        correction_target = str(EventType.CAT_WENT_INSIDE)

    return JSONResponse(
        {
            "rfid": rfid,
            "name": row["name"] or "Inconnu",
            "photo_url": f"/cat-photo/{rfid}.jpg" if row["cat_image"] else None,
            "location": loc["location"],
            "since": loc["since"],
            "last_entry": last_entry_iso,
            "last_exit": last_exit_iso,
            "date": local_date.isoformat(),
            "correction_target": correction_target,
            "stats": {
                "entries": entries_count,
                "exits": exits_count,
                "glances": glances_count,
                "prey_blocked": prey_blocked_count,
                "seconds_outside": seconds_outside,
                "seconds_outside_label": _format_duration(seconds_outside),
            },
        }
    )


@webui_app.post("/app-api/v1/presence/cats/{rfid}/correct")
async def post_presence_cat_correct(
    rfid: str, request: Request, _user: str = Depends(require_session)
):
    body = await request.json()
    event_type = body.get("event_type")
    if event_type not in (str(EventType.CAT_WENT_OUTSIDE), str(EventType.CAT_WENT_INSIDE)):
        raise HTTPException(status_code=400, detail="invalid event_type")
    ok = _insert_manual_presence_event(CONFIG["KITTYHACK_DATABASE_PATH"], rfid, event_type)
    if not ok:
        raise HTTPException(status_code=500, detail="failed to insert correction")
    return JSONResponse({"ok": True})


_PRESENCE_EVENTS_FILTERS = {"in_out", "glances", "prey_blocked", "all"}


@webui_app.get("/app-api/v1/presence/cats/{rfid}/events")
async def get_presence_cat_events(
    rfid: str, filter: str = "all", _user: str = Depends(require_session)
):
    if filter not in _PRESENCE_EVENTS_FILTERS:
        raise HTTPException(status_code=400, detail="invalid filter")
    database = CONFIG["KITTYHACK_DATABASE_PATH"]
    raw_events = _list_events_for_rfid_filtered(database, rfid, filter)
    cat_name_dict = CatsRepo.get_cat_name_rfid_dict(database)
    events = _serialize_events(raw_events, cat_name_dict)
    return JSONResponse({"events": events})


_FR_DAY_ABBR = ["lu", "ma", "me", "je", "ve", "sa", "di"]


@webui_app.get("/app-api/v1/presence/cats/{rfid}/monthly-prey")
async def get_presence_cat_monthly_prey(rfid: str, _user: str = Depends(require_session)):
    database = CONFIG["KITTYHACK_DATABASE_PATH"]
    months = _monthly_prey_counts_for_rfid(database, rfid, months=6)
    total = sum(m["count"] for m in months)
    return JSONResponse({"months": months, "total": total})


@webui_app.get("/app-api/v1/presence/cats/{rfid}/weekly")
async def get_presence_cat_weekly(
    rfid: str, offset: int = 0, _user: str = Depends(require_session)
):
    database = CONFIG["KITTYHACK_DATABASE_PATH"]
    tz = ZoneInfo(CONFIG["TIMEZONE"])
    today_local = datetime.now(tz).date()
    window_end = today_local - timedelta(days=7 * max(0, offset))
    window_start = window_end - timedelta(days=6)
    days = [window_start + timedelta(days=i) for i in range(7)]

    events_df = _presence_events_for_rfid(database, rfid)
    prey_df = _presence_prey_blocks_for_rfid(database, rfid)

    total_entries = 0
    total_seconds_outside = 0
    total_prey_blocked = 0
    day_rows = []
    for d in days:
        entries_count, seconds_outside, __ = _day_stats_for_events(events_df, d)
        total_entries += entries_count
        total_seconds_outside += seconds_outside
        marks = _day_timeline_for_events(events_df, d)
        prey_marks = _day_timeline_for_events(prey_df, d)
        total_prey_blocked += len(prey_marks)

        ticks = [
            {
                "frac": frac,
                "type": "in" if is_inside else "out",
                "block_id": block_id,
                "thumb_url": f"/thumb/{block_id}.jpg" if block_id is not None else None,
            }
            for frac, is_inside, block_id in marks
        ] + [
            {
                "frac": frac,
                "type": "prey",
                "block_id": block_id,
                "thumb_url": f"/thumb/{block_id}.jpg" if block_id is not None else None,
            }
            for frac, __, block_id in prey_marks
        ]

        day_rows.append(
            {
                "date": d.isoformat(),
                "day_abbr": _FR_DAY_ABBR[d.weekday()],
                "entries": entries_count,
                "seconds_outside": seconds_outside,
                "seconds_outside_label": _format_duration(seconds_outside),
                "ticks": ticks,
            }
        )

    return JSONResponse(
        {
            "window_start": window_start.isoformat(),
            "window_end": window_end.isoformat(),
            "offset": max(0, offset),
            "days": day_rows,
            "totals": {
                "avg_entries": round(total_entries / len(days), 1),
                "avg_seconds_outside": round(total_seconds_outside / len(days)),
                "avg_seconds_outside_label": _format_duration(round(total_seconds_outside / len(days))),
                "prey_blocked": total_prey_blocked,
            },
        }
    )


# ---------------------------------------------------------------------------
# Cat settings (name/RFID/photo/door-access switches/delete) - 07.10, Sid
# ("l'édition des paramètres par chat doit rester accessible... y a des
# paramètres de réglage de droit avec la chatière"): real page now (not
# merged inline below the stat tiles like the old Shiny version), reusing
# the exact same DB helpers cats.py's manage_cat_save() calls.
# ---------------------------------------------------------------------------

_RFID_RE = re.compile(r"^[0-9A-Fa-f]{16}$")


def _cat_row_by_rfid(df, rfid: str):
    match = df[df["rfid"] == rfid]
    if match.empty:
        raise HTTPException(status_code=404, detail="unknown cat")
    return match.iloc[0]


@webui_app.get("/app-api/v1/presence/cats/{rfid}/settings")
async def get_cat_settings(rfid: str, _user: str = Depends(require_session)):
    from src.baseconfig import AllowedToEnter, AllowedToExit

    database = CONFIG["KITTYHACK_DATABASE_PATH"]
    df = CatsRepo.db_get_cats(database, ReturnDataCatDB.all)
    row = _cat_row_by_rfid(df, rfid)

    return JSONResponse(
        {
            "id": int(row["id"]),
            "name": row["name"],
            "rfid": row["rfid"],
            "photo_url": f"/cat-photo/{rfid}.jpg" if row["cat_image"] else None,
            "enable_prey_detection": bool(int(row.get("enable_prey_detection", 1))),
            "block_exit_after_prey": bool(int(row.get("block_exit_after_prey", 1))),
            "allow_entry": bool(int(row.get("allow_entry", 1))),
            "allow_exit": bool(int(row.get("allow_exit", 1))),
            # Mirrors cats.py's entry_mode_per_cat/exit_mode_per_cat/prey_style
            # gating - the switch stays visible but disabled (same reasoning
            # as the Shiny card) when the global mode doesn't allow per-cat
            # control, rather than hiding it outright.
            "entry_editable": CONFIG["ALLOWED_TO_ENTER"] == AllowedToEnter.CONFIGURE_PER_CAT,
            "exit_editable": CONFIG["ALLOWED_TO_EXIT"] == AllowedToExit.CONFIGURE_PER_CAT,
            "prey_editable": bool(CONFIG["MOUSE_CHECK_ENABLED"]),
            "block_exit_editable": bool(CONFIG["BLOCK_EXIT_AFTER_PREY_ENTRY_ENABLED"]),
            "bio": {
                "species": row.get("species") or "",
                "sex": row.get("sex") or "",
                "sterilized": None if pd.isna(row.get("sterilized")) else bool(int(row.get("sterilized"))),
                "birth_date": row.get("birth_date") or "",
                "weight_kg": None if pd.isna(row.get("weight_kg")) else float(row.get("weight_kg")),
                "breed_primary": row.get("breed_primary") or "",
                "breed_secondary": row.get("breed_secondary") or "",
                "bio_comments": row.get("bio_comments") or "",
            },
        }
    )


@webui_app.post("/app-api/v1/presence/cats/{rfid}/settings")
async def post_cat_settings(rfid: str, request: Request, _user: str = Depends(require_session)):
    database = CONFIG["KITTYHACK_DATABASE_PATH"]
    df = CatsRepo.db_get_cats(database, ReturnDataCatDB.all)
    row = _cat_row_by_rfid(df, rfid)
    cat_id = int(row["id"])

    body = await request.json()
    new_name = str(body.get("name", row["name"])).strip()
    new_rfid = str(body.get("rfid", row["rfid"])).strip().upper()
    if new_rfid and not _RFID_RE.match(new_rfid):
        raise HTTPException(
            status_code=400, detail="RFID invalide : 16 caractères hexadécimaux (0-9, A-F) attendus"
        )
    enable_prey = bool(body.get("enable_prey_detection", row.get("enable_prey_detection", 1)))
    allow_entry = bool(body.get("allow_entry", row.get("allow_entry", 1)))
    allow_exit = bool(body.get("allow_exit", row.get("allow_exit", 1)))
    block_exit_after_prey = bool(
        body.get("block_exit_after_prey", row.get("block_exit_after_prey", 1))
    )

    result = CatsRepo.db_update_cat_data_by_id(
        database, cat_id, new_name, new_rfid, None, enable_prey, allow_entry, allow_exit
    )
    if not result.success:
        raise HTTPException(status_code=500, detail=result.message)
    CatsRepo.set_block_exit_after_prey(database, cat_id, block_exit_after_prey)

    bio = body.get("bio")
    if isinstance(bio, dict):
        CatsRepo.set_bio(database, cat_id, bio)

    return JSONResponse({"ok": True, "rfid": new_rfid})


@webui_app.post("/app-api/v1/presence/cats/{rfid}/photo")
async def post_cat_photo(rfid: str, request: Request, _user: str = Depends(require_session)):
    database = CONFIG["KITTYHACK_DATABASE_PATH"]
    df = CatsRepo.db_get_cats(database, ReturnDataCatDB.all)
    row = _cat_row_by_rfid(df, rfid)
    cat_id = int(row["id"])

    form = await request.form()
    upload = form.get("photo")
    if upload is None:
        raise HTTPException(status_code=400, detail="missing 'photo' file")

    import tempfile

    suffix = os.path.splitext(upload.filename or "")[1] or ".jpg"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(await upload.read())
        tmp_path = tmp.name
    try:
        result = CatsRepo.db_update_cat_data_by_id(
            database,
            cat_id,
            row["name"],
            row["rfid"],
            tmp_path,
            bool(int(row.get("enable_prey_detection", 1))),
            bool(int(row.get("allow_entry", 1))),
            bool(int(row.get("allow_exit", 1))),
        )
    finally:
        os.unlink(tmp_path)
    if not result.success:
        raise HTTPException(status_code=500, detail=result.message)
    return JSONResponse({"ok": True})


@webui_app.delete("/app-api/v1/presence/cats/{rfid}")
async def delete_cat(rfid: str, _user: str = Depends(require_session)):
    database = CONFIG["KITTYHACK_DATABASE_PATH"]
    df = CatsRepo.db_get_cats(database, ReturnDataCatDB.all)
    row = _cat_row_by_rfid(df, rfid)
    result = CatsRepo.db_delete_cat_by_id(database, int(row["id"]))
    if not result.success:
        raise HTTPException(status_code=500, detail=result.message)
    return JSONResponse({"ok": True})


# ---------------------------------------------------------------------------
# Wallpaper gallery - 07.10, Sid ("choisir parmi des wallpaper, voir
# télécharger depuis smartphone, comme pour meelo"): small gallery of
# uploaded background images for the React pages, same shape as Meelo's
# mobile wallpaper picker (up to MAX_WALLPAPERS, pick one active, remove
# any). No resize/compression server-side - Pillow isn't even installed in
# this image, and the uploaded file is stored exactly as the browser sent it
# (same as the existing cat-photo upload).
# ---------------------------------------------------------------------------

_WALLPAPER_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
_MAX_WALLPAPERS = 4


@webui_app.get("/app-api/v1/wallpaper")
async def get_wallpapers(_user: str = Depends(require_session)):
    d = wallpapers_dir()
    os.makedirs(d, exist_ok=True)
    files = sorted(
        (f for f in os.listdir(d) if os.path.splitext(f)[1].lower() in _WALLPAPER_EXTENSIONS),
        key=lambda f: os.path.getmtime(os.path.join(d, f)),
    )
    return JSONResponse(
        {
            "active": CONFIG.get("WALLPAPER_ACTIVE") or None,
            "images": [{"filename": f, "url": f"/app-api/v1/wallpaper/file/{f}"} for f in files],
        }
    )


@webui_app.get("/app-api/v1/wallpaper/file/{filename}")
async def get_wallpaper_file(filename: str, _user: str = Depends(require_session)):
    safe_name = os.path.basename(filename)
    path = os.path.join(wallpapers_dir(), safe_name)
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(path)


@webui_app.post("/app-api/v1/wallpaper")
async def post_wallpaper(request: Request, _user: str = Depends(require_session)):
    d = wallpapers_dir()
    os.makedirs(d, exist_ok=True)
    existing = [f for f in os.listdir(d) if os.path.splitext(f)[1].lower() in _WALLPAPER_EXTENSIONS]
    if len(existing) >= _MAX_WALLPAPERS:
        raise HTTPException(
            status_code=400,
            detail=f"Maximum {_MAX_WALLPAPERS} fonds d'écran - supprimez-en un d'abord.",
        )
    form = await request.form()
    upload = form.get("image")
    if upload is None:
        raise HTTPException(status_code=400, detail="missing 'image' file")
    ext = os.path.splitext(upload.filename or "")[1].lower()
    if ext not in _WALLPAPER_EXTENSIONS:
        ext = ".jpg"
    filename = f"{uuid.uuid4().hex}{ext}"
    with open(os.path.join(d, filename), "wb") as f:
        f.write(await upload.read())
    return JSONResponse({"ok": True, "filename": filename, "url": f"/app-api/v1/wallpaper/file/{filename}"})


@webui_app.post("/app-api/v1/wallpaper/active")
async def post_wallpaper_active(request: Request, _user: str = Depends(require_session)):
    body = await request.json()
    filename = str(body.get("filename") or "")
    if filename:
        safe_name = os.path.basename(filename)
        if not os.path.isfile(os.path.join(wallpapers_dir(), safe_name)):
            raise HTTPException(status_code=404, detail="unknown wallpaper")
        filename = safe_name
    CONFIG["WALLPAPER_ACTIVE"] = filename
    update_single_config_parameter("WALLPAPER_ACTIVE")
    return JSONResponse({"ok": True, "active": filename or None})


@webui_app.delete("/app-api/v1/wallpaper/{filename}")
async def delete_wallpaper(filename: str, _user: str = Depends(require_session)):
    safe_name = os.path.basename(filename)
    path = os.path.join(wallpapers_dir(), safe_name)
    if os.path.isfile(path):
        os.remove(path)
    if CONFIG.get("WALLPAPER_ACTIVE") == safe_name:
        CONFIG["WALLPAPER_ACTIVE"] = ""
        update_single_config_parameter("WALLPAPER_ACTIVE")
    return JSONResponse({"ok": True})


# 07.10, Sid ("encore de flou... je sais pas à combien tu es - rajoute un
# réglage dans apparence et je modifie moi"): after guessing twice (18px,
# then 14px) and still being off, a real slider in Apparence beats a third
# guess.
@webui_app.get("/app-api/v1/appearance/photo-blur")
async def get_photo_blur(_user: str = Depends(require_session)):
    return JSONResponse({"blur_px": int(CONFIG.get("PRESENCE_PHOTO_BLUR_PX", 10))})


@webui_app.post("/app-api/v1/appearance/photo-blur")
async def post_photo_blur(request: Request, _user: str = Depends(require_session)):
    body = await request.json()
    try:
        blur_px = max(0, min(40, int(body.get("blur_px"))))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="invalid blur_px")
    CONFIG["PRESENCE_PHOTO_BLUR_PX"] = blur_px
    update_single_config_parameter("PRESENCE_PHOTO_BLUR_PX")
    return JSONResponse({"ok": True, "blur_px": blur_px})


@webui_app.get("/appearance")
@webui_app.get("/appearance/")
async def serve_appearance_page(_user: str = Depends(require_session)):
    return _serve_spa_index()


# ---------------------------------------------------------------------------
# Configuration - migrated section by section (configuration.py is 3469
# lines, one giant "Save everything" handler for the whole Shiny page -
# deliberately NOT replicated that way here; each React section saves
# independently, same as the password/add-user handlers already were on the
# Shiny side). Low-risk sections first per the migration plan - "System"
# (reboot/update/tokens) last, since a bug there could break the ability to
# recover the app at all.
#
# General: language/timezone/date format, own password, add a user, push
# notification toggles, periodic version check, update repository.
# ---------------------------------------------------------------------------

_NOTIFY_KEYS = [
    "NOTIFY_CAT_ENTERED",
    "NOTIFY_CAT_EXITED",
    "NOTIFY_GLANCE_OUTSIDE",
    "NOTIFY_PREY_DETECTED",
    "NOTIFY_MOTION_OUTSIDE",
    "NOTIFY_MOTION_INSIDE",
]


@webui_app.get("/app-api/v1/config/general")
async def get_config_general(_user: str = Depends(require_session)):
    return JSONResponse(
        {
            "language": CONFIG["LANGUAGE"],
            "timezone": CONFIG["TIMEZONE"],
            "date_format": CONFIG["DATE_FORMAT"],
            "periodic_version_check": bool(CONFIG["PERIODIC_VERSION_CHECK"]),
            "update_repository_mode": CONFIG.get("UPDATE_REPOSITORY_MODE", "standard"),
            "update_repository": CONFIG.get("UPDATE_REPOSITORY", ""),
            "notify": {k: bool(CONFIG[k]) for k in _NOTIFY_KEYS},
        }
    )


@webui_app.post("/app-api/v1/config/general")
async def post_config_general(request: Request, _user: str = Depends(require_session)):
    body = await request.json()

    CONFIG["LANGUAGE"] = str(body.get("language") or CONFIG["LANGUAGE"])
    CONFIG["TIMEZONE"] = str(body.get("timezone") or CONFIG["TIMEZONE"])
    CONFIG["DATE_FORMAT"] = str(body.get("date_format") or CONFIG["DATE_FORMAT"])
    CONFIG["PERIODIC_VERSION_CHECK"] = bool(body.get("periodic_version_check", CONFIG["PERIODIC_VERSION_CHECK"]))

    update_mode = str(body.get("update_repository_mode") or "standard").strip().lower()
    if update_mode not in ("standard", "beta", "custom"):
        raise HTTPException(status_code=400, detail="invalid update_repository_mode")
    CONFIG["UPDATE_REPOSITORY_MODE"] = update_mode
    CONFIG["UPDATE_REPOSITORY"] = (
        str(body.get("update_repository") or "").strip() if update_mode == "custom" else ""
    )

    notify = body.get("notify") or {}
    for key in _NOTIFY_KEYS:
        if key in notify:
            CONFIG[key] = bool(notify[key])

    for key in (
        "LANGUAGE", "TIMEZONE", "DATE_FORMAT", "PERIODIC_VERSION_CHECK",
        "UPDATE_REPOSITORY_MODE", "UPDATE_REPOSITORY", *_NOTIFY_KEYS,
    ):
        update_single_config_parameter(key)

    set_language(CONFIG["LANGUAGE"])
    return JSONResponse({"ok": True})


@webui_app.post("/app-api/v1/config/password")
async def post_config_password(request: Request, username: str = Depends(require_session)):
    body = await request.json()
    current_pw = str(body.get("current_password") or "")
    new_pw = str(body.get("new_password") or "")
    new_pw2 = str(body.get("new_password2") or "")

    if not _check_credentials(username, current_pw):
        raise HTTPException(status_code=400, detail="Mot de passe actuel incorrect.")
    if len(new_pw) < 8:
        raise HTTPException(status_code=400, detail="Le nouveau mot de passe doit faire au moins 8 caractères.")
    if new_pw != new_pw2:
        raise HTTPException(status_code=400, detail="Les nouveaux mots de passe ne correspondent pas.")

    set_password(username, new_pw)
    return JSONResponse({"ok": True})


@webui_app.post("/app-api/v1/config/users")
async def post_config_add_user(request: Request, _user: str = Depends(require_session)):
    body = await request.json()
    new_username = str(body.get("username") or "").strip()
    new_pw = str(body.get("password") or "")
    new_pw2 = str(body.get("password2") or "")

    if not new_username:
        raise HTTPException(status_code=400, detail="Nom d'utilisateur manquant.")
    if username_exists(new_username):
        raise HTTPException(
            status_code=409,
            detail="Ce nom d'utilisateur existe déjà. Utilises-en un autre, ou change le mot de passe de cet utilisateur à la place.",
        )
    if len(new_pw) < 8:
        raise HTTPException(status_code=400, detail="Le mot de passe doit faire au moins 8 caractères.")
    if new_pw != new_pw2:
        raise HTTPException(status_code=400, detail="Les mots de passe ne correspondent pas.")

    set_password(new_username, new_pw)
    return JSONResponse({"ok": True})


# Camera: source (internal/IP), stream URL, advanced IP-camera pipeline
# (downscale/resolution/fps/hw-decode - remote-mode only, which this
# deployment always is).
@webui_app.get("/app-api/v1/config/camera")
async def get_config_camera(_user: str = Depends(require_session)):
    return JSONResponse(
        {
            "camera_source": CONFIG["CAMERA_SOURCE"],
            "ip_camera_url": CONFIG["IP_CAMERA_URL"],
            "enable_decode_scale_pipeline": bool(CONFIG.get("ENABLE_IP_CAMERA_DECODE_SCALE_PIPELINE", False)),
            "ip_camera_target_resolution": CONFIG.get("IP_CAMERA_TARGET_RESOLUTION", "640x360"),
            "ip_camera_fps_limit": int(CONFIG.get("IP_CAMERA_PIPELINE_FPS_LIMIT", 10)),
            "ip_camera_hw_decode": CONFIG.get("IP_CAMERA_HW_DECODE", "auto") or "auto",
            "is_remote_mode": is_remote_mode(),
        }
    )


@webui_app.post("/app-api/v1/config/camera")
async def post_config_camera(request: Request, _user: str = Depends(require_session)):
    import re as _re

    body = await request.json()
    camera_source = str(body.get("camera_source") or "internal")
    if camera_source not in ("internal", "ip_camera"):
        raise HTTPException(status_code=400, detail="invalid camera_source")
    ip_camera_url = str(body.get("ip_camera_url") or "")

    if camera_source == "ip_camera" and not _re.match(
        r"^(rtsp|http|https|rtmp|udp|tcp|file)://", ip_camera_url, _re.IGNORECASE
    ):
        raise HTTPException(
            status_code=400,
            detail="URL de flux invalide - doit commencer par rtsp://, http://, https://, rtmp://, udp://, tcp:// ou file://",
        )

    enable_pipeline = bool(body.get("enable_decode_scale_pipeline", False))
    if enable_pipeline:
        from src.system import DependencyInstaller

        if not DependencyInstaller.ensure_ffmpeg_installed():
            raise HTTPException(
                status_code=409,
                detail="FFmpeg est nécessaire pour le pipeline downscale mais n'a pas pu être installé automatiquement.",
            )

    CONFIG["CAMERA_SOURCE"] = camera_source
    CONFIG["IP_CAMERA_URL"] = ip_camera_url
    CONFIG["ENABLE_IP_CAMERA_DECODE_SCALE_PIPELINE"] = enable_pipeline
    CONFIG["IP_CAMERA_TARGET_RESOLUTION"] = str(body.get("ip_camera_target_resolution") or "640x360")
    CONFIG["IP_CAMERA_PIPELINE_FPS_LIMIT"] = int(body.get("ip_camera_fps_limit", 10))
    CONFIG["IP_CAMERA_HW_DECODE"] = str(body.get("ip_camera_hw_decode") or "auto")

    for key in (
        "CAMERA_SOURCE", "IP_CAMERA_URL", "ENABLE_IP_CAMERA_DECODE_SCALE_PIPELINE",
        "IP_CAMERA_TARGET_RESOLUTION", "IP_CAMERA_PIPELINE_FPS_LIMIT", "IP_CAMERA_HW_DECODE",
    ):
        update_single_config_parameter(key)

    return JSONResponse({"ok": True})


# Door control: detection thresholds, model selection, entry/exit modes +
# time ranges, PIR sensitivity. The biggest single section (886 lines in
# configuration.py) - the entry/exit decision-logic flowchart SVGs are
# NOT replicated here (pure illustration, not interactive).
@webui_app.get("/app-api/v1/config/models")
async def get_config_models(_user: str = Depends(require_session)):
    from src.model import YoloModel

    models: list[dict] = []
    try:
        for folder in os.listdir("./tflite"):
            if os.path.isdir(os.path.join("./tflite", folder)):
                models.append({"id": f"tflite::{folder}", "label": folder.replace("_", " ").title()})
    except Exception:
        pass
    try:
        for m in YoloModel.get_model_list():
            models.append({"id": f"yolo::{m['unique_id']}", "label": m["full_display_name"]})
    except Exception:
        pass
    return JSONResponse({"models": models})


@webui_app.get("/app-api/v1/config/door-control")
async def get_config_door_control(_user: str = Depends(require_session)):
    from src.baseconfig import AllowedToEnter, AllowedToExit

    selected_model = (
        f"tflite::{CONFIG['TFLITE_MODEL_VERSION']}"
        if CONFIG.get("TFLITE_MODEL_VERSION")
        else f"yolo::{CONFIG['YOLO_MODEL']}"
    )
    return JSONResponse(
        {
            "min_threshold": float(CONFIG["MIN_THRESHOLD"]),
            "mouse_threshold": float(CONFIG["MOUSE_THRESHOLD"]),
            "cat_threshold": float(CONFIG["CAT_THRESHOLD"]),
            "min_seconds_to_analyze": float(CONFIG.get("MIN_SECONDS_TO_ANALYZE", 1.5) or 1.5),
            "lock_duration_after_prey_detection": int(CONFIG["LOCK_DURATION_AFTER_PREY_DETECTION"]),
            "mouse_check_enabled": bool(CONFIG["MOUSE_CHECK_ENABLED"]),
            "block_exit_after_prey_entry_enabled": bool(CONFIG["BLOCK_EXIT_AFTER_PREY_ENTRY_ENABLED"]),
            "block_exit_after_prey_entry_duration": int(CONFIG["BLOCK_EXIT_AFTER_PREY_ENTRY_DURATION"]),
            "selected_model": selected_model,
            "use_camera_for_cat_detection": bool(CONFIG["USE_CAMERA_FOR_CAT_DETECTION"]),
            "use_camera_for_motion_detection": bool(CONFIG["USE_CAMERA_FOR_MOTION_DETECTION"]),
            "combine_pir_and_camera_outside_motion": bool(CONFIG["COMBINE_PIR_AND_CAMERA_OUTSIDE_MOTION"]),
            "pause_camera_when_idle": bool(CONFIG["PAUSE_CAMERA_WHEN_IDLE"]),
            "allowed_to_enter": CONFIG["ALLOWED_TO_ENTER"].value,
            "allowed_to_exit": CONFIG["ALLOWED_TO_EXIT"].value,
            "exit_ranges": [
                {
                    "enabled": bool(CONFIG[f"ALLOWED_TO_EXIT_RANGE{i}"]),
                    "from": CONFIG[f"ALLOWED_TO_EXIT_RANGE{i}_FROM"],
                    "to": CONFIG[f"ALLOWED_TO_EXIT_RANGE{i}_TO"],
                }
                for i in (1, 2, 3)
            ],
            "pir_inside_threshold": float(CONFIG["PIR_INSIDE_THRESHOLD"]),
            "immediate_lock_after_passage": bool(CONFIG["IMMEDIATE_LOCK_AFTER_PASSAGE"]),
        }
    )


_TIME_HHMM_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


@webui_app.post("/app-api/v1/config/door-control")
async def post_config_door_control(request: Request, _user: str = Depends(require_session)):
    from src.baseconfig import AllowedToEnter, AllowedToExit

    body = await request.json()

    allowed_to_enter = str(body.get("allowed_to_enter") or "")
    allowed_to_exit = str(body.get("allowed_to_exit") or "")
    try:
        AllowedToEnter(allowed_to_enter)
        AllowedToExit(allowed_to_exit)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid allowed_to_enter/allowed_to_exit")

    exit_ranges = body.get("exit_ranges") or []
    if len(exit_ranges) != 3:
        raise HTTPException(status_code=400, detail="exit_ranges must have exactly 3 entries")
    for r in exit_ranges:
        for field in ("from", "to"):
            v = str(r.get(field) or "00:00")
            if not _TIME_HHMM_RE.match(v):
                raise HTTPException(status_code=400, detail=f"invalid time '{v}', expected HH:MM")

    # 07.10: "ensure only one of the two is set" - same as the Shiny save
    # handler's own comment, otherwise a stale TFLITE_MODEL_VERSION/YOLO_MODEL
    # from a previous selection could silently win over the new one wherever
    # the backend picks which of the two to load.
    selected_model = str(body.get("selected_model") or "")
    if selected_model.startswith("tflite::"):
        CONFIG["TFLITE_MODEL_VERSION"] = selected_model.split("::", 1)[1]
        CONFIG["YOLO_MODEL"] = ""
    elif selected_model.startswith("yolo::"):
        CONFIG["YOLO_MODEL"] = selected_model.split("::", 1)[1]
        CONFIG["TFLITE_MODEL_VERSION"] = ""

    CONFIG["MIN_THRESHOLD"] = float(body.get("min_threshold", CONFIG["MIN_THRESHOLD"]))
    CONFIG["MOUSE_THRESHOLD"] = float(body.get("mouse_threshold", CONFIG["MOUSE_THRESHOLD"]))
    CONFIG["CAT_THRESHOLD"] = float(body.get("cat_threshold", CONFIG["CAT_THRESHOLD"]))
    CONFIG["MIN_SECONDS_TO_ANALYZE"] = float(body.get("min_seconds_to_analyze", 1.5))
    CONFIG["LOCK_DURATION_AFTER_PREY_DETECTION"] = int(body.get("lock_duration_after_prey_detection", 60))
    CONFIG["MOUSE_CHECK_ENABLED"] = bool(body.get("mouse_check_enabled", True))
    CONFIG["BLOCK_EXIT_AFTER_PREY_ENTRY_ENABLED"] = bool(body.get("block_exit_after_prey_entry_enabled", False))
    CONFIG["BLOCK_EXIT_AFTER_PREY_ENTRY_DURATION"] = int(body.get("block_exit_after_prey_entry_duration", 60))
    CONFIG["USE_CAMERA_FOR_CAT_DETECTION"] = bool(body.get("use_camera_for_cat_detection", False))
    CONFIG["USE_CAMERA_FOR_MOTION_DETECTION"] = bool(body.get("use_camera_for_motion_detection", False))
    CONFIG["COMBINE_PIR_AND_CAMERA_OUTSIDE_MOTION"] = bool(body.get("combine_pir_and_camera_outside_motion", False))
    CONFIG["PAUSE_CAMERA_WHEN_IDLE"] = bool(body.get("pause_camera_when_idle", False))
    CONFIG["ALLOWED_TO_ENTER"] = AllowedToEnter(allowed_to_enter)
    CONFIG["ALLOWED_TO_EXIT"] = AllowedToExit(allowed_to_exit)
    for i, r in zip((1, 2, 3), exit_ranges):
        CONFIG[f"ALLOWED_TO_EXIT_RANGE{i}"] = bool(r.get("enabled", False))
        CONFIG[f"ALLOWED_TO_EXIT_RANGE{i}_FROM"] = str(r.get("from") or "00:00")
        CONFIG[f"ALLOWED_TO_EXIT_RANGE{i}_TO"] = str(r.get("to") or "00:00")
    CONFIG["PIR_INSIDE_THRESHOLD"] = float(body.get("pir_inside_threshold", CONFIG["PIR_INSIDE_THRESHOLD"]))
    CONFIG["IMMEDIATE_LOCK_AFTER_PASSAGE"] = bool(body.get("immediate_lock_after_passage", False))

    for key in (
        "MIN_THRESHOLD", "MOUSE_THRESHOLD", "CAT_THRESHOLD", "MIN_SECONDS_TO_ANALYZE",
        "LOCK_DURATION_AFTER_PREY_DETECTION", "MOUSE_CHECK_ENABLED",
        "BLOCK_EXIT_AFTER_PREY_ENTRY_ENABLED", "BLOCK_EXIT_AFTER_PREY_ENTRY_DURATION",
        "TFLITE_MODEL_VERSION", "YOLO_MODEL", "USE_CAMERA_FOR_CAT_DETECTION",
        "USE_CAMERA_FOR_MOTION_DETECTION", "COMBINE_PIR_AND_CAMERA_OUTSIDE_MOTION",
        "PAUSE_CAMERA_WHEN_IDLE", "ALLOWED_TO_ENTER", "ALLOWED_TO_EXIT",
        "ALLOWED_TO_EXIT_RANGE1", "ALLOWED_TO_EXIT_RANGE1_FROM", "ALLOWED_TO_EXIT_RANGE1_TO",
        "ALLOWED_TO_EXIT_RANGE2", "ALLOWED_TO_EXIT_RANGE2_FROM", "ALLOWED_TO_EXIT_RANGE2_TO",
        "ALLOWED_TO_EXIT_RANGE3", "ALLOWED_TO_EXIT_RANGE3_FROM", "ALLOWED_TO_EXIT_RANGE3_TO",
        "PIR_INSIDE_THRESHOLD", "IMMEDIATE_LOCK_AFTER_PASSAGE",
    ):
        update_single_config_parameter(key)

    return JSONResponse({"ok": True})


@webui_app.get("/app-api/v1/config/live-view")
async def get_config_live_view(_user: str = Depends(require_session)):
    return JSONResponse({"refresh_interval": float(CONFIG["LIVE_VIEW_REFRESH_INTERVAL"])})


@webui_app.post("/app-api/v1/config/live-view")
async def post_config_live_view(request: Request, _user: str = Depends(require_session)):
    body = await request.json()
    try:
        interval = float(body.get("refresh_interval"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="invalid refresh_interval")
    if interval not in (0.1, 0.2, 0.5, 1.0, 2.0, 3.0, 5.0, 10.0):
        raise HTTPException(status_code=400, detail="invalid refresh_interval")
    CONFIG["LIVE_VIEW_REFRESH_INTERVAL"] = interval
    update_single_config_parameter("LIVE_VIEW_REFRESH_INTERVAL")
    return JSONResponse({"ok": True})


@webui_app.get("/app-api/v1/config/pictures-view")
async def get_config_pictures_view(_user: str = Depends(require_session)):
    return JSONResponse(
        {
            "max_photos_count": int(CONFIG["MAX_PHOTOS_COUNT"]),
            "max_pictures_per_event_with_rfid": int(CONFIG["MAX_PICTURES_PER_EVENT_WITH_RFID"]),
            "max_pictures_per_event_without_rfid": int(CONFIG["MAX_PICTURES_PER_EVENT_WITHOUT_RFID"]),
            "elements_per_page": int(CONFIG["ELEMENTS_PER_PAGE"]),
        }
    )


@webui_app.post("/app-api/v1/config/pictures-view")
async def post_config_pictures_view(request: Request, _user: str = Depends(require_session)):
    body = await request.json()
    try:
        max_photos_count = max(100, int(body.get("max_photos_count")))
        max_with_rfid = max(0, int(body.get("max_pictures_per_event_with_rfid")))
        max_without_rfid = max(0, int(body.get("max_pictures_per_event_without_rfid")))
        elements_per_page = max(1, int(body.get("elements_per_page")))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="invalid numeric value")

    CONFIG["MAX_PHOTOS_COUNT"] = max_photos_count
    CONFIG["MAX_PICTURES_PER_EVENT_WITH_RFID"] = max_with_rfid
    CONFIG["MAX_PICTURES_PER_EVENT_WITHOUT_RFID"] = max_without_rfid
    CONFIG["ELEMENTS_PER_PAGE"] = elements_per_page
    for key in (
        "MAX_PHOTOS_COUNT", "MAX_PICTURES_PER_EVENT_WITH_RFID",
        "MAX_PICTURES_PER_EVENT_WITHOUT_RFID", "ELEMENTS_PER_PAGE",
    ):
        update_single_config_parameter(key)
    return JSONResponse({"ok": True})


@webui_app.get("/app-api/v1/config/mqtt")
async def get_config_mqtt(_user: str = Depends(require_session)):
    return JSONResponse(
        {
            "mqtt_enabled": bool(CONFIG.get("MQTT_ENABLED", False)),
            "mqtt_broker_address": CONFIG.get("MQTT_BROKER_ADDRESS", ""),
            "mqtt_broker_port": int(CONFIG.get("MQTT_BROKER_PORT", 1883)),
            "mqtt_username": CONFIG.get("MQTT_USERNAME", "") or "",
            "mqtt_password": CONFIG.get("MQTT_PASSWORD", "") or "",
            "mqtt_image_publish_interval": int(CONFIG["MQTT_IMAGE_PUBLISH_INTERVAL"]),
            "mqtt_device_id": CONFIG["MQTT_DEVICE_ID"],
        }
    )


@webui_app.post("/app-api/v1/config/mqtt")
async def post_config_mqtt(request: Request, _user: str = Depends(require_session)):
    body = await request.json()
    try:
        port = int(body.get("mqtt_broker_port", 1883))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="invalid mqtt_broker_port")
    if not (1 <= port <= 65535):
        raise HTTPException(status_code=400, detail="mqtt_broker_port must be 1-65535")

    CONFIG["MQTT_ENABLED"] = bool(body.get("mqtt_enabled", False))
    CONFIG["MQTT_BROKER_ADDRESS"] = str(body.get("mqtt_broker_address") or "")
    CONFIG["MQTT_BROKER_PORT"] = port
    CONFIG["MQTT_USERNAME"] = str(body.get("mqtt_username") or "")
    CONFIG["MQTT_PASSWORD"] = str(body.get("mqtt_password") or "")
    CONFIG["MQTT_IMAGE_PUBLISH_INTERVAL"] = float(body.get("mqtt_image_publish_interval", 5))

    for key in (
        "MQTT_ENABLED", "MQTT_BROKER_ADDRESS", "MQTT_BROKER_PORT",
        "MQTT_USERNAME", "MQTT_PASSWORD", "MQTT_IMAGE_PUBLISH_INTERVAL",
    ):
        update_single_config_parameter(key)
    return JSONResponse({"ok": True})


# Advanced: loglevel, remote inference FPS/device, IP camera watchdog,
# RFID reader kill-switch. Hostname and the non-remote-mode-only fields
# (local CPU-core toggle, WLAN watchdog, post-reboot remote-wait timeout)
# are NOT replicated - hostnamectl/the /etc/hosts rewrite it would run
# target a bare-metal Pi, not this Docker container, and the others simply
# don't apply since this deployment is always remote-mode.
@webui_app.get("/app-api/v1/config/advanced")
async def get_config_advanced(_user: str = Depends(require_session)):
    return JSONResponse(
        {
            "loglevel": CONFIG["LOGLEVEL"],
            "remote_inference_max_fps": float(CONFIG.get("REMOTE_INFERENCE_MAX_FPS", 10.0) or 10.0),
            "restart_ip_camera_stream_on_failure": bool(CONFIG["RESTART_IP_CAMERA_STREAM_ON_FAILURE"]),
            "inference_device": CONFIG.get("INFERENCE_DEVICE", "cpu") or "cpu",
            "disable_rfid_reader": bool(CONFIG["DISABLE_RFID_READER"]),
        }
    )


@webui_app.post("/app-api/v1/config/advanced")
async def post_config_advanced(request: Request, _user: str = Depends(require_session)):
    body = await request.json()
    loglevel = str(body.get("loglevel") or "INFO")
    if loglevel not in ("DEBUG", "INFO", "WARN", "ERROR", "CRITICAL"):
        raise HTTPException(status_code=400, detail="invalid loglevel")
    inference_device = str(body.get("inference_device") or "cpu")
    if inference_device not in ("cpu", "cuda:0", "intel:gpu"):
        raise HTTPException(status_code=400, detail="invalid inference_device")

    if inference_device.startswith("intel:"):
        from src.system import DependencyInstaller

        if not DependencyInstaller.ensure_openvino_installed():
            raise HTTPException(
                status_code=409,
                detail="Le paquet openvino n'a pas pu être installé automatiquement. "
                "Installe-le manuellement (pip install openvino) et réessaie.",
            )

    CONFIG["LOGLEVEL"] = loglevel
    CONFIG["REMOTE_INFERENCE_MAX_FPS"] = max(1.0, min(60.0, float(body.get("remote_inference_max_fps", 10.0))))
    CONFIG["RESTART_IP_CAMERA_STREAM_ON_FAILURE"] = bool(body.get("restart_ip_camera_stream_on_failure", True))
    CONFIG["INFERENCE_DEVICE"] = inference_device
    CONFIG["DISABLE_RFID_READER"] = bool(body.get("disable_rfid_reader", False))

    for key in (
        "LOGLEVEL", "REMOTE_INFERENCE_MAX_FPS", "RESTART_IP_CAMERA_STREAM_ON_FAILURE",
        "INFERENCE_DEVICE", "DISABLE_RFID_READER",
    ):
        update_single_config_parameter(key)
    return JSONResponse({"ok": True})


# Add new cat - 09.09, Sid: moved here (from its own top-level nav tab) on
# the Shiny side too, since adding a cat is a rare, one-off action.
@webui_app.get("/app-api/v1/config/add-cat-state")
async def get_config_add_cat_state(_user: str = Depends(require_session)):
    from src.baseconfig import AllowedToEnter, AllowedToExit

    return JSONResponse(
        {
            "entry_mode_per_cat": CONFIG["ALLOWED_TO_ENTER"] == AllowedToEnter.CONFIGURE_PER_CAT,
            "exit_mode_per_cat": CONFIG["ALLOWED_TO_EXIT"] == AllowedToExit.CONFIGURE_PER_CAT,
            "prey_check_enabled": bool(CONFIG["MOUSE_CHECK_ENABLED"]),
        }
    )


@webui_app.post("/app-api/v1/config/add-cat")
async def post_config_add_cat(request: Request, _user: str = Depends(require_session)):
    from src.baseconfig import AllowedToEnter, AllowedToExit

    form = await request.form()
    name = str(form.get("name") or "").strip()
    rfid = str(form.get("rfid") or "").strip().upper()
    if not name:
        raise HTTPException(status_code=400, detail="Nom manquant.")
    if rfid and not _RFID_RE.match(rfid):
        raise HTTPException(status_code=400, detail="RFID invalide : 16 caractères hexadécimaux (0-9, A-F) attendus")

    prey = str(form.get("prey", "true")).lower() == "true"
    allow_entry = (
        str(form.get("allow_entry", "true")).lower() == "true"
        if CONFIG["ALLOWED_TO_ENTER"] == AllowedToEnter.CONFIGURE_PER_CAT
        else True
    )
    allow_exit = (
        str(form.get("allow_exit", "true")).lower() == "true"
        if CONFIG["ALLOWED_TO_EXIT"] == AllowedToExit.CONFIGURE_PER_CAT
        else True
    )

    tmp_path = None
    upload = form.get("photo")
    if upload is not None and getattr(upload, "filename", None):
        import tempfile

        suffix = os.path.splitext(upload.filename or "")[1] or ".jpg"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(await upload.read())
            tmp_path = tmp.name

    try:
        result = CatsRepo.db_add_new_cat(
            CONFIG["KITTYHACK_DATABASE_PATH"], name, rfid, tmp_path, prey, allow_entry, allow_exit
        )
    finally:
        if tmp_path:
            os.unlink(tmp_path)

    if not result.success:
        raise HTTPException(status_code=500, detail=result.message)
    return JSONResponse({"ok": True})


# ---------------------------------------------------------------------------
# System - API tokens (safe, reversible) + the device reboot/shutdown
# actions (NOT reversible, real physical/host consequences). Built and
# syntax-checked, but deliberately never live-invoked during this session -
# "Restart/Shutdown this device" runs ServiceOps.systemcmd(["/sbin/reboot"]
# or "/usr/sbin/shutdown") inside the container, which on this host can
# plausibly reboot/shut down the whole NAS (all of Sid's other stacks with
# it), not just this container. Sid should test these herself, not me,
# while she's actually watching.
# ---------------------------------------------------------------------------


@webui_app.get("/app-api/v1/system/api-tokens")
async def get_system_api_tokens(_user: str = Depends(require_session)):
    from src.api import list_tokens

    return JSONResponse({"tokens": list_tokens()})


@webui_app.post("/app-api/v1/system/api-tokens")
async def post_system_api_tokens(request: Request, _user: str = Depends(require_session)):
    from src.api import create_token

    body = await request.json()
    label = str(body.get("label") or "").strip()
    if not label:
        raise HTTPException(status_code=400, detail="Un label est requis.")
    raw, record = create_token(label)
    return JSONResponse({"token": raw, "id": record.get("id"), "label": record.get("label")})


@webui_app.delete("/app-api/v1/system/api-tokens/{token_id}")
async def delete_system_api_token(token_id: str, _user: str = Depends(require_session)):
    from src.api import revoke_token

    ok = revoke_token(token_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Token introuvable - peut-être déjà révoqué.")
    return JSONResponse({"ok": True})


def _trigger_target_power_action(action: str) -> bool:
    """Reboot/shutdown the physical Kittyflap Pi - same logic as
    system_tab.py's own helper (duplicated, not imported: that one is
    nested inside register_system_tab(), not a module-level function)."""
    if not is_remote_mode():
        return False
    act = str(action or "").strip().lower()
    if act not in ("reboot", "shutdown"):
        return False

    target_host = str(CONFIG.get("REMOTE_TARGET_HOST") or "").strip()
    ok = False
    try:
        from src.remote.control_client import RemoteControlClient

        client = RemoteControlClient.instance()
        client.ensure_started()
        if client.wait_until_ready(timeout=2.0):
            ok = bool(
                client.request_target_reboot(timeout=2.0)
                if act == "reboot"
                else client.request_target_shutdown(timeout=2.0)
            )
    except Exception as e:
        logging.warning(f"[REMOTE_MODE] Target {act} trigger via websocket failed: {e}")

    if (not ok) and target_host:
        import urllib.error
        import urllib.request

        try:
            endpoint = "reboot" if act == "reboot" else "shutdown"
            req = urllib.request.Request(f"http://{target_host}/api/{endpoint}", data=b"", method="POST")
            with urllib.request.urlopen(req, timeout=2.0) as resp:
                ok = int(getattr(resp, "status", 200) or 200) < 300
        except urllib.error.URLError:
            ok = False
        except Exception:
            ok = False

    return bool(ok)


@webui_app.post("/app-api/v1/system/target-reboot")
async def post_system_target_reboot(_user: str = Depends(require_session)):
    if not _trigger_target_power_action("reboot"):
        raise HTTPException(
            status_code=502,
            detail="Échec du déclenchement du redémarrage. Vérifie la connexion distante et REMOTE_TARGET_HOST.",
        )
    return JSONResponse({"ok": True})


@webui_app.post("/app-api/v1/system/target-shutdown")
async def post_system_target_shutdown(_user: str = Depends(require_session)):
    if not _trigger_target_power_action("shutdown"):
        raise HTTPException(
            status_code=502,
            detail="Échec du déclenchement de l'arrêt. Vérifie la connexion distante et REMOTE_TARGET_HOST.",
        )
    return JSONResponse({"ok": True})


@webui_app.post("/app-api/v1/system/local-reboot")
async def post_system_local_reboot(_user: str = Depends(require_session)):
    from src.system import ServiceOps

    ServiceOps.systemcmd(["/sbin/reboot"])
    return JSONResponse({"ok": True})


@webui_app.post("/app-api/v1/system/local-shutdown")
async def post_system_local_shutdown(_user: str = Depends(require_session)):
    from src.system import ServiceOps

    ServiceOps.systemcmd(["/usr/sbin/shutdown", "-H", "now"])
    return JSONResponse({"ok": True})


# Remote connection history - 04.10, Sid ("une rubrique info qui recense
# les déconnexions... que je puisse me rendre compte que ça tient la
# route"): same duration/uptime math as remote_connection_log.py's own
# ui_remote_connection_log(), just returned as JSON.
def _format_duration_hms(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds} s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} min"
    hours = minutes // 60
    rem_min = minutes % 60
    if hours < 24:
        return f"{hours} h {rem_min:02d}" if rem_min else f"{hours} h"
    days = hours // 24
    rem_h = hours % 24
    return f"{days} j {rem_h}h" if rem_h else f"{days} j"


@webui_app.get("/app-api/v1/system/remote-connection-log")
async def get_remote_connection_log(_user: str = Depends(require_session)):
    from src.database import RemoteConnectionLogRepo

    database = CONFIG["KITTYHACK_DATABASE_PATH"]
    df = RemoteConnectionLogRepo.get_recent(database, limit=500)
    if df.empty:
        return JSONResponse({"empty": True})

    tz = ZoneInfo(CONFIG["TIMEZONE"])
    df["ts_utc"] = pd.to_datetime(df["created_at"], format="mixed", utc=True, errors="coerce")
    df = df.dropna(subset=["ts_utc"]).sort_values("ts_utc", ascending=False).reset_index(drop=True)

    now = datetime.now(tz)
    durations = []
    for i in range(len(df)):
        start = df.iloc[i]["ts_utc"].tz_convert(tz)
        end = now if i == 0 else df.iloc[i - 1]["ts_utc"].tz_convert(tz)
        durations.append((end - start).total_seconds())

    total_connected_s = sum(d for d, ev in zip(durations, df["event"]) if ev == "connected")
    total_disconnected_s = sum(d for d, ev in zip(durations, df["event"]) if ev == "disconnected")
    total_s = total_connected_s + total_disconnected_s
    uptime_pct = (100.0 * total_connected_s / total_s) if total_s > 0 else 0.0
    disconnect_count = int((df["event"] == "disconnected").sum())
    longest_connected_s = max((d for d, ev in zip(durations, df["event"]) if ev == "connected"), default=0.0)
    span_label = df.iloc[-1]["ts_utc"].tz_convert(tz).strftime("%d.%m.%Y")

    rows = []
    for i, row in df.iterrows():
        local_dt = row["ts_utc"].tz_convert(tz)
        is_connected = row["event"] == "connected"
        rows.append(
            {
                "connected": is_connected,
                "at": local_dt.strftime("%d.%m.%Y %H:%M:%S"),
                "duration_label": _format_duration_hms(durations[i]),
                "ongoing": i == 0,
                "reason": str(row.get("reason") or "").strip() or None,
            }
        )

    return JSONResponse(
        {
            "empty": False,
            "uptime_pct": round(uptime_pct, 1),
            "disconnect_count": disconnect_count,
            "longest_streak_label": _format_duration_hms(longest_connected_s),
            "since_label": span_label,
            "rows": rows,
        }
    )


# Info - read-only version/connection/disk summary. The self-update trigger
# flow (git pull, pip install, version-mismatch gating, disk-space gate,
# release notes, progress polling - ui_info() in info.py, ~250 lines) is
# deliberately NOT replicated: it's the single highest-risk piece of the
# whole Configuration page (can leave the running app mid-update with no
# one watching), more so than even System's reboot/shutdown. Left on Shiny
# for a session where Sid is actually awake and watching - this read-only
# summary covers what she'd check day-to-day.
@webui_app.get("/app-api/v1/system/info")
async def get_system_info(_user: str = Depends(require_session)):
    import src.startup as startup
    from src.helper import SystemInfo

    target_version = "unknown"
    if is_remote_mode():
        try:
            from src.remote.control_client import RemoteControlClient

            client = RemoteControlClient.instance()
            client.ensure_started()
            if client.wait_until_ready(timeout=0):
                info = client.request_target_version(timeout=0.6) or client.get_target_version_info()
                if info and str(info.get("git_version") or "").strip():
                    target_version = str(info.get("git_version") or "unknown")
        except Exception:
            target_version = "unknown"

    return JSONResponse(
        {
            "this_device_version": startup.git_version,
            "target_device_version": target_version,
            "latest_version": CONFIG.get("LATEST_VERSION", "unknown"),
            "free_disk_mb": round(SystemInfo.get_free_disk_space(), 1),
            "total_disk_mb": round(SystemInfo.get_total_disk_space(), 1),
            "is_remote_mode": is_remote_mode(),
        }
    )


# 08.10, Sid ("je vais jamais reprendre les màj de la page officielle, vu
# qu'on a tout forké. Ce qu'il faut qu'on sache c'est quand il y a une màj,
# pour voir ce qui est intéressant dedans et reprendre cet élément dans
# notre fork"): read-only upstream check, no update trigger at all - the
# actual update_kittyhack() mechanism (halts the backend, git-checkouts
# live code, reinstalls deps) stays out of scope entirely, she doesn't want
# it. Just "is there something new upstream, what does it say".
@webui_app.get("/app-api/v1/system/upstream-check")
async def get_system_upstream_check(_user: str = Depends(require_session)):
    import src.startup as startup
    from src.helper import Versioning

    latest = Versioning.read_latest_kittyhack_version(timeout=8)
    notes = ""
    has_update = False
    if latest and latest != "unknown":
        has_update = not Versioning.is_same_kittyhack_version(startup.git_version, latest)
        if has_update:
            notes = Versioning.fetch_github_release_notes(latest)
            notes = Versioning.filter_release_notes_for_language(notes, CONFIG.get("LANGUAGE", "en"))

    CONFIG["LATEST_VERSION"] = latest
    update_single_config_parameter("LATEST_VERSION")

    return JSONResponse(
        {
            "current_version": startup.git_version,
            "latest_version": latest,
            "has_update": has_update,
            "release_notes": notes,
        }
    )


# ---------------------------------------------------------------------------
# AI Training - migrated section by section like Configuration.
# ---------------------------------------------------------------------------


# Model Training - submit (Label Studio export or manual zip upload),
# status polling, cancel. All actually safe: enqueue_model_training() just
# POSTs a zip to an external training server over HTTPS, nothing touches
# this box's own running code/services the way the self-update flow does.
@webui_app.get("/app-api/v1/ai-training/model-training/state")
async def get_ai_training_model_training_state(_user: str = Depends(require_session)):
    from src.helper import is_valid_uuid4
    from src.model.remote_trainer import RemoteModelTrainer

    job_id = CONFIG.get("MODEL_TRAINING", "")
    in_progress = is_valid_uuid4(job_id)
    if in_progress:
        status_label = RemoteModelTrainer.check_model_training_result(
            show_notification=False, show_in_progress=False, return_pretty_status=True
        )
        return JSONResponse({"in_progress": True, "job_id": job_id, "status_label": status_label})

    server_status = RemoteModelTrainer.get_server_status()
    return JSONResponse({"in_progress": False, "server_status": server_status})


@webui_app.post("/app-api/v1/ai-training/model-training/submit")
async def post_ai_training_model_training_submit(request: Request, _user: str = Depends(require_session)):
    import tempfile

    from src.helper import is_valid_uuid4
    from src.model.remote_trainer import RemoteModelTrainer
    from src.model.yolo_model import YoloModel

    form = await request.form()
    model_name = str(form.get("model_name") or "")
    user_name = str(form.get("user_name") or "")
    email = str(form.get("email") or "")
    model_variant = str(form.get("base_model") or "n").strip().lower()
    if model_variant not in {"n", "s", "m", "l", "x", "26n", "26s", "26m", "26l", "26x"}:
        raise HTTPException(status_code=400, detail="Modèle de base invalide.")
    try:
        image_size = int(form.get("image_size") or 320)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Taille d'image invalide.")
    if image_size not in set(YoloModel.get_supported_image_sizes()):
        raise HTTPException(status_code=400, detail="Taille d'image invalide.")
    if email and not re.match(r"[^@]+@[^@]+\.[^@]+", email):
        raise HTTPException(status_code=400, detail="Adresse email invalide.")

    if email and email != CONFIG["EMAIL"]:
        CONFIG["EMAIL"] = email
        update_single_config_parameter("EMAIL")
    if user_name and user_name != CONFIG["USER_NAME"]:
        CONFIG["USER_NAME"] = user_name
        update_single_config_parameter("USER_NAME")

    upload = form.get("file")
    if upload is None or not getattr(upload, "filename", "").endswith(".zip"):
        raise HTTPException(status_code=400, detail="Un fichier ZIP est requis.")
    zip_file_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
            tmp.write(await upload.read())
            zip_file_path = tmp.name

        result = await asyncio.to_thread(
            RemoteModelTrainer.enqueue_model_training,
            zip_file_path,
            model_name,
            user_name,
            email,
            yolo_model_variant=model_variant,
            image_size=image_size,
        )
    finally:
        if zip_file_path and os.path.exists(zip_file_path):
            try:
                os.remove(zip_file_path)
            except Exception:
                pass

    if is_valid_uuid4(result):
        CONFIG["MODEL_TRAINING"] = result
        update_single_config_parameter("MODEL_TRAINING")
        return JSONResponse({"ok": True, "job_id": result})

    error_messages = {
        "invalid_file": "Le fichier n'est pas un ZIP de données d'entraînement valide.",
        "destination_unreachable": "Le serveur d'entraînement est injoignable. Réessaie plus tard.",
        "destination_not_found": "Le serveur d'entraînement est injoignable. Réessaie plus tard.",
        "file_too_large": "Le fichier est trop volumineux.",
    }
    raise HTTPException(status_code=502, detail=error_messages.get(result, f"Erreur : {result}"))


@webui_app.post("/app-api/v1/ai-training/model-training/cancel")
async def post_ai_training_model_training_cancel(_user: str = Depends(require_session)):
    from src.model.remote_trainer import RemoteModelTrainer

    job_id = CONFIG.get("MODEL_TRAINING", "")
    if job_id:
        await asyncio.to_thread(RemoteModelTrainer.cancel_model_training, job_id)
    CONFIG["MODEL_TRAINING"] = ""
    update_single_config_parameter("MODEL_TRAINING")
    return JSONResponse({"ok": True})


@webui_app.get("/app-api/v1/ai-training/model-options")
async def get_ai_training_model_options(_user: str = Depends(require_session)):
    from src.model.yolo_model import YoloModel

    return JSONResponse(
        {
            "image_sizes": YoloModel.get_supported_image_sizes(),
            "user_name": CONFIG.get("USER_NAME", ""),
            "email": CONFIG.get("EMAIL", ""),
        }
    )


# Model Management - list/activate/rename/delete installed YOLO models
# (yolo_modules.py's manage_yolo_model_server/activate_yolo_model_server).
@webui_app.get("/app-api/v1/ai-training/models")
async def get_ai_training_models(_user: str = Depends(require_session)):
    from src.model import YoloModel

    active_id = CONFIG.get("YOLO_MODEL")
    active_is_yolo = not (CONFIG.get("TFLITE_MODEL_VERSION") or "").strip()

    models = []
    for m in YoloModel.get_model_list() or []:
        unique_id = m.get("unique_id")
        if not unique_id:
            continue
        yolo_variant = (m.get("yolo_variant") or "yolov8n.pt").strip() or "yolov8n.pt"
        variant_match = re.search(r"yolov\d+\s*([nslmx])", yolo_variant.lower())
        variant_letter = variant_match.group(1).upper() if variant_match else "N"
        models.append({
            "unique_id": unique_id,
            "display_name": m.get("display_name", ""),
            "creation_date": m.get("creation_date", ""),
            "is_active": bool(active_is_yolo and unique_id == active_id),
            "fps": m.get("effective_fps"),
            "model_image_size": m.get("model_image_size", 320) or 320,
            "variant_letter": variant_letter,
        })
    return JSONResponse({"models": models})


@webui_app.post("/app-api/v1/ai-training/models/{unique_id}/activate")
async def post_ai_training_model_activate(unique_id: str, _user: str = Depends(require_session)):
    from src.model import YoloModel
    from src.backend import reload_model_handler_runtime

    if CONFIG.get("YOLO_MODEL") == unique_id and not (CONFIG.get("TFLITE_MODEL_VERSION") or "").strip():
        return JSONResponse({"ok": True, "already_active": True})

    model_path = YoloModel.get_model_path(unique_id)
    if not model_path:
        return JSONResponse({"ok": False, "error": "not_found"}, status_code=404)

    CONFIG["YOLO_MODEL"] = unique_id
    CONFIG["TFLITE_MODEL_VERSION"] = ""
    update_single_config_parameter("YOLO_MODEL")
    update_single_config_parameter("TFLITE_MODEL_VERSION")

    reload_ok, _handler = await asyncio.to_thread(reload_model_handler_runtime)
    return JSONResponse({"ok": True, "live_reload": bool(reload_ok)})


@webui_app.post("/app-api/v1/ai-training/models/{unique_id}/rename")
async def post_ai_training_model_rename(unique_id: str, request: Request, _user: str = Depends(require_session)):
    from src.model import YoloModel

    body = await request.json()
    name = (body.get("name") or "").strip()
    if not name:
        return JSONResponse({"ok": False, "error": "empty_name"}, status_code=400)
    success = YoloModel.rename_model(unique_id, name)
    return JSONResponse({"ok": bool(success)})


@webui_app.delete("/app-api/v1/ai-training/models/{unique_id}")
async def delete_ai_training_model(unique_id: str, _user: str = Depends(require_session)):
    from src.model import YoloModel

    if CONFIG.get("YOLO_MODEL") == unique_id:
        return JSONResponse({"ok": False, "error": "in_use"}, status_code=400)
    success = YoloModel.delete_model(unique_id)
    return JSONResponse({"ok": bool(success)})


# Retroactive Model Review - re-run the active model on past events and
# surface only the disagreements (model_review_modules.py + ai_training.py's
# ui_model_review/on_run_model_review). The scan itself is the same blocking
# call the Shiny original used, via asyncio.to_thread for the same reason as
# the corrections export above.
@webui_app.get("/app-api/v1/ai-training/model-review/state")
async def get_ai_training_model_review_state(_user: str = Depends(require_session)):
    from src.database import ModelReviewRepo

    database = CONFIG["KITTYHACK_DATABASE_PATH"]
    running = _model_review_process_lock.locked()
    # 08.10, Sid ("j'espérais en avoir à chaque fois un peu moins, pas 2x
    # plus"): scoped to the CURRENTLY active model's own backlog - a past
    # model switch leaves its old pending rows behind (upsert_disagreement's
    # unique key includes model_version), no longer relevant once that model
    # isn't active anymore. Still queryable directly in the DB if ever needed.
    pending = ModelReviewRepo.get_pending_count(database, model_version=CONFIG.get("YOLO_MODEL"))
    return JSONResponse({
        "running": running,
        "pending": pending,
        "last_result": _model_review_last_result,
    })


@webui_app.post("/app-api/v1/ai-training/model-review/run")
async def post_ai_training_model_review_run(_user: str = Depends(require_session)):
    global _model_review_last_result
    from src.model.retroactive_review import run_retroactive_review_batch

    if not _model_review_process_lock.acquire(blocking=False):
        return JSONResponse({"ok": False, "error": "already_running"}, status_code=409)
    _model_review_cancel_event.clear()
    try:
        # 06.10, Sid ("il faut quand même que je puisse lancer un scan de
        # tout"): same bound the Shiny original used - comfortably covers
        # her whole history today, still bounded so one run can't run forever.
        result = await asyncio.to_thread(run_retroactive_review_batch, 1000, _model_review_cancel_event)
    except Exception as e:
        logging.error(f"[MODEL_REVIEW] Scan failed: {e}")
        result = {"error": "exception"}
    finally:
        _model_review_process_lock.release()
    _model_review_last_result = result
    return JSONResponse({"ok": not result.get("error"), "result": result})


@webui_app.post("/app-api/v1/ai-training/model-review/cancel")
async def post_ai_training_model_review_cancel(_user: str = Depends(require_session)):
    # 08.10, Sid ("faudra faire un bouton du coup", after having to restart
    # the whole container mid-scan because it was starving the live
    # detection loop - see retroactive_review.py's run_retroactive_review_batch
    # comment): sets the flag the running scan checks between blocks/photos.
    # Returns immediately - the scan itself stops within a photo or two, the
    # frontend keeps polling /state until `running` goes false.
    if not _model_review_process_lock.locked():
        return JSONResponse({"ok": False, "error": "not_running"}, status_code=409)
    _model_review_cancel_event.set()
    return JSONResponse({"ok": True})


@webui_app.get("/app-api/v1/ai-training/model-review/queue")
async def get_ai_training_model_review_queue(_user: str = Depends(require_session)):
    from src.database import ModelReviewRepo

    database = CONFIG["KITTYHACK_DATABASE_PATH"]
    active_model = CONFIG.get("YOLO_MODEL")
    cat_names = CatsRepo.get_cat_names_list(database)
    cat_name_dict = CatsRepo.get_cat_name_rfid_dict(database)
    df = ModelReviewRepo.get_pending(database, limit=50, model_version=active_model)

    def _display(n):
        if not n:
            return None
        return "prey" if n == "prey" else n

    rows = []
    for __, review in df.iterrows():
        rfid = review.get("event_rfid")
        block_id = review.get("event_block_id")
        rfid_is_real = review.get("event_rfid_is_real")
        rows.append({
            "id": int(review["id"]),
            "photo_id": int(review["photo_id"]),
            "event_block_id": None if pd.isna(block_id) else int(block_id),
            "old_name": _display(review.get("old_name")),
            "old_probability": float(review.get("old_probability") or 0),
            "new_name": _display(review.get("new_name")),
            "new_probability": float(review.get("new_probability") or 0),
            "rfid_label": cat_name_dict.get(rfid) if rfid else None,
            # 08.10, Sid ("le RFID pris en compte dans la révision, il se
            # base sur quoi?"): True = real chip read, False = video
            # model's guess only, None = unknown provenance (pre-migration
            # row, or pd.isna coming back from the LEFT JOIN).
            "rfid_confirmed": None if pd.isna(rfid_is_real) else bool(rfid_is_real),
        })
    return JSONResponse({
        "pending": ModelReviewRepo.get_pending_count(database, model_version=active_model),
        "rows": rows,
        "cat_names": cat_names,
    })


def _canonical_cat_name(name: str) -> str:
    """09.10, Sid (Label Studio's own label-rename validation error surfaced
    it: "nala (237)/pookie (28)/boubou (373)/patoune (189) annotations"):
    confirmed live - 1082 annotations across the project used a lowercase
    cat name as a SEPARATE label from the capitalized one (e.g. "boubou"
    vs "Boubou", 9 effective classes instead of 5), diluting training data
    for every affected cat. Root cause: _normalize_name() in
    retroactive_review.py lowercases for internal comparison (correct,
    case-insensitive matching), but that lowercased value was then written
    straight into detection_feedback.corrected_name/exported to Label
    Studio as-is instead of being mapped back to the cat's real display
    casing. Applied at every write site below so this can't recur; the
    1082 existing mismatched LS annotations still need a one-time manual
    fix (Label Studio's own "rename label" merges annotations under the
    old name into the new one - the UI she was already in when she found
    this)."""
    if not name:
        return name
    database = CONFIG["KITTYHACK_DATABASE_PATH"]
    cat_name_dict = CatsRepo.get_cat_name_rfid_dict(database)
    by_lower = {n.strip().lower(): n for n in cat_name_dict.values() if n}
    return by_lower.get(name.strip().lower(), name)


def _model_review_write_correction(review_id: int, photo_id: int, row: dict, corrected_name: str) -> None:
    database = CONFIG["KITTYHACK_DATABASE_PATH"]
    corrected_name = _canonical_cat_name(corrected_name)
    old_idx = row.get("old_object_index")
    if old_idx is not None and not pd.isna(old_idx):
        object_index, original_name, original_probability = int(old_idx), row.get("old_name"), float(row.get("old_probability") or 0)
    else:
        existing_feedback = DetectionFeedbackRepo.get_for_photo(database, photo_id)
        object_index = 0
        while object_index in existing_feedback:
            object_index += 1
        original_name, original_probability = None, 0.0

    from src.database import ModelReviewRepo

    result = DetectionFeedbackRepo.upsert(
        database,
        photo_id=photo_id,
        object_index=object_index,
        original_name=original_name,
        original_probability=original_probability,
        x=float(row["new_x"]), y=float(row["new_y"]),
        width=float(row["new_width"]), height=float(row["new_height"]),
        corrected_name=corrected_name,
        confirmed=True,
    )
    if not result.success:
        raise RuntimeError(result.message)
    ModelReviewRepo.mark_reviewed(database, review_id, "confirmed_new")


@webui_app.post("/app-api/v1/ai-training/model-review/{review_id}/keep-old")
async def post_ai_training_model_review_keep_old(review_id: int, _user: str = Depends(require_session)):
    from src.database import ModelReviewRepo

    result = ModelReviewRepo.mark_reviewed(CONFIG["KITTYHACK_DATABASE_PATH"], review_id, "kept_old")
    return JSONResponse({"ok": bool(result.success)})


@webui_app.post("/app-api/v1/ai-training/model-review/{review_id}/confirm-new")
async def post_ai_training_model_review_confirm_new(review_id: int, _user: str = Depends(require_session)):
    from src.database import ModelReviewRepo

    row = ModelReviewRepo.get_one(CONFIG["KITTYHACK_DATABASE_PATH"], review_id)
    if row is None:
        return JSONResponse({"ok": False, "error": "gone"}, status_code=404)
    try:
        _model_review_write_correction(review_id, row["photo_id"], row, row.get("new_name"))
    except Exception as e:
        logging.error(f"[MODEL_REVIEW] Failed to confirm new tag for review {review_id}: {e}")
        return JSONResponse({"ok": False, "error": "save_failed"}, status_code=500)
    return JSONResponse({"ok": True})


# 09.10, Sid ("la revérification doit gérer automatiquement de réattribuer
# cet événement-là au bon chat tout du long, pour relancer un entrainement
# et permettre aux détections suivantes d'apprendre mieux"): confirmed RFID
# chip reads (events.rfid_is_real) used directly as ground truth - no model,
# no inference, see src.model.rfid_ground_truth's own module docstring for
# the full reasoning and safety rules (never touches a "prey" detection).
@webui_app.get("/app-api/v1/ai-training/rfid-ground-truth/state")
async def get_ai_training_rfid_ground_truth_state(_user: str = Depends(require_session)):
    from src.database import RfidGroundTruthRepo
    from src.model.rfid_ground_truth import _next_unprocessed_rfid_blocks

    database = CONFIG["KITTYHACK_DATABASE_PATH"]
    running = _rfid_ground_truth_process_lock.locked()
    remaining = len(_next_unprocessed_rfid_blocks(database, 100000))
    return JSONResponse({
        "running": running,
        "remaining_blocks": remaining,
        "last_result": _rfid_ground_truth_last_result,
    })


@webui_app.post("/app-api/v1/ai-training/rfid-ground-truth/preview")
async def post_ai_training_rfid_ground_truth_preview(_user: str = Depends(require_session)):
    from src.model.rfid_ground_truth import run_rfid_ground_truth_batch

    if not _rfid_ground_truth_process_lock.acquire(blocking=False):
        return JSONResponse({"ok": False, "error": "already_running"}, status_code=409)
    try:
        # Dry run: computes the exact same plan the real run would, writes
        # nothing, marks nothing processed - safe to call repeatedly.
        result = await asyncio.to_thread(run_rfid_ground_truth_batch, 100000, None, True)
    except Exception as e:
        logging.error(f"[RFID_GROUND_TRUTH] Preview failed: {e}")
        result = {"error": "exception"}
    finally:
        _rfid_ground_truth_process_lock.release()
    return JSONResponse({"ok": not result.get("error"), "result": result})


@webui_app.post("/app-api/v1/ai-training/rfid-ground-truth/run")
async def post_ai_training_rfid_ground_truth_run(_user: str = Depends(require_session)):
    global _rfid_ground_truth_last_result
    from src.model.rfid_ground_truth import run_rfid_ground_truth_batch

    if not _rfid_ground_truth_process_lock.acquire(blocking=False):
        return JSONResponse({"ok": False, "error": "already_running"}, status_code=409)
    _rfid_ground_truth_cancel_event.clear()
    try:
        result = await asyncio.to_thread(
            run_rfid_ground_truth_batch, 100000, _rfid_ground_truth_cancel_event, False
        )
    except Exception as e:
        logging.error(f"[RFID_GROUND_TRUTH] Run failed: {e}")
        result = {"error": "exception"}
    finally:
        _rfid_ground_truth_process_lock.release()
    _rfid_ground_truth_last_result = result
    return JSONResponse({"ok": not result.get("error"), "result": result})


@webui_app.post("/app-api/v1/ai-training/rfid-ground-truth/cancel")
async def post_ai_training_rfid_ground_truth_cancel(_user: str = Depends(require_session)):
    if not _rfid_ground_truth_process_lock.locked():
        return JSONResponse({"ok": False, "error": "not_running"}, status_code=409)
    _rfid_ground_truth_cancel_event.set()
    return JSONResponse({"ok": True})


# 08.10, Sid ("on peut mettre en place une validation automatique en cas de
# désaccord... si RFID détecté et que avant/après ne correspondent pas, on
# met autre = RFID?"): limited to pure cat-vs-cat identity confusion -
# never touches a row where old_name OR new_name is "prey" or None/"rien".
# Her own concern ("il pourrait y avoir une proie non détectée qui
# passerait entre les gouttes"): if the flagged object is genuinely a prey
# item (not the cat itself mislabeled), forcing it to the RFID cat would
# write bad training data saying "this prey-looking box is actually your
# cat" - excluded entirely, left for manual review like today.
def _rfid_auto_resolve_plan(database: str, model_version: str) -> list[dict]:
    """Read-only: classify every pending row for this model version into an
    action (or none). Shared by the preview (counts only) and the real run
    (actually writes) so they can never disagree on what counts as safe."""
    from src.database import ModelReviewRepo

    cat_name_dict = CatsRepo.get_cat_name_rfid_dict(database)
    plan = []
    # 08.10: get_pending() has no offset param (ORDER BY id ASC LIMIT N,
    # always the same N rows) - a real bug caught before deploy, not after:
    # looping that call without consuming rows (as the read-only preview
    # does) would re-fetch the SAME page forever. One single large-limit
    # fetch instead - a few thousand rows is a trivial read, no real
    # pagination needed (unlike the scan's own 1000-event cap, which bounds
    # actual YOLO inference work, not a plain SELECT).
    df = ModelReviewRepo.get_pending(database, limit=100000, model_version=model_version)
    if not df.empty:
        for __, review in df.iterrows():
            rfid = review.get("event_rfid")
            rfid_is_real = review.get("event_rfid_is_real")
            # 08.10, Sid ("il n'y a qu'une vraie lecture de puce, le reste
            # c'est de la video, ca devrait pas influencer l'ouverture" -
            # same principle applies here, this feature exists specifically
            # because RFID is supposed to be the one 100%-reliable source):
            # only trust event_rfid when it's a CONFIRMED real chip read.
            # None (pre-migration rows - the entire backlog as of this fix)
            # and False (known video guess) both get treated as "no RFID",
            # same as if the column were empty - never auto-resolve off an
            # unverified guess.
            rfid_cat = (
                cat_name_dict.get(rfid) if rfid and rfid_is_real is True else None
            )
            old_name = (review.get("old_name") or "").strip()
            new_name = (review.get("new_name") or "").strip()
            old_lc = old_name.lower()
            new_lc = new_name.lower()
            rfid_lc = (rfid_cat or "").lower()

            action = None
            if rfid_cat:
                if old_lc and old_lc == rfid_lc:
                    action = "keep-old"
                elif new_lc and new_lc == rfid_lc:
                    action = "confirm-new"
                elif (
                    old_lc and old_lc != "prey"
                    and new_lc and new_lc != "prey"
                ):
                    action = "other"
            plan.append({
                "review_id": int(review["id"]),
                "photo_id": int(review["photo_id"]),
                "action": action,
                "rfid_cat": rfid_cat,
            })
    return plan


@webui_app.get("/app-api/v1/ai-training/model-review/rfid-auto-resolve-preview")
async def get_rfid_auto_resolve_preview(_user: str = Depends(require_session)):
    database = CONFIG["KITTYHACK_DATABASE_PATH"]
    active_model = CONFIG.get("YOLO_MODEL")
    plan = await asyncio.to_thread(_rfid_auto_resolve_plan, database, active_model)
    counts = {"keep-old": 0, "confirm-new": 0, "other": 0, "skipped": 0}
    for item in plan:
        counts[item["action"] or "skipped"] += 1
    return JSONResponse({"total_pending": len(plan), "counts": counts})


@webui_app.post("/app-api/v1/ai-training/model-review/rfid-auto-resolve")
async def post_rfid_auto_resolve(_user: str = Depends(require_session)):
    from src.database import ModelReviewRepo

    database = CONFIG["KITTYHACK_DATABASE_PATH"]
    active_model = CONFIG.get("YOLO_MODEL")

    def _run():
        plan = _rfid_auto_resolve_plan(database, active_model)
        counts = {"keep-old": 0, "confirm-new": 0, "other": 0, "skipped": 0, "failed": 0}
        for item in plan:
            if item["action"] is None:
                counts["skipped"] += 1
                continue
            row = ModelReviewRepo.get_one(database, item["review_id"])
            if row is None:
                continue  # already resolved by hand in the meantime
            try:
                if item["action"] == "keep-old":
                    ModelReviewRepo.mark_reviewed(database, item["review_id"], "kept_old")
                else:
                    corrected = row.get("new_name") if item["action"] == "confirm-new" else item["rfid_cat"]
                    _model_review_write_correction(item["review_id"], item["photo_id"], row, corrected)
                counts[item["action"]] += 1
            except Exception as e:
                logging.error(f"[MODEL_REVIEW] RFID auto-resolve failed for review {item['review_id']}: {e}")
                counts["failed"] += 1
        return counts

    counts = await asyncio.to_thread(_run)
    return JSONResponse({"counts": counts})


@webui_app.post("/app-api/v1/ai-training/model-review/{review_id}/other")
async def post_ai_training_model_review_other(review_id: int, request: Request, _user: str = Depends(require_session)):
    from src.database import ModelReviewRepo

    body = await request.json()
    other_name = (body.get("name") or "").strip()
    if not other_name:
        return JSONResponse({"ok": False, "error": "empty_name"}, status_code=400)
    row = ModelReviewRepo.get_one(CONFIG["KITTYHACK_DATABASE_PATH"], review_id)
    if row is None:
        return JSONResponse({"ok": False, "error": "gone"}, status_code=404)
    try:
        _model_review_write_correction(review_id, row["photo_id"], row, other_name)
    except Exception as e:
        logging.error(f"[MODEL_REVIEW] Failed to save 'other' correction for review {review_id}: {e}")
        return JSONResponse({"ok": False, "error": "save_failed"}, status_code=500)
    return JSONResponse({"ok": True})


@webui_app.get("/configuration")
@webui_app.get("/configuration/")
async def serve_configuration_page(_user: str = Depends(require_session)):
    return _serve_spa_index()


@webui_app.get("/chronology")
@webui_app.get("/chronology/")
async def serve_chronology_page(_user: str = Depends(require_session)):
    return _serve_spa_index()


@webui_app.get("/presence")
@webui_app.get("/presence/")
@webui_app.get("/presence/{rest:path}")
async def serve_presence_page(_user: str = Depends(require_session)):
    # Catch-all on the trailing path (e.g. /presence/cat/<rfid>) so a page
    # reload or a deep link lands here too - react-router then reads the
    # real browser URL client-side and renders the right sub-route.
    return _serve_spa_index()


# 08.10: was missing - "ai-training" had no serve route at all here, only
# ever reachable through app.py's dispatcher while still in SHINY_TAB_PATHS.
# Needed before that slug can ever move to FASTAPI_TAB_PATHS (see app.py's
# comment on the Configuration cutover for the same pattern), caught while
# adding remote-mode-doc's own route just below.
@webui_app.get("/ai-training")
@webui_app.get("/ai-training/")
async def serve_ai_training_page(_user: str = Depends(require_session)):
    return _serve_spa_index()


# 08.10, Sid ("pour la doc, tu peux créer une page et la conserver?"): React
# port of doc/remote-mode.md, linked from Configuration's Info section.
@webui_app.get("/remote-mode-doc")
@webui_app.get("/remote-mode-doc/")
async def serve_remote_mode_doc_page(_user: str = Depends(require_session)):
    return _serve_spa_index()
