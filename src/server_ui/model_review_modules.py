"""Per-row action buttons for the retroactive model-review queue (AI Training
tab, 05.10, Sid). Same pattern as yolo_modules.py's activate/modify buttons:
one Shiny module instance per row, namespaced by a per-row unique id."""

import logging
import pandas as pd
from shiny import ui, reactive, module
from faicons import icon_svg

from src.baseconfig import CONFIG, set_language
from src.database import DetectionFeedbackRepo, ModelReviewRepo
from src.server_ui.state import reload_trigger_ai

_ = set_language(CONFIG["LANGUAGE"])


@module.ui
def btn_model_review_keep_old():
    return ui.input_action_button(
        id="btn_keep_old",
        label=_("Keep old"),
        icon=icon_svg("clock-rotate-left", margin_left="0", margin_right="5px"),
        class_="btn-sm btn-outline-secondary",
    )


@module.ui
def btn_model_review_confirm_new():
    return ui.input_action_button(
        id="btn_confirm_new",
        label=_("Confirm new"),
        icon=icon_svg("check", margin_left="0", margin_right="5px"),
        class_="btn-sm btn-primary",
    )


@module.ui
def picker_model_review_other(cat_names: list[str]):
    """05.10, Sid ("c'est aucun des deux, c'est patoune"): both the stored
    tag and the model's new guess can be wrong at once - a 3rd way out,
    picking the real cat directly, same known-cats list the Photos tab's
    own correction UI uses.

    ``cat_names`` is passed in (fetched ONCE by the caller) rather than
    queried here - with a queue of thousands of pending rows, a DB query
    per row on every single re-render was part of what made each click slow
    enough to starve the websocket heartbeat ("écran gris" disconnect).

    05.10, Sid ("il me faudra aussi Rien et Proie"): same two non-cat
    outcomes the Photos tab's own correction UI offers (correction_labels +
    its separate "false positive" action) - "Prey"/"none" are the exact
    values that pipeline already understands downstream (see
    _export_corrections_blocking's corrected.lower() != "none" check)."""
    choices = {name: name for name in cat_names}
    choices["Prey"] = _("Prey (not a cat)")
    choices["none"] = _("Nothing (false detection)")
    return ui.div(
        ui.input_select("sel_other_name", None, choices=choices, width="170px"),
        ui.input_action_button(
            "btn_confirm_other", _("Other"),
            icon=icon_svg("pen", margin_left="0", margin_right="5px"),
            class_="btn-sm btn-outline-primary",
        ),
        class_="d-flex gap-2 align-items-center flex-wrap",
    )


def _resolve_object_index(row: dict, photo_id: int) -> tuple[int, str | None, float]:
    """Shared by Confirm-new and Other: which detection_feedback slot to
    write into, and what the "original" values were for that slot."""
    old_idx = row.get("old_object_index")
    if not pd.isna(old_idx):
        return int(old_idx), row.get("old_name"), float(row.get("old_probability") or 0)
    existing_feedback = DetectionFeedbackRepo.get_for_photo(CONFIG["KITTYHACK_DATABASE_PATH"], photo_id)
    object_index = 0
    while object_index in existing_feedback:
        object_index += 1
    return object_index, None, 0.0


def _write_correction(review_id: int, photo_id: int, row: dict, corrected_name: str) -> None:
    """Shared by Confirm-new and Other - the only difference between them is
    where ``corrected_name`` comes from."""
    object_index, original_name, original_probability = _resolve_object_index(row, photo_id)

    result = DetectionFeedbackRepo.upsert(
        CONFIG["KITTYHACK_DATABASE_PATH"],
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

    ModelReviewRepo.mark_reviewed(CONFIG["KITTYHACK_DATABASE_PATH"], review_id, "confirmed_new")


@module.server
def model_review_row_server(input, output, session, review_id: int, photo_id: int):
    """Server logic for one review-queue row, identified by its
    model_review_queue.id (``review_id``)."""

    @reactive.effect
    @reactive.event(input.btn_keep_old)
    def on_keep_old():
        result = ModelReviewRepo.mark_reviewed(
            CONFIG["KITTYHACK_DATABASE_PATH"], review_id, "kept_old"
        )
        if result.success:
            ui.notification_show(_("Kept the existing tag."), duration=4, type="message")
        else:
            logging.error(f"[MODEL_REVIEW] Failed to mark review {review_id} kept_old: {result.message}")
            ui.notification_show(_("Could not save this decision."), duration=6, type="error")
        reload_trigger_ai.set(reload_trigger_ai.get() + 1)

    @reactive.effect
    @reactive.event(input.btn_confirm_new)
    def on_confirm_new():
        try:
            row = ModelReviewRepo.get_one(CONFIG["KITTYHACK_DATABASE_PATH"], review_id)
            if row is None:
                raise ValueError(f"Review row {review_id} is no longer pending")
            _write_correction(review_id, photo_id, row, row.get("new_name"))
            ui.notification_show(
                _("Confirmed: {name}").format(name=row.get("new_name")),
                duration=4, type="message",
            )
        except Exception as e:
            logging.error(f"[MODEL_REVIEW] Failed to confirm new tag for review {review_id}: {e}")
            ui.notification_show(_("Could not save this correction."), duration=6, type="error")
        reload_trigger_ai.set(reload_trigger_ai.get() + 1)

    @reactive.effect
    @reactive.event(input.btn_confirm_other)
    def on_confirm_other():
        try:
            other_name = (input.sel_other_name() or "").strip()
            if not other_name:
                raise ValueError("No cat selected")
            row = ModelReviewRepo.get_one(CONFIG["KITTYHACK_DATABASE_PATH"], review_id)
            if row is None:
                raise ValueError(f"Review row {review_id} is no longer pending")
            _write_correction(review_id, photo_id, row, other_name)
            ui.notification_show(
                _("Confirmed: {name}").format(name=other_name), duration=4, type="message",
            )
        except Exception as e:
            logging.error(f"[MODEL_REVIEW] Failed to save 'other' correction for review {review_id}: {e}")
            ui.notification_show(_("Could not save this correction."), duration=6, type="error")
        reload_trigger_ai.set(reload_trigger_ai.get() + 1)
