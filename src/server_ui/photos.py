"""Pictures tab handlers."""

import html as html_module
import json
import os
import pandas as pd
from datetime import datetime, timedelta
from types import SimpleNamespace
from shiny import render, ui, reactive
import logging
from zoneinfo import ZoneInfo
from faicons import icon_svg
import math
import asyncio
from src.baseconfig import CONFIG, set_language, update_single_config_parameter
from src.helper import DateTimeUtil
from src.system import LabelStudioInstall
from src.database import (
    CatsRepo,
    DatabaseCore,
    DetectionFeedbackRepo,
    EventsRepo,
    ReturnDataPhotosDB,
)
from src.paths import pictures_original_dir
from src.mode import is_remote_mode
from src.labelstudio_api import upload_image_to_labelstudio_project
from src.server_ui.state import reload_trigger_photos
from src.server_ui.context import SessionContext

_ = set_language(CONFIG["LANGUAGE"])

if is_remote_mode():
    from src.remote.hardware import Magnets, Pir  # type: ignore
else:
    from src.magnets_rfid import Magnets
    from src.pir import Pir


def register_photos(input, output, session, ctx: SessionContext):
    """Register Pictures tab UI and handlers."""

    def _cat_filter_choices() -> dict:
        """{"": "All cats", rfid: name, ...} for the photos-page cat filter dropdown."""
        choices = {"": _("All cats")}
        try:
            for rfid, name in CatsRepo.get_cat_name_rfid_dict(
                CONFIG["KITTYHACK_DATABASE_PATH"]
            ).items():
                choices[rfid] = name
        except Exception:
            pass
        return choices

    def _selected_cat_rfid_filter() -> str:
        """04.10, Sid ("filtre par nom de chat"): selected cat's rfid, or "" for no filter."""
        try:
            return str(input.selCatFilter() or "")
        except Exception:
            return ""

    @output
    @render.ui
    def ui_photos_date():
        """
        Creates a UI component for selecting and filtering photos by date.

        The UI component includes:
        - A date selector with decrement and increment buttons.
        - A "Today" button to quickly select the current date.
        - Switches to filter photos to show only detected cats or mice.

        Returns:
            uiDateBar (ui.div): A UI div element containing the date selection and filtering controls.
        """
        uiDateBar = ui.div(
            ui.row(
                ui.div(
                    ui.div(
                        ui.input_action_button(
                            "button_decrement",
                            "",
                            icon=icon_svg("angle-left", margin_right="auto"),
                            class_="btn-date-control",
                        ),
                        class_="col-auto px-1",
                    ),
                    ui.div(
                        ui.input_date(
                            "date_selector", "", format=CONFIG["DATE_FORMAT"]
                        ),
                        class_="col-auto px-1",
                    ),
                    ui.div(
                        ui.input_action_button(
                            "button_increment",
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
                        "button_today",
                        _("Today"),
                        icon=icon_svg("calendar-day"),
                        class_="btn-date-filter",
                    ),
                    class_="col-auto px-1",
                ),
                ui.div(
                    ui.input_action_button(
                        "button_reload",
                        "",
                        icon=icon_svg("rotate", margin_right="auto"),
                        class_="btn-date-filter",
                    ),
                    class_="col-auto px-1",
                ),
                class_="d-flex justify-content-center align-items-center",  # Centers elements horizontally and prevents wrapping
            ),
            ui.br(),
            ui.row(
                ui.div(
                    ui.input_switch(
                        "button_cat_only",
                        _("Show detected cats only"),
                        CONFIG["SHOW_CATS_ONLY"],
                    ),
                    class_="col-auto btn-date-filter px-1",
                ),
                ui.div(
                    ui.input_switch(
                        "button_mouse_only",
                        _("Show detected mice only"),
                        CONFIG["SHOW_MICE_ONLY"],
                    ),
                    class_="col-auto btn-date-filter px-1",
                ),
                ui.div(
                    ui.input_switch(
                        "button_detection_overlay",
                        _("Show detection overlay"),
                        CONFIG["SHOW_IMAGES_WITH_OVERLAY"],
                    ),
                    class_="col-auto btn-date-filter px-1",
                ),
                ui.div(
                    ui.input_switch(
                        "button_events_view",
                        _("Group pictures to events"),
                        CONFIG["GROUP_PICTURES_TO_EVENTS"],
                    ),
                    class_="col-auto btn-date-filter px-1",
                ),
                class_="d-flex justify-content-center align-items-center",  # Centers elements horizontally
            ),
            ui.br(),
            ui.row(
                ui.div(
                    ui.input_select(
                        "selCatFilter",
                        _("Filter by cat"),
                        _cat_filter_choices(),
                        selected="",
                        width="100%",
                    ),
                    class_="col-auto px-1",
                    style_="min-width: 220px;",
                ),
                class_="d-flex justify-content-center align-items-center",
            ),
            class_="container",  # Adds centering within a smaller container
        )
        return uiDateBar

    @reactive.Effect
    @reactive.event(input.button_cat_only)
    def update_config_show_cats_only():
        CONFIG["SHOW_CATS_ONLY"] = input.button_cat_only()
        update_single_config_parameter("SHOW_CATS_ONLY")

    @reactive.Effect
    @reactive.event(input.button_mouse_only)
    def update_config_show_mice_only():
        CONFIG["SHOW_MICE_ONLY"] = input.button_mouse_only()
        update_single_config_parameter("SHOW_MICE_ONLY")

    @reactive.Effect
    @reactive.event(input.button_decrement, ignore_none=True)
    def dec_ui_photos_date():
        """
        Decrease the date in the UI date selector by one day.
        This function retrieves the current date from the input date selector,
        decreases it by one day, and updates the date input using the session's
        send_input_message method.
        Returns:
            None
        """
        # Get the current date from the input
        current_date = input.date_selector()

        # Only proceed if the date is set
        if current_date:
            new_date = pd.to_datetime(current_date).date() - timedelta(days=1)
            # Update the date input using session.send_input_message
            session.send_input_message(
                "date_selector", {"value": new_date.strftime("%Y-%m-%d")}
            )

    @reactive.Effect
    @reactive.event(input.button_increment, ignore_none=True)
    def inc_ui_photos_date():
        """
        Increments the date selected in the UI by one day.
        This function retrieves the current date from a date selector input,
        increments it by one day, and updates the date selector input with
        the new date.
        Returns:
            None
        """
        # Get the current date from the input
        current_date = input.date_selector()

        # Only proceed if the date is set
        if current_date:
            new_date = pd.to_datetime(current_date).date() + timedelta(days=1)
            # Update the date input using session.send_input_message
            session.send_input_message(
                "date_selector", {"value": new_date.strftime("%Y-%m-%d")}
            )

    def _photos_filters_to_utc_range() -> tuple[str, str]:
        date_start = DateTimeUtil.format_date_minmax(input.date_selector(), True)
        date_end = DateTimeUtil.format_date_minmax(input.date_selector(), False)
        timezone = ZoneInfo(CONFIG["TIMEZONE"])
        date_start_utc = (
            datetime.strptime(date_start, "%Y-%m-%d %H:%M:%S")
            .replace(tzinfo=timezone)
            .astimezone(ZoneInfo("UTC"))
            .strftime("%Y-%m-%d %H:%M:%S%z")
        )
        date_end_utc = (
            datetime.strptime(date_end, "%Y-%m-%d %H:%M:%S")
            .replace(tzinfo=timezone)
            .astimezone(ZoneInfo("UTC"))
            .strftime("%Y-%m-%d %H:%M:%S%z")
        )
        return date_start_utc, date_end_utc

    def _photos_total_pages() -> tuple[int, int]:
        date_start_utc, date_end_utc = _photos_filters_to_utc_range()
        total_count = EventsRepo.db_count_photos(
            CONFIG["KITTYHACK_DATABASE_PATH"],
            date_start_utc,
            date_end_utc,
            input.button_cat_only(),
            input.button_mouse_only(),
            CONFIG["MOUSE_THRESHOLD"],
            rfid_filter=_selected_cat_rfid_filter(),
        )
        per_page = max(1, int(CONFIG["ELEMENTS_PER_PAGE"]))
        total_pages = max(1, int(math.ceil(float(total_count) / float(per_page))))
        return total_count, total_pages

    @reactive.Effect
    @reactive.event(
        input.button_reload,
        input.date_selector,
        input.button_cat_only,
        input.selCatFilter,
        input.button_mouse_only,
        reload_trigger_photos,
        ignore_none=True,
    )
    def reset_photos_page_on_filter_change():
        if input.button_events_view():
            return
        try:
            __count, total_pages = _photos_total_pages()
            session.send_input_message(
                "photos_page", {"value": 1, "min": 1, "max": total_pages}
            )
        except Exception:
            pass

    @reactive.Effect
    @reactive.event(input.photos_prev_page, ignore_none=True)
    def photos_prev_page():
        if input.button_events_view():
            return
        try:
            __count, total_pages = _photos_total_pages()
            current = int(input.photos_page() or 1)
            new_val = max(1, min(total_pages, current - 1))
            session.send_input_message(
                "photos_page", {"value": new_val, "min": 1, "max": total_pages}
            )
        except Exception:
            pass

    @reactive.Effect
    @reactive.event(input.photos_next_page, ignore_none=True)
    def photos_next_page():
        if input.button_events_view():
            return
        try:
            __count, total_pages = _photos_total_pages()
            current = int(input.photos_page() or 1)
            new_val = max(1, min(total_pages, current + 1))
            session.send_input_message(
                "photos_page", {"value": new_val, "min": 1, "max": total_pages}
            )
        except Exception:
            pass

    @reactive.Effect
    @reactive.event(input.button_today, ignore_none=True)
    def reset_ui_photos_date():
        # Get the current date
        now = datetime.now()
        session.send_input_message("date_selector", {"value": now.strftime("%Y-%m-%d")})

    @output
    @render.ui
    @reactive.event(input.button_events_view, ignore_none=True)
    def ui_photos_events():
        if input.button_events_view():
            return ui.output_ui("ui_events_by_date")
        else:
            return ui.div(
                ui.output_ui("ui_photos_cards_nav"),
                ui.output_ui("ui_photos_cards"),
                ui.output_ui("ui_photos_cards_nav_bottom"),
            )

    def _photos_nav_state():
        date_start = DateTimeUtil.format_date_minmax(input.date_selector(), True)
        date_end = DateTimeUtil.format_date_minmax(input.date_selector(), False)
        timezone = ZoneInfo(CONFIG["TIMEZONE"])
        date_start = (
            datetime.strptime(date_start, "%Y-%m-%d %H:%M:%S")
            .replace(tzinfo=timezone)
            .astimezone(ZoneInfo("UTC"))
            .strftime("%Y-%m-%d %H:%M:%S%z")
        )
        date_end = (
            datetime.strptime(date_end, "%Y-%m-%d %H:%M:%S")
            .replace(tzinfo=timezone)
            .astimezone(ZoneInfo("UTC"))
            .strftime("%Y-%m-%d %H:%M:%S%z")
        )

        total_count = EventsRepo.db_count_photos(
            CONFIG["KITTYHACK_DATABASE_PATH"],
            date_start,
            date_end,
            input.button_cat_only(),
            input.button_mouse_only(),
            CONFIG["MOUSE_THRESHOLD"],
            rfid_filter=_selected_cat_rfid_filter(),
        )

        per_page = max(1, int(CONFIG["ELEMENTS_PER_PAGE"]))
        total_pages = max(1, int(math.ceil(float(total_count) / float(per_page))))

        try:
            current_page = int(input.photos_page())
        except Exception:
            current_page = 1
        current_page = max(1, min(total_pages, current_page))

        # Keep input bounds synced (use raw input message since update_numeric isn't used elsewhere)
        try:
            session.send_input_message(
                "photos_page", {"value": current_page, "min": 1, "max": total_pages}
            )
        except Exception:
            pass

        return current_page, total_pages, total_count

    @output
    @render.ui
    @reactive.event(
        input.button_reload,
        input.date_selector,
        input.button_cat_only,
        input.selCatFilter,
        input.button_mouse_only,
        reload_trigger_photos,
        ignore_none=True,
    )
    def ui_photos_cards_nav():
        if input.button_events_view():
            return ui.div()

        current_page, total_pages, total_count = _photos_nav_state()

        return ui.div(
            ui.div(
                ui.input_action_button(
                    "photos_prev_page",
                    "",
                    icon=icon_svg("angle-left"),
                    class_="btn-page-control",
                ),
                ui.input_numeric(
                    "photos_page",
                    _("Page"),
                    value=current_page,
                    min=1,
                    max=total_pages,
                    step=1,
                    width="4rem",
                ),
                ui.tags.span(f"/ {total_pages}", class_="photos-page-total"),
                ui.input_action_button(
                    "photos_next_page",
                    "",
                    icon=icon_svg("angle-right"),
                    class_="btn-page-control",
                ),
                ui.tags.span(
                    f"{total_count} " + _("pictures"),
                    class_="photos-count",
                ),
                class_="photos-pager",
            ),
            class_="container",
        )

    # 04.10, Sid ("il faut le paginateur aussi en bas, sinon je dois tout
    # remonter"): a second, display-only pager under the cards. Deliberately
    # NOT a second editable input_numeric mirroring "photos_page" - that
    # risked fighting with reset_photos_page_on_filter_change (below) and the
    # top nav's own bounds-sync over who owns the input's value each render.
    # Just prev/next buttons (own ids) driving the same "photos_page" input
    # the top nav already owns, plus plain text for page/count.
    @output
    @render.ui
    @reactive.event(
        input.button_reload,
        input.date_selector,
        input.button_cat_only,
        input.selCatFilter,
        input.button_mouse_only,
        reload_trigger_photos,
        input.photos_page,
        ignore_none=True,
    )
    def ui_photos_cards_nav_bottom():
        if input.button_events_view():
            return ui.div()

        total_count, total_pages = _photos_total_pages()
        try:
            current_page = max(1, min(total_pages, int(input.photos_page() or 1)))
        except Exception:
            current_page = 1

        return ui.div(
            ui.div(
                ui.input_action_button(
                    "photos_prev_page_bottom",
                    "",
                    icon=icon_svg("angle-left"),
                    class_="btn-page-control",
                ),
                ui.tags.span(f"{current_page} / {total_pages}", class_="photos-page-total"),
                ui.input_action_button(
                    "photos_next_page_bottom",
                    "",
                    icon=icon_svg("angle-right"),
                    class_="btn-page-control",
                ),
                ui.tags.span(
                    f"{total_count} " + _("pictures"),
                    class_="photos-count",
                ),
                class_="photos-pager",
            ),
            class_="container",
        )

    @reactive.effect
    @reactive.event(input.photos_prev_page_bottom, ignore_none=True)
    def _on_photos_prev_page_bottom():
        __count, total_pages = _photos_total_pages()
        current = int(input.photos_page() or 1)
        new_val = max(1, min(total_pages, current - 1))
        session.send_input_message(
            "photos_page", {"value": new_val, "min": 1, "max": total_pages}
        )

    @reactive.effect
    @reactive.event(input.photos_next_page_bottom, ignore_none=True)
    def _on_photos_next_page_bottom():
        __count, total_pages = _photos_total_pages()
        current = int(input.photos_page() or 1)
        new_val = max(1, min(total_pages, current + 1))
        session.send_input_message(
            "photos_page", {"value": new_val, "min": 1, "max": total_pages}
        )

    def _boxes_overlap(a, b, pad=2.0):
        """AABB intersection test on x/y/width/height percentages, with a
        small padding tolerance since labels sit just inside each box's
        corner - two boxes close enough for their labels to visually touch
        even without the boxes themselves strictly overlapping."""
        ax1, ay1 = a.x - pad, a.y - pad
        ax2, ay2 = a.x + a.width + pad, a.y + a.height + pad
        bx1, by1 = b.x - pad, b.y - pad
        bx2, by2 = b.x + b.width + pad, b.y + b.height + pad
        return ax1 < bx2 and ax2 > bx1 and ay1 < by2 and ay2 > by1

    def _assign_label_slots(detected_objects):
        """05.10, Sid ("les étiquettes se mettent toujours les unes sur les
        autres"): the old top/bottom-by-index alternation didn't actually
        look at whether boxes overlap, so two detections with the same
        index parity (or 3+ overlapping at once) could still collide.
        Greedy graph coloring instead: walk objects in order, give each one
        the lowest-numbered slot not already used by an object it actually
        overlaps - overlapping boxes are then guaranteed different slots,
        however many there are."""
        slots = []
        for i, obj in enumerate(detected_objects):
            used = {
                slots[j]
                for j in range(i)
                if _boxes_overlap(obj, detected_objects[j])
            }
            slot = 0
            while slot in used:
                slot += 1
            slots.append(slot)
        return slots

    def _build_photo_card(
        data_row, cat_name_dict, show_overlay: bool, extra_class: str = "",
        photo_feedback: dict | None = None,
    ):
        """Build a single photo card UI element."""
        mouse_probability = data_row["mouse_probability"]

        event_text = data_row["event_text"]
        if event_text:
            detected_objects = EventsRepo.read_event_from_json(event_text)
        else:
            detected_objects = []
        photo_feedback = photo_feedback or {}

        try:
            photo_timestamp = pd.to_datetime(
                DateTimeUtil.get_local_date_from_utc_date(data_row["created_at"])
            ).strftime("%H:%M:%S")
        except ValueError:
            photo_timestamp = "Unknown date"

        if data_row["rfid"]:
            cat_name = cat_name_dict.get(
                data_row["rfid"], _("Unknown RFID: {}".format(data_row["rfid"]))
            )
        else:
            cat_name = _("No RFID found")

        card_footer_mouse = f"{icon_svg('magnifying-glass')} {mouse_probability:.1f}%"
        if cat_name:
            card_footer_cat = f" | {icon_svg('cat')} {cat_name}"
        else:
            card_footer_cat = ""

        pid = int(data_row["id"])
        thumb_src = f"/thumb/{pid}.jpg"
        orig_src = f"/orig/{pid}.jpg"

        img_html = f'''<div class="kh-photo-thumb" data-photo-id="{pid}" data-orig-src="{orig_src}">
                <img src="{thumb_src}" loading="lazy" decoding="async" />'''

        # 05.10, Sid ("je puisse tagger directement depuis ces photos"): a
        # manually-drawn box (no matching entry in detected_objects, just a
        # detection_feedback row with an object_index beyond the model's own
        # list - see handle_photos_detection_add) needs to render too, even
        # on a photo the model found nothing on at all.
        extra_fb_items = sorted(
            (idx, fb) for idx, fb in photo_feedback.items() if idx >= len(detected_objects)
        )
        if show_overlay and (detected_objects or extra_fb_items):
            # 09.09, Sid: same validate/correct affordance as the event
            # modal (see event_modal.py's startCrossfade... no, its
            # handle_detection_correction / event-modal.js) - a real
            # detection label here is clickable to confirm/correct/mark as
            # false, keyed by the same (photo_id, object_index) pair
            # DetectionFeedbackRepo uses everywhere. A plain <button> (not a
            # div) so app.js's existing photo-lightbox click handler already
            # ignores it (it excludes any click landing on a button/a/
            # .kh-photo-actions) - no extra wiring needed there.
            extra_boxes = [
                SimpleNamespace(
                    x=fb.get("x") or 0, y=fb.get("y") or 0,
                    width=fb.get("width") or 0, height=fb.get("height") or 0,
                )
                for __, fb in extra_fb_items
            ]
            label_slots = _assign_label_slots(list(detected_objects) + extra_boxes)
            # Slot -> (side, px offset): alternate top/bottom first, then
            # step further out for a 3rd/4th truly-overlapping detection.
            slot_positions = [
                ("top", 4), ("bottom", 4), ("top", 26), ("bottom", 26),
                ("top", 48), ("bottom", 48),
            ]
            for obj_idx, detected_object in enumerate(detected_objects):
                obj_name = (detected_object.object_name or "").strip()
                if obj_name.lower() == "false-accept":
                    continue
                # 04.10, Sid: label INSIDE the box instead of outside it - offsetting from
                # the box's own bottom edge broke for a tall box already spanning most of
                # the frame (see event-modal.js for the full reasoning). 05.10: slot comes
                # from real overlap detection now (_assign_label_slots), not just index
                # parity, so 2+ boxes that actually collide always land on different spots.
                near_top = detected_object.y < 8
                side, px = slot_positions[label_slots[obj_idx] % len(slot_positions)]
                if near_top and side == "top":
                    side = "bottom"
                label_pos = f"{side}: {px}px"
                fb = photo_feedback.get(obj_idx)
                label_text = f"{html_module.escape(obj_name)} ({detected_object.probability:.0f}%)"
                if fb is not None:
                    reviewed_as = fb.get("original_name") if fb.get("confirmed") else fb.get("corrected_name")
                    if reviewed_as is None:
                        label_text = f"✗ {label_text}"
                    elif reviewed_as != obj_name:
                        label_text = f"✓ {label_text} → {html_module.escape(str(reviewed_as))}"
                    else:
                        label_text = f"✓ {label_text}"
                img_html += f'''
                <div class="kh-detect-box" style="left:{detected_object.x}%; top:{detected_object.y}%; width:{detected_object.width}%; height:{detected_object.height}%;">
                    <button type="button" class="kh-detect-label kh-detect-label-clickable" style="{label_pos};"
                        data-pid="{pid}" data-obj-idx="{obj_idx}" data-obj-name="{html_module.escape(obj_name)}">
                        {label_text}
                    </button>
                </div>'''

            # Manually-added boxes: not yet editable/removable by clicking
            # (handle_photos_detection_correction only knows the model's own
            # detected_objects indices), so rendered as plain text, not a
            # button - styled distinctly (green) so it's clear it's a human
            # addition, not a model guess.
            for slot_i, (obj_idx, fb) in enumerate(extra_fb_items):
                box = extra_boxes[slot_i]
                near_top = box.y < 8
                side, px = slot_positions[
                    label_slots[len(detected_objects) + slot_i] % len(slot_positions)
                ]
                if near_top and side == "top":
                    side = "bottom"
                label_pos = f"{side}: {px}px"
                added_name = html_module.escape(str(fb.get("corrected_name") or "?"))
                img_html += f'''
                <div class="kh-detect-box kh-detect-box-manual" style="left:{box.x}%; top:{box.y}%; width:{box.width}%; height:{box.height}%;">
                    <span class="kh-detect-label" style="{label_pos};">✓ {added_name} ({_("added")})</span>
                </div>'''

        img_html += "</div>"

        ls_disabled = (
            not CONFIG.get("LABELSTUDIO_API_TOKEN")
            or not CONFIG.get("LABELSTUDIO_PROJECT")
            or not LabelStudioInstall.get_labelstudio_status()
        )

        card_class = "image-container kh-photo-card" + (
            " image-container-alert"
            if mouse_probability >= CONFIG["MOUSE_THRESHOLD"]
            else ""
        )
        if extra_class:
            card_class += f" {extra_class}"

        return ui.card(
            ui.card_header(
                ui.div(
                    ui.HTML(f"{photo_timestamp} | {data_row['id']}"),
                ),
            ),
            ui.HTML(img_html),
            ui.card_footer(
                ui.div(
                    ui.div(
                        ui.tooltip(
                            ui.HTML(card_footer_mouse),
                            _("Mouse probability"),
                            options={"trigger": "hover"},
                        ),
                        ui.HTML(card_footer_cat),
                        class_="kh-photo-footer-info",
                    ),
                    ui.div(
                        ui.tooltip(
                            ui.tags.a(
                                ui.HTML(
                                    str(
                                        icon_svg(
                                            "image", margin_left="0", margin_right="0"
                                        )
                                    )
                                ),
                                href=f"/orig/{pid}.jpg",
                                download=f"kittyhack_photo_{pid}.jpg",
                                class_="btn btn-icon-square btn-outline-primary kh-photo-action-btn",
                            ),
                            _("Download picture"),
                            options={"trigger": "hover"},
                        ),
                        ui.tooltip(
                            ui.tags.button(
                                ui.HTML(
                                    str(
                                        icon_svg(
                                            "vector-square", margin_left="0", margin_right="0"
                                        )
                                    )
                                ),
                                type="button",
                                class_="btn btn-icon-square btn-outline-success kh-photo-action-btn kh-photo-add-detection-btn",
                                **{"data-pid": str(pid)},
                            ),
                            _("Tag a missed cat/prey directly on this picture"),
                            options={"trigger": "hover"},
                        ),
                        ui.tooltip(
                            ui.input_action_button(
                                id=f"photo_send_ls_{pid}",
                                label="",
                                icon=icon_svg(
                                    "upload", margin_left="0", margin_right="0"
                                ),
                                class_="btn-icon-square btn-outline-secondary kh-photo-action-btn",
                                disabled_=ls_disabled,
                            ),
                            _("Send picture to Label Studio"),
                            options={"trigger": "hover"},
                        ),
                        ui.tooltip(
                            ui.input_action_button(
                                id=f"photo_delete_{pid}",
                                label="",
                                icon=icon_svg(
                                    "trash-can", margin_left="0", margin_right="0"
                                ),
                                class_="btn-icon-square btn-outline-danger kh-photo-action-btn",
                            ),
                            _("Delete picture"),
                            options={"trigger": "hover"},
                        ),
                        class_="kh-photo-actions",
                    ),
                    class_="kh-photo-footer-row",
                ),
            ),
            id=f"photo_card_{pid}",
            class_=card_class,
        )

    @output
    @render.ui
    @reactive.event(
        input.photos_page,
        input.button_events_view,  # to clear when switching view mode
        input.button_detection_overlay,  # to toggle overlays without extra reloads
        input.button_reload,
        input.date_selector,
        input.button_cat_only,
        input.selCatFilter,
        input.button_mouse_only,
        reload_trigger_photos,
        ignore_none=True,
    )
    def ui_photos_cards():
        if input.button_events_view():
            return ui.div()

        date_start_utc, date_end_utc = _photos_filters_to_utc_range()

        total_count = EventsRepo.db_count_photos(
            CONFIG["KITTYHACK_DATABASE_PATH"],
            date_start_utc,
            date_end_utc,
            input.button_cat_only(),
            input.button_mouse_only(),
            CONFIG["MOUSE_THRESHOLD"],
            rfid_filter=_selected_cat_rfid_filter(),
        )
        per_page = max(1, int(CONFIG["ELEMENTS_PER_PAGE"]))
        total_pages = max(1, int(math.ceil(float(total_count) / float(per_page))))

        try:
            page_number = int(input.photos_page())
        except Exception:
            page_number = 1
        page_number = max(1, min(total_pages, page_number))

        # EventsRepo.db_get_photos uses a reverse paging scheme; convert "newest page=1" into its index.
        page_index = max(0, total_pages - page_number)

        df_photos = EventsRepo.db_get_photos(
            CONFIG["KITTYHACK_DATABASE_PATH"],
            ReturnDataPhotosDB.all_except_photos,
            date_start_utc,
            date_end_utc,
            input.button_cat_only(),
            input.button_mouse_only(),
            CONFIG["MOUSE_THRESHOLD"],
            page_index,
            per_page,
            rfid_filter=_selected_cat_rfid_filter(),
        )

        if df_photos.empty:
            logging.info("No pictures for the selected filter criteria found.")
            return ui.div(
                ui.div(
                    ui.HTML(f"{icon_svg('image', height='2.5em', width='2.5em')}"),
                    ui.p(_("No pictures for the selected filter criteria found.")),
                    class_="kh-empty-state",
                ),
            )

        cat_name_dict = CatsRepo.get_cat_name_rfid_dict(
            CONFIG["KITTYHACK_DATABASE_PATH"]
        )
        show_overlay = bool(input.button_detection_overlay())

        # 09.09, Sid: batched lookup of any existing human corrections for
        # this page's photos, same reasoning as the event modal's own
        # batched fetch - avoids one query per card.
        feedback_by_photo = (
            DetectionFeedbackRepo.get_for_photos(
                CONFIG["KITTYHACK_DATABASE_PATH"], df_photos["id"].tolist()
            )
            if show_overlay
            else {}
        )

        ui_cards = [
            _build_photo_card(
                row, cat_name_dict, show_overlay,
                photo_feedback=feedback_by_photo.get(int(row["id"])),
            )
            for _, row in df_photos.iterrows()
        ]

        # Same correction-choice set as the event modal (registered cats +
        # "Prey") - see event_modal.py's own correction_labels for why this
        # is sourced from the cat registry rather than the active model's
        # labels.txt.
        correction_labels = CatsRepo.get_cat_names_list(
            CONFIG["KITTYHACK_DATABASE_PATH"]
        ) + ["Prey"]

        return ui.div(
            ui.tags.script(
                ui.HTML(
                    json.dumps(
                        {
                            "correctionLabels": correction_labels,
                            "i18n": {
                                "confirm": _("Correct"),
                                "falsePositive": _("Not a real detection"),
                            },
                        }
                    )
                ),
                type="application/json",
                id="kh_photos_correction_data",
            ),
            ui.tags.div(
                *ui_cards,
                class_="kh-photo-grid",
                id="photos_grid",
                **{"data-per-page": str(per_page)},
            ),
        )

        # Per-photo action handlers: delete, send to Label Studio
        # These are dynamic based on photo IDs currently on the page.

    # ---- Detection feedback (validate/correct) - ungrouped Photos grid ----
    # 09.09, Sid: same validate/correct feature as the event modal, now also
    # reachable straight from the ungrouped Photos view (not just from
    # inside an event's own player) - both write to the exact same
    # detection_feedback table, keyed by (photo_id, object_index), so a
    # correction made from either place shows up in the other.
    @reactive.effect
    @reactive.event(input.photos_client_correction)
    def handle_photos_detection_correction():
        try:
            payload = json.loads(input.photos_client_correction() or "{}")
            pid = int(payload.get("pid"))
            obj_idx = int(payload.get("idx"))
            action = str(payload.get("action") or "")
            corrected_name = payload.get("correctedName")

            df = DatabaseCore.read_df_from_database(
                CONFIG["KITTYHACK_DATABASE_PATH"],
                f"SELECT event_text FROM events WHERE id = {pid}",
            )
            if df.empty:
                raise ValueError(f"Unknown photo id {pid}")
            event_text = df.iloc[0]["event_text"]
            objs = EventsRepo.read_event_from_json(event_text) if event_text else []
            if obj_idx < 0 or obj_idx >= len(objs):
                raise ValueError(f"Object index {obj_idx} out of range for photo {pid}")
            dobj = objs[obj_idx]

            if action == "confirm":
                confirmed, final_corrected_name = True, None
            elif action == "false_positive":
                confirmed, final_corrected_name = False, "none"
            elif action == "correct":
                if not corrected_name:
                    raise ValueError("Missing correctedName for a 'correct' action")
                confirmed, final_corrected_name = False, str(corrected_name)
            else:
                raise ValueError(f"Unknown correction action '{action}'")

            result = DetectionFeedbackRepo.upsert(
                CONFIG["KITTYHACK_DATABASE_PATH"],
                photo_id=pid,
                object_index=obj_idx,
                original_name=dobj.object_name,
                original_probability=dobj.probability or 0,
                x=dobj.x, y=dobj.y, width=dobj.width, height=dobj.height,
                corrected_name=final_corrected_name,
                confirmed=confirmed,
            )
            if not result.success:
                raise RuntimeError(result.message)

            if confirmed:
                msg = _("Confirmed: {name}").format(name=dobj.object_name)
            elif final_corrected_name == "none":
                msg = _("Marked as a false detection ({name})").format(name=dobj.object_name)
            else:
                msg = _("Corrected: {old} → {new}").format(
                    old=dobj.object_name, new=final_corrected_name
                )
            ui.notification_show(msg, type="message", duration=3)
        except Exception as e:
            logging.warning(f"[DETECTION_FEEDBACK] Failed to record correction (photos grid): {e}")
            ui.notification_show(
                _("Failed to record the correction."), type="error", duration=5
            )

    # 05.10, Sid ("je puisse tagger directement depuis ces photos"): a box
    # the model missed entirely, drawn by hand (photos-correction-client.js's
    # draw mode) rather than correcting an existing detection - same table,
    # but with no "original" detection behind it (original_name=None,
    # original_probability=0) and a fresh object_index that can't collide
    # with the photo's real detections or any earlier manual addition.
    @reactive.effect
    @reactive.event(input.photos_client_add_detection)
    def handle_photos_detection_add():
        try:
            payload = json.loads(input.photos_client_add_detection() or "{}")
            pid = int(payload.get("pid"))
            x = float(payload.get("x"))
            y = float(payload.get("y"))
            width = float(payload.get("width"))
            height = float(payload.get("height"))
            label = str(payload.get("label") or "").strip()
            if not label:
                raise ValueError("Missing label for a manually added detection")

            df = DatabaseCore.read_df_from_database(
                CONFIG["KITTYHACK_DATABASE_PATH"],
                f"SELECT event_text FROM events WHERE id = {pid}",
            )
            if df.empty:
                raise ValueError(f"Unknown photo id {pid}")
            event_text = df.iloc[0]["event_text"]
            objs = EventsRepo.read_event_from_json(event_text) if event_text else []

            existing_feedback = DetectionFeedbackRepo.get_for_photo(
                CONFIG["KITTYHACK_DATABASE_PATH"], pid
            )
            new_idx = len(objs)
            while new_idx in existing_feedback:
                new_idx += 1

            result = DetectionFeedbackRepo.upsert(
                CONFIG["KITTYHACK_DATABASE_PATH"],
                photo_id=pid,
                object_index=new_idx,
                original_name=None,
                original_probability=0,
                x=x, y=y, width=width, height=height,
                corrected_name=label,
                confirmed=False,
            )
            if not result.success:
                raise RuntimeError(result.message)

            ui.notification_show(
                _("Added: {name}").format(name=label), type="message", duration=3
            )
        except Exception as e:
            logging.warning(f"[DETECTION_FEEDBACK] Failed to record a manually added detection: {e}")
            ui.notification_show(
                _("Failed to add the detection."), type="error", duration=5
            )

    _photo_action_registered_ids: set = set()

    @reactive.effect
    def _register_photo_actions():
        """Dynamically register per-photo action handlers for current page."""
        try:
            if input.button_events_view():
                return
        except Exception:
            pass

        try:
            date_start_utc, date_end_utc = _photos_filters_to_utc_range()
            total_count = EventsRepo.db_count_photos(
                CONFIG["KITTYHACK_DATABASE_PATH"],
                date_start_utc,
                date_end_utc,
                input.button_cat_only(),
                input.button_mouse_only(),
                CONFIG["MOUSE_THRESHOLD"],
                rfid_filter=_selected_cat_rfid_filter(),
            )
            per_page = max(1, int(CONFIG["ELEMENTS_PER_PAGE"]))
            total_pages = max(1, int(math.ceil(float(total_count) / float(per_page))))
            try:
                page_number = int(input.photos_page() or 1)
            except Exception:
                page_number = 1
            page_number = max(1, min(total_pages, page_number))
            page_index = max(0, total_pages - page_number)
            df_photos = EventsRepo.db_get_photos(
                CONFIG["KITTYHACK_DATABASE_PATH"],
                ReturnDataPhotosDB.only_ids,
                date_start_utc,
                date_end_utc,
                input.button_cat_only(),
                input.button_mouse_only(),
                CONFIG["MOUSE_THRESHOLD"],
                page_index,
                per_page,
                rfid_filter=_selected_cat_rfid_filter(),
            )
        except Exception:
            df_photos = pd.DataFrame()

        current_ids = (
            set(df_photos["id"].tolist())
            if not df_photos.empty and "id" in df_photos.columns
            else set()
        )
        new_ids = current_ids - _photo_action_registered_ids

        for pid in new_ids:
            _photo_action_registered_ids.add(pid)
            _register_single_photo_delete(int(pid))
            _register_single_photo_send_ls(int(pid))

    def _register_single_photo_delete(pid: int):
        @reactive.effect
        @reactive.event(input[f"photo_delete_{pid}"])
        def _handler():
            result = EventsRepo.delete_photo_by_id(
                CONFIG["KITTYHACK_DATABASE_PATH"], pid
            )
            if result.success:
                ui.notification_show(
                    _("Photo {} deleted successfully.").format(pid),
                    duration=5,
                    type="message",
                )
                # Card is removed client-side instantly via JS.
                # Backfill: pull the next photo into the current page so it stays full.
                try:
                    date_start_utc, date_end_utc = _photos_filters_to_utc_range()
                    total_count = EventsRepo.db_count_photos(
                        CONFIG["KITTYHACK_DATABASE_PATH"],
                        date_start_utc,
                        date_end_utc,
                        input.button_cat_only(),
                        input.button_mouse_only(),
                        CONFIG["MOUSE_THRESHOLD"],
                        rfid_filter=_selected_cat_rfid_filter(),
                    )

                    if total_count == 0:
                        reload_trigger_photos.set(reload_trigger_photos.get() + 1)
                        return

                    per_page = max(1, int(CONFIG["ELEMENTS_PER_PAGE"]))
                    total_pages = max(
                        1, int(math.ceil(float(total_count) / float(per_page)))
                    )
                    current_page = int(input.photos_page() or 1)

                    if current_page > total_pages:
                        # Current page no longer exists — navigate back
                        session.send_input_message(
                            "photos_page",
                            {
                                "value": max(1, total_pages),
                                "min": 1,
                                "max": total_pages,
                            },
                        )
                        return

                        # Re-query the current page to find a replacement card
                    page_index = max(0, total_pages - current_page)
                    df_page = EventsRepo.db_get_photos(
                        CONFIG["KITTYHACK_DATABASE_PATH"],
                        ReturnDataPhotosDB.all_except_photos,
                        date_start_utc,
                        date_end_utc,
                        input.button_cat_only(),
                        input.button_mouse_only(),
                        CONFIG["MOUSE_THRESHOLD"],
                        page_index,
                        per_page,
                        rfid_filter=_selected_cat_rfid_filter(),
                    )

                    if not df_page.empty:
                        page_ids = set(int(r) for r in df_page["id"].tolist())
                        # The replacement is any ID from this page we haven't seen yet
                        new_ids = page_ids - _photo_action_registered_ids
                        if new_ids:
                            cat_name_dict = CatsRepo.get_cat_name_rfid_dict(
                                CONFIG["KITTYHACK_DATABASE_PATH"]
                            )
                            show_overlay = False
                            try:
                                show_overlay = bool(input.button_detection_overlay())
                            except Exception:
                                pass
                            for new_pid in new_ids:
                                row = df_page[df_page["id"] == new_pid].iloc[0]
                                card = _build_photo_card(
                                    row,
                                    cat_name_dict,
                                    show_overlay,
                                    extra_class="kh-fadein",
                                )
                                ui.insert_ui(card, "#photos_grid", where="beforeEnd")
                                _photo_action_registered_ids.add(new_pid)
                                _register_single_photo_delete(int(new_pid))
                                _register_single_photo_send_ls(int(new_pid))

                                # Update total-pages display via page input bounds
                    session.send_input_message(
                        "photos_page",
                        {"value": current_page, "min": 1, "max": total_pages},
                    )
                except Exception:
                    pass
            else:
                ui.notification_show(
                    _("An error occurred while deleting the photo: {}").format(
                        result.message
                    ),
                    duration=10,
                    type="error",
                )

    def _register_single_photo_send_ls(pid: int):
        @reactive.effect
        @reactive.event(input[f"photo_send_ls_{pid}"])
        async def _handler():
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

            img_bytes = None
            try:
                fp = os.path.join(pictures_original_dir(), f"{pid}.jpg")
                if os.path.exists(fp):
                    with open(fp, "rb") as f:
                        img_bytes = f.read()
            except Exception:
                img_bytes = None

            if img_bytes is None:
                try:
                    df = DatabaseCore.read_df_from_database(
                        CONFIG["KITTYHACK_DATABASE_PATH"],
                        f"SELECT original_image FROM events WHERE id = {int(pid)}",
                    )
                    if not df.empty:
                        ob = df.iloc[0].get("original_image")
                        if isinstance(ob, (bytes, bytearray)) and len(ob) > 0:
                            img_bytes = bytes(ob)
                except Exception:
                    pass

            if not isinstance(img_bytes, (bytes, bytearray)) or len(img_bytes) == 0:
                ui.notification_show(
                    _("Could not load the image."), type="warning", duration=5
                )
                return

            ui.notification_show(
                ui.HTML(
                    '<div class="d-flex align-items-center gap-2">'
                    '<div class="spinner-border spinner-border-sm" role="status" aria-hidden="true"></div>'
                    f"<span>{_('Sending picture to Label Studio...')}</span>"
                    "</div>"
                ),
                id="ls_upload_progress",
                type="message",
                duration=None,
            )

            filename = f"kittyhack_photo_{pid}.jpg"
            success = await asyncio.to_thread(
                upload_image_to_labelstudio_project,
                project_id=int(project_id),
                image_bytes=img_bytes,
                filename=filename,
                token=api_token,
            )

            ui.notification_remove("ls_upload_progress")

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
