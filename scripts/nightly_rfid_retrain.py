#!/usr/bin/env python3
"""Nightly pipeline (09.10, Sid: "on peut en faire un script qui tourne
toutes les nuits, envoie à labelstudio et genere un entrainement? pendant
quelques temps en tout cas"):

  1. RFID ground-truth reassignment (src.model.rfid_ground_truth) - no
     model, no inference, just confirmed chip reads correcting whatever the
     model got wrong, block by block.
  2. Push every pending confirmed correction to Label Studio (same pipeline
     the "Envoyer tout en arrière-plan" button already uses).
  3. If enough NEW confirmed corrections have piled up since the last time
     this script submitted a training run (Sid's own call: "seuil minimum",
     not a training every single night regardless), submit one to the
     remote training server.

Auto-activation of a finished training's resulting model is NOT built here
- it already exists (RemoteModelTrainer.check_model_training_result, 05.10)
and only needs to actually be CALLED to fire; this script calls it itself
at the top of every run so a finished job gets picked up even on a night
nobody opens the AI Training tab.

Deliberately a standalone cron script, not a background asyncio task inside
the web server - trivial to disable (remove the cron line) once Sid's done
experimenting with it ("pendant quelques temps en tout cas"), and runs fine
independently of the server container's own restarts.

Run via: docker exec kittyhack-remote python3 /app/scripts/nightly_rfid_retrain.py
Scheduled from OLDMEDIA's host crontab (DSM) - there is no cron daemon
inside this container image, so it cannot schedule itself.
"""
import logging
import sqlite3
import sys
import time

sys.path.insert(0, "/app")

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [NIGHTLY_RFID] %(message)s", stream=sys.stdout
)

# 09.10, Sid: minimum number of newly CONFIRMED corrections, accumulated
# since this script's own last training submission, before bothering the
# remote training server - matches the existing manual "batch of 50"
# convention elsewhere in the app (AiTraining.tsx's "Envoyer un lot (50)
# manuellement"). Avoids submitting a near-empty training run on a quiet
# night just because the cron fired.
MIN_NEW_CORRECTIONS_FOR_TRAINING = 50


def _ensure_state_table(database: str) -> None:
    conn = sqlite3.connect(database, timeout=30)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS nightly_rfid_retrain_state "
        "(id INTEGER PRIMARY KEY CHECK (id = 1), last_training_submitted_at TEXT)"
    )
    conn.commit()
    conn.close()


def _get_last_training_submitted_at(database: str) -> str | None:
    _ensure_state_table(database)
    conn = sqlite3.connect(database, timeout=30)
    row = conn.execute(
        "SELECT last_training_submitted_at FROM nightly_rfid_retrain_state WHERE id = 1"
    ).fetchone()
    conn.close()
    return row[0] if row and row[0] else None


def _set_last_training_submitted_at(database: str, ts: str) -> None:
    _ensure_state_table(database)
    conn = sqlite3.connect(database, timeout=30)
    conn.execute(
        "INSERT INTO nightly_rfid_retrain_state (id, last_training_submitted_at) VALUES (1, ?) "
        "ON CONFLICT(id) DO UPDATE SET last_training_submitted_at = excluded.last_training_submitted_at",
        (ts,),
    )
    conn.commit()
    conn.close()


def main() -> None:
    from src.baseconfig import CONFIG
    from src.database import DatabaseCore, DetectionFeedbackRepo
    from src.helper import is_valid_uuid4
    from src.model.remote_trainer import RemoteModelTrainer
    from src.model.rfid_ground_truth import run_rfid_ground_truth_batch
    from src.model.yolo_model import YoloModel
    from src.webui_api import _export_corrections_blocking

    database = CONFIG["KITTYHACK_DATABASE_PATH"]

    # Step 0: a previous run already in flight? Check on it FIRST - this is
    # what actually triggers the existing auto-download/activate logic,
    # which otherwise only fires when a human opens the AI Training tab or
    # the container restarts. Also avoids ever having two trainings queued
    # at once (the remote server itself may not support that cleanly).
    job_id = CONFIG.get("MODEL_TRAINING", "")
    if is_valid_uuid4(job_id):
        logging.info(f"A training job is already tracked ({job_id}) - checking its status instead of starting a new one.")
        RemoteModelTrainer.check_model_training_result(show_notification=True, show_in_progress=False)
        logging.info("Done for tonight (training still in flight or just resolved).")
        return

    # Step 1: RFID ground truth - confirmed chip reads reassign whole
    # motion blocks directly, see src.model.rfid_ground_truth's own
    # docstring for the full reasoning and safety rules.
    rgt_result = run_rfid_ground_truth_batch(num_blocks=100000, cancel_event=None, dry_run=False)
    logging.info(f"RFID ground truth: {rgt_result}")

    # Step 2: push every pending confirmed correction to Label Studio - not
    # just tonight's RFID ones, any manual correction made during the day
    # too. Loops in batches of 50 until nothing's left (or a batch makes no
    # progress at all, to avoid spinning forever on a real failure).
    sent_total = 0
    while True:
        sent, failed, remaining = _export_corrections_blocking(50)
        sent_total += sent
        logging.info(f"Export batch: sent={sent} failed={failed} remaining={remaining}")
        if remaining <= 0 or (sent == 0 and failed == 0):
            break
        time.sleep(1)

    # Step 3: only bother the remote training server once enough NEW
    # material has piled up since this script's own last submission -
    # counted directly in detection_feedback (not "sent_total" above, which
    # would under-count across multiple quiet nights: a correction already
    # exported on a below-threshold night would never be counted again).
    since = _get_last_training_submitted_at(database) or "2000-01-01 00:00:00"
    df = DatabaseCore.read_df_from_database(
        database,
        f"SELECT COUNT(*) AS c FROM detection_feedback WHERE confirmed = 1 AND created_at > '{since}'",
    )
    new_since_last_training = int(df.iloc[0]["c"]) if not df.empty else 0
    logging.info(f"{new_since_last_training} confirmed correction(s) since the last training submission ({since}).")

    if new_since_last_training < MIN_NEW_CORRECTIONS_FOR_TRAINING:
        logging.info(
            f"Below the threshold ({MIN_NEW_CORRECTIONS_FOR_TRAINING}) - skipping training run for tonight."
        )
        return

    saved_project = CONFIG.get("LABELSTUDIO_PROJECT", "").strip()
    if not saved_project:
        logging.error("No Label Studio project configured (LABELSTUDIO_PROJECT) - cannot submit training.")
        return

    import os
    import tempfile
    import time as _time

    from src.labelstudio_api import export_labelstudio_project_as_zip

    zip_file_path = os.path.join(tempfile.gettempdir(), f"nightly_rfid_retrain_{int(_time.time())}.zip")
    try:
        ok = export_labelstudio_project_as_zip(int(saved_project), zip_file_path)
        if not ok:
            logging.error("Label Studio project export failed - aborting training submission.")
            return

        # Match the currently active model's own image size (keeps
        # training comparable to what's live today); variant stays the
        # lightweight default ("n") the manual UI also defaults to.
        active_model = CONFIG.get("YOLO_MODEL")
        image_size = YoloModel.get_model_image_size(active_model) if active_model else 320
        model_name = f"auto-rfid-{_time.strftime('%Y%m%d-%H%M')}"

        result = RemoteModelTrainer.enqueue_model_training(
            zip_file_path,
            model_name,
            CONFIG.get("USER_NAME", ""),
            CONFIG.get("EMAIL", ""),
            yolo_model_variant="n",
            image_size=image_size or 320,
        )
    finally:
        if os.path.exists(zip_file_path):
            try:
                os.remove(zip_file_path)
            except Exception:
                pass

    if is_valid_uuid4(result):
        from src.baseconfig import update_single_config_parameter
        from src.helper import DateTimeUtil

        CONFIG["MODEL_TRAINING"] = result
        update_single_config_parameter("MODEL_TRAINING")
        _set_last_training_submitted_at(database, DateTimeUtil.get_utc_date_string(_time.time()))
        logging.info(f"Training submitted: job_id={result}, model_name={model_name}, {new_since_last_training} new correction(s).")
    else:
        logging.error(f"Training submission failed: {result}")


if __name__ == "__main__":
    main()
