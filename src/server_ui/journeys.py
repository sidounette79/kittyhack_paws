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
import logging
import os
import re
import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
from faicons import icon_svg
from shiny import reactive, render, ui

from src.baseconfig import CONFIG, set_language
from src.database import CatsRepo, DatabaseCore
from src.server_ui.event_modal import btn_show_event, show_event_server
from src.helper import DateTimeUtil, EventType
from src.labelstudio_api import upload_image_to_labelstudio_project
from src.system import LabelStudioInstall
from src.server_ui.context import SessionContext

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
    "chatiere": lambda: _("Flap cam"),
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


def _list_flap_events_for_date(date_start_utc: str, date_end_utc: str) -> list[dict]:
    """One row per motion block for the day, with a representative photo id
    (for its thumbnail) - db_get_motion_blocks() doesn't carry an id, so this
    is a small purpose-built query rather than reusing it."""
    database = CONFIG["KITTYHACK_DATABASE_PATH"]
    try:
        columns_info = DatabaseCore.read_column_info_from_database(database, "events")
        column_names = set(info[1] for info in (columns_info or []))
    except Exception:
        column_names = set()
    deleted_filter = " AND deleted != 1" if "deleted" in column_names else ""

    stmt = f"""
        SELECT block_id, MIN(id) AS first_id, MAX(created_at) AS created_at, event_type, rfid
        FROM events
        WHERE created_at BETWEEN '{date_start_utc}' AND '{date_end_utc}'{deleted_filter}
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


def _correlate(events: list[dict], samples: list[dict]) -> list[dict]:
    """Attach nearby watchdog samples to flap events (within
    CORRELATION_WINDOW_S); mutate events in place with a "cluster" key and
    return the leftover samples with no nearby event."""
    remaining = list(samples)
    # Newest-first processing (events are already sorted that way) so a
    # sample between two close events attaches to the more recent one.
    for ev in events:
        cluster, still_remaining = [], []
        for s in remaining:
            delta = abs((s["dt"] - ev["dt"]).total_seconds())
            if delta <= CORRELATION_WINDOW_S:
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


def _presence_direction_text(
    event_type, cat_name: str | None, rfid: str | None = None, event_dt=None
) -> str | None:
    """"<Name> est entre(e)/sorti(e)" for a real in/out event, or None for
    any other event type (left to the plain icon+name display)."""
    is_known = cat_name is not None
    feminine = is_known and cat_name.strip().lower() in _FEMALE_CAT_NAMES
    subject = html_module.escape(str(cat_name)) if is_known else "Un animal"
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
        last_status = _last_known_status_before(rfid, event_dt) if (rfid and event_dt) else None
        if last_status == "out":
            text = _("{cat} walked by outside.").format(cat=subject) if is_known else _(
                "A cat walked by outside."
            )
        else:
            text = _("{cat} looked outside but didn't go out.").format(cat=subject) if is_known else _(
                "A cat looked outside but didn't go out."
            )
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
        ev.get("event_type"), resolved_name, rfid=ev.get("rfid"), event_dt=ev.get("dt")
    )
    cat_line = (
        direction_text
        if direction_text is not None
        else f'{icon_svg("cat", margin_left="0")} {html_module.escape(str(cat_name_display))}'
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
                project_id = CONFIG.get("LABELSTUDIO_PROJECT", "").strip()
                api_token = CONFIG.get("LABELSTUDIO_API_TOKEN", "").strip()
                if not project_id or not api_token:
                    ui.notification_show(
                        _(
                            "Label Studio is not configured. Please set an API token and select a project in the settings."
                        ),
                        type="warning",
                        duration=5,
                    )
                    return
                if not LabelStudioInstall.get_labelstudio_status():
                    ui.notification_show(
                        _("Label Studio is not running."), type="warning", duration=5
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
                        f"<span>{_('Sending picture to Label Studio...')}</span>"
                        "</div>"
                    ),
                    id="journey_ls_upload_progress",
                    type="message",
                    duration=None,
                )
                success = await asyncio.to_thread(
                    upload_image_to_labelstudio_project,
                    project_id=int(project_id),
                    image_bytes=img_bytes,
                    filename=filename,
                    token=api_token,
                )
                ui.notification_remove("journey_ls_upload_progress")
                if success:
                    ui.notification_show(
                        _("Picture sent to Label Studio successfully."),
                        type="message",
                        duration=3,
                    )
                else:
                    ui.notification_show(
                        _("Failed to send picture to Label Studio."),
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
