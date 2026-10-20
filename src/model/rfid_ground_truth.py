"""Automatic cat reassignment from confirmed RFID reads (09.10, Sid: "la
revérification doit gérer automatiquement de réattribuer cet événement-là au
bon chat tout du long, pour relancer un entrainement et permettre aux
détections suivantes d'apprendre mieux").

Unlike ``src.model.retroactive_review`` (which re-runs YOLO inference and
flags disagreements for Sid to review by hand), this module needs NO model
at all: a real chip read (``events.rfid_is_real = 1``) is already more
reliable than any fresh inference, so it's used directly as ground truth.
For every motion block with a confirmed real RFID read, every photo's
already-stored dominant cat-or-prey detection is compared against the
chip's own cat - a disagreement writes a CONFIRMED ``detection_feedback``
correction immediately (same pipeline Sid's manual corrections already use,
see DetectionFeedbackRepo), no review queue, no inference, no waiting.

Same safety principle as the existing RFID auto-resolve feature in
webui_api.py (08.10, Sid: "il pourrait y avoir une proie non détectée qui
passerait entre les gouttes"): never touches a photo whose current dominant
detection is "prey" - forcing a prey-looking box to be "actually the cat"
would write bad training data. Left for manual review like today.
"""
import logging
import time

from src.baseconfig import CONFIG
from src.database import (
    CatsRepo,
    DatabaseCore,
    DetectionFeedbackRepo,
    EventsRepo,
    RfidGroundTruthRepo,
)
from src.model.retroactive_review import _effective_dominant_cat


def _next_unprocessed_rfid_blocks(database: str, limit: int) -> list[int]:
    """Most recent motion blocks with a CONFIRMED real RFID read first,
    skipping any block already processed by this feature. Mirrors
    retroactive_review._next_unscanned_block_ids's own ordering/skip logic."""
    if not DatabaseCore.check_if_column_exists(database, "events", "rfid_is_real"):
        return []
    already = RfidGroundTruthRepo.get_processed_block_ids(database)
    df = DatabaseCore.read_df_from_database(
        database,
        "SELECT block_id FROM events WHERE block_id IS NOT NULL AND deleted != 1 "
        "AND rfid_is_real = 1 AND rfid != '' "
        "GROUP BY block_id ORDER BY MAX(id) DESC",
    )
    if df.empty:
        return []
    out = []
    for b in df["block_id"].tolist():
        b = int(b)
        if b in already:
            continue
        out.append(b)
        if len(out) >= limit:
            break
    return out


def run_rfid_ground_truth_batch(num_blocks: int = 200, cancel_event=None, dry_run: bool = False) -> dict:
    """Blocking - call via asyncio.to_thread, same pattern as the other
    AI-training batch jobs. No model loaded, no inference, no CPU-core
    isolation needed - a few thousand plain-text comparisons is trivial.
    ``dry_run`` computes the exact same plan without writing anything or
    marking blocks processed, for an honest preview (never an estimate)."""
    database = CONFIG["KITTYHACK_DATABASE_PATH"]
    cat_name_dict = CatsRepo.get_cat_name_rfid_dict(database)  # rfid -> display name
    cat_names_lc = sorted(set(n.strip().lower() for n in cat_name_dict.values() if n))

    block_ids = _next_unprocessed_rfid_blocks(database, num_blocks)
    if not block_ids:
        return {
            "blocks": 0, "photos_checked": 0, "corrected": 0, "already_correct": 0,
            "skipped_prey": 0, "skipped_no_detection": 0, "elapsed_s": 0.0,
            "cancelled": False, "dry_run": dry_run, "all_done": True,
        }

    blocks_processed = 0
    photos_checked = 0
    corrected = 0
    already_correct = 0
    skipped_prey = 0
    skipped_no_detection = 0
    cancelled = False
    t0 = time.monotonic()

    for block_id in block_ids:
        if cancel_event is not None and cancel_event.is_set():
            cancelled = True
            break

        df = DatabaseCore.read_df_from_database(
            database,
            f"SELECT id, event_text, rfid FROM events WHERE block_id = {int(block_id)} AND deleted != 1",
        )
        if df.empty:
            if not dry_run:
                RfidGroundTruthRepo.mark_blocks_processed(database, [block_id])
            blocks_processed += 1
            continue

        # Every photo in a block shares the same tag_id/rfid (set once per
        # motion block, not per photo - confirmed elsewhere in the codebase,
        # see EventsRepo's own "every element of the block has the same
        # tag_id" comment).
        block_rfid = df.iloc[0].get("rfid")
        rfid_cat = cat_name_dict.get(block_rfid)
        if not rfid_cat:
            # Selected because rfid_is_real=1, but the tag has since been
            # removed/renamed in the cats table - nothing safe to do here.
            if not dry_run:
                RfidGroundTruthRepo.mark_blocks_processed(database, [block_id])
            blocks_processed += 1
            continue
        rfid_cat_lc = rfid_cat.strip().lower()

        for __, row in df.iterrows():
            photo_id = int(row["id"])
            photos_checked += 1
            event_text = row.get("event_text")
            if not event_text:
                continue
            old_objects = EventsRepo.read_event_from_json(event_text)
            old_idx, old_name, old_prob = _effective_dominant_cat(photo_id, old_objects, cat_names_lc, database)

            if old_name == rfid_cat_lc:
                already_correct += 1
                continue
            if old_name == "prey":
                skipped_prey += 1
                continue
            if old_idx is None:
                skipped_no_detection += 1
                continue

            # Box for the corrected index - from the original detection when
            # it's within range, else from an existing feedback row (an
            # index added beyond the original list via a past manual
            # "Confirm new"/"Other", same fallback retroactive_review uses).
            if old_idx < len(old_objects):
                obj = old_objects[old_idx]
                nx, ny, nw, nh = obj.x, obj.y, obj.width, obj.height
            else:
                fb_row = DetectionFeedbackRepo.get_for_photo(database, photo_id).get(old_idx)
                nx, ny, nw, nh = (
                    (fb_row["x"], fb_row["y"], fb_row["width"], fb_row["height"]) if fb_row else (0, 0, 0, 0)
                )

            if dry_run:
                corrected += 1
                continue

            result = DetectionFeedbackRepo.upsert(
                database,
                photo_id=photo_id,
                object_index=old_idx,
                original_name=old_name,
                original_probability=old_prob,
                x=nx, y=ny, width=nw, height=nh,
                corrected_name=rfid_cat,
                confirmed=True,
            )
            if result.success:
                corrected += 1
            else:
                logging.warning(
                    f"[RFID_GROUND_TRUTH] Failed to write correction for photo {photo_id}: {result.message}"
                )

        if not dry_run:
            RfidGroundTruthRepo.mark_blocks_processed(database, [block_id])
        blocks_processed += 1

    elapsed = time.monotonic() - t0
    logging.info(
        f"[RFID_GROUND_TRUTH] {'Preview' if dry_run else 'Run'} "
        f"{'cancelled' if cancelled else 'done'}: {blocks_processed} block(s), {photos_checked} photo(s) "
        f"checked, {corrected} correction(s), {already_correct} already correct, "
        f"{skipped_prey} skipped (prey), {skipped_no_detection} skipped (no detection), {elapsed:.1f}s."
    )
    return {
        "blocks": blocks_processed,
        "photos_checked": photos_checked,
        "corrected": corrected,
        "already_correct": already_correct,
        "skipped_prey": skipped_prey,
        "skipped_no_detection": skipped_no_detection,
        "elapsed_s": elapsed,
        "cancelled": cancelled,
        "dry_run": dry_run,
    }
