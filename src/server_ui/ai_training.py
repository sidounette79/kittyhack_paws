"""AI training handlers."""

import os
import threading
import pandas as pd
from src.clock import monotonic_time
from shiny import render, ui, reactive
import logging
from faicons import icon_svg
import re
import hashlib
import asyncio
from src.baseconfig import CONFIG, set_language, update_single_config_parameter
from src.helper import is_valid_uuid4
from src.mode import is_remote_mode
from src.database import DetectionFeedbackRepo, EventsRepo, ModelReviewRepo, CatsRepo
from src.model import YoloModel, RemoteModelTrainer
from src.model.retroactive_review import run_retroactive_review_batch
from src.shiny_wrappers import uix
from src.server_ui.state import (
    reload_trigger_ai,
    reload_trigger_config,
    ls_project_choices,
)
from src.server_ui.helpers import _disable_input, collapsible_section
from src.server_ui.context import SessionContext

_ = set_language(CONFIG["LANGUAGE"])

if is_remote_mode():
    from src.remote.hardware import Magnets, Pir  # type: ignore
else:
    from src.magnets_rfid import Magnets
    from src.pir import Pir

from src.server_ui.yolo_modules import (
    btn_yolo_modify,
    btn_yolo_activate,
    manage_yolo_model_server,
    activate_yolo_model_server,
)
from src.server_ui.model_review_modules import (
    btn_model_review_keep_old,
    btn_model_review_confirm_new,
    picker_model_review_other,
    model_review_row_server,
)


# 06.10, Sid ("4 passages de chatière sans aucune notif"): module-level,
# process-wide lock - NOT the per-session reactive.Value below. Confirmed
# live in the logs: 5 "Loading a standalone copy of the active model"
# launches stacked up between 06:20 and 07:20 with only ONE "Pilot done" in
# between, almost certainly because reloading the page (or a second tab)
# gets a brand-new Shiny session, and model_review_running is re-created
# False for that new session even while the OLD session's scan is still
# running in its own background thread - the per-session guard genuinely
# cannot see across sessions. 5 full YOLO model instances fighting for 4
# CPU cores for an hour is a very plausible reason real-time detection (and
# therefore notifications) got starved. This lock is shared by every
# session in the process, so a second "Scan everything" click from ANY tab
# is refused outright instead of stacking.
_model_review_process_lock = threading.Lock()


def register_ai_training(input, output, session, ctx: SessionContext):
    """Register AI Training / Label Studio tab handlers."""

    model_review_running = reactive.Value(False)
    model_review_last_result = reactive.Value(None)

    @output
    @render.ui
    @reactive.event(reload_trigger_ai, ignore_none=True)
    def ui_ai_training():

        # Check if a model training is in progress
        training_status = RemoteModelTrainer.check_model_training_result(
            show_in_progress=True, return_pretty_status=True
        )

        # Show user notifications, if they are any
        if getattr(ctx, "show_user_notifications", None):
            ctx.show_user_notifications()

            # URLs for different languages
        wiki_url = {
            "de": "https://github.com/floppyFK/kittyhack/wiki/%5BDE%5D-Kittyhack-v2.0-%E2%80%90-Eigene-KI%E2%80%90Modelle-trainieren",
            "en": "https://github.com/floppyFK/kittyhack/wiki/%5BEN%5D-Kittyhack-v2.0-%E2%80%90-Train-own-AI%E2%80%90Models",
        }.get(
            CONFIG["LANGUAGE"],
            "https://github.com/floppyFK/kittyhack/wiki/%5BEN%5D-Kittyhack-v2.0-%E2%80%90-Train-own-AI%E2%80%90Models",
        )

        # --- Server maintenance check when no training is in progress ---
        server_status = None
        training_in_progress = is_valid_uuid4(CONFIG["MODEL_TRAINING"])
        if not training_in_progress:
            server_status = RemoteModelTrainer.get_server_status()

            # Build Model Training section content depending on maintenance state or active training
        if training_in_progress:
            # Already in progress: show existing status UI only
            training_content = ui.div(
                ui.markdown(
                    _("A model training is currently in progress.")
                    + (
                        _(
                            "You will be notified by email when the training is finished."
                        )
                        if CONFIG["EMAIL"]
                        else ""
                    )
                ),
                ui.markdown(_("Current status: {}").format(training_status)),
                ui.br(),
                ui.br(),
                ui.input_task_button(
                    "btn_reload_model_training_status",
                    _("Reload Model Training Status"),
                    class_="btn-primary",
                ),
                ui.hr(),
                ui.markdown(
                    _("Your individual Training ID:")
                    + "\n\n"
                    + f"`{CONFIG['MODEL_TRAINING']}`"
                ),
                ui.help_text(_("(use this ID for support requests)")),
                ui.br(),
                ui.br(),
                ui.input_task_button(
                    "btn_cancel_model_training",
                    _("Cancel Model Training"),
                    class_="btn-danger",
                ),
                id="model_training_status",
                style_="text-align: center;",
            )
        else:
            # No training in progress: check maintenance or timeout/error
            is_maintenance = bool(
                server_status and server_status.get("maintenance", False)
            )
            maintenance_msg = (server_status or {}).get("message", "")
            if server_status is None:
                # Timeout or request failure: inform user
                training_content = ui.div(
                    ui.markdown(
                        f"{icon_svg('triangle-exclamation', margin_left='-0.1em')} "
                        + _(
                            "Unable to reach the model training server right now. Please check your network or try again later."
                        )
                    ),
                    ui.br(),
                    ui.markdown(
                        _(
                            "If the issue persists, the server may be temporarily unavailable."
                        )
                    ),
                    style_="text-align: center;",
                )
            elif is_maintenance:
                # Show maintenance notice and do not render upload inputs
                msg = _(
                    "The model training server is currently in maintenance. Please try again later."
                )
                if maintenance_msg:
                    msg += "\n\n" + maintenance_msg
                training_content = ui.div(
                    ui.markdown(f"{icon_svg('wrench', margin_left='-0.1em')} {msg}"),
                    style_="text-align: center;",
                )
            else:
                # Show the original upload form
                model_training_base_model_input = ui.input_select(
                    "model_training_base_model",
                    _("Base model"),
                    {
                        "n": "YOLOv8n",
                        "s": "YOLOv8s",
                        "m": "YOLOv8m",
                        "l": "YOLOv8l",
                        "x": "YOLOv8x",
                        "26n": "YOLO26n (expérimental - pas garanti côté serveur)",
                        "26s": "YOLO26s (expérimental)",
                        "26m": "YOLO26m (expérimental)",
                        "26l": "YOLO26l (expérimental)",
                        "26x": "YOLO26x (expérimental)",
                    },
                    selected="n",
                    width="90%",
                )
                model_training_image_size_input = ui.input_select(
                    "model_training_image_size",
                    _("Image size"),
                    {str(s): str(s) for s in YoloModel.get_supported_image_sizes()},
                    selected="320",
                    width="90%",
                )

                if not is_remote_mode():
                    model_training_base_model_input = _disable_input(
                        model_training_base_model_input
                    )
                    model_training_image_size_input = _disable_input(
                        model_training_image_size_input
                    )

                    # Build training data source options (Label Studio export
                    # removed 24.10 - manual zip upload is the only source now)
                ls_training_source_options = {
                    "__manual__": _("Upload project zip manually"),
                }
                default_training_source = "__manual__"

                training_content = ui.div(
                    ui.div(
                        ui.div(
                            ui.markdown(_("### Training Data")),
                            ui.input_select(
                                "model_training_source",
                                _("Training data source"),
                                ls_training_source_options,
                                selected=default_training_source,
                                width="90%",
                            ),
                            ui.output_ui("ui_model_training_project_note"),
                            ui.output_ui("ui_model_training_data_upload"),
                            ui.input_text(
                                "model_name",
                                _("Model Name (optional)"),
                                placeholder=_("Enter a name for your model"),
                                width="90%",
                            ),
                            ui.input_text(
                                "user_name",
                                _("Username (optional)"),
                                value=CONFIG["USER_NAME"],
                                placeholder=_("Enter your name"),
                                width="90%",
                            ),
                            ui.input_text(
                                "email_notification",
                                _("Email for Notification (optional)"),
                                value=CONFIG["EMAIL"],
                                placeholder=_("Enter your email address"),
                                width="90%",
                            ),
                            ui.help_text(
                                _(
                                    "If you provide an email address, you will be notified when the model training is finished."
                                )
                            ),
                            (
                                ui.div(
                                    ui.tags.button(
                                        ui.tags.span(
                                            "\u25b6",
                                            class_="toggle-chevron",
                                            style_="display:inline-block; transition:transform .2s;",
                                        ),
                                        " ",
                                        _("Advanced options"),
                                        type="button",
                                        class_="btn btn-link p-0 info-toggle-btn",
                                        style_="text-decoration:none;",
                                        **{
                                            "data-bs-toggle": "collapse",
                                            "data-bs-target": "#model_training_advanced_options_body",
                                            "aria-expanded": "false",
                                            "aria-controls": "model_training_advanced_options_body",
                                        },
                                    ),
                                    ui.div(
                                        ui.div(
                                            ui.br(),
                                            model_training_base_model_input,
                                            ui.help_text(
                                                _(
                                                    "Base model (YOLOv8n/s/m/l/x): larger variants are usually more accurate, but they run slower and need more CPU/RAM. "
                                                    "Choose smaller variants (e.g. 'n') if your device struggles to keep up in real-time."
                                                )
                                            ),
                                            model_training_image_size_input,
                                            ui.help_text(
                                                _(
                                                    "Image size (imgsz): higher values can improve detection of small objects, but increase computation and can reduce FPS."
                                                )
                                            ),
                                            ui.help_text(
                                                _(
                                                    "Defaults: YOLOv8n and image size 320."
                                                )
                                            ),
                                            class_="kh-info-box",
                                        ),
                                        id="model_training_advanced_options_body",
                                        class_="collapse info-toggle-body",
                                        style_="margin-top:6px;",
                                    ),
                                    class_="kh-info-toggle",
                                    style_="width: 90%; text-align: left; margin: 0.75rem auto 0 auto;",
                                )
                                if is_remote_mode()
                                else ui.div(
                                    ui.br(),
                                    ui.hr(),
                                    ui.help_text(
                                        _(
                                            "Advanced model options are only available if you run Kittyhack on a separate server (remote mode)."
                                        )
                                    ),
                                    style_="text-align: center;",
                                ),
                            ),
                            ui.br(),
                            ui.hr(),
                            ui.br(),
                            ui.input_task_button(
                                "submit_model_training",
                                _("Submit Model for Training"),
                                class_="btn-primary",
                            ),
                            ui.tags.script(
                                ui.HTML("""
                                    (function() {
                                        var btn = document.getElementById('submit_model_training');
                                        if (!btn) return;
                                        btn.disabled = true;
                                        var tid = setInterval(function() {
                                            if (!document.body.contains(btn)) { clearInterval(tid); return; }

                                            // Manual upload: enable when file upload is complete
                                            var bar = document.querySelector('#model_training_data_progress .progress-bar');
                                            var fileUploaded = bar && bar.textContent.trim().toLowerCase() === 'upload complete';
                                            btn.disabled = !fileUploaded;
                                        }, 300);
                                    })();
                                """)
                            ),
                            id="model_training_form",
                            style_="display: flex; flex-direction: column; align-items: center; justify-content: center; width: 100%;",
                        ),
                        style_="text-align: center; width: 100%;",
                    ),
                )

        ui_ai_training = ui.div(
            ui.div(
                ui.br(),
                # Compact intro (replaces the old Description card)
                ui.div(
                    ui.markdown(
                        _(
                            "Train your own AI model: label images with Label Studio, then submit them for training."
                        )
                        + " "
                        + _("Read the instructions before starting:")
                    ),
                    ui.HTML(
                        f'<a href="{wiki_url}" target="_blank" class="btn btn-sm btn-outline-secondary" style="margin-top:0.25rem; white-space: normal; max-width: 100%; display: inline-flex; align-items: center; justify-content: center; flex-wrap: wrap; text-align: center;">'
                        f'<i class="fa fa-clipboard-list" style="margin-right:5px;"></i>'
                        + _("Instructions for training your own model")
                        + "</a>"
                    ),
                    style_="text-align:center; margin-bottom:1.5rem;",
                ),
                collapsible_section(
                    "ai_model_training",
                    _("Model Training"),
                    (
                        _("A model training is in progress.")
                        if training_in_progress
                        else _("Upload labeled data and submit for model training.")
                    ),
                    ui.div(
                        training_content,
                        class_="generic-container align-left",
                        style_="padding-left:1rem !important; padding-right:1rem !important;",
                    ),
                ),
                collapsible_section(
                    "ai_model_management",
                    _("Model Management"),
                    _("Rename, activate, or remove your trained models."),
                    ui.div(
                        ui.output_ui("manage_yolo_models_table"),
                        class_="generic-container align-left",
                        style_="padding-left:1rem !important; padding-right:1rem !important;",
                    ),
                ),
                collapsible_section(
                    "ai_model_review",
                    _("Retroactive Model Review"),
                    _("Re-run the active model on already-tagged photos and review where it disagrees with what's stored."),
                    ui.div(
                        ui.output_ui("ui_model_review"),
                        class_="generic-container align-left",
                        style_="padding-left:1rem !important; padding-right:1rem !important;",
                    ),
                ),
                class_="generic-container align-left",
                style_="padding-left:1rem !important; padding-right:1rem !important;",
            ),
            ui.br(),
            ui.br(),
            ui.br(),
        )
        return ui_ai_training

    @render.ui
    @reactive.event(reload_trigger_ai, ignore_none=True)
    def manage_yolo_models_table():
        # Keep the table reasonably fresh while the AI tab is open.
        reactive.invalidate_later(60)

        try:
            available_models = YoloModel.get_model_list() or []
            if not available_models:
                return ui.div(
                    _("Nothing here yet. Please train a model first."),
                    class_="text-muted small",
                )

            rows = []
            for model in available_models:
                unique_btn_id = hashlib.md5(os.urandom(16)).hexdigest()
                unique_model_id = model.get("unique_id")
                if not unique_model_id:
                    continue

                is_active_model = (CONFIG.get("YOLO_MODEL") == unique_model_id) and (
                    not (CONFIG.get("TFLITE_MODEL_VERSION") or "").strip()
                )

                fps = model.get("effective_fps", None)
                fps_text = "—"
                try:
                    if fps is not None:
                        fps_text = f"{float(fps):.1f}"
                except Exception:
                    fps_text = "—"

                model_image_size = model.get("model_image_size", 320) or 320
                yolo_variant = (
                    model.get("yolo_variant") or "yolov8n.pt"
                ).strip() or "yolov8n.pt"

                variant_letter = "N"
                variant_title = _("YOLO v8 Nano")
                variant_match = re.search(r"yolov\d+\s*([nslmx])", yolo_variant.lower())
                if variant_match:
                    v = variant_match.group(1)
                    variant_map = {
                        "n": ("N", _("YOLO v8 Nano")),
                        "s": ("S", _("YOLO v8 Small")),
                        "m": ("M", _("YOLO v8 Medium")),
                        "l": ("L", _("YOLO v8 Large")),
                        "x": ("X", _("YOLO v8 Extra Large")),
                    }
                    variant_letter, variant_title = variant_map.get(
                        v, (v.upper(), _("YOLO v8 model"))
                    )

                def build_badges(id_suffix: str):
                    _badges = [
                        ui.tooltip(
                            ui.span(variant_letter, class_="badge text-bg-secondary"),
                            variant_title,
                            id=f"tooltip_yolo_variant_{unique_btn_id}_{id_suffix}",
                            options={"trigger": "hover"},
                        ),
                        ui.tooltip(
                            ui.span(
                                str(int(model_image_size)),
                                class_="badge text-bg-secondary",
                            ),
                            _("Model image size: {}px").format(int(model_image_size)),
                            id=f"tooltip_yolo_img_size_{unique_btn_id}_{id_suffix}",
                            options={"trigger": "hover"},
                        ),
                        ui.tooltip(
                            ui.span(
                                f"{fps_text} FPS", class_="badge text-bg-secondary"
                            ),
                            _("Average FPS with this model"),
                            id=f"tooltip_yolo_fps_{unique_btn_id}_{id_suffix}",
                            options={"trigger": "hover"},
                        ),
                    ]
                    if is_active_model:
                        _badges.insert(
                            0,
                            ui.tooltip(
                                ui.span(_("Active"), class_="badge text-bg-success"),
                                _("Currently active model"),
                                id=f"tooltip_yolo_active_{unique_btn_id}_{id_suffix}",
                                options={"trigger": "hover"},
                            ),
                        )
                    return _badges

                badges_desktop = build_badges("desktop")
                badges_mobile = build_badges("mobile")

                actions = ui.div(
                    ui.tooltip(
                        btn_yolo_activate(f"btn_yolo_activate_{unique_btn_id}"),
                        _("Use this model now"),
                        id=f"tooltip_yolo_activate_{unique_btn_id}",
                        options={"trigger": "hover"},
                    ),
                    ui.tooltip(
                        btn_yolo_modify(f"btn_yolo_modify_{unique_btn_id}"),
                        _("Modify or delete this model"),
                        id=f"tooltip_yolo_modify_{unique_btn_id}",
                        options={"trigger": "hover"},
                    ),
                    class_="d-flex gap-1 justify-content-end",
                )

                # Register event listeners for the buttons
                activate_yolo_model_server(
                    f"btn_yolo_activate_{unique_btn_id}", unique_model_id
                )
                manage_yolo_model_server(
                    f"btn_yolo_modify_{unique_btn_id}", unique_model_id
                )

                name_cell = ui.div(
                    ui.div(
                        ui.span(model.get("display_name", ""), class_="kh-model-name"),
                        class_="d-flex align-items-start gap-2",
                    ),
                    ui.div(model.get("creation_date", ""), class_="kh-model-sub"),
                    ui.div(
                        *badges_mobile, class_="d-flex d-sm-none flex-wrap gap-1 mt-1"
                    ),
                )

                specs_cell = ui.div(
                    ui.div(
                        *badges_desktop,
                        class_="d-none d-sm-flex flex-wrap gap-1 justify-content-center",
                    ),
                )

                rows.append(
                    ui.tags.tr(
                        ui.tags.td(name_cell),
                        ui.tags.td(
                            specs_cell, class_="kh-model-specs kh-model-specs-col"
                        ),
                        ui.tags.td(actions, class_="kh-model-actions"),
                    )
                )

            table = ui.tags.table(
                ui.tags.tbody(*rows),
                class_="table table-sm align-middle table_models_overview",
            )

            return ui.div(table, class_="table-responsive")

        except Exception as e:
            logging.error(f"Failed to build YOLO model management table: {e}")
            return ui.div(
                _("Nothing here yet. Please train a model first."),
                class_="text-muted small",
            )

    def _run_model_review_blocking(num_events: int) -> dict:
        """Blocking - see _export_corrections_blocking above for why this
        must go through asyncio.to_thread rather than run inline."""
        return run_retroactive_review_batch(num_events)

    @reactive.effect
    @reactive.event(input.btn_run_model_review, ignore_none=True)
    async def on_run_model_review():
        if model_review_running.get():
            return
        # 06.10: process-wide lock, not just this session's Value - see the
        # comment on _model_review_process_lock above for why the
        # per-session guard alone let 5 scans stack up for an hour.
        if not _model_review_process_lock.acquire(blocking=False):
            ui.notification_show(
                _("A scan is already running (maybe from another tab/device) - wait for it to finish first."),
                duration=10, type="warning",
            )
            return
        model_review_running.set(True)
        reload_trigger_ai.set(reload_trigger_ai.get() + 1)
        try:
            # 06.10, Sid ("il faut quand même que je puisse lancer un scan
            # de tout"): her whole history is currently 71 events total -
            # 1000 comfortably covers everything in one click, room to grow,
            # still bounded so a single run can't run forever.
            result = await asyncio.to_thread(_run_model_review_blocking, 1000)
        except Exception as e:
            logging.error(f"[MODEL_REVIEW] Pilot run failed: {e}")
            result = {"error": "exception"}
        finally:
            _model_review_process_lock.release()
        model_review_last_result.set(result)
        model_review_running.set(False)

        if result.get("error"):
            ui.notification_show(
                _("The review could not run (no active model or inference failed - check the logs)."),
                duration=8, type="error",
            )
        elif result.get("all_scanned"):
            ui.notification_show(
                _("Every event has already been scanned with this model. Train and activate a new model to get a fresh batch."),
                duration=10, type="message",
            )
        else:
            ui.notification_show(
                _("Reviewed {scanned} photo(s) from {events} event(s): {flagged} disagreement(s) to check.").format(
                    scanned=result.get("scanned", 0),
                    events=result.get("events", 0),
                    flagged=result.get("flagged", 0),
                ),
                duration=10, type="message",
            )
        reload_trigger_ai.set(reload_trigger_ai.get() + 1)

    @render.ui
    @reactive.event(reload_trigger_ai, ignore_none=True)
    def ui_model_review():
        pending = ModelReviewRepo.get_pending_count(CONFIG["KITTYHACK_DATABASE_PATH"])
        # 06.10: also check the process-wide lock, not just this session's
        # own flag - a fresh reload/second tab must see "Running..." too
        # when a DIFFERENT session's scan is still in flight.
        running = model_review_running.get() or _model_review_process_lock.locked()
        last_result = model_review_last_result.get()

        intro = ui.p(
            _(
                "Runs the currently active model again on photos from N events, and flags only the "
                "ones where it now disagrees with what's stored. Each run advances to the next, older "
                "batch of events (never the same ones twice) - nothing is overwritten automatically, "
                "you confirm or dismiss each one below."
            ),
            class_="text-muted small",
        )

        run_button = ui.input_action_button(
            "btn_run_model_review",
            _("Scan everything with the active model") if not running else _("Running..."),
            icon=icon_svg("arrows-rotate"),
            disabled=running,
            class_="btn-primary",
        )

        status_bits = [intro, run_button]

        if last_result and not last_result.get("error"):
            status_bits.append(
                ui.p(
                    _("Last run: {scanned} photo(s) from {events} event(s) scanned in {elapsed:.0f}s, {flagged} flagged.").format(
                        scanned=last_result.get("scanned", 0),
                        events=last_result.get("events", 0),
                        elapsed=last_result.get("elapsed_s", 0.0),
                        flagged=last_result.get("flagged", 0),
                    ),
                    class_="text-muted small mt-2",
                )
            )

        if pending == 0:
            status_bits.append(
                ui.p(_("No disagreements waiting for review right now."), class_="text-muted small mt-2")
            )
            return ui.div(*status_bits)

        # 05.10, Sid ("ça se ferme toujours + écran gris" at 2812 pending
        # items): rendering up to 200 full row cards - each previously
        # running its own DB query just for the cat-name dropdown - on
        # EVERY click was slow enough to starve the websocket heartbeat and
        # trigger Shiny's disconnect overlay. Small, fixed-size page instead,
        # and cat_names fetched once here, not per row.
        REVIEW_PAGE_SIZE = 50
        rows = []
        cat_names = CatsRepo.get_cat_names_list(CONFIG["KITTYHACK_DATABASE_PATH"])
        cat_name_dict = CatsRepo.get_cat_name_rfid_dict(CONFIG["KITTYHACK_DATABASE_PATH"])
        df = ModelReviewRepo.get_pending(CONFIG["KITTYHACK_DATABASE_PATH"], limit=REVIEW_PAGE_SIZE)
        # 06.10, Sid ("deux événements quasi similaires, avec un autre chat -
        # je peine à les distinguer en mode nuit"): toggle the row background
        # every time the underlying event (block_id) changes, so consecutive
        # rows from the SAME passage share a color and a new passage stands
        # out immediately - same "fond actuel / fond bleu, en alternance" she
        # asked for.
        prev_block_id = None
        alt_row = False
        for __, review in df.iterrows():
            review_id = int(review["id"])
            photo_id = int(review["photo_id"])
            unique_btn_id = hashlib.md5(os.urandom(16)).hexdigest()

            block_id = review.get("event_block_id")
            if block_id is not None and not pd.isna(block_id):
                block_id = int(block_id)
            if block_id != prev_block_id:
                alt_row = not alt_row
                prev_block_id = block_id
            row_class = "d-flex gap-3 align-items-center kh-model-review-row" + (
                " kh-model-review-row-alt" if alt_row else ""
            )

            rfid = review.get("event_rfid")
            rfid_cat_name = None
            if rfid:
                rfid_cat_name = cat_name_dict.get(rfid)
                rfid_label = rfid_cat_name or _("Unknown RFID: {}").format(rfid)
            else:
                rfid_label = _("No RFID found")

            # 05.10, Sid ("j'espère surtout que mes chats seront des chats
            # et pas des proies"): "prey" is the one label worth making
            # unmistakable here - everything else is already a raw cat name.
            def _display_name(n):
                if not n:
                    return _("nothing")
                return _("prey") if n == "prey" else n

            old_label = _display_name(review.get("old_name"))
            old_prob = review.get("old_probability") or 0
            new_label = _display_name(review.get("new_name"))
            new_prob = review.get("new_probability") or 0

            model_review_row_server(f"model_review_row_{unique_btn_id}", review_id, photo_id)

            rows.append(
                ui.div(
                    # 05.10, Sid ("je vois rien, un moyen d'agrandir?"): same
                    # markup the Photos tab uses (kh-photo-thumb +
                    # data-orig-src) - app.js already has a global delegated
                    # click handler that opens any such thumb in a lightbox
                    # at full size, no extra wiring needed here.
                    ui.tags.div(
                        {"class": "kh-photo-thumb kh-model-review-thumb", "data-orig-src": f"/orig/{photo_id}.jpg"},
                        ui.img(src=f"/thumb/{photo_id}.jpg", loading="lazy", decoding="async"),
                    ),
                    ui.div(
                        ui.div(
                            ui.span(_("Before: "), class_="text-muted"),
                            ui.strong(f"{old_label} ({old_prob:.0f}%)"),
                        ),
                        ui.div(
                            ui.span(_("After: "), class_="text-muted"),
                            ui.strong(f"{new_label} ({new_prob:.0f}%)"),
                        ),
                        ui.div(
                            ui.span(_("RFID: "), class_="text-muted"),
                            ui.strong(rfid_label),
                            class_="small",
                        ),
                        ui.div(
                            btn_model_review_keep_old(f"model_review_row_{unique_btn_id}"),
                            btn_model_review_confirm_new(f"model_review_row_{unique_btn_id}"),
                            class_="d-flex gap-2 mt-1 flex-wrap",
                        ),
                        ui.div(
                            picker_model_review_other(f"model_review_row_{unique_btn_id}", cat_names, rfid_cat_name),
                            class_="mt-1",
                        ),
                        class_="flex-grow-1",
                    ),
                    class_=row_class,
                )
            )

        heading_text = (
            _("Showing the first {shown} of {n} disagreement(s) waiting for review (most recent handled first):").format(
                shown=len(rows), n=pending
            )
            if pending > REVIEW_PAGE_SIZE
            else _("{n} disagreement(s) waiting for review:").format(n=pending)
        )
        status_bits.append(
            ui.div(
                ui.p(heading_text, class_="fw-bold mt-3"),
                ui.div(*rows, class_="d-flex flex-column gap-2"),
            )
        )
        return ui.div(*status_bits)

    @reactive.Effect
    @reactive.event(input.btn_reload_model_training_status)
    def on_reload_model_training_status():
        reload_trigger_ai.set(reload_trigger_ai.get() + 1)
        reload_trigger_config.set(reload_trigger_config.get() + 1)

    @reactive.Effect
    @reactive.event(input.btn_cancel_model_training)
    def on_cancel_model_training():
        # Check if a model training is in progress
        if not is_valid_uuid4(CONFIG["MODEL_TRAINING"]):
            ui.notification_show(
                _("No model training in progress."), duration=15, type="error"
            )
            CONFIG["MODEL_TRAINING"] = ""
            update_single_config_parameter("MODEL_TRAINING")
            reload_trigger_ai.set(reload_trigger_ai.get() + 1)
            return

            # Show confirmation dialog
        m = ui.modal(
            _("Do you really want to cancel the model training?"),
            title=_("Cancel Model Training"),
            easy_close=True,
            footer=ui.div(
                ui.input_action_button("btn_modal_cancel_training_ok", _("OK")),
                ui.input_action_button("btn_modal_cancel", _("Cancel")),
            ),
        )
        ui.modal_show(m)

    @reactive.effect
    @reactive.event(input.btn_modal_cancel_training_ok)
    def modal_cancel_training():
        ui.modal_remove()
        # Cancel the model training
        RemoteModelTrainer.cancel_model_training(CONFIG["MODEL_TRAINING"])
        CONFIG["MODEL_TRAINING"] = ""
        update_single_config_parameter("MODEL_TRAINING")
        ui.notification_show(
            _("Model training cancelled."), duration=15, type="message"
        )
        reload_trigger_ai.set(reload_trigger_ai.get() + 1)

    @output
    @render.ui
    def ui_model_training_data_upload():
        """Show/hide file upload based on training data source selection."""
        try:
            selected = input.model_training_source()
            if selected == "__manual__" or selected is None:
                return ui.div(
                    uix.input_file(
                        "model_training_data",
                        _("Upload Label-Studio Training Data (ZIP file)"),
                        accept=".zip",
                        multiple=False,
                        width="90%",
                    ),
                    ui.br(),
                    style_="width: 100%; max-width: 720px; margin: 0 auto; display: flex; flex-direction: column; align-items: center;",
                )
            else:
                return ui.div()
        except Exception:
            return ui.div(
                uix.input_file(
                    "model_training_data",
                    _("Upload Label-Studio Training Data (ZIP file)"),
                    accept=".zip",
                    multiple=False,
                    width="90%",
                ),
                ui.br(),
                style_="width: 100%; max-width: 720px; margin: 0 auto; display: flex; flex-direction: column; align-items: center;",
            )

    @output
    @render.ui
    def ui_model_training_project_note():
        # 24.10: Label Studio export removed - this readiness note only ever
        # applied to that source, never to a manual zip upload.
        return ui.div()

    @reactive.Effect
    @reactive.event(input.submit_model_training)
    def on_submit_model_training():
        zip_file_path = None

        if input.model_training_data() is None:
            ui.notification_show(
                _("Please upload a ZIP file with the training data."),
                duration=10,
                type="error",
            )
            return

            # Check if the file is a ZIP file
        if not input.model_training_data()[0]["name"].endswith(".zip"):
            ui.notification_show(
                _(
                    "The uploaded file is not a ZIP file. Please upload a valid ZIP file."
                ),
                duration=10,
                type="error",
            )
            return

            # Get the uploaded file path
        zip_file_path = input.model_training_data()[0]["datapath"]
        logging.info(f"[MODEL_TRAINING] User uploaded ZIP file: {zip_file_path}")

        model_name = input.model_name()
        email_notification = input.email_notification()
        user_name = input.user_name()

        # Advanced model parameters are only available in remote-mode.
        # In target-mode, the select inputs are not rendered in the DOM,
        # so accessing them would raise a SilentException.
        if is_remote_mode():
            model_variant = (
                str(input.model_training_base_model() or "n").strip().lower()
            )
            image_size_raw = str(input.model_training_image_size() or "320").strip()
        else:
            model_variant = "n"
            image_size_raw = "320"

        if model_variant not in {"n", "s", "m", "l", "x", "26n", "26s", "26m", "26l", "26x"}:
            ui.notification_show(
                _("Invalid base model selection."), duration=10, type="error"
            )
            return

        try:
            image_size_candidate = int(image_size_raw)
        except Exception:
            ui.notification_show(
                _("Invalid image size selection."), duration=10, type="error"
            )
            return

        supported_sizes = set(YoloModel.get_supported_image_sizes())
        if image_size_candidate not in supported_sizes:
            ui.notification_show(
                _("Invalid image size selection."), duration=10, type="error"
            )
            return

        image_size = int(image_size_candidate)

        if email_notification:
            # Validate the email address
            if not re.match(r"[^@]+@[^@]+\.[^@]+", email_notification):
                ui.notification_show(
                    _("Please enter a valid email address."), duration=10, type="error"
                )
                return
            if email_notification != CONFIG["EMAIL"]:
                # Update the email address in the config
                CONFIG["EMAIL"] = email_notification
                update_single_config_parameter("EMAIL")

        if user_name and (user_name != CONFIG["USER_NAME"]):
            CONFIG["USER_NAME"] = user_name
            update_single_config_parameter("USER_NAME")

        logging.info(
            f"Enqueued model training: Model Name: '{model_name}', Email: '{email_notification}', "
            f"ZIP file: '{zip_file_path}', YOLOv8 variant: '{model_variant}', image size: {image_size}"
        )

        # Start the model training process
        result = RemoteModelTrainer.enqueue_model_training(
            zip_file_path,
            model_name,
            user_name,
            email_notification,
            yolo_model_variant=model_variant,
            image_size=image_size,
        )
        if is_valid_uuid4(result):
            # Update the config with the training details
            CONFIG["MODEL_TRAINING"] = result
            update_single_config_parameter("MODEL_TRAINING")
        elif result == "invalid_file":
            ui.notification_show(
                _(
                    "The uploaded file is not a valid Label Studio training data ZIP file."
                ),
                duration=15,
                type="error",
            )
            return
        elif result in ["destination_unreachable", "destination_not_found"]:
            ui.notification_show(
                _(
                    "The destination for the model training is unreachable. Please check your network connection or try again later."
                ),
                duration=15,
                type="error",
            )
            return
        else:
            ui.notification_show(
                _("An error occurred while starting the model training: {}").format(
                    result
                ),
                duration=15,
                type="error",
            )
            return

        reload_trigger_ai.set(reload_trigger_ai.get() + 1)

