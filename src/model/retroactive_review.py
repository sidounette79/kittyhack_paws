"""Retroactive re-evaluation of already-tagged photos with the currently
active model (05.10, Sid: "appliquer le nouveau modele retroactivement sur
les photos deja taggees pour voir s'il apprenait bien").

Read-only against ``events``/``detection_feedback`` - this module only ever
writes to ``model_review_queue`` (a disposable review queue, see
ModelReviewRepo). Nothing here overwrites stored data; Sid reviews each
flagged photo by hand on the AI Training tab and only THAT action ever
writes a real ``detection_feedback`` correction, through the same pipeline
already used for the Photos tab's manual corrections.

Loads its OWN, separate YOLO model instance rather than touching the live
``model_runtime.model_handler`` - that one is driven continuously by the
real-time camera loop and is not safe to share with a one-off batch job
running from a different thread. This means a review run adds its own,
temporary CPU load on top of live detection for its duration.
"""
import logging
import multiprocessing
import time

import numpy as np
import cv2

from src.baseconfig import CONFIG
from src.model.model_handler import ModelHandler
from src.model.yolo_model import YoloModel
from src.database import DatabaseCore, EventsRepo, ReturnDataPhotosDB, ModelReviewRepo, DetectionFeedbackRepo


def _decode_image(image_bytes: bytes):
    if not image_bytes:
        return None
    arr = np.frombuffer(image_bytes, dtype=np.uint8)
    frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    return frame


_PREY_LABELS = ("prey", "beute")


def _normalize_name(name: str) -> str:
    """05.10, Sid ("j'espère surtout que mes chats seront des chats et pas
    des proies"): a cat misclassified as prey is the one disagreement that
    actually matters most (false prey-block/notification on her own cat) -
    without this, it was invisible, folded into a generic "nothing found"
    because "prey"/"beute" weren't in cat_names at all. Normalized to one
    label regardless of which of the two the model's labels.txt uses."""
    name = (name or "").strip().lower()
    return "prey" if name in _PREY_LABELS else name


def _effective_dominant_cat(photo_id: int, detected_objects: list, cat_names: list[str], database: str):
    """Highest-probability cat-or-prey "ground truth" for a photo - the
    stored event_text, but with any of Sid's already-CONFIRMED corrections
    (detection_feedback) substituted in by object_index.

    05.10, Sid ("est-ce qu'il ne faudra pas qu'un futur modèle compare
    l'original et les corrections déjà passées"): without this, "old" was
    always the raw, never-updated event_text - so a later, better model
    that correctly agreed with HER correction would still get flagged as a
    fresh disagreement (it still disagrees with the untouched original),
    making her re-confirm the same photo every single retrain cycle. Using
    the latest confirmed value as the real baseline means a model that
    catches up with her past corrections just quietly stops being flagged.
    A confirmed "none" (false positive) removes that index from
    consideration entirely, rather than being compared as a name.
    Returns (index, name, probability) or (None, None, 0.0)."""
    feedback = DetectionFeedbackRepo.get_for_photo(database, photo_id)
    relevant = set(cat_names) | {"prey"}
    best = (None, None, 0.0)

    for idx, obj in enumerate(detected_objects):
        fb = feedback.get(idx)
        if fb and fb.get("confirmed"):
            raw_name = fb.get("corrected_name") or fb.get("original_name")
            if not raw_name or raw_name.strip().lower() == "none":
                continue  # confirmed false positive - not a candidate
            name = _normalize_name(raw_name)
            prob = float(fb.get("original_probability") or obj.probability or 0)
        else:
            name = _normalize_name(obj.object_name)
            prob = float(obj.probability)
        if name in relevant and prob > best[2]:
            best = (idx, name, prob)

    # Corrections added beyond the original list (e.g. a cat the original
    # pass missed entirely, confirmed via "Confirm new"/"Other" on an index
    # with no prior detection) - treated as full-confidence once confirmed.
    for idx, fb in feedback.items():
        if idx < len(detected_objects) or not fb.get("confirmed"):
            continue
        raw_name = fb.get("corrected_name")
        if not raw_name or raw_name.strip().lower() == "none":
            continue
        name = _normalize_name(raw_name)
        if name in relevant and 100.0 > best[2]:
            best = (idx, name, 100.0)

    return best


def _dominant_cat_from_new(detected_objects: list, cat_names: list[str]):
    """Highest-probability cat-or-prey detection dict from a fresh inference
    pass (keys: name/probability/x/y/w/h, percent coords). Returns
    (name, probability, obj_dict) or (None, 0.0, None)."""
    relevant = set(cat_names) | {"prey"}
    best = (None, 0.0, None)
    for obj in detected_objects:
        name = _normalize_name(obj.get("name"))
        prob = float(obj.get("probability") or 0.0)
        if name in relevant and prob > best[1]:
            best = (name, prob, obj)
    return best


def _next_unscanned_block_ids(database: str, model_version: str, num_events: int) -> list[int]:
    """05.10, Sid ("une fois que j'ai fait les 50, ça lance les 50
    précédents?"): most recent events first, but skipping any block already
    scanned for this exact model_version - so a second pilot run naturally
    advances to older, not-yet-seen events instead of re-scanning (and
    re-flagging/clobbering already-reviewed decisions in) the same window."""
    already_scanned = ModelReviewRepo.get_scanned_block_ids(database, model_version)
    df = DatabaseCore.read_df_from_database(
        database,
        "SELECT block_id FROM events WHERE block_id IS NOT NULL AND deleted != 1 "
        "GROUP BY block_id ORDER BY MAX(id) DESC",
    )
    if df.empty:
        return []
    out = []
    for b in df["block_id"].tolist():
        b = int(b)
        if b in already_scanned:
            continue
        out.append(b)
        if len(out) >= num_events:
            break
    return out


def run_retroactive_review_batch(num_events: int = 50) -> dict:
    """Blocking - call via asyncio.to_thread from the UI, same pattern as
    the corrections export. Returns a summary dict."""
    database = CONFIG["KITTYHACK_DATABASE_PATH"]

    model_version = CONFIG.get("YOLO_MODEL")
    if not model_version:
        return {"error": "no_active_model"}
    model_path = YoloModel.get_model_path(model_version)
    model_image_size = YoloModel.get_model_image_size(model_version) or 320
    if not model_path:
        return {"error": "model_not_found"}

    logging.info(f"[MODEL_REVIEW] Loading a standalone copy of the active model ({model_version}) for review...")
    handler = ModelHandler(
        model="yolo",
        modeldir=model_path,
        labelfile="labels.txt",
        model_image_size=model_image_size,
        num_threads=multiprocessing.cpu_count(),
    )
    try:
        handler.load_model()
    except Exception as e:
        logging.error(f"[MODEL_REVIEW] Failed to load model for review: {e}")
        return {"error": "model_load_failed"}

    cat_names = handler.cat_names  # already lowercased, see ModelHandler.__init__

    block_ids = _next_unscanned_block_ids(database, model_version, num_events)
    if not block_ids:
        return {"scanned": 0, "flagged": 0, "events": 0, "model_version": model_version, "all_scanned": True}

    scanned = 0
    flagged = 0
    t0 = time.monotonic()

    for block_id in block_ids:
        df = EventsRepo.db_get_photos_by_block_id(
            database, block_id, ReturnDataPhotosDB.all_original_image
        )
        if df.empty:
            ModelReviewRepo.mark_blocks_scanned(database, [block_id], model_version)
            continue
        for __, row in df.iterrows():
            event_text = row.get("event_text")
            if not event_text:
                continue
            frame = _decode_image(row.get("original_image"))
            if frame is None:
                continue

            old_objects = EventsRepo.read_event_from_json(event_text)
            old_idx, old_name, old_prob = _effective_dominant_cat(
                int(row["id"]), old_objects, cat_names, database
            )

            try:
                __, __, new_detected = handler._yolo(frame, handler.input_size)
            except Exception as e:
                logging.warning(f"[MODEL_REVIEW] Inference failed for photo {row['id']}: {e}")
                continue
            new_name, new_prob, new_obj = _dominant_cat_from_new(new_detected, cat_names)

            scanned += 1

            if old_name == new_name:
                continue  # agreement (including both None) - nothing to review

            flagged += 1
            if new_obj is not None:
                nx, ny, nw, nh = new_obj["x"], new_obj["y"], new_obj["w"], new_obj["h"]
            else:
                # Model now finds no cat at all where one was stored before -
                # keep the OLD box so the review card still has something to
                # point at (there's no new box to show). old_idx can point
                # at a confirmed correction beyond the original list (added
                # via "Confirm new"/"Other"), which has its own stored box.
                if old_idx is not None and old_idx < len(old_objects):
                    old_obj = old_objects[old_idx]
                    nx, ny, nw, nh = old_obj.x, old_obj.y, old_obj.width, old_obj.height
                elif old_idx is not None:
                    fb_row = DetectionFeedbackRepo.get_for_photo(database, int(row["id"])).get(old_idx)
                    nx, ny, nw, nh = (
                        (fb_row["x"], fb_row["y"], fb_row["width"], fb_row["height"]) if fb_row else (0, 0, 0, 0)
                    )
                else:
                    nx, ny, nw, nh = (0, 0, 0, 0)

            ModelReviewRepo.upsert_disagreement(
                database,
                photo_id=int(row["id"]),
                old_object_index=old_idx,
                old_name=old_name,
                old_probability=old_prob,
                new_name=new_name or "",
                new_probability=new_prob,
                x=nx, y=ny, width=nw, height=nh,
                model_version=model_version,
            )

        # Mark the whole block scanned once its photos are done, regardless
        # of whether any disagreement was found - that's what makes the
        # NEXT pilot run skip it and move to older events.
        ModelReviewRepo.mark_blocks_scanned(database, [block_id], model_version)

    elapsed = time.monotonic() - t0
    logging.info(
        f"[MODEL_REVIEW] Pilot done: {scanned} photos scanned across {len(block_ids)} events, "
        f"{flagged} disagreement(s) flagged, {elapsed:.1f}s."
    )
    return {
        "scanned": scanned,
        "flagged": flagged,
        "events": len(block_ids),
        "model_version": model_version,
        "elapsed_s": elapsed,
    }
