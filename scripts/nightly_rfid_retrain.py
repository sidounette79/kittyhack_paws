#!/usr/bin/env python3
"""Nightly RFID ground-truth reassignment (09.10, Sid: "on peut en faire un
script qui tourne toutes les nuits...").

Originally a 3-step pipeline (RFID reassignment, push corrections to Label
Studio, submit a training run once enough material piled up). 24.10, Sid
("tu peux gentiment attaquer de supprimer labelstudio de kittyhack... on
ne l'utilise plus"): steps 2 and 3 were entirely built around Label Studio
(export_corrections_to_labelstudio / export_labelstudio_project_as_zip),
now gone - training happens through Paws Review's own rebalance/export
pipeline instead (see KITTYHACK_ROADMAP.md). Only step 1 remains: it was
always independent (no model, no inference, just confirmed chip reads
correcting whatever the model got wrong, block by block) and stays useful
on its own.

Deliberately a standalone cron script, not a background asyncio task inside
the web server - runs fine independently of the server container's own
restarts.

Run via: docker exec kittyhack-remote python3 /app/scripts/nightly_rfid_retrain.py
Scheduled from OLDMEDIA's host crontab (DSM) - there is no cron daemon
inside this container image, so it cannot schedule itself. 24.10: checked
the real DSM Task Scheduler (synoschedtask --get) - this script isn't
actually scheduled there right now, despite this note; nothing currently
invokes it.
"""
import logging
import sys

sys.path.insert(0, "/app")

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [NIGHTLY_RFID] %(message)s", stream=sys.stdout
)


def main() -> None:
    from src.model.rfid_ground_truth import run_rfid_ground_truth_batch

    rgt_result = run_rfid_ground_truth_batch(num_blocks=100000, cancel_event=None, dry_run=False)
    logging.info(f"RFID ground truth: {rgt_result}")


if __name__ == "__main__":
    main()
