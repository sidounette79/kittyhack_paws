"""Remote<->Kittyflap connection history (04.10, Sid).

"J'aimerais etre sure qu'elle est fiable... une rubrique info qui
recense les deconnexions, un peu sur le mode de parcours, deconnexions,
reconnexions, duree en ligne, que je puisse me rendre compte que ca
tient la route." Persisted (RemoteConnectionLogRepo), not just the live
status dot, so it actually builds a real history across every restart.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
from faicons import icon_svg
from shiny import reactive, render, ui

from src.baseconfig import CONFIG, set_language
from src.database import RemoteConnectionLogRepo
from src.server_ui.context import SessionContext

_ = set_language(CONFIG["LANGUAGE"])


def _format_duration(seconds: float) -> str:
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


def register_remote_connection_log(input, output, session, ctx: SessionContext):
    reload_trigger = reactive.Value(0)

    @reactive.effect
    @reactive.event(input.btn_reload_remote_connection_log)
    def _on_reload_remote_connection_log():
        reload_trigger.set(reload_trigger() + 1)

    @output
    @render.ui
    @reactive.event(reload_trigger, ignore_none=False)
    def ui_remote_connection_log():
        database = CONFIG["KITTYHACK_DATABASE_PATH"]
        df = RemoteConnectionLogRepo.get_recent(database, limit=500)

        header = ui.div(
            {"class": "kh-presence-stats-header", "style": "padding-top:0;"},
            ui.div(_("Connection history"), class_="kh-presence-stats-title"),
            ui.input_action_button(
                "btn_reload_remote_connection_log",
                "",
                icon=icon_svg("rotate"),
                class_="btn-date-filter",
                style_="position:absolute; right:0; top:-4px;",
            ),
        )

        if df.empty:
            return ui.div(
                header,
                ui.div(
                    {"class": "kh-empty-state"},
                    icon_svg("wifi"),
                    ui.p(_("No connection history yet.")),
                ),
            )

        tz = ZoneInfo(CONFIG["TIMEZONE"])
        df["ts_utc"] = pd.to_datetime(df["created_at"], format="mixed", utc=True, errors="coerce")
        df = df.dropna(subset=["ts_utc"]).sort_values("ts_utc", ascending=False).reset_index(drop=True)

        now = datetime.now(tz)

        # First pass: per-row duration (this state, from its start to the
        # next transition / now for the current one), in chronological order
        # under the hood regardless of display order.
        durations = []
        for i in range(len(df)):
            start = df.iloc[i]["ts_utc"].tz_convert(tz)
            end = now if i == 0 else df.iloc[i - 1]["ts_utc"].tz_convert(tz)
            durations.append((end - start).total_seconds())

        total_connected_s = sum(d for d, row in zip(durations, df["event"]) if row == "connected")
        total_disconnected_s = sum(d for d, row in zip(durations, df["event"]) if row == "disconnected")
        total_s = total_connected_s + total_disconnected_s
        uptime_pct = (100.0 * total_connected_s / total_s) if total_s > 0 else 0.0
        disconnect_count = int((df["event"] == "disconnected").sum())
        longest_connected_s = max(
            (d for d, row in zip(durations, df["event"]) if row == "connected"), default=0.0
        )
        span_label = _("since {date}").format(date=df.iloc[-1]["ts_utc"].tz_convert(tz).strftime("%d.%m.%Y"))

        summary = ui.div(
            {"class": "kh-presence-detail-stats kh-presence-detail-stats-3col"},
            ui.div(
                ui.div(f"{uptime_pct:.1f}%", class_="kh-presence-detail-stat-value"),
                ui.div(_("Uptime"), class_="kh-presence-detail-stat-label"),
                class_="kh-presence-detail-stat-box",
            ),
            ui.div(
                ui.div(str(disconnect_count), class_="kh-presence-detail-stat-value"),
                ui.div(_("Disconnections"), class_="kh-presence-detail-stat-label"),
                class_="kh-presence-detail-stat-box",
            ),
            ui.div(
                ui.div(_format_duration(longest_connected_s), class_="kh-presence-detail-stat-value"),
                ui.div(_("Longest stable streak"), class_="kh-presence-detail-stat-label"),
                class_="kh-presence-detail-stat-box",
            ),
        )

        rows = []
        for i, row in df.iterrows():
            local_dt = row["ts_utc"].tz_convert(tz)
            is_connected = row["event"] == "connected"
            duration_s = durations[i]

            badge_class = "kh-dir-in" if is_connected else "kh-dir-out"
            label = _("Connected") if is_connected else _("Disconnected")
            icon_name = "wifi" if is_connected else "triangle-exclamation"
            reason = str(row.get("reason") or "").strip()
            still_ongoing = (
                f" ({_('ongoing')})" if i == 0 else ""
            )

            rows.append(
                ui.div(
                    {"class": "kh-journey-item kh-journey-event"},
                    ui.div(
                        {"class": "kh-journey-event-main"},
                        ui.div(
                            {
                                "class": "kh-presence-detail-status-icon",
                                "style": "width:40px;height:40px;flex-shrink:0;",
                            },
                            icon_svg(icon_name),
                        ),
                        ui.div(
                            {"class": "kh-journey-event-info"},
                            ui.div(
                                local_dt.strftime("%d.%m.%Y %H:%M:%S"),
                                class_="kh-journey-event-time",
                            ),
                            ui.div(
                                ui.span(label, class_=badge_class, style_="font-weight:600;"),
                                f" · {_format_duration(duration_s)}{still_ongoing}",
                                class_="kh-journey-event-cat",
                            ),
                            (
                                ui.div(reason, class_="kh-presence-stats-day-summary")
                                if reason
                                else None
                            ),
                        ),
                    ),
                )
            )

        return ui.div(
            header,
            ui.div(summary, style_="margin: 14px 0 18px;"),
            ui.div({"class": "kh-presence-tl-legend"}, _("{span} · newest first").format(span=span_label)),
            ui.div({"class": "kh-journey-list", "style": "margin-top:10px;"}, *rows),
        )
