"""SureFlap-style dashboard: per-cat presence cards + big touch-friendly door controls."""

import base64
import json
import logging
import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
from shiny import render, ui, reactive
from faicons import icon_svg

from src.baseconfig import CONFIG, set_language
from src.database import CatsRepo, ReturnDataCatDB, DatabaseCore
from src.helper import EventType, DateTimeUtil
from src.mode import is_remote_mode
from src.backend import manual_door_override
from src.server_ui.state import reload_trigger_config, reload_trigger_photos
from src.server_ui.context import SessionContext
from src.server_ui.event_modal import btn_show_event, show_event_server
from src.server_ui.cats import build_cat_settings_card
from src.server_ui.journeys import _event_card_html

_ = set_language(CONFIG["LANGUAGE"])

if is_remote_mode():
    from src.remote.hardware import Magnets  # type: ignore
else:
    from src.magnets_rfid import Magnets

_PRESENCE_EVENT_TYPES = (
    EventType.CAT_WENT_INSIDE,
    EventType.CAT_WENT_PROBABLY_INSIDE,
    EventType.CAT_WENT_INSIDE_WITH_MOUSE,
    EventType.CAT_WENT_OUTSIDE,
)

_INSIDE_EVENT_TYPES = (
    EventType.CAT_WENT_INSIDE,
    EventType.CAT_WENT_PROBABLY_INSIDE,
    EventType.CAT_WENT_INSIDE_WITH_MOUSE,
)

# 05.10, Sid (page merge, step 1 - "coups d'oeil" + "proies bloquees"
# cards): these two never had a passage at all, so they're not in
# _PRESENCE_EVENT_TYPES above (which _day_stats_for_events' entries/seconds-
# outside math assumes are always in/out pairs) - fetched and counted
# separately, same dedup-by-block_id reasoning as _presence_events_for_rfid.
_GLANCE_EVENT_TYPE = "motion_outside_only"
_PREY_BLOCKED_EVENT_TYPE = "motion_outside_with_mouse"


def _sql_quote(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _event_type_first(event_type) -> str:
    """The real event_type, stripping any ',suffix' flags (e.g.
    "cat_went_outside,per_cat_prey_detection_disabled").

    06.10, Sid ("ses stats ne sont pas justes" / Parcours showing just
    "Boubou" with no "est sorti"): every exact-match comparison against the
    raw event_type column (`== EventType.X`, `in (_INSIDE_EVENT_TYPES)`,
    SQL `event_type IN (...)`) silently excluded EVERY event for a cat with
    per-cat prey detection disabled (currently just Boubou) - his motion
    always carries that extra flag, so he was invisible to presence status,
    entry/exit counts, the 24h timeline and the Parcours direction label.
    Compare against this instead."""
    if not event_type:
        return ""
    return str(event_type).split(",", 1)[0]


def _event_type_sql_in(column: str, types) -> str:
    """SQL condition matching ``column`` against any of ``types``, tolerant
    of a trailing ',suffix' flag - same reasoning as _event_type_first,
    for the queries that filter in SQL rather than in Python."""
    parts = [f"{column} LIKE {_sql_quote(str(t) + '%')}" for t in types]
    return "(" + " OR ".join(parts) + ")"


def _latest_presence_for_rfid(database: str, rfid: str):
    """Return (event_type, created_at) of the most recent in/out event for this cat, or (None, None)."""
    types_cond = _event_type_sql_in("event_type", _PRESENCE_EVENT_TYPES)
    stmt = f"""
        SELECT event_type, created_at FROM events
        WHERE rfid = {_sql_quote(rfid)} AND {types_cond} AND deleted = 0
        ORDER BY created_at DESC LIMIT 1
    """
    df = DatabaseCore.read_df_from_database(database, stmt)
    if df.empty:
        return None, None
    return df.iloc[0]["event_type"], df.iloc[0]["created_at"]


def _format_elapsed(created_at: str) -> str:
    try:
        ts = pd.to_datetime(created_at, utc=True)
        now = pd.Timestamp.now(tz="UTC")
        seconds = max(0, int((now - ts).total_seconds()))
    except Exception:
        return _("Unknown")

    if seconds < 60:
        return _("just now")
    minutes = seconds // 60
    if minutes < 60:
        return _("{n} min ago").format(n=minutes)
    hours = minutes // 60
    if hours < 24:
        return _("{n} h ago").format(n=hours)
    days = hours // 24
    return _("{n} d ago").format(n=days)


_FR_DAY_ABBR = ["lu", "ma", "me", "je", "ve", "sa", "di"]  # Python weekday(): 0=lundi


def _presence_prey_blocks_for_rfid(database: str, rfid: str) -> pd.DataFrame:
    """04.10 (Sid, "trait d'une autre couleur... quand une proie est
    detectee... cadeaux bloques"): dedup'd, UTC-timestamped prey-blocked
    sightings for this cat (columns: block_id, ts_utc). A blocked entry
    conclude_motion_event_type's conclusion stays motion_outside_with_mouse. Same
    frame-spam dedup as _presence_events_for_rfid (one row per block_id)."""
    stmt = f"""
        SELECT block_id, event_type, created_at FROM events
        WHERE rfid = {_sql_quote(rfid)} AND {_event_type_sql_in("event_type", [EventType.MOTION_OUTSIDE_WITH_MOUSE])} AND deleted = 0
        ORDER BY created_at ASC
    """
    df = DatabaseCore.read_df_from_database(database, stmt)
    if df.empty:
        return df
    # 04.10, Sid: same mixed-format created_at issue as _presence_events_for_rfid above.
    df["ts_utc"] = pd.to_datetime(df["created_at"], format="mixed", utc=True, errors="coerce")
    df = df.dropna(subset=["ts_utc"])
    if df.empty:
        return df
    df["dedup_key"] = df["block_id"]
    missing = df["dedup_key"].isna()
    df.loc[missing, "dedup_key"] = df.index[missing]
    return df.sort_values("ts_utc").drop_duplicates(subset="dedup_key", keep="first")


def _last_entry_for_rfid(database: str, rfid: str):
    """Most recent 'went inside' event for this cat, regardless of day.
    Returns (created_at_utc_str, local_dt_naive) or (None, None).
    03.10: keep the raw UTC string around for elapsed-time math (same
    UTC-aware comparison as _format_elapsed) - comparing a tz-aware "now"
    against the tz-naive local_dt directly crashed with "can't subtract
    offset-naive and offset-aware datetimes" the first time around."""
    types_cond = _event_type_sql_in("event_type", _INSIDE_EVENT_TYPES)
    stmt = f"""
        SELECT created_at FROM events
        WHERE rfid = {_sql_quote(rfid)} AND {types_cond} AND deleted = 0
        ORDER BY created_at DESC LIMIT 1
    """
    df = DatabaseCore.read_df_from_database(database, stmt)
    if df.empty:
        return None, None
    created_at_utc = df.iloc[0]["created_at"]
    try:
        local_str = DateTimeUtil.get_local_date_from_utc_date(created_at_utc)
        local_dt = pd.to_datetime(local_str).to_pydatetime()
    except Exception:
        local_dt = None
    return created_at_utc, local_dt


def _presence_events_for_rfid(database: str, rfid: str) -> pd.DataFrame:
    """Full dedup'd, UTC-timestamped, sorted in/out event history for this
    cat (columns: event_type, ts_utc). Shared by every day/week presence
    stat so a week view queries the DB once, not once per day.

    03.10, BUG REEL #1 trouve par Sid (618/301 entrees - impossible): `created_at`
    n'est PAS stocke dans un format de texte homogene ('2026-10-03 20:45:15'
    sans fuseau sur certaines lignes, '...19:58:49.1932+00:00' avec fuseau et
    microsecondes sur d'autres) - un simple `BETWEEN` SQL sur ces chaines
    compare du texte, pas des instants, et matchait des lignes de n'importe
    quel autre jour selon le format. Fix: tout parser proprement en UTC cote
    Python (pandas gere les formats melanges) et ne JAMAIS comparer ces
    chaines brutes en SQL.

    03.10, BUG REEL #2 (toujours 618/300 apres le fix #1): le compte etait
    toujours faux car `events` a UNE LIGNE PAR PHOTO CAPTUREE pendant un
    passage (jusqu'a ~100-200 lignes pour un seul vrai passage, toutes avec
    le meme `block_id` - verifie sur Patoune: 820 lignes brutes mais
    seulement 11 `block_id` distincts ce jour-la). journeys.py le savait deja
    et groupait par block_id pour sa chronologie ; cette fonction ne le
    faisait pas. Fix: dedupliquer par block_id (une ligne representative par
    block) avant de compter."""
    types_cond = _event_type_sql_in("event_type", _PRESENCE_EVENT_TYPES)
    stmt = f"""
        SELECT block_id, event_type, created_at FROM events
        WHERE rfid = {_sql_quote(rfid)} AND {types_cond} AND deleted = 0
        ORDER BY created_at ASC
    """
    df = DatabaseCore.read_df_from_database(database, stmt)
    if df.empty:
        return df

    # 04.10, Sid ("ma correction de Pookie ne met pas a jour le graphique"):
    # created_at mixes two formats (see BUG REEL #1 above) but this call was
    # missing format="mixed" - pandas infers ONE format from the bulk of
    # rows, so the plain "YYYY-MM-DD HH:MM:SS" format _insert_manual_presence_event
    # writes (no microseconds/offset) silently failed to parse and got
    # dropped by dropna() below, right after being correctly inserted.
    df["ts_utc"] = pd.to_datetime(df["created_at"], format="mixed", utc=True, errors="coerce")
    df = df.dropna(subset=["ts_utc"])
    if df.empty:
        return df

    # One row per block_id (first event of that block wins); rows with no
    # block_id (shouldn't normally happen) are each kept as their own block.
    # 03.10: .where(cond, df.index) crashed on some cats' data (AssertionError
    # deep in pandas' internals, version-sensitive) - .loc assignment instead,
    # no ambiguity about what's being aligned to what.
    df["dedup_key"] = df["block_id"]
    missing = df["dedup_key"].isna()
    df.loc[missing, "dedup_key"] = df.index[missing]
    return df.sort_values("ts_utc").drop_duplicates(subset="dedup_key", keep="first")


def _day_bounds_utc(local_date) -> tuple[pd.Timestamp, pd.Timestamp]:
    tz = ZoneInfo(CONFIG["TIMEZONE"])
    day_start = pd.Timestamp(
        datetime.combine(local_date, datetime.min.time()), tz=tz
    ).tz_convert("UTC")
    return day_start, day_start + pd.Timedelta(days=1)


def _day_stats_for_events(events_df: pd.DataFrame, local_date) -> tuple[int, int, int]:
    """(entries_count, seconds_outside, exits_count) for this local calendar
    day, from an already-fetched/dedup'd events dataframe (see
    _presence_events_for_rfid). Temps dehors = somme des intervalles
    sortie->entree qui se terminent CE jour-la (un depart sans retour dans
    la journee n'est pas compte - simplification deliberee, pas de notion
    de "toujours dehors depuis hier" ici).

    05.10, Sid (page merge, step 1 - "une carte pour les sorties"):
    exits_count added alongside the pre-existing two - same day-bounds
    check as entries, just on the CAT_WENT_OUTSIDE side of the loop below
    instead of the inside one."""
    if events_df.empty:
        return 0, 0, 0
    day_start, day_end = _day_bounds_utc(local_date)

    entries_count = 0
    exits_count = 0
    seconds_outside = 0
    pending_outside_ts = None
    for __, row in events_df.iterrows():
        ts = row["ts_utc"]
        if _event_type_first(row["event_type"]) in _INSIDE_EVENT_TYPES:
            if day_start <= ts < day_end:
                entries_count += 1
                if pending_outside_ts is not None:
                    seconds_outside += max(0, int((ts - pending_outside_ts).total_seconds()))
            pending_outside_ts = None
        else:
            if day_start <= ts < day_end:
                exits_count += 1
            pending_outside_ts = ts

    return entries_count, seconds_outside, exits_count


def _day_glance_prey_counts(database: str, rfid: str, local_date) -> tuple[int, int]:
    """(glances_count, prey_blocked_count) for this cat on this local
    calendar day - distinct motion blocks, not raw photo rows (see
    _presence_events_for_rfid's BUG REEL #2 for why raw rows would wildly
    overcount: hundreds of frames per real visit)."""
    day_start, day_end = _day_bounds_utc(local_date)
    stmt = f"""
        SELECT block_id, event_type, created_at FROM events
        WHERE rfid = {_sql_quote(rfid)}
        AND (event_type LIKE '{_GLANCE_EVENT_TYPE}%' OR event_type LIKE '{_PREY_BLOCKED_EVENT_TYPE}%')
        AND deleted = 0
    """
    df = DatabaseCore.read_df_from_database(database, stmt)
    if df.empty:
        return 0, 0
    df["ts_utc"] = pd.to_datetime(df["created_at"], format="mixed", utc=True, errors="coerce")
    df = df.dropna(subset=["ts_utc"])
    df = df[(df["ts_utc"] >= day_start) & (df["ts_utc"] < day_end)]
    if df.empty:
        return 0, 0
    # motion_outside_with_mouse can also contain "motion_outside_only" as a
    # substring-false-positive? No - LIKE prefix match on event_type, which
    # is an exact enum string (possibly with ",suffix" flags appended, see
    # EventType docstring) - startswith is the correct, unambiguous test.
    glances = df[df["event_type"].str.startswith(_GLANCE_EVENT_TYPE)]["block_id"].nunique()
    prey_blocked = df[df["event_type"].str.startswith(_PREY_BLOCKED_EVENT_TYPE)]["block_id"].nunique()
    return int(glances), int(prey_blocked)


# 07.10, Sid ("quand je clique sur Entree, j'ai le filtre de la
# chronologie... tout l'historique"): which event_type prefix(es) match each
# clickable stat tile - LIKE-prefix, same reasoning as
# _day_glance_prey_counts (handles the ",suffix" flags without needing a
# second Python-side filter pass).
# 07.10, Sid ("pas tres utile d'avoir la liste des entrees et la liste des
# sorties... les deux clics renvoient a une liste fusionnee"): Entries and
# Exits tiles both open this same "in_out" merged view now - two separate
# in-only/out-only lists weren't useful on their own. "all" (the new 6th
# tile) has no type filter at all - every event type for this cat, same mix
# Chronologie/Parcours show but scoped to one cat.
_EVENTS_FILTER_TYPE_PREFIXES = {
    "in_out": (
        EventType.CAT_WENT_INSIDE,
        EventType.CAT_WENT_PROBABLY_INSIDE,
        EventType.CAT_WENT_INSIDE_WITH_MOUSE,
        EventType.CAT_WENT_OUTSIDE,
    ),
    "glances": (_GLANCE_EVENT_TYPE,),
    "prey_blocked": (_PREY_BLOCKED_EVENT_TYPE,),
    # "all" deliberately absent - _list_events_for_rfid_filtered treats a
    # missing key as "no type filter at all", not "no results".
}


def _list_events_for_rfid_filtered(database: str, rfid: str, filter_type: str) -> list[dict]:
    """This cat's full history (no date limit, per Sid's explicit choice)
    for one of the stat tiles - same row shape (block_id/first_id/dt/
    event_type/rfid) _event_card_html() already knows how to render,
    journeys.py's own "simple" Chronologie card style. ``filter_type ==
    "all"`` (the 6th tile, full per-cat chronology) applies no type filter
    at all - unknown filter_type values still return nothing."""
    if filter_type != "all" and filter_type not in _EVENTS_FILTER_TYPE_PREFIXES:
        return []
    prefixes = _EVENTS_FILTER_TYPE_PREFIXES.get(filter_type)
    type_clause = (
        " AND (" + " OR ".join(f"event_type LIKE '{p}%'" for p in prefixes) + ")" if prefixes else ""
    )
    stmt = f"""
        SELECT block_id, MIN(id) AS first_id, MAX(created_at) AS created_at, event_type, rfid
        FROM events
        WHERE rfid = {_sql_quote(rfid)}{type_clause} AND deleted = 0
        GROUP BY block_id
        ORDER BY block_id DESC
    """
    try:
        df = DatabaseCore.read_df_from_database(database, stmt)
    except Exception as e:
        logging.error(f"[PRESENCE] Failed to query filtered events for rfid {rfid}: {e}")
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


def _day_timeline_for_events(events_df: pd.DataFrame, local_date) -> list[tuple[float, bool, int | None]]:
    """04.10 (Sid, "graphique sur 24h avec un trait pour les entrees...et un
    trait pour les sorties"): list of (fraction_of_day 0..1, is_inside,
    block_id) for every real in/out crossing that local calendar day, for
    plotting as ticks along a 24h strip. block_id lets each tick open the
    same event photo modal as Parcours/Vue en direct (04.10, "cliquer sur
    le trait... atteindre aussi les photos liees")."""
    if events_df.empty:
        return []
    day_start, day_end = _day_bounds_utc(local_date)
    tz = ZoneInfo(CONFIG["TIMEZONE"])

    marks = []
    mask = (events_df["ts_utc"] >= day_start) & (events_df["ts_utc"] < day_end)
    for __, row in events_df[mask].iterrows():
        local_dt = row["ts_utc"].tz_convert(tz)
        seconds_of_day = local_dt.hour * 3600 + local_dt.minute * 60 + local_dt.second
        block_id = row.get("block_id")
        block_id = int(block_id) if pd.notna(block_id) else None
        marks.append((seconds_of_day / 86400.0, _event_type_first(row["event_type"]) in _INSIDE_EVENT_TYPES, block_id))
    return marks


def _format_duration(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds} s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} min"
    hours = minutes // 60
    rem_min = minutes % 60
    return f"{hours} h {rem_min:02d}" if rem_min else f"{hours} h"


def _insert_manual_presence_event(database: str, rfid: str, event_type: str) -> bool:
    """Insert a synthetic presence-correction event (03.10, Sid: cats that
    come in through the humans' own door never trigger the flap, so the
    recorded presence can go stale/wrong with no real event to fix it).
    Reuses the real CAT_WENT_INSIDE/OUTSIDE types on purpose - every other
    presence reader (grid, detail view, day stats, Journey tab) then just
    treats it like a normal event, no special-casing needed elsewhere."""
    result = DatabaseCore.lock_database()
    if not result.success:
        return False
    try:
        conn = sqlite3.connect(database, timeout=30)
        cursor = conn.cursor()
        now_utc = datetime.now(ZoneInfo("UTC")).strftime("%Y-%m-%d %H:%M:%S")
        cursor.execute(
            "INSERT INTO events (created_at, event_type, rfid, event_text, deleted) VALUES (?, ?, ?, ?, 0)",
            (now_utc, event_type, rfid, "Manual correction"),
        )
        new_id = cursor.lastrowid
        cursor.execute("UPDATE events SET block_id = ? WHERE id = ?", (new_id, new_id))
        conn.commit()
        conn.close()
    except Exception as e:
        logging.error(f"[PRESENCE] Failed to insert manual presence correction: {e}")
        return False
    finally:
        DatabaseCore.release_database()
    return True


def register_presence(input, output, session, ctx: SessionContext):
    """Register the SureFlap-style presence/dashboard tab handlers."""

    presence_selected_rfid = reactive.Value(None)
    # 05.10, Sid (page merge, step 2): toggled by the photo click in
    # ui_presence_detail - shows that cat's build_cat_settings_card() inline
    # instead of switching to a separate "Manage cats" tab.
    presence_cat_settings_open = reactive.Value(False)
    presence_selected_date = reactive.Value(
        datetime.now(ZoneInfo(CONFIG["TIMEZONE"])).date()
    )
    presence_stats_view = reactive.Value(None)  # None | "outside" | "entries"
    presence_stats_week_offset = reactive.Value(0)  # 0 = the 7 days ending today
    # 07.10, Sid ("quand je clique sur Entree, j'ai le filtre de la
    # chronologie... en dessous des 5 boutons"): None | "entries" | "exits" |
    # "glances" | "prey_blocked" - which of the 4 discrete-event stat tiles
    # is expanded, showing that cat's full history for that event type right
    # below the tiles (same simple card style as the Chronologie tab).
    # "Temps a l'exterieur" keeps its existing weekly-graph behaviour
    # (presence_stats_view above) - a duration, not a discrete event type.
    presence_events_filter = reactive.Value(None)

    @reactive.effect
    @reactive.event(input.presence_card_click)
    def on_presence_card_click():
        presence_selected_rfid.set(input.presence_card_click())
        presence_selected_date.set(datetime.now(ZoneInfo(CONFIG["TIMEZONE"])).date())
        presence_events_filter.set(None)

    @reactive.effect
    @reactive.event(input.presence_detail_back)
    def on_presence_detail_back():
        presence_selected_rfid.set(None)
        presence_cat_settings_open.set(False)
        presence_events_filter.set(None)

    @reactive.effect
    @reactive.event(input.presence_open_cat_settings)
    def on_presence_open_cat_settings():
        # 05.10, Sid (page merge, step 2 - "supprimer la page gerer les
        # chats"): show that cat's settings card inline, right here,
        # instead of switching to the (now hidden) "Manage cats" tab.
        presence_cat_settings_open.set(not presence_cat_settings_open.get())

    @reactive.effect
    @reactive.event(input.presence_correct_click)
    def on_presence_correct_click():
        try:
            data = json.loads(input.presence_correct_click())
            target_rfid = data["rfid"]
            target_event_type = data["event_type"]
        except Exception:
            return
        ok = _insert_manual_presence_event(
            CONFIG["KITTYHACK_DATABASE_PATH"], target_rfid, target_event_type
        )
        if ok:
            ui.notification_show(_("Presence corrected."), duration=4, type="message")
            reload_trigger_photos.set(reload_trigger_photos.get() + 1)
        else:
            ui.notification_show(_("Failed to correct presence."), duration=6, type="error")

    @reactive.effect
    @reactive.event(input.presence_stats_open)
    def on_presence_stats_open():
        presence_stats_view.set(input.presence_stats_open())
        presence_stats_week_offset.set(0)

    @reactive.effect
    @reactive.event(input.presence_stats_back)
    def on_presence_stats_back():
        presence_stats_view.set(None)

    @reactive.effect
    @reactive.event(input.presence_stats_week_prev)
    def on_presence_stats_week_prev():
        presence_stats_week_offset.set(presence_stats_week_offset() + 1)

    @reactive.effect
    @reactive.event(input.presence_events_filter_open)
    def on_presence_events_filter_open():
        clicked = input.presence_events_filter_open()
        # Toggle: clicking the already-open tile again collapses it.
        presence_events_filter.set(None if presence_events_filter() == clicked else clicked)

    @reactive.effect
    @reactive.event(input.presence_events_filter_back)
    def on_presence_events_filter_back():
        presence_events_filter.set(None)

    @reactive.effect
    @reactive.event(input.presence_stats_week_next)
    def on_presence_stats_week_next():
        presence_stats_week_offset.set(max(0, presence_stats_week_offset() - 1))

    @reactive.effect
    @reactive.event(input.presence_day_pick)
    def on_presence_day_pick():
        try:
            presence_selected_date.set(
                datetime.strptime(input.presence_day_pick(), "%Y-%m-%d").date()
            )
        except Exception:
            pass

    @output
    @render.ui
    @reactive.event(
        ctx.reload_trigger_cats, reload_trigger_config, ctx.live_status, ignore_none=True
    )
    def ui_presence_doors():
        magnets = getattr(Magnets, "instance", None)
        inside_unlocked = bool(magnets.get_inside_state()) if magnets else False
        outside_unlocked = bool(magnets.get_outside_state()) if magnets else False

        def door_card(label_text, is_unlocked, button_id):
            state_text = _("Unlocked") if is_unlocked else _("Locked")
            state_icon = icon_svg("lock-open") if is_unlocked else icon_svg("lock")
            btn_label = _("Lock now") if is_unlocked else _("Unlock now")
            btn_icon = icon_svg("lock") if is_unlocked else icon_svg("unlock")
            return ui.card(
                ui.div(
                    ui.div(
                        ui.h5(label_text, style_="margin-bottom: 2px;"),
                        ui.div(
                            {"class": "kh-presence-door-state"},
                            state_icon,
                            ui.span(state_text),
                        ),
                        style_="flex: 1 1 auto;",
                    ),
                    ui.input_action_button(
                        button_id,
                        btn_label,
                        icon=btn_icon,
                        class_="btn-lg kh-big-touch-btn "
                        + ("btn-outline-danger" if is_unlocked else "btn-outline-success"),
                    ),
                    class_="kh-presence-door-row",
                ),
                class_="kh-presence-door-card",
            )

        return ui.div(
            {"class": "kh-presence-doors"},
            door_card(_("Inside"), inside_unlocked, "presence_toggle_inside"),
            door_card(_("Outside"), outside_unlocked, "presence_toggle_outside"),
        )

    @reactive.Effect
    @reactive.event(input.presence_toggle_inside)
    def on_presence_toggle_inside():
        magnets = getattr(Magnets, "instance", None)
        if magnets is None:
            return
        if magnets.get_inside_state():
            logging.info("[SERVER] Manual override from Presence tab - lock inside now")
            manual_door_override["lock_inside"] = True
        else:
            logging.info("[SERVER] Manual override from Presence tab - unlock inside now")
            manual_door_override["unlock_inside"] = True

    @reactive.Effect
    @reactive.event(input.presence_toggle_outside)
    def on_presence_toggle_outside():
        magnets = getattr(Magnets, "instance", None)
        if magnets is None:
            return
        if magnets.get_outside_state():
            logging.info("[SERVER] Manual override from Presence tab - lock outside now")
            manual_door_override["lock_outside"] = True
        else:
            logging.info("[SERVER] Manual override from Presence tab - unlock outside now")
            manual_door_override["unlock_outside"] = True

    @output
    @render.ui
    @reactive.event(
        ctx.reload_trigger_cats, reload_trigger_config, reload_trigger_photos, ignore_none=True
    )
    def ui_presence_cats():
        df_cats = CatsRepo.db_get_cats(CONFIG["KITTYHACK_DATABASE_PATH"], ReturnDataCatDB.all)
        if df_cats.empty:
            return ui.div(
                {"class": "kh-empty-state"},
                icon_svg("cat"),
                ui.p(_("No cats registered yet.")),
            )

        cards = []
        for __, row in df_cats.iterrows():
            rfid = row["rfid"]
            name = row["name"] or _("Unknown")

            if row["cat_image"]:
                try:
                    decoded_picture = base64.b64encode(row["cat_image"]).decode("utf-8")
                except Exception:
                    decoded_picture = None
            else:
                decoded_picture = None

            photo_html = (
                ui.img(
                    {"class": "kh-presence-photo"},
                    src=f"data:image/jpeg;base64,{decoded_picture}",
                )
                if decoded_picture
                else ui.div({"class": "kh-presence-photo kh-presence-photo-placeholder"}, icon_svg("cat"))
            )

            event_type, created_at = (None, None)

            if rfid:
                event_type, created_at = _latest_presence_for_rfid(
                    CONFIG["KITTYHACK_DATABASE_PATH"], rfid
                )

            if event_type is None:
                location_text = _("Unknown")
                badge_class = "kh-presence-badge-unknown"
                badge_icon = icon_svg("question")
                elapsed_text = _("Never seen yet")
            elif _event_type_first(event_type) in _INSIDE_EVENT_TYPES:
                location_text = _("Inside")
                badge_class = "kh-presence-badge-inside"
                badge_icon = icon_svg("house")
                elapsed_text = _format_elapsed(created_at)
            else:
                location_text = _("Outside")
                badge_class = "kh-presence-badge-outside"
                badge_icon = icon_svg("tree")
                elapsed_text = _format_elapsed(created_at)

            card_attrs = {"class": "kh-presence-card"}
            if rfid:
                card_attrs["class"] += " kh-presence-card-clickable"
                card_attrs["onclick"] = (
                    f"Shiny.setInputValue('presence_card_click', {json.dumps(rfid)}, "
                    "{priority: 'event'})"
                )

            cards.append(
                ui.div(
                    card_attrs,
                    ui.div(
                        {"class": "kh-presence-photo-wrap"},
                        photo_html,
                        ui.div({"class": f"kh-presence-status-badge {badge_class}"}, badge_icon),
                    ),
                    ui.div(
                        {"class": "kh-presence-card-body"},
                        ui.div(name, class_="kh-presence-name"),
                        ui.div(location_text, class_="kh-presence-location"),
                        ui.div(elapsed_text, class_="kh-presence-elapsed"),
                    ),
                )
            )

        return ui.div({"class": "kh-presence-grid"}, *cards)

    @output
    @render.ui
    @reactive.event(
        presence_selected_rfid, presence_selected_date, reload_trigger_photos,
        presence_cat_settings_open, ctx.reload_trigger_cats,
        ignore_none=False,
    )
    def ui_presence_detail():
        rfid = presence_selected_rfid()
        if not rfid:
            return ui.div()

        database = CONFIG["KITTYHACK_DATABASE_PATH"]
        df_cats = CatsRepo.db_get_cats(database, ReturnDataCatDB.all)
        match = df_cats[df_cats["rfid"] == rfid]
        if match.empty:
            presence_selected_rfid.set(None)
            return ui.div()
        row = match.iloc[0]
        name = row["name"] or _("Unknown")

        decoded_picture = None
        if row["cat_image"]:
            try:
                decoded_picture = base64.b64encode(row["cat_image"]).decode("utf-8")
            except Exception:
                decoded_picture = None
        photo_src = f"data:image/jpeg;base64,{decoded_picture}" if decoded_picture else None

        event_type, created_at = _latest_presence_for_rfid(database, rfid)
        if event_type is None:
            location_text = _("Unknown")
            badge_class = "kh-presence-badge-unknown"
            badge_icon = icon_svg("question")
            elapsed_text = _("Never seen yet")
        elif _event_type_first(event_type) in _INSIDE_EVENT_TYPES:
            location_text = _("Inside")
            badge_class = "kh-presence-badge-inside"
            badge_icon = icon_svg("house")
            elapsed_text = _format_elapsed(created_at)
        else:
            location_text = _("Outside")
            badge_class = "kh-presence-badge-outside"
            badge_icon = icon_svg("tree")
            elapsed_text = _format_elapsed(created_at)

        last_entry_utc, last_entry_local = _last_entry_for_rfid(database, rfid)
        if last_entry_utc is None:
            last_entry_block = ui.div(_("No entry recorded yet."), class_="kh-presence-detail-noentry")
        else:
            ts = pd.to_datetime(last_entry_utc, utc=True)
            now = pd.Timestamp.now(tz="UTC")
            seconds = max(0, int((now - ts).total_seconds()))
            if seconds < 60:
                rel = _("just now")
            elif seconds < 3600:
                rel = _("{n} min ago").format(n=seconds // 60)
            elif seconds < 86400:
                rel = _("{n} h ago").format(n=seconds // 3600)
            else:
                rel = _("{n} d ago").format(n=seconds // 86400)
            abs_str = last_entry_local.strftime(" %H:%M") if last_entry_local else ""
            last_entry_block = ui.div(
                ui.div(_("Last entry"), class_="kh-presence-detail-label"),
                ui.div(
                    ui.span(rel, class_="kh-presence-detail-rel"),
                    ui.span(abs_str, class_="kh-presence-detail-abs"),
                    class_="kh-presence-detail-lastentry",
                ),
            )

        today_local = datetime.now(ZoneInfo(CONFIG["TIMEZONE"])).date()
        selected_date = presence_selected_date()
        day_pills = []
        for delta in range(7, -1, -1):
            d = today_local - timedelta(days=delta)
            is_selected = d == selected_date
            day_pills.append(
                ui.div(
                    {
                        "class": "kh-presence-day-pill"
                        + (" kh-presence-day-pill-selected" if is_selected else ""),
                        "onclick": (
                            f"Shiny.setInputValue('presence_day_pick', "
                            f"{json.dumps(d.strftime('%Y-%m-%d'))}, {{priority: 'event'}})"
                        ),
                    },
                    ui.div(str(d.day), class_="kh-presence-day-num"),
                    ui.div(_FR_DAY_ABBR[d.weekday()], class_="kh-presence-day-abbr"),
                )
            )

        entries_count, seconds_outside, exits_count = _day_stats_for_events(
            _presence_events_for_rfid(database, rfid), selected_date
        )
        glances_count, prey_blocked_count = _day_glance_prey_counts(database, rfid, selected_date)

        # 03.10 (Sid): clicking the house/tree icon corrects a presence that
        # the flap never actually tracked (cat came in through the humans'
        # door). Only offered when we have a clear current state to flip.
        status_icon_attrs = {"class": "kh-presence-detail-status-icon"}
        if event_type is not None:
            correction_target = (
                EventType.CAT_WENT_OUTSIDE
                if _event_type_first(event_type) in _INSIDE_EVENT_TYPES
                else EventType.CAT_WENT_INSIDE
            )
            status_icon_attrs["class"] += " kh-presence-status-icon-clickable"
            status_icon_attrs["onclick"] = (
                "Shiny.setInputValue('presence_correct_click', "
                f"{json.dumps(json.dumps({'rfid': rfid, 'event_type': correction_target}))}, "
                "{priority: 'event'})"
            )

        header_bg = (
            f"background-image: url('data:image/jpeg;base64,{decoded_picture}');"
            if decoded_picture
            else ""
        )

        return ui.div(
            {"class": "kh-presence-detail"},
            ui.div(
                {"class": "kh-presence-detail-header", "style": header_bg},
                ui.div(
                    {
                        "class": "kh-presence-detail-back",
                        "onclick": "Shiny.setInputValue('presence_detail_back', Math.random(), {priority: 'event'})",
                    },
                    icon_svg("arrow-left"),
                ),
                # 05.10, Sid (page merge, step 1 - "en cliquant dessus on
                # irait dans les elements qui sont sous gerer les chats"):
                # same Shiny.setInputValue pattern as the back-arrow above.
                ui.img(
                    {
                        "class": "kh-presence-detail-avatar kh-presence-avatar-clickable",
                        "onclick": "Shiny.setInputValue('presence_open_cat_settings', "
                        + json.dumps(rfid) + ", {priority: 'event'})",
                    },
                    src=photo_src,
                )
                if photo_src
                else ui.div(
                    {
                        "class": "kh-presence-detail-avatar kh-presence-photo-placeholder kh-presence-avatar-clickable",
                        "onclick": "Shiny.setInputValue('presence_open_cat_settings', "
                        + json.dumps(rfid) + ", {priority: 'event'})",
                    },
                    icon_svg("cat"),
                ),
                ui.div(name, class_="kh-presence-detail-name"),
            ),
            ui.div({"class": "kh-presence-day-strip"}, *day_pills),
            ui.div(
                {"class": f"kh-presence-detail-status {badge_class}"},
                ui.div(
                    ui.div(location_text, class_="kh-presence-detail-status-text"),
                    ui.div(elapsed_text, class_="kh-presence-detail-status-elapsed"),
                ),
                ui.div(status_icon_attrs, badge_icon),
            ),
            last_entry_block,
            ui.div(
                {"class": "kh-presence-detail-stats"},
                # 07.10, Sid: explicit tile order - Entree/Sortie first (both
                # open the same merged "in_out" list - two separate in-only/
                # out-only lists weren't useful on their own), then Cadeaux/
                # Coups d'oeil, then Temps a l'exterieur, then Activite
                # totale (every event type for this cat, no filter at all).
                ui.div(
                    {
                        "class": "kh-presence-detail-stat-box kh-presence-stat-box-clickable"
                        + (" kh-presence-stat-box-active" if presence_events_filter() == "in_out" else ""),
                        "onclick": "Shiny.setInputValue('presence_events_filter_open', 'in_out', {priority: 'event'})",
                    },
                    ui.div(str(entries_count), class_="kh-presence-detail-stat-value"),
                    ui.div(_("Entries"), class_="kh-presence-detail-stat-label"),
                ),
                ui.div(
                    {
                        "class": "kh-presence-detail-stat-box kh-presence-stat-box-clickable"
                        + (" kh-presence-stat-box-active" if presence_events_filter() == "in_out" else ""),
                        "onclick": "Shiny.setInputValue('presence_events_filter_open', 'in_out', {priority: 'event'})",
                    },
                    ui.div(str(exits_count), class_="kh-presence-detail-stat-value"),
                    ui.div(_("Exits"), class_="kh-presence-detail-stat-label"),
                ),
                ui.div(
                    {
                        "class": "kh-presence-detail-stat-box kh-presence-stat-box-clickable"
                        + (" kh-presence-stat-box-active" if presence_events_filter() == "prey_blocked" else ""),
                        "onclick": "Shiny.setInputValue('presence_events_filter_open', 'prey_blocked', {priority: 'event'})",
                    },
                    ui.div(str(prey_blocked_count), class_="kh-presence-detail-stat-value"),
                    ui.div(_("Prey blocked"), class_="kh-presence-detail-stat-label"),
                ),
                ui.div(
                    {
                        "class": "kh-presence-detail-stat-box kh-presence-stat-box-clickable"
                        + (" kh-presence-stat-box-active" if presence_events_filter() == "glances" else ""),
                        "onclick": "Shiny.setInputValue('presence_events_filter_open', 'glances', {priority: 'event'})",
                    },
                    ui.div(str(glances_count), class_="kh-presence-detail-stat-value"),
                    ui.div(_("Glances"), class_="kh-presence-detail-stat-label"),
                ),
                ui.div(
                    {
                        "class": "kh-presence-detail-stat-box kh-presence-stat-box-clickable",
                        "onclick": "Shiny.setInputValue('presence_stats_open', 'outside', {priority: 'event'})",
                    },
                    ui.div(_format_duration(seconds_outside), class_="kh-presence-detail-stat-value"),
                    ui.div(_("Time outside"), class_="kh-presence-detail-stat-label"),
                ),
                ui.div(
                    {
                        "class": "kh-presence-detail-stat-box kh-presence-stat-box-clickable"
                        + (" kh-presence-stat-box-active" if presence_events_filter() == "all" else ""),
                        "onclick": "Shiny.setInputValue('presence_events_filter_open', 'all', {priority: 'event'})",
                    },
                    ui.div(icon_svg("list", margin_left="0"), class_="kh-presence-detail-stat-value"),
                    ui.div(_("Total activity"), class_="kh-presence-detail-stat-label"),
                ),
            ),
            # 05.10, Sid (page merge, step 2 - "en cliquant [la photo] on
            # irait dans les elements qui sont sous gerer les chats"): the
            # exact same card build_cat_settings_card() builds for the old
            # list page, just for this one cat (`row` already has every
            # field it needs - same ReturnDataCatDB.all fetch above).
            # server-ui.js's RFID live-validation looks up this exact id
            # (#kh_manage_cats_i18n) regardless of which page rendered the
            # card - same id as cats.py's own version, reused here so the
            # French messages still show instead of silently falling back
            # to the hardcoded English defaults in that JS.
            ui.tags.div(
                {
                    "id": "kh_manage_cats_i18n",
                    "style": "display:none;",
                    "data-msg-empty": _(
                        "No RFID entered. Cat identification only via camera (if enabled). See CONFIGURATION section for details."
                    ),
                    "data-msg-valid": _("Valid RFID"),
                    "data-msg-invalid": _(
                        "Invalid RFID. Must be exactly 16 hex characters (0-9, A-F)."
                    ),
                }
            )
            if presence_cat_settings_open.get()
            else None,
            ui.div(
                build_cat_settings_card(row),
                ui.div(
                    ui.input_action_button(
                        id="mng_cat_save_changes",
                        label=_("Save all changes"),
                        icon=icon_svg("floppy-disk"),
                    ),
                    style_="text-align: center; padding: 16px 0 32px 0;",
                ),
                id="manage_cats_container",
            )
            if presence_cat_settings_open.get()
            else None,
        )

    _EVENTS_FILTER_LABELS = {
        "in_out": lambda: _("Entries & exits"),
        "glances": lambda: _("Glances"),
        "prey_blocked": lambda: _("Prey blocked"),
        "all": lambda: _("Total activity"),
    }

    @output
    @render.ui
    @reactive.event(presence_events_filter, presence_selected_rfid, reload_trigger_photos, ignore_none=False)
    def ui_presence_events_filtered():
        """07.10, Sid ("moi je voulais une nouvelle page" - the inline
        version below the stat tiles worked but didn't read as "new content"
        on mobile): a real full-page view instead, same back-button pattern
        as the "Temps a l'exterieur" weekly stats page - this cat's full
        history (no date limit) for whichever of the 4 discrete-event stat
        tiles was clicked, same simple card style as the Chronologie tab."""
        filter_type = presence_events_filter()
        rfid = presence_selected_rfid()
        if not filter_type or not rfid:
            return ui.div()

        database = CONFIG["KITTYHACK_DATABASE_PATH"]
        cat_name_dict = CatsRepo.get_cat_name_rfid_dict(database)
        cat_name = cat_name_dict.get(rfid) or _("Unknown")
        label_fn = _EVENTS_FILTER_LABELS.get(filter_type)
        label = label_fn() if label_fn else ""

        events = _list_events_for_rfid_filtered(database, rfid, filter_type)
        if not events:
            body = ui.div(
                ui.HTML(f"<p class='text-center text-muted small'>{_('No pictures found.')}</p>"),
                class_="container",
            )
        else:
            html_parts = []
            for ev in events:
                btn_id = f"btn_show_event_presence_filter_{ev['block_id']}"
                show_event_server(btn_id, ev["block_id"])
                html_parts.append(
                    _event_card_html(ev, cat_name_dict, str(btn_show_event(btn_id)), simple=True)
                )
            body = ui.div(
                ui.HTML('<div class="kh-journey-list kh-chrono-list">' + "".join(html_parts) + "</div>"),
                class_="container",
            )

        return ui.div(
            {"class": "kh-presence-stats-detail"},
            ui.div(
                {"class": "kh-presence-stats-header"},
                ui.div(
                    {
                        "class": "kh-presence-detail-back",
                        "onclick": "Shiny.setInputValue('presence_events_filter_back', Math.random(), {priority: 'event'})",
                    },
                    icon_svg("arrow-left"),
                ),
                ui.div(f"{label} — {cat_name}", class_="kh-presence-stats-title"),
            ),
            body,
        )

    @output
    @render.ui
    @reactive.event(
        presence_stats_view, presence_stats_week_offset, reload_trigger_photos,
        ignore_none=False,
    )
    def ui_presence_stats_detail():
        """04.10 (Sid, remplace les onglets Temps/Entrees separes du 03.10):
        "un graphique sur 24h avec un trait pour les entrees...vert et un
        trait pour les sorties...rouge, pour tous les jours" - pour reperer
        une recurrence visuellement plutot qu'un simple compte par jour.
        Un seul graphique combine les deux, donc plus besoin de l'ancien
        choix d'onglet. Deliberement PAS de ligne "repas" - lie a un
        distributeur de nourriture SureFlap, Kittyhack n'en a pas."""
        rfid = presence_selected_rfid()
        if not presence_stats_view() or not rfid:
            return ui.div()

        database = CONFIG["KITTYHACK_DATABASE_PATH"]
        tz = ZoneInfo(CONFIG["TIMEZONE"])
        today_local = datetime.now(tz).date()
        offset = presence_stats_week_offset()
        window_end = today_local - timedelta(days=7 * offset)
        window_start = window_end - timedelta(days=6)

        days = [window_start + timedelta(days=i) for i in range(7)]
        events_df = _presence_events_for_rfid(database, rfid)
        prey_df = _presence_prey_blocks_for_rfid(database, rfid)

        def _tick(frac: float, css_class: str, block_id: int | None):
            if block_id is None:
                return ui.div(
                    {"class": f"kh-presence-tl-tick {css_class}", "style": f"left: {frac * 100:.3f}%;"}
                )
            # 04.10, Sid: "cliquer sur le trait... atteindre aussi les photos
            # liees" - reuse the same event detail modal as Parcours/Vue en
            # direct, overlaid invisibly on the tick (CSS .kh-presence-tl-tick-btn)
            # rather than a second, separate photo viewer.
            btn_id = f"btn_show_event_tl_{block_id}"
            show_event_server(btn_id, block_id)
            return ui.div(
                {"class": "kh-presence-tl-tick-wrap", "style": f"left: {frac * 100:.3f}%;"},
                ui.div({"class": f"kh-presence-tl-tick {css_class}", "style": "left: 0;"}),
                ui.div({"class": "kh-presence-tl-tick-btn"}, btn_show_event(btn_id)),
            )

        total_entries = 0
        total_seconds_outside = 0
        total_prey_blocked = 0
        day_rows = []
        for d in reversed(days):
            entries_count, seconds_outside, __ = _day_stats_for_events(events_df, d)
            total_entries += entries_count
            total_seconds_outside += seconds_outside
            marks = _day_timeline_for_events(events_df, d)
            prey_marks = _day_timeline_for_events(prey_df, d)
            total_prey_blocked += len(prey_marks)

            ticks = [
                _tick(frac, "kh-presence-tl-tick-in" if is_inside else "kh-presence-tl-tick-out", block_id)
                for frac, is_inside, block_id in marks
            ] + [
                _tick(frac, "kh-presence-tl-tick-prey", block_id)
                for frac, __, block_id in prey_marks
            ]

            day_rows.append(
                ui.div(
                    {"class": "kh-presence-stats-day-row"},
                    ui.div(
                        ui.span(_FR_DAY_ABBR[d.weekday()].capitalize(), class_="kh-presence-stats-day-abbr"),
                        ui.span(f" {d.day:02d}.{d.month:02d}", class_="kh-presence-stats-day-date"),
                        ui.span(
                            f"{entries_count} · {_format_duration(seconds_outside)}",
                            class_="kh-presence-stats-day-summary",
                        ),
                    ),
                    ui.div(
                        {"class": "kh-presence-tl-track"},
                        ui.div({"class": "kh-presence-tl-gridline", "style": "left: 25%;"}),
                        ui.div({"class": "kh-presence-tl-gridline", "style": "left: 50%;"}),
                        ui.div({"class": "kh-presence-tl-gridline", "style": "left: 75%;"}),
                        *ticks,
                    ),
                )
            )

        avg_entries = total_entries / len(days)
        avg_outside = total_seconds_outside / len(days)

        return ui.div(
            {"class": "kh-presence-stats-detail"},
            ui.div(
                {"class": "kh-presence-stats-header"},
                ui.div(
                    {
                        "class": "kh-presence-detail-back",
                        "onclick": "Shiny.setInputValue('presence_stats_back', Math.random(), {priority: 'event'})",
                    },
                    icon_svg("arrow-left"),
                ),
                ui.div(_("Access"), class_="kh-presence-stats-title"),
            ),
            ui.div(
                {"class": "kh-presence-stats-nav"},
                ui.div(
                    {
                        "class": "kh-presence-stats-nav-arrow",
                        "onclick": "Shiny.setInputValue('presence_stats_week_prev', Math.random(), {priority: 'event'})",
                    },
                    icon_svg("chevron-left"),
                ),
                ui.div(
                    f"{window_start.strftime('%d.%m')} – {window_end.strftime('%d.%m')}",
                    class_="kh-presence-stats-nav-label",
                ),
                ui.div(
                    {
                        "class": "kh-presence-stats-nav-arrow"
                        + (" kh-presence-stats-nav-arrow-disabled" if offset == 0 else ""),
                        "onclick": "" if offset == 0 else "Shiny.setInputValue('presence_stats_week_next', Math.random(), {priority: 'event'})",
                    },
                    icon_svg("chevron-right"),
                ),
            ),
            ui.div(
                {"class": "kh-presence-detail-stats kh-presence-detail-stats-3col"},
                ui.div(
                    ui.div(f"{avg_entries:.1f}", class_="kh-presence-detail-stat-value"),
                    ui.div(_("Entries/day avg"), class_="kh-presence-detail-stat-label"),
                    class_="kh-presence-detail-stat-box",
                ),
                ui.div(
                    ui.div(_format_duration(round(avg_outside)), class_="kh-presence-detail-stat-value"),
                    ui.div(_("Time outside/day avg"), class_="kh-presence-detail-stat-label"),
                    class_="kh-presence-detail-stat-box",
                ),
                ui.div(
                    ui.div(str(total_prey_blocked), class_="kh-presence-detail-stat-value"),
                    ui.div(_("Prey blocked"), class_="kh-presence-detail-stat-label"),
                    class_="kh-presence-detail-stat-box",
                ),
            ),
            ui.div(
                {"class": "kh-presence-tl-legend"},
                ui.span({"class": "kh-presence-tl-legend-dot kh-presence-tl-tick-in"}),
                _("Entry"),
                ui.span({"class": "kh-presence-tl-legend-dot kh-presence-tl-tick-out"}),
                _("Exit"),
                ui.span({"class": "kh-presence-tl-legend-dot kh-presence-tl-tick-prey"}),
                _("Prey blocked"),
            ),
            ui.div({"class": "kh-presence-stats-day-list"}, *day_rows),
        )

    @output
    @render.ui
    @reactive.event(
        presence_selected_rfid, presence_stats_view, presence_events_filter, ignore_none=False
    )
    def ui_presence():
        # 07.10, Sid ("moi je voulais une nouvelle page" - the inline version
        # below the tiles worked but wasn't what she wanted): same full-page
        # swap-with-back-button pattern as the "Temps a l'exterieur" stats
        # view just above.
        if presence_selected_rfid() and presence_events_filter():
            return ui.output_ui("ui_presence_events_filtered")
        if presence_selected_rfid() and presence_stats_view():
            return ui.output_ui("ui_presence_stats_detail")
        if presence_selected_rfid():
            return ui.output_ui("ui_presence_detail")
        return ui.div(
            ui.output_ui("ui_presence_doors"),
            ui.br(),
            ui.output_ui("ui_presence_cats"),
        )
