"""Journey tab: one unified, chronological view of a cat's movements across
the outdoor watchdog cameras (terrasse, jardin_japonais, entree) and the
flap's own camera (chatiere / Kittyhack events), for one calendar day.

09.09, Sid: this is the "correlated" version she explicitly chose over a
flat chronological mix - watchdog captures taken within CORRELATION_WINDOW_S
of a real Kittyhack flap event are shown attached to that event (the
"journey" leading up to it going through the flap), while captures with no
nearby event (a cat that never came inside, or came in outside a
CORRELATION_WINDOW_S window) are still shown on their own so nothing is
hidden. Also carries the delete / send-to-Label-Studio actions she asked
for on watchdog captures specifically.
"""

import glob
import html as html_module
import json
import logging
import os
import re
import time
import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import requests
from faicons import icon_svg
from shiny import reactive, render, ui

from src.baseconfig import CONFIG, set_language
from src.database import CatsRepo, DatabaseCore
from src.server_ui.event_modal import btn_show_event, show_event_server
from src.helper import DateTimeUtil, EventType
from src.server_ui.context import SessionContext

# 20.10, Sid ("comme on a supprimé Label Studio et qu'on fait tout par peer
# review") - remplace l'ancien envoi vers Label Studio (service arrêté,
# upload_image_to_labelstudio_project / LabelStudioInstall retirés) : le
# bouton "envoyer" de Parcours dépose maintenant directement dans la file
# d'ingestion manuelle de Paws Review (/app-api/manual-drop/upload, voir
# app/main.py côté paws-review, repris par ingest_manual.py toutes les
# 5min). Même hôte, même convention que le garde-fou GPU (src/api.py
# detection/status) : IP LAN du host, pas 127.0.0.1 (conteneurs/projets
# compose séparés) ni le domaine public (inutile, même machine).
PAWS_REVIEW_URL = os.environ.get("PAWS_REVIEW_URL", "http://192.168.178.127:8082")
PAWS_REVIEW_UPLOAD_TOKEN = os.environ.get("PAWS_REVIEW_UPLOAD_TOKEN", "")


def _detections_sidecar_for_paws_review(sample_path: str) -> str | None:
    """21.10, Sid ("dans paws review n'arrivent que les boîtes de reo
    chatière [...] mais sans les boîtes") - chatière's own local model
    already ran on this exact frame (see watchdog.py's CameraWatcher,
    visible in Parcours as "boubou 70%") and its result is saved as a
    sidecar next to the sample (see _save_training_sample). Translates
    watchdog's own key ("name") to the key Paws Review's model_review.py
    actually writes ("label") - the rest of the schema (probability/x/y/
    width/height, percent, top-left-anchored) already matches exactly.
    Returns None (no field sent at all) for a peripheral-camera sample
    with no sidecar - manual_drop_upload below then falls back to its
    original behaviour (empty boxes, drawn by hand)."""
    sidecar_path = sample_path[:-4] + ".json" if sample_path.lower().endswith((".jpg", ".jpeg")) else None
    if not sidecar_path or not os.path.isfile(sidecar_path):
        return None
    try:
        import json as _json
        with open(sidecar_path, "r", encoding="utf-8") as f:
            raw = _json.load(f)
        boxes = [
            {
                "label": d.get("name"),
                "probability": d.get("probability"),
                "x": d.get("x"), "y": d.get("y"),
                "width": d.get("width"), "height": d.get("height"),
            }
            for d in raw if d.get("name")
        ]
        return _json.dumps(boxes) if boxes else None
    except Exception as e:
        logging.warning(f"[JOURNEY] Could not read detections sidecar for {sample_path}: {e}")
        return None


def _upload_to_paws_review(image_bytes: bytes, filename: str, detections_json: str | None = None) -> bool:
    try:
        data = {"detections": detections_json} if detections_json else {}
        resp = requests.post(
            f"{PAWS_REVIEW_URL}/app-api/manual-drop/upload",
            files={"file": (filename, image_bytes, "image/jpeg")},
            data=data,
            headers={"x-upload-token": PAWS_REVIEW_UPLOAD_TOKEN},
            timeout=15,
        )
        resp.raise_for_status()
        return True
    except Exception as e:
        logging.warning(f"[JOURNEY] Paws Review upload failed: {e}")
        return False

_ = set_language(CONFIG["LANGUAGE"])

# Must match watchdog.py's TRAINING_SAMPLES_DIR - kept as a separate literal
# there too (that module has no dependency on src.paths).
TRAINING_SAMPLES_DIR = "/data/watchdog_training_samples"

# How close (in seconds) a watchdog capture must be to a flap event's
# timestamp to be considered part of the same "journey".
CORRELATION_WINDOW_S = 300

_CAM_LABELS = {
    # 09.09, Sid: deliberately NOT the shared "Flap camera" string used
    # elsewhere (live_view.py's outdoor-camera grid) - that one is too long
    # and, inside the 150px-wide journey capture card, pushed the "· HH:MM:SS"
    # timestamp clean off the label ("ça bouffe tout l'espace et on voit pas
    # la date/heure"). This one is short by design.
    # 09.10, Sid ("chatière int et chatière ext"): the original flap
    # camera, physically moved indoors facing the flap (see watchdog.py's
    # CAMERAS comment) - "chatiere" relabeled to disambiguate now that
    # there are two.
    "chatiere": lambda: _("Chatière ext"),
    "chatiere_int": lambda: _("Chatière int"),
    "terrasse": lambda: _("Terrace"),
    "jardin_japonais": lambda: _("Japanese garden"),
    "entree": lambda: _("Entrance"),
}

_SAMPLE_RE = re.compile(r"^(?P<cam>[a-z_]+)_(?P<date>\d{8})_(?P<time>\d{6})\.jpg$")


def _cam_label(cam_name: str) -> str:
    fn = _CAM_LABELS.get(cam_name)
    return fn() if fn else cam_name


def _list_training_samples_for_date(date_str: str) -> list[dict]:
    """Return watchdog training-sample captures for one local calendar date
    (``YYYY-MM-DD``), newest first."""
    compact = date_str.replace("-", "")
    pattern = os.path.join(TRAINING_SAMPLES_DIR, f"*_{compact}_*.jpg")
    results = []
    for filepath in glob.glob(pattern):
        filename = os.path.basename(filepath)
        m = _SAMPLE_RE.match(filename)
        if not m:
            continue
        try:
            dt = datetime.strptime(m.group("date") + m.group("time"), "%Y%m%d%H%M%S")
        except ValueError:
            continue
        results.append({"filename": filename, "cam": m.group("cam"), "dt": dt})
    results.sort(key=lambda r: r["dt"], reverse=True)
    return results


def _local_day_utc_range(local_date) -> tuple[str, str]:
    """Same date -> UTC-range conversion as photos.py's date filter."""
    date_start = DateTimeUtil.format_date_minmax(local_date, True)
    date_end = DateTimeUtil.format_date_minmax(local_date, False)
    tz = ZoneInfo(CONFIG["TIMEZONE"])
    date_start_utc = (
        datetime.strptime(date_start, "%Y-%m-%d %H:%M:%S")
        .replace(tzinfo=tz)
        .astimezone(ZoneInfo("UTC"))
        .strftime("%Y-%m-%d %H:%M:%S%z")
    )
    date_end_utc = (
        datetime.strptime(date_end, "%Y-%m-%d %H:%M:%S")
        .replace(tzinfo=tz)
        .astimezone(ZoneInfo("UTC"))
        .strftime("%Y-%m-%d %H:%M:%S%z")
    )
    return date_start_utc, date_end_utc


def _list_flap_events_for_date(
    date_start_utc: str,
    date_end_utc: str,
    cats_only: bool = False,
    mouse_only: bool = False,
    mouse_probability: float = 0.0,
    rfid_filter: str = "",
) -> list[dict]:
    """One row per motion block for the day, with a representative photo id
    (for its thumbnail) - db_get_motion_blocks() doesn't carry an id, so this
    is a small purpose-built query rather than reusing it.

    08.10, Sid ("mode groupé" in Photos): the optional filters mirror
    db_get_motion_blocks()'s exact same ones (same WHERE clauses) - the
    Shiny original's grouped-events view shares EventsRepo.db_get_motion_blocks
    with the Live tab, but that function has no row id to build a thumbnail
    from, so the Photos React port needed its own query combining both."""
    database = CONFIG["KITTYHACK_DATABASE_PATH"]
    try:
        columns_info = DatabaseCore.read_column_info_from_database(database, "events")
        column_names = set(info[1] for info in (columns_info or []))
    except Exception:
        column_names = set()
    deleted_filter = " AND deleted != 1" if "deleted" in column_names else ""
    # 08.10, Sid: distinguish a real chip read from the video model's guess
    # (see loop.py/camera.py's tag_id_is_real) - MAX() is safe here since
    # every element of a block shares the same tag_id/is_real pair.
    has_rfid_is_real = "rfid_is_real" in column_names
    rfid_is_real_select = ", MAX(rfid_is_real) AS rfid_is_real" if has_rfid_is_real else ""

    extra_filters = ""
    if rfid_filter:
        extra_filters += f" AND rfid = '{rfid_filter.replace(chr(39), chr(39) + chr(39))}'"
    elif cats_only:
        extra_filters += " AND rfid != ''"
    if mouse_only:
        extra_filters += f" AND mouse_probability >= {mouse_probability}"

    stmt = f"""
        SELECT block_id, MIN(id) AS first_id, MIN(created_at) AS started_at,
               MAX(created_at) AS created_at, event_type, rfid,
               MAX(mouse_probability) AS mouse_probability, COUNT(*) AS n_rows{rfid_is_real_select}
        FROM events
        WHERE created_at BETWEEN '{date_start_utc}' AND '{date_end_utc}'{deleted_filter}{extra_filters}
        GROUP BY block_id
        ORDER BY block_id DESC
    """
    try:
        df = DatabaseCore.read_df_from_database(database, stmt)
    except Exception as e:
        logging.error(f"[JOURNEY] Failed to query flap events: {e}")
        return []

    events = []
    for __, row in df.iterrows():
        try:
            local_str = DateTimeUtil.get_local_date_from_utc_date(row["created_at"])
            dt = pd.to_datetime(local_str).to_pydatetime().replace(tzinfo=None)
            started_local_str = DateTimeUtil.get_local_date_from_utc_date(row["started_at"])
            started_at = pd.to_datetime(started_local_str).to_pydatetime().replace(tzinfo=None)
        except Exception:
            continue
        rfid_is_real_val = row["rfid_is_real"] if has_rfid_is_real else None
        mouse_prob_val = row["mouse_probability"]
        events.append(
            {
                "block_id": int(row["block_id"]),
                "first_id": int(row["first_id"]),
                "dt": dt,
                "started_at": started_at,
                "event_type": row["event_type"],
                "rfid": row["rfid"],
                # None = unknown provenance (pre-migration row), True = real
                # chip read, False = video-only guess.
                "rfid_is_real": (
                    None if rfid_is_real_val is None or pd.isna(rfid_is_real_val)
                    else bool(rfid_is_real_val)
                ),
                # 09.10, Sid ("l'icône souris pour chronologie et parcours"):
                # highest mouse_probability of any photo in this block - a
                # NULL/NaN column (no detection ran, or the row predates it)
                # reads back as 0.0 here, same guard as _serialize_picture's.
                "mouse_probability": (
                    0.0 if mouse_prob_val is None or pd.isna(mouse_prob_val)
                    else float(mouse_prob_val)
                ),
                # 02.11, Sid ("ça me dédouble les événements"): raw frame count
                # for this block, used by _merge_adjacent_blocks_into_visits()
                # below to weight identity votes when pooling several blocks
                # of the same real visit together.
                "n_rows": int(row["n_rows"]) if not pd.isna(row["n_rows"]) else 1,
            }
        )
    return events


def _list_recent_flap_events(limit: int = 50) -> list[dict]:
    """09.10, Sid ("finalement ca me fait une page en trop... page chats, en
    dessous des 4 chips des chats, les 50 derniers événements... et on ne
    garde pas la page chronologie"): the N most recent motion blocks across
    ALL days, not scoped to one calendar day - same row shape as
    _list_flap_events_for_date so _serialize_events() in webui_api.py works
    unchanged on either."""
    database = CONFIG["KITTYHACK_DATABASE_PATH"]
    try:
        columns_info = DatabaseCore.read_column_info_from_database(database, "events")
        column_names = set(info[1] for info in (columns_info or []))
    except Exception:
        column_names = set()
    deleted_filter = " AND deleted != 1" if "deleted" in column_names else ""
    has_rfid_is_real = "rfid_is_real" in column_names
    rfid_is_real_select = ", MAX(rfid_is_real) AS rfid_is_real" if has_rfid_is_real else ""

    stmt = f"""
        SELECT block_id, MIN(id) AS first_id, MIN(created_at) AS started_at,
               MAX(created_at) AS created_at, event_type, rfid,
               MAX(mouse_probability) AS mouse_probability, COUNT(*) AS n_rows{rfid_is_real_select}
        FROM events
        WHERE block_id IS NOT NULL{deleted_filter}
        GROUP BY block_id
        ORDER BY block_id DESC
        LIMIT {int(limit)}
    """
    try:
        df = DatabaseCore.read_df_from_database(database, stmt)
    except Exception as e:
        logging.error(f"[JOURNEY] Failed to query recent flap events: {e}")
        return []

    events = []
    for __, row in df.iterrows():
        try:
            local_str = DateTimeUtil.get_local_date_from_utc_date(row["created_at"])
            dt = pd.to_datetime(local_str).to_pydatetime().replace(tzinfo=None)
            started_local_str = DateTimeUtil.get_local_date_from_utc_date(row["started_at"])
            started_at = pd.to_datetime(started_local_str).to_pydatetime().replace(tzinfo=None)
        except Exception:
            continue
        rfid_is_real_val = row["rfid_is_real"] if has_rfid_is_real else None
        mouse_prob_val = row["mouse_probability"]
        events.append(
            {
                "block_id": int(row["block_id"]),
                "first_id": int(row["first_id"]),
                "dt": dt,
                "started_at": started_at,
                "event_type": row["event_type"],
                "rfid": row["rfid"],
                "rfid_is_real": (
                    None if rfid_is_real_val is None or pd.isna(rfid_is_real_val)
                    else bool(rfid_is_real_val)
                ),
                "mouse_probability": (
                    0.0 if mouse_prob_val is None or pd.isna(mouse_prob_val)
                    else float(mouse_prob_val)
                ),
                "n_rows": int(row["n_rows"]) if not pd.isna(row["n_rows"]) else 1,
            }
        )
    return events


# 02.11, Sid ("ça me dédouble les événements... Nala arrive, vient devant la
# chatière et rentre" - un seul vrai passage affiché en 4, parfois 7+, lignes
# distinctes "X en balade"/"X est entré", chacune avec un nom différent):
# chaque micro-coupure de motion_outside (vote d'identité vidéo qui bascule,
# garde-fou MAX_MOTION_BLOCK_SECONDS, etc. - voir loop.py) clôture le bloc en
# cours et en redémarre un immédiatement (écart mesuré en prod: ~0.1-0.3s,
# jamais un vrai trou). Corriger ça dans loop.py reviendrait à assouplir la
# boucle de contrôle temps réel de la porte - risqué, et hors sujet pour un
# problème qui n'est que de l'affichage/des stats, pas de la sécurité (RFID
# garde la priorité absolue à l'entrée, intouché ici). Cette fonction fusionne
# donc a posteriori, en lecture seule, les blocs consécutifs d'un même vrai
# passage - ne touche jamais à loop.py ni à la boucle de contrôle physique.
VISIT_MERGE_WINDOW_S = 5.0


_REAL_TRANSITION_EVENT_TYPES = (
    EventType.CAT_WENT_INSIDE,
    EventType.CAT_WENT_PROBABLY_INSIDE,
    EventType.CAT_WENT_INSIDE_WITH_MOUSE,
    EventType.CAT_WENT_OUTSIDE,
)


def _merge_one_visit(group: list[dict]) -> dict:
    """Collapse one group of consecutive blocks (same real visit) into a
    single event dict. Identity: a real RFID read anywhere in the group
    always wins (ground truth); otherwise the rfid with the most pooled
    n_rows across the group wins (more actual evidence beats a short,
    noisy tail fragment - this is exactly what would have kept the
    02.11 "Pookie est entré" visit correctly attributed to whichever cat
    dominated the whole approach, not just its last few frames).

    Conclusion (event_type): the LAST block with a REAL transition
    (_REAL_TRANSITION_EVENT_TYPES - an actual in/out crossing), not just
    the chronologically last block in the group. 02.11, real bug caught by
    Sid ("10:51 En vrai: Pookie est sorti - alors que c'est noté 'Pookie en
    balade'"): block A correctly concludes cat_went_outside, then an
    immediately-following block B (cat still visible, now already outside,
    nothing left to transition) correctly concludes motion_outside_only on
    its OWN - that per-block conclusion is right in isolation, but blindly
    taking "the last block's" for the merged visit threw away A's real
    exit in favour of B's mere glance. A glance can never outrank a real
    crossing regardless of which one happened later; only decided by
    recency when there are two (or more) real crossings in the same group.
    Timestamp: the FIRST block's start, since that's when the real visit
    began."""
    group_sorted = sorted(group, key=lambda e: e.get("started_at") or e["dt"])
    if len(group_sorted) == 1:
        ev = dict(group_sorted[0])
        ev.setdefault("block_ids", [ev["block_id"]])
        ev.setdefault("end_dt", ev["dt"])
        ev.setdefault("rfid_votes", {ev["rfid"]: int(ev.get("n_rows") or 1)} if ev.get("rfid") else {})
        return ev

    first, last = group_sorted[0], group_sorted[-1]
    # 02.11, Sid ("confronter les deux votes [Kittyflap/Reolink] avec le
    # RFID") - exposé telle quelle (pas juste le gagnant) pour que
    # l'appelant (webui_api._apply_reolink_cross_check) puisse calculer la
    # force du consensus Kittyflap pour CE gagnant précis, et la comparer
    # au consensus Reolink sur la même fenêtre - un gagnant Kittyflap à
    # 52% n'a pas le même poids qu'un gagnant à 95%.
    totals: dict[str, int] = {}
    for e in group_sorted:
        if e.get("rfid"):
            totals[e["rfid"]] = totals.get(e["rfid"], 0) + int(e.get("n_rows") or 1)

    real = next((e for e in group_sorted if e.get("rfid_is_real") is True), None)
    if real is not None:
        rfid, rfid_is_real = real["rfid"], True
    else:
        rfid = max(totals, key=lambda k: totals[k]) if totals else (last.get("rfid") or "")
        rfid_is_real = False if any(e.get("rfid_is_real") is False for e in group_sorted) else None

    last_real_transition = next(
        (
            e
            for e in reversed(group_sorted)
            if _event_type_first(e.get("event_type")) in _REAL_TRANSITION_EVENT_TYPES
        ),
        None,
    )
    event_type = last_real_transition["event_type"] if last_real_transition is not None else last["event_type"]

    return {
        "block_id": first["block_id"],
        "block_ids": [e["block_id"] for e in group_sorted],
        "first_id": first["first_id"],
        "dt": first.get("started_at") or first["dt"],
        "end_dt": last["dt"],
        "started_at": first.get("started_at") or first["dt"],
        "event_type": event_type,
        "rfid": rfid,
        "rfid_is_real": rfid_is_real,
        "rfid_votes": totals,
        "mouse_probability": max((e.get("mouse_probability") or 0.0) for e in group_sorted),
        "n_rows": sum(int(e.get("n_rows") or 1) for e in group_sorted),
    }


def _merge_adjacent_blocks_into_visits(
    events: list[dict], window_seconds: float = VISIT_MERGE_WINDOW_S
) -> list[dict]:
    """Merge consecutive motion blocks into one visit when they're close in
    time (<= window_seconds, measured end of one block to start of the
    next). Never merges across two blocks that EACH carry a real (chip-read,
    not video-guessed) RFID for a different cat - that's genuine ground
    truth of two different passages, the only case this refuses to merge
    regardless of how close in time they are. Read-only, display/stats
    layer only - loop.py's real-time block-splitting is untouched."""
    if not events:
        return events
    ordered = sorted(events, key=lambda e: e.get("started_at") or e["dt"])

    groups: list[list[dict]] = []
    current: list[dict] | None = None
    group_real_rfid: str | None = None
    prev_end = None
    for ev in ordered:
        start = ev.get("started_at") or ev["dt"]
        is_real = ev.get("rfid_is_real") is True
        conflicts = (
            current is not None
            and group_real_rfid is not None
            and is_real
            and ev["rfid"] != group_real_rfid
        )
        gap_ok = (
            current is not None
            and prev_end is not None
            and (start - prev_end).total_seconds() <= window_seconds
        )
        if current is not None and gap_ok and not conflicts:
            current.append(ev)
        else:
            if current is not None:
                groups.append(current)
            current = [ev]
            group_real_rfid = None
        if is_real and group_real_rfid is None:
            group_real_rfid = ev["rfid"]
        prev_end = ev["dt"]
    if current is not None:
        groups.append(current)

    merged = [_merge_one_visit(g) for g in groups]
    merged.sort(key=lambda e: e["dt"], reverse=True)
    return merged


# 02.11, Sid ("trouver un moyen de fusionner ce que pense HK et ce que
# pense reo... confronter les deux votes avec le RFID", puis "de nuit,
# reolink a aussi une meilleure vision que HK - meilleur IR"): un chat
# tout près de la chatière n'est souvent visible, côté caméra Kittyflap,
# que de face à très courte distance (voir src/model/identity.py -
# fréquemment juste la tête/les oreilles, floues, sans le poitrail/les
# pattes qui portent le vrai signal distinctif) alors que Reolink garde un
# angle de profil exploitable même tout près, et un meilleur capteur IR
# de nuit. Les deux pipelines d'identité tournent aujourd'hui en parfaite
# indépendance (loop.py pour Kittyflap, watchdog.py pour Reolink) -
# Reolink n'a jamais voix au chapitre sur le nom finalement affiché.
#
# Cas réel mesuré le 02.11 (visite 08:29:52-08:30:23, vraie identité
# confirmée visuellement par Sid = Nala) : côté Kittyflap, 238 frames
# votent Pookie contre 52 Nala (82% de consensus interne - majoritaire,
# mais pas franc) ; côté Reolink, 6 frames sur 6 votent Nala (100%).
# Sommer les votes bruts des deux caméras (238+0 Pookie vs 52+6 Nala)
# donnerait TOUJOURS Pookie - Kittyflap capture ~10x plus souvent que
# Reolink (~10 fps contre ~1 frame/s), donc un gagnant Kittyflap fragile
# écraserait numériquement un gagnant Reolink solide mais plus rare.
# Comparer plutôt la FORCE DE CONSENSUS propre à chaque caméra (part de
# ses propres votes qui vont à son propre gagnant) - une caméra sûre
# d'elle (même peu bavarde) doit l'emporter sur une caméra majoritaire
# mais hésitante.
#
# 02.11 (déplacé depuis webui_api.py) : presence.py a besoin de cette même
# fusion+cross-check pour corriger ses propres stats (entrées/sorties,
# temps dehors, dedans/dehors), mais webui_api.py importe DEPUIS
# presence.py (pas l'inverse) - donc ce code vit ici, le module commun aux
# deux, plutôt que dans webui_api.py. EVENT_FRAMES_DIR/_EVENT_FRAME_RE
# dupliqués depuis webui_api.py (même raisonnement que leur propre
# duplication depuis watchdog.py : modules indépendants, pas de dépendance
# à créer juste pour deux constantes).
EVENT_FRAMES_DIR = "/data/watchdog_event_frames"
_EVENT_FRAME_RE = re.compile(r"^(?P<cam>[a-z_]+)_(?P<date>\d{8})_(?P<time>\d{6})_(?P<ms>\d{3})\.jpg$")
_EVENT_FRAME_PAD_S = 15.0
_REOLINK_MIN_FRAMES_FOR_OVERRIDE = 3
# See _apply_reolink_cross_check's docstring - bounds the cold-cache cost
# of resolving presence.py's full-history stats as real history keeps
# accumulating, at the cost of not cross-checking identity for visits
# older than this (they keep their Kittyflap-only resolution).
_REOLINK_CROSS_CHECK_MAX_AGE_DAYS = 3

# even scoped to one date, glob.glob() over several thousand files measured
# ~0.9s on real data - fine once, too slow once per visit per PAGE load
# (chronology/journey/presence stats routinely touch dozens of visits).
# Cached per date: a past/closed date's file list never changes once that
# day is over, so it's cached forever (within this process's lifetime);
# TODAY's is still actively growing, so it gets a short TTL instead of
# indefinite caching. Capped at _REOLINK_INDEX_CACHE_MAX_DATES entries
# (evict oldest) so repeatedly browsing many distinct historical dates
# can't grow this without bound.
_reolink_frame_index_cache: dict[str, tuple[float, list[tuple[datetime, str]]]] = {}
_REOLINK_INDEX_CACHE_TTL_TODAY_S = 60.0
_REOLINK_INDEX_CACHE_MAX_DATES = 30


def _reolink_frame_index_for_date(date_str: str) -> list[tuple[datetime, str]]:
    """(dt, filepath) for every chatiere_* frame on this local calendar date
    (YYYYMMDD), filename parsing only - no file I/O beyond the directory
    listing itself. EVENT_FRAMES_DIR holds every Reolink frame ever
    captured (500k+ files across the full history) - glob.glob() scoped to
    `chatiere_{date_str}_*.jpg` only touches that one day's files (a few
    thousand, not half a million). That's fine for a single detail view
    (opened once), but would be a severe regression if called once per
    visit for a whole chronology/journey/presence-stats PAGE of visits -
    which is exactly what _apply_reolink_cross_check does below."""
    today_str = datetime.now(ZoneInfo(CONFIG["TIMEZONE"])).strftime("%Y%m%d")
    cached = _reolink_frame_index_cache.get(date_str)
    if cached is not None:
        cached_at, index = cached
        if date_str != today_str or (time.monotonic() - cached_at) < _REOLINK_INDEX_CACHE_TTL_TODAY_S:
            return index

    out = []
    for filepath in glob.glob(os.path.join(EVENT_FRAMES_DIR, f"chatiere_{date_str}_*.jpg")):
        filename = os.path.basename(filepath)
        m = _EVENT_FRAME_RE.match(filename)
        if not m:
            continue
        try:
            dt = datetime.strptime(m.group("date") + m.group("time"), "%Y%m%d%H%M%S")
        except ValueError:
            continue
        out.append((dt, filepath))

    if len(_reolink_frame_index_cache) >= _REOLINK_INDEX_CACHE_MAX_DATES:
        oldest_key = min(_reolink_frame_index_cache, key=lambda k: _reolink_frame_index_cache[k][0])
        del _reolink_frame_index_cache[oldest_key]
    _reolink_frame_index_cache[date_str] = (time.monotonic(), out)
    return out


def _reolink_top1_name(filepath: str) -> str | None:
    """This frame's own highest-probability detection name, read from its
    JSON sidecar - only called for frames that actually fall inside some
    visit's time window (see _apply_reolink_cross_check), never for a
    whole day's worth of frames at once."""
    json_path = filepath[:-4] + ".json"
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:
        return None
    if not raw:
        return None
    best = max(raw, key=lambda d: d.get("probability", 0))
    return best.get("name") or None


def _apply_reolink_cross_check(visits: list[dict], cat_name_dict: dict) -> list[dict]:
    """Mutate each merged visit's rfid/rfid_is_real in place when Reolink's
    own internal consensus for a DIFFERENT cat is stronger than Kittyflap's
    for its current winner - never when a real RFID read is already present
    (ground truth, untouchable), and never on fewer than
    _REOLINK_MIN_FRAMES_FOR_OVERRIDE Reolink frames (too little evidence to
    trust over a real Kittyflap majority, however weak).

    Builds the Reolink frame index ONCE per distinct calendar date actually
    needed across all `visits` (not once per visit - see
    _reolink_frame_index_for_date's docstring), then does the per-visit
    windowing/tallying purely in memory.

    Only considers visits within _REOLINK_CROSS_CHECK_MAX_AGE_DAYS: each
    extra distinct calendar date touched costs one more
    glob.glob() of a few thousand files (~1s, measured) on a cold cache -
    presence.py's stat functions resolve ALL history (months, eventually
    years), and without this bound the cold cost grows without limit as
    more days accumulate (996 blocks/16 days measured 02.11 -> ~24s cold
    already). Older visits keep whatever Kittyflap-only identity they
    already had - correct the vast majority of the time, and presence
    stats care about recent activity far more than a years-old visit's
    exact identity."""
    cutoff = datetime.now() - timedelta(days=_REOLINK_CROSS_CHECK_MAX_AGE_DAYS)
    candidates = [
        v
        for v in visits
        if v.get("rfid_is_real") is not True and (v.get("rfid_votes") or {}) and v["dt"] >= cutoff
    ]
    if not candidates:
        return visits

    windows = {}
    dates_needed: set[str] = set()
    for v in candidates:
        start = v["dt"] - timedelta(seconds=_EVENT_FRAME_PAD_S)
        end = v["end_dt"] + timedelta(seconds=_EVENT_FRAME_PAD_S)
        windows[id(v)] = (start, end)
        d = start.date()
        while d <= end.date():
            dates_needed.add(d.strftime("%Y%m%d"))
            d += timedelta(days=1)

    frame_index: list[tuple[datetime, str]] = []
    for date_str in dates_needed:
        frame_index.extend(_reolink_frame_index_for_date(date_str))
    frame_index.sort(key=lambda t: t[0])

    name_to_rfid = {v: k for k, v in cat_name_dict.items()}
    for visit in candidates:
        start, end = windows[id(visit)]
        reo_tally: dict[str, int] = {}
        for dt, filepath in frame_index:
            if start <= dt <= end:
                name = _reolink_top1_name(filepath)
                if name:
                    reo_tally[name] = reo_tally.get(name, 0) + 1
        reo_total = sum(reo_tally.values())
        if reo_total < _REOLINK_MIN_FRAMES_FOR_OVERRIDE:
            continue

        kf_votes: dict[str, int] = visit.get("rfid_votes") or {}
        kf_total = sum(kf_votes.values())
        kf_rfid = visit.get("rfid")
        kf_name = cat_name_dict.get(kf_rfid, kf_rfid or "")
        kf_consensus = kf_votes.get(kf_rfid, 0) / kf_total if kf_total else 0.0

        reo_name, reo_votes = max(reo_tally.items(), key=lambda kv: kv[1])
        if reo_name == kf_name:
            continue
        reo_consensus = reo_votes / reo_total
        if reo_consensus > kf_consensus:
            reo_rfid = name_to_rfid.get(reo_name)
            if reo_rfid:
                logging.info(
                    f"[JOURNEY] Reolink cross-check: visit at {visit['dt']} - Kittyflap said "
                    f"'{kf_name}' ({kf_consensus:.0%} of {kf_total} frames), Reolink says "
                    f"'{reo_name}' ({reo_consensus:.0%} of {reo_total} frames) - overriding."
                )
                visit["rfid"] = reo_rfid
                visit["rfid_is_real"] = False
    return visits


def _merge_and_cross_check_visits(raw_events: list[dict], cat_name_dict: dict) -> list[dict]:
    """The one call site every endpoint/stat function should use instead of
    _merge_adjacent_blocks_into_visits directly - same merge, plus the
    Reolink cross-check above."""
    visits = _merge_adjacent_blocks_into_visits(raw_events)
    return _apply_reolink_cross_check(visits, cat_name_dict)


def _resolve_visit_block_ids(block_id: int, search_limit: int = 5000) -> list[int]:
    """02.11, Sid ("avant je voyais le clapet et le bout de queue qui
    montrait qu'il était bien rentré"): the frontend's event detail view
    (EventModal, GET /app-api/v1/events/{block_id}) only ever requests ONE
    block_id - the merged visit's primary one (see
    _merge_adjacent_blocks_into_visits above). Without this, the detail view
    for a merged visit only shows the FIRST original block's photos, missing
    everything from the later blocks - including, for an entry, the actual
    crossing frame, which usually happens near the END of a fragmented
    sequence, not the start. Resolves one original block_id back to every
    original block_id of the visit it belongs to, so callers can fetch
    photos/timeline for the whole visit instead of just its first fragment.
    Falls back to [block_id] alone if it can't be found (older than
    search_limit, or was never part of a merge)."""
    raw = _list_recent_flap_events(search_limit)
    merged = _merge_adjacent_blocks_into_visits(raw)
    for visit in merged:
        ids = visit.get("block_ids") or [visit["block_id"]]
        if block_id in ids:
            return ids
    return [block_id]


def _correlate(events: list[dict], samples: list[dict]) -> list[dict]:
    """Attach nearby watchdog samples to flap events (within
    CORRELATION_WINDOW_S of the event's full [start, end] span, not just a
    single point - 02.11: events can now be a merged multi-block visit
    spanning minutes, see _merge_adjacent_blocks_into_visits() above);
    mutate events in place with a "cluster" key and return the leftover
    samples with no nearby event."""
    remaining = list(samples)
    # Newest-first processing (events are already sorted that way) so a
    # sample between two close events attaches to the more recent one.
    for ev in events:
        start = ev["dt"]
        end = ev.get("end_dt", ev["dt"])
        cluster, still_remaining = [], []
        for s in remaining:
            if (start - s["dt"]).total_seconds() <= CORRELATION_WINDOW_S and (s["dt"] - end).total_seconds() <= CORRELATION_WINDOW_S:
                cluster.append(s)
            else:
                still_remaining.append(s)
        cluster.sort(key=lambda s: s["dt"])
        ev["cluster"] = cluster
        remaining = still_remaining
    return remaining


# 09.09, Sid: "quand c'est au même moment, mettre dans le même carré sur la
# même ligne" - two different outdoor cameras catching the same passage a
# few seconds apart were showing up as two separate full-width cards.
# Chain-grouped (gap <= window between consecutive captures, chronologically)
# rather than a fixed window from the first one, so one cat wandering past
# several cameras over a minute still reads as a single moment.
SOLO_GROUP_WINDOW_S = 60


def _group_solo_samples(solo_samples: list[dict]) -> list[list[dict]]:
    """Chain-group solo captures with no nearby flap event into one shared
    timeline entry when they're within SOLO_GROUP_WINDOW_S of each other."""
    if not solo_samples:
        return []
    ordered = sorted(solo_samples, key=lambda s: s["dt"])
    groups = [[ordered[0]]]
    for s in ordered[1:]:
        if (s["dt"] - groups[-1][-1]["dt"]).total_seconds() <= SOLO_GROUP_WINDOW_S:
            groups[-1].append(s)
        else:
            groups.append([s])
    return groups


def _capture_card_html(sample: dict) -> str:
    filename = html_module.escape(sample["filename"])
    cam = html_module.escape(_cam_label(sample["cam"]))
    time_str = sample["dt"].strftime("%H:%M:%S")
    src = f"/watchdog-sample/{filename}"
    return f'''
    <div class="kh-journey-capture" data-filename="{filename}">
        <img src="{src}" loading="lazy" decoding="async" />
        <div class="kh-journey-capture-label">{cam} &middot; {time_str}</div>
        <div class="kh-journey-capture-actions">
            <button type="button" class="kh-journey-action-btn" data-journey-action="send"
                data-filename="{filename}" title="{html_module.escape(_("Send to Label Studio"))}">
                {icon_svg("upload", margin_left="0", margin_right="0")}
            </button>
            <button type="button" class="kh-journey-action-btn kh-journey-action-delete" data-journey-action="delete"
                data-filename="{filename}" title="{html_module.escape(_("Delete"))}">
                {icon_svg("trash-can", margin_left="0", margin_right="0")}
            </button>
        </div>
    </div>'''


_INSIDE_EVENT_TYPES = (
    EventType.CAT_WENT_INSIDE,
    EventType.CAT_WENT_PROBABLY_INSIDE,
    EventType.CAT_WENT_INSIDE_WITH_MOUSE,
)

# 03.10, Sid: for gender-correct French phrasing ("est entre" vs "est
# entree") - Nala is the only female, Boubou/Patoune/Pookie are male.
# Hardcoded rather than a DB column: a 4-cat household, told directly by
# Sid, not expected to grow or change. French-only on purpose (not routed
# through _()) since gender agreement on a verb ending isn't something the
# plain EN->FR msgid translation handles, and this app is French-only here.
_FEMALE_CAT_NAMES = {"nala"}


def _event_type_first(event_type) -> str:
    """The real event_type, stripping any ',suffix' flags (e.g.
    "cat_went_outside,per_cat_prey_detection_disabled").

    06.10, Sid ("deux événements Boubou sans 'est sorti'"): exact-match
    comparisons against the raw event_type column silently excluded every
    event for a cat with per-cat prey detection disabled (currently
    Boubou) - his motion always carries that extra flag, so Parcours fell
    back to the plain icon+name display with no direction. Same fix as
    presence.py's own _event_type_first."""
    if not event_type:
        return ""
    return str(event_type).split(",", 1)[0]


def _last_known_status_before(rfid: str, before_dt_local) -> str | None:
    """07.10, Sid ("quand il regarde dehors OU dedans... la, il est deja
    dehors, il passe"): a MOTION_OUTSIDE_ONLY event with an RFID match can
    mean two very different things - a cat still INSIDE glancing out through
    the flap without crossing, or a cat ALREADY outside just being picked up
    nearby (RFID range, or a watchdog camera correlated by time only). The
    direct signal is this cat's own last known in/out status right before
    this event - not guessed from what happens after. Returns "in", "out",
    or None if no prior event exists for this rfid at all (new/never-seen
    cat - treated as "in" by the caller, the safer/previous default)."""
    if not rfid:
        return None
    try:
        tz = ZoneInfo(CONFIG["TIMEZONE"])
        before_utc = (
            before_dt_local.replace(tzinfo=tz).astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%d %H:%M:%S%z")
        )
        database = CONFIG["KITTYHACK_DATABASE_PATH"]
        columns_info = DatabaseCore.read_column_info_from_database(database, "events")
        column_names = set(info[1] for info in (columns_info or []))
        deleted_filter = " AND deleted != 1" if "deleted" in column_names else ""
        stmt = f"""
            SELECT event_type FROM events
            WHERE rfid = '{rfid.replace(chr(39), chr(39) + chr(39))}'
              AND created_at < '{before_utc}'{deleted_filter}
            ORDER BY created_at DESC LIMIT 1
        """
        df = DatabaseCore.read_df_from_database(database, stmt)
        if df.empty:
            return None
        last_type = _event_type_first(df.iloc[0]["event_type"])
        if last_type in _INSIDE_EVENT_TYPES:
            return "in"
        if last_type == EventType.CAT_WENT_OUTSIDE:
            return "out"
        return None
    except Exception as e:
        logging.warning(f"[JOURNEY] Failed to resolve last known status for rfid {rfid}: {e}")
        return None


def _rfid_uncertainty_marker(rfid_is_real: bool | None) -> str:
    """08.10, Sid ("il n'y a qu'une vraie lecture de puce, le reste c'est de
    la video, ca devrait pas influencer l'ouverture" - then, seeing "Nala"
    shown for what was actually Patoune/Pookie: "c'est quoi qui dit Nala?"):
    ``rfid_is_real is False`` means this identity came from the video
    model's guess, not a genuine chip read - flag it visibly rather than
    presenting a guess with the same confidence as a confirmed scan. None
    (pre-migration rows, provenance unknown) and True (real read) both stay
    unmarked - only a KNOWN guess gets flagged."""
    if rfid_is_real is False:
        return ' <span class="kh-rfid-unconfirmed">(non confirmé)</span>'
    return ""


def _presence_direction_text(
    event_type, cat_name: str | None, rfid: str | None = None, event_dt=None,
    rfid_is_real: bool | None = None,
) -> str | None:
    """"<Name> est entre(e)/sorti(e)" for a real in/out event, or None for
    any other event type (left to the plain icon+name display)."""
    is_known = cat_name is not None
    feminine = is_known and cat_name.strip().lower() in _FEMALE_CAT_NAMES
    subject = html_module.escape(str(cat_name)) if is_known else "Un animal"
    # 10.10, Sid ("au lieu d'avoir le texte complet... on peut juste avoir
    # un petit rond de couleur? Comme le rond de connexion dans la
    # bannière, avec le texte qui s'affiche si on clique"): no longer
    # appended to the text itself - the React side (EventCard.tsx) renders
    # this as a small tappable dot from the separate rfid_confirmed field
    # _serialize_events already exposes, same pattern as TopBanner's own
    # connection dot. _rfid_uncertainty_marker/its HTML-inline marker stays
    # for the still-Shiny pages that haven't migrated to this pattern yet.
    real_type = _event_type_first(event_type)

    # 04.10, Sid: meme convention couleur que les badges maison/arbre de
    # l'onglet Presence et les icones fleches partagees (helper.py).
    if real_type in _INSIDE_EVENT_TYPES:
        return f'<span class="kh-dir-in">{subject} est entré{"e" if feminine else ""}</span>'
    if real_type == EventType.CAT_WENT_OUTSIDE:
        return f'<span class="kh-dir-out">{subject} est sorti{"e" if feminine else ""}</span>'
    # 07.10, Sid ("boubou est passé devant = jete un oeil, faut que ca note
    # ca" then "la il est deja dehors, il passe... si suivi d'aucune entree
    # = est passe"): a glance (motion outside, no actual passage) was
    # falling through to the plain icon+name fallback. But "looked outside
    # but didn't go out" (same wording the push notification already uses)
    # only makes sense for a cat that was INSIDE glancing out - for a cat
    # ALREADY outside (picked up again by the flap's own sensor, or just
    # correlated by timing with a watchdog capture), it reads as nonsense.
    # Distinguished by this cat's own last known in/out status right before
    # this event, not guessed from what happens after - direct rather than
    # inferred.
    if real_type == EventType.MOTION_OUTSIDE_ONLY:
        # 09.10, Sid ("au lieu de dire: boubou a regardé mais sans rentrer:
        # on peut juste mettre (dedans comme dehors) un chat est passé" then
        # "ou plus joli: chat en balade"): the previous wording needed
        # last_status (out vs unknown/in) to pick between two different
        # sentences - replaced with one that reads fine either way, so that
        # branch is gone. _direction_code() in webui_api.py still computes
        # glance_in/glance_out separately for the icon/color, untouched.
        text = _("{cat} on the prowl.").format(cat=subject) if is_known else _("A cat is on the prowl.")
        return f'<span class="kh-dir-glance">{text}</span>'
    return None


def _event_card_html(
    ev: dict, cat_name_dict: dict, show_event_btn_html: str = "", simple: bool = False
) -> str:
    """``simple=True`` (06.10, Sid, new "Chronologie" tab - "un truc simple
    et immediatement lisible comme sureflap, mais on garde l'icone pour voir
    les photos"): drops the inline thumbnail and the correlated watchdog-
    camera cluster entirely - just the direction line, time, and the same
    show-event icon button as a link to the real photos, modeled on her
    Sureflap-style reference screenshot (flat list, no raw capture grid).
    Parcours (this same card, simple=False) keeps everything as before."""
    resolved_name = cat_name_dict.get(ev["rfid"]) if ev["rfid"] else None
    cat_name_display = resolved_name or (
        _("Unknown RFID: {}").format(ev["rfid"]) if ev["rfid"] else _("No RFID found")
    )
    direction_text = _presence_direction_text(
        ev.get("event_type"), resolved_name, rfid=ev.get("rfid"), event_dt=ev.get("dt"),
        rfid_is_real=ev.get("rfid_is_real"),
    )
    cat_line = (
        direction_text
        if direction_text is not None
        else (
            f'{icon_svg("cat", margin_left="0")} {html_module.escape(str(cat_name_display))}'
            + (_rfid_uncertainty_marker(ev.get("rfid_is_real")) if resolved_name else "")
        )
    )

    time_str = ev["dt"].strftime("%H:%M:%S")
    # 07.10, Sid ("je peux avoir la date sur la ligne? juste 06.10, pas
    # besoin de l'annee"): the simple card is also used for the Presence
    # tab's per-cat filtered history, which spans many days unlike
    # Chronologie/Parcours (always scoped to one picked date) - no year,
    # matches the DD.MM format used elsewhere in this app.
    date_str = ev["dt"].strftime("%d.%m")
    action_block = (
        f'<div class="kh-journey-event-action">{show_event_btn_html}</div>'
        if show_event_btn_html
        else ""
    )

    if simple:
        return f'''
        <div class="kh-journey-item kh-chrono-event">
            <div class="kh-chrono-event-info">
                <div class="kh-chrono-event-time">{icon_svg("door-open", margin_left="0")} {date_str} &middot; {time_str}</div>
                <div class="kh-chrono-event-cat">{cat_line}</div>
            </div>
            {action_block}
        </div>'''

    thumb_src = f"/thumb/{ev['first_id']}.jpg"
    cluster_html = "".join(_capture_card_html(s) for s in ev["cluster"])
    cluster_block = (
        f'<div class="kh-journey-cluster">{cluster_html}</div>' if cluster_html else ""
    )
    # 04.10, Sid: "un lien direct avec les photos entre parcours et photos -
    # quand je clique, ca envoie vers la photo en question" - reuse the
    # SAME event detail modal (playback/photos/download) the magnifying-
    # glass button already opens on the Live view events table, rather than
    # building a second, separate photo viewer.
    return f'''
    <div class="kh-journey-item kh-journey-event">
        <div class="kh-journey-event-main">
            <img class="kh-journey-event-thumb" src="{thumb_src}" loading="lazy" decoding="async" />
            <div class="kh-journey-event-info">
                <div class="kh-journey-event-time">{icon_svg("door-open", margin_left="0")} {time_str}</div>
                <div class="kh-journey-event-cat">{cat_line}</div>
            </div>
            {action_block}
        </div>
        {cluster_block}
    </div>'''


def _solo_group_html(group: list[dict]) -> str:
    caps = "".join(_capture_card_html(s) for s in group)
    return f'''
    <div class="kh-journey-item kh-journey-solo">
        <div class="kh-journey-cluster kh-journey-cluster-standalone">{caps}</div>
    </div>'''


def register_journeys(input, output, session, ctx: SessionContext):
    """Register the Journey tab UI and handlers."""

    reload_trigger_journeys = reactive.Value(0)

    @output
    @render.ui
    def ui_journey_date():
        return ui.div(
            ui.row(
                ui.div(
                    ui.div(
                        ui.input_action_button(
                            "journey_button_decrement",
                            "",
                            icon=icon_svg("angle-left", margin_right="auto"),
                            class_="btn-date-control",
                        ),
                        class_="col-auto px-1",
                    ),
                    ui.div(
                        ui.input_date(
                            "journey_date_selector", "", format=CONFIG["DATE_FORMAT"]
                        ),
                        class_="col-auto px-1",
                    ),
                    ui.div(
                        ui.input_action_button(
                            "journey_button_increment",
                            "",
                            icon=icon_svg("angle-right", margin_right="auto"),
                            class_="btn-date-control",
                        ),
                        class_="col-auto px-1",
                    ),
                    class_="d-flex justify-content-center align-items-center flex-nowrap",
                ),
                ui.div(
                    ui.input_action_button(
                        "journey_button_today",
                        _("Today"),
                        icon=icon_svg("calendar-day"),
                        class_="btn-date-filter",
                    ),
                    class_="col-auto px-1",
                ),
                ui.div(
                    ui.input_action_button(
                        "journey_button_reload",
                        "",
                        icon=icon_svg("rotate", margin_right="auto"),
                        class_="btn-date-filter",
                    ),
                    class_="col-auto px-1",
                ),
                class_="d-flex justify-content-center align-items-center",
            ),
            class_="container",
        )

    @reactive.Effect
    @reactive.event(input.journey_button_decrement, ignore_none=True)
    def dec_journey_date():
        current_date = input.journey_date_selector()
        if current_date:
            new_date = pd.to_datetime(current_date).date() - timedelta(days=1)
            session.send_input_message(
                "journey_date_selector", {"value": new_date.strftime("%Y-%m-%d")}
            )

    @reactive.Effect
    @reactive.event(input.journey_button_increment, ignore_none=True)
    def inc_journey_date():
        current_date = input.journey_date_selector()
        if current_date:
            new_date = pd.to_datetime(current_date).date() + timedelta(days=1)
            session.send_input_message(
                "journey_date_selector", {"value": new_date.strftime("%Y-%m-%d")}
            )

    @reactive.Effect
    @reactive.event(input.journey_button_today, ignore_none=True)
    def reset_journey_date():
        now = datetime.now()
        session.send_input_message(
            "journey_date_selector", {"value": now.strftime("%Y-%m-%d")}
        )

    @output
    @render.ui
    @reactive.event(
        input.journey_date_selector,
        input.journey_button_reload,
        reload_trigger_journeys,
        ignore_none=True,
    )
    def ui_journey_timeline():
        try:
            local_date = input.journey_date_selector()
            date_str = local_date.strftime("%Y-%m-%d")
            date_start_utc, date_end_utc = _local_day_utc_range(local_date)

            events = _list_flap_events_for_date(date_start_utc, date_end_utc)
            samples = _list_training_samples_for_date(date_str)
            solo_samples = _correlate(events, samples)
            solo_groups = _group_solo_samples(solo_samples)

            if not events and not solo_groups:
                return ui.div(
                    ui.HTML(f"<p class='text-center'>{_('No pictures found.')}</p>"),
                    class_="container",
                )

            cat_name_dict = CatsRepo.get_cat_name_rfid_dict(
                CONFIG["KITTYHACK_DATABASE_PATH"]
            )

            timeline_items = [(ev["dt"], "event", ev) for ev in events]
            timeline_items += [(g[-1]["dt"], "solo_group", g) for g in solo_groups]
            timeline_items.sort(key=lambda t: t[0], reverse=True)

            html_parts = []
            for __, kind, item in timeline_items:
                if kind == "event":
                    btn_id = f"btn_show_event_journey_{item['block_id']}"
                    show_event_server(btn_id, item["block_id"])
                    html_parts.append(
                        _event_card_html(item, cat_name_dict, str(btn_show_event(btn_id)))
                    )
                else:
                    html_parts.append(_solo_group_html(item))

            return ui.div(
                ui.HTML('<div class="kh-journey-list">' + "".join(html_parts) + "</div>"),
                class_="container",
            )
        except Exception as e:
            logging.error(f"[JOURNEY] Failed to build timeline: {e}")
            return ui.div(
                ui.HTML(f"<p class='text-center'>{_('Failed to load the journey.')}</p>"),
                class_="container",
            )

    def _safe_sample_path(filename: str) -> str | None:
        """Reject anything that isn't a plain filename matching the expected
        watchdog-sample pattern, before touching the filesystem."""
        if not filename or not _SAMPLE_RE.match(filename):
            return None
        if os.path.basename(filename) != filename:
            return None
        path = os.path.join(TRAINING_SAMPLES_DIR, filename)
        if not os.path.abspath(path).startswith(os.path.abspath(TRAINING_SAMPLES_DIR) + os.sep):
            return None
        return path

    @reactive.effect
    @reactive.event(input.journey_capture_action)
    async def handle_journey_capture_action():
        try:
            import json

            payload = json.loads(input.journey_capture_action() or "{}")
            filename = str(payload.get("filename") or "")
            action = str(payload.get("action") or "")

            path = _safe_sample_path(filename)
            if not path:
                raise ValueError(f"Invalid or unsafe filename '{filename}'")

            if action == "delete":
                if os.path.exists(path):
                    os.remove(path)
                reload_trigger_journeys.set(reload_trigger_journeys() + 1)
                ui.notification_show(_("Capture deleted."), type="message", duration=3)

            elif action == "send":
                if not PAWS_REVIEW_UPLOAD_TOKEN:
                    ui.notification_show(
                        _("Paws Review upload token is not configured."),
                        type="warning",
                        duration=5,
                    )
                    return
                if not os.path.exists(path):
                    raise ValueError(f"Capture file no longer exists: {filename}")
                with open(path, "rb") as f:
                    img_bytes = f.read()

                ui.notification_show(
                    ui.HTML(
                        '<div class="d-flex align-items-center gap-2">'
                        '<div class="spinner-border spinner-border-sm" role="status" aria-hidden="true"></div>'
                        f"<span>{_('Sending picture to Paws Review...')}</span>"
                        "</div>"
                    ),
                    id="journey_ls_upload_progress",
                    type="message",
                    duration=None,
                )
                detections_json = _detections_sidecar_for_paws_review(path)
                success = await asyncio.to_thread(
                    _upload_to_paws_review, img_bytes, filename, detections_json
                )
                ui.notification_remove("journey_ls_upload_progress")
                if success:
                    ui.notification_show(
                        _("Picture sent to Paws Review successfully."),
                        type="message",
                        duration=3,
                    )
                else:
                    ui.notification_show(
                        _("Failed to send picture to Paws Review."),
                        type="error",
                        duration=5,
                    )
            else:
                raise ValueError(f"Unknown journey capture action '{action}'")
        except Exception as e:
            logging.warning(f"[JOURNEY] Failed to handle capture action: {e}")
            ui.notification_show(
                _("Failed to process the requested action."), type="error", duration=5
            )

    # ---- Chronologie: 06.10, Sid - a second, simpler tab reusing the same
    # flap-event data as Parcours (just renamed from "Chronologie" back to
    # "Parcours", see ui.py), but WITHOUT the correlated watchdog-camera
    # captures (the raw per-camera snapshot grid she circled/crossed out as
    # noise in her Sureflap/Carba reference screenshots) and without solo
    # capture groups (those are 100% raw camera captures, no flap event at
    # all). Own input ids throughout (chronology_* instead of journey_*) -
    # two ui.input_date()/ui.input_action_button() widgets can't share one
    # Shiny input id across two simultaneously-mounted nav_panels.

    @output
    @render.ui
    def ui_chronology_date():
        return ui.div(
            ui.row(
                ui.div(
                    ui.div(
                        ui.input_action_button(
                            "chronology_button_decrement",
                            "",
                            icon=icon_svg("angle-left", margin_right="auto"),
                            class_="btn-date-control",
                        ),
                        class_="col-auto px-1",
                    ),
                    ui.div(
                        ui.input_date(
                            "chronology_date_selector", "", format=CONFIG["DATE_FORMAT"]
                        ),
                        class_="col-auto px-1",
                    ),
                    ui.div(
                        ui.input_action_button(
                            "chronology_button_increment",
                            "",
                            icon=icon_svg("angle-right", margin_right="auto"),
                            class_="btn-date-control",
                        ),
                        class_="col-auto px-1",
                    ),
                    class_="d-flex justify-content-center align-items-center flex-nowrap",
                ),
                ui.div(
                    ui.input_action_button(
                        "chronology_button_today",
                        _("Today"),
                        icon=icon_svg("calendar-day"),
                        class_="btn-date-filter",
                    ),
                    class_="col-auto px-1",
                ),
                ui.div(
                    ui.input_action_button(
                        "chronology_button_reload",
                        "",
                        icon=icon_svg("rotate", margin_right="auto"),
                        class_="btn-date-filter",
                    ),
                    class_="col-auto px-1",
                ),
                class_="d-flex justify-content-center align-items-center",
            ),
            class_="container",
        )

    @reactive.Effect
    @reactive.event(input.chronology_button_decrement, ignore_none=True)
    def dec_chronology_date():
        current_date = input.chronology_date_selector()
        if current_date:
            new_date = pd.to_datetime(current_date).date() - timedelta(days=1)
            session.send_input_message(
                "chronology_date_selector", {"value": new_date.strftime("%Y-%m-%d")}
            )

    @reactive.Effect
    @reactive.event(input.chronology_button_increment, ignore_none=True)
    def inc_chronology_date():
        current_date = input.chronology_date_selector()
        if current_date:
            new_date = pd.to_datetime(current_date).date() + timedelta(days=1)
            session.send_input_message(
                "chronology_date_selector", {"value": new_date.strftime("%Y-%m-%d")}
            )

    @reactive.Effect
    @reactive.event(input.chronology_button_today, ignore_none=True)
    def reset_chronology_date():
        now = datetime.now()
        session.send_input_message(
            "chronology_date_selector", {"value": now.strftime("%Y-%m-%d")}
        )

    @output
    @render.ui
    @reactive.event(
        input.chronology_date_selector,
        input.chronology_button_reload,
        reload_trigger_journeys,
        ignore_none=True,
    )
    def ui_chronology_timeline():
        try:
            local_date = input.chronology_date_selector()
            date_start_utc, date_end_utc = _local_day_utc_range(local_date)

            events = _list_flap_events_for_date(date_start_utc, date_end_utc)
            if not events:
                return ui.div(
                    ui.HTML(f"<p class='text-center'>{_('No pictures found.')}</p>"),
                    class_="container",
                )

            cat_name_dict = CatsRepo.get_cat_name_rfid_dict(
                CONFIG["KITTYHACK_DATABASE_PATH"]
            )

            html_parts = []
            for ev in events:
                btn_id = f"btn_show_event_chronology_{ev['block_id']}"
                show_event_server(btn_id, ev["block_id"])
                html_parts.append(
                    _event_card_html(
                        ev, cat_name_dict, str(btn_show_event(btn_id)), simple=True
                    )
                )

            return ui.div(
                ui.HTML('<div class="kh-journey-list kh-chrono-list">' + "".join(html_parts) + "</div>"),
                class_="container",
            )
        except Exception as e:
            logging.error(f"[CHRONOLOGY] Failed to build timeline: {e}")
            return ui.div(
                ui.HTML(f"<p class='text-center'>{_('Failed to load the journey.')}</p>"),
                class_="container",
            )
