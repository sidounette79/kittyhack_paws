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
running from a different thread. As of 09.10 (real incident the day
before: a scan starved the live loop enough that cats got stuck outside),
this standalone instance also runs pinned to its own CPU core(s) via a
worker process - see _REVIEW_NUM_THREADS - genuinely isolated from the
live loop's cores, not just "its own model copy" sharing the same ones.
That CPU pinning only takes effect when this module's own inference runs
on CPU, though: with the 19.10 GPU switch (INFERENCE_DEVICE=intel:gpu),
this module's handler targets the GPU too, same as the live loop, and
model_handler.py's "GPU devices always use the direct inference path"
bypasses the worker-process/CPU-pinning path entirely for any non-cpu
device - see _wait_for_live_loop_idle() below for how this module now
yields the shared GPU to the live loop instead.
"""
import logging
import time

import numpy as np
import cv2

from src.baseconfig import CONFIG
from src.backend import model_runtime
from src.model.model_handler import ModelHandler
from src.model.yolo_model import YoloModel
from src.database import DatabaseCore, EventsRepo, ReturnDataPhotosDB, ModelReviewRepo, DetectionFeedbackRepo

# 19.10, Sid ("prioriser toujours la détection plutôt que l'analyse"): since
# the GPU switch, review's own ModelHandler also targets intel:gpu (see
# model_handler.py - "GPU devices always use the direct inference path",
# which bypasses the 09.10 CPU-core pinning entirely for any non-cpu
# device). Rather than force review back onto CPU and lose the GPU speed,
# review instead watches the LIVE handler's own effective_fps
# (model_runtime.model_handler.get_effective_fps_snapshot()) and pauses
# itself whenever that crosses _LIVE_BUSY_FPS_THRESHOLD - idle trickle is
# ~0.3-0.5fps, a real passage runs the live loop at ~8fps, so this cleanly
# tells a genuine passage apart from idle background inference. Checked
# between every photo in the review loop, same cadence as cancel_event.
_LIVE_BUSY_FPS_THRESHOLD = 2.0
_LIVE_BUSY_POLL_INTERVAL_S = 0.2


def _wait_for_live_loop_idle(cancel_event) -> bool:
    """Block while the live detection loop is actively busy (a real passage
    in progress), so the review scan always yields the shared GPU to it.
    Returns False if cancelled while waiting, True once safe to proceed
    (including when the live FPS can't be read at all - never block the
    scan indefinitely over a missing/broken status read)."""
    while True:
        try:
            eff_fps, __ = model_runtime.model_handler.get_effective_fps_snapshot()
        except Exception:
            return True
        if not eff_fps or eff_fps < _LIVE_BUSY_FPS_THRESHOLD:
            return True
        if cancel_event is not None and cancel_event.is_set():
            return False
        time.sleep(_LIVE_BUSY_POLL_INTERVAL_S)


def _decode_image(image_bytes: bytes):
    if not image_bytes:
        return None
    arr = np.frombuffer(image_bytes, dtype=np.uint8)
    frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    return frame


_PREY_LABELS = ("prey", "beute")

# 09.10, Sid (real incident the day before: "le scan a complètement étouffé
# le NAS... mes chats sont restés coincés dehors"): ModelHandler only uses
# its CPU-pinned worker-process mode (see model_handler.py's load_model) -
# the one that can actually be kept off the live detection loop's cores via
# cpu_affinity - when num_threads is below the container's total core
# count. The 08.10 fix reverted to num_threads=cpu_count() instead (forcing
# the fast synchronous "direct_inference" path) specifically because this
# module called handler._yolo() expecting that path's plain 3-tuple return,
# and the worker-process path returns a bare job_id instead - crashed on
# every single photo back then. Fixed properly this time: _run_inference()
# below speaks both calling conventions, so a real low num_threads (genuine
# OS-level core isolation, not just a sleep between calls) finally works
# without breaking anything. 1 of this container's 4 cores, leaving 3 for
# live detection + the web server - conservative on purpose, can be raised
# later once proven safe in practice.
_REVIEW_NUM_THREADS = 1
_REVIEW_WORKER_RESULT_TIMEOUT_S = 30.0


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


def _run_inference(handler: ModelHandler, frame):
    """Run one inference through whichever protocol this handler actually
    uses - ModelHandler._yolo means two different things depending on
    num_threads (see module-level comment above): a plain synchronous call
    returning a 3-tuple directly ("direct_inference" path), or a bare
    job_id that must be separately polled via _get_result() (worker-process
    path, the one that actually honors CPU-core isolation). Returns the
    same (mouse_probability, own_cat_probability, detected_objects) 3-tuple
    either way, or raises on failure/timeout."""
    if handler._model_worker is not None:
        job_id = handler._send_to_worker(frame, handler.input_size)
        result = handler._get_result(job_id, timeout=_REVIEW_WORKER_RESULT_TIMEOUT_S)
        if result is None:
            raise TimeoutError(f"worker result timed out after {_REVIEW_WORKER_RESULT_TIMEOUT_S}s")
        return result
    return handler._yolo(frame, handler.input_size)


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


def run_retroactive_review_batch(num_events: int = 50, cancel_event=None) -> dict:
    """Blocking - call via asyncio.to_thread from the UI, same pattern as
    the corrections export. Returns a summary dict.

    08.10, Sid ("stoppe le scan de révision, les chats ne peuvent pas
    rentrer"): this batch job loads its own model copy (see module docstring)
    and runs CPU-bound inference synchronously, one photo at a time, fast
    enough to starve the live detection loop's own inference for its whole
    duration when a large ``num_events`` is requested - there was previously
    no way to stop it short of restarting the whole container. ``cancel_event``
    (a threading.Event, optional) is checked between blocks AND between
    individual photos within a block, so a cancel request lands within a
    photo or two rather than waiting out the current block."""
    database = CONFIG["KITTYHACK_DATABASE_PATH"]

    model_version = CONFIG.get("YOLO_MODEL")
    if not model_version:
        return {"error": "no_active_model"}
    model_path = YoloModel.get_model_path(model_version)
    model_image_size = YoloModel.get_model_image_size(model_version) or 320
    if not model_path:
        return {"error": "model_not_found"}

    # See _REVIEW_NUM_THREADS's comment above: a real, CPU-pinned worker
    # process, isolated from the live detection loop's cores - not the
    # cpu_count()-wide "direct" path this used to be forced into.
    logging.info(
        f"[MODEL_REVIEW] Loading a standalone copy of the active model ({model_version}) for review "
        f"({_REVIEW_NUM_THREADS} thread(s), leaving the rest for the live detection loop)..."
    )
    handler = ModelHandler(
        model="yolo",
        modeldir=model_path,
        labelfile="labels.txt",
        model_image_size=model_image_size,
        num_threads=_REVIEW_NUM_THREADS,
    )
    try:
        handler.load_model()
    except Exception as e:
        logging.error(f"[MODEL_REVIEW] Failed to load model for review: {e}")
        return {"error": "model_load_failed"}

    cat_names = handler.cat_names  # already lowercased, see ModelHandler.__init__

    block_ids = _next_unscanned_block_ids(database, model_version, num_events)
    if not block_ids:
        # 09.10: ModelHandler.__del__ would eventually stop the worker
        # process anyway (whenever the garbage collector gets to it), but
        # not reliably "now" - explicit shutdown here too so an empty scan
        # doesn't leave a CPU-pinned process hanging around regardless.
        if handler._model_worker is not None:
            handler._input_queue.put(None)
            handler._model_worker.join(timeout=2)
        return {"scanned": 0, "flagged": 0, "events": 0, "model_version": model_version, "all_scanned": True}

    scanned = 0
    flagged = 0
    cancelled = False
    t0 = time.monotonic()

    try:
        for block_id in block_ids:
            if cancel_event is not None and cancel_event.is_set():
                cancelled = True
                break
            df = EventsRepo.db_get_photos_by_block_id(
                database, block_id, ReturnDataPhotosDB.all_original_image
            )
            if df.empty:
                ModelReviewRepo.mark_blocks_scanned(database, [block_id], model_version)
                continue
            block_cancelled = False
            for __, row in df.iterrows():
                if cancel_event is not None and cancel_event.is_set():
                    block_cancelled = True
                    break
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

                if not _wait_for_live_loop_idle(cancel_event):
                    block_cancelled = True
                    break

                try:
                    __, __, new_detected = _run_inference(handler, frame)
                except Exception as e:
                    logging.warning(f"[MODEL_REVIEW] Inference failed for photo {row['id']}: {e}")
                    continue
                finally:
                    # 08.10, Sid ("les chats ne peuvent pas rentrer"): a short
                    # yield after every inference call gives the live detection
                    # loop's own thread real scheduling opportunities between
                    # bursts, rather than this one running flat-out for the
                    # scan's whole duration with zero gaps. Kept even with real
                    # CPU-core isolation now (09.10) - cheap insurance, and the
                    # worker process itself still shares the host's I/O/memory
                    # bus with the live loop even when pinned to its own core(s).
                    time.sleep(0.3)
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

            if block_cancelled:
                # Leave this block unmarked - a partially-scanned block must be
                # retried in full next time, not skipped as "already done".
                cancelled = True
                break

            # Mark the whole block scanned once its photos are done, regardless
            # of whether any disagreement was found - that's what makes the
            # NEXT pilot run skip it and move to older events.
            ModelReviewRepo.mark_blocks_scanned(database, [block_id], model_version)
    finally:
        # 09.10: don't wait on Python's garbage collector to free up this
        # scan's pinned core(s) - stop the worker process the moment the
        # scan itself is done (or cancelled, or crashes), not "eventually".
        if handler._model_worker is not None:
            handler._input_queue.put(None)
            handler._model_worker.join(timeout=2)

    elapsed = time.monotonic() - t0
    logging.info(
        f"[MODEL_REVIEW] Pilot {'cancelled' if cancelled else 'done'}: {scanned} photos scanned across "
        f"{len(block_ids)} events, {flagged} disagreement(s) flagged, {elapsed:.1f}s."
    )
    return {
        "scanned": scanned,
        "flagged": flagged,
        "events": len(block_ids),
        "model_version": model_version,
        "elapsed_s": elapsed,
        "cancelled": cancelled,
    }
