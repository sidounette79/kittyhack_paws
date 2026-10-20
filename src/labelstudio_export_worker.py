"""13.10, Sid (real 502 confirmed live - "Starting YOLO export..." then
nothing, request cut by the reverse proxy before the one-by-one download
of 6584 images could finish): the Label Studio export + submit-for-
training flow used to run synchronously inside a single HTTP request,
which this project's current size (growing, not shrinking) will keep
outrunning no matter how generous a timeout gets bumped to. Same pattern
as model_download_worker.py - a standalone background process, state
polled from disk - covering both halves of the job (export zip, then
submit it) so the caller only has to start this once and poll, instead of
a two-step dance across two different long operations.
"""
import argparse
import json
import logging
import os
import sys
import time
from typing import Any


def _atomic_write_json(path: str, data: dict[str, Any]) -> None:
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp_path, path)


def _merge_write_json(path: str, updates: dict[str, Any]) -> dict[str, Any]:
    current = _read_json(path)
    if not isinstance(current, dict):
        current = {}
    current.update(updates)
    _atomic_write_json(path, current)
    return current


def _read_json(path: str) -> dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception:
        return {}


def run_export_and_submit(
    *,
    project_id: int,
    host: str,
    port: int,
    ls_token: str | None,
    state_path: str,
    model_name: str,
    user_name: str,
    email: str,
    yolo_model_variant: str,
    image_size: int,
) -> None:
    from src.helper import is_valid_uuid4
    from src.labelstudio_api import export_labelstudio_project_as_zip
    from src.model.remote_trainer import RemoteModelTrainer

    started_at = time.time()
    state: dict[str, Any] = {
        "status": "exporting",
        "written": 0,
        "total": 0,
        "started_at": started_at,
        "finished_at": 0.0,
        "error": "",
        "job_id": "",
        "pid": os.getpid(),
    }
    _merge_write_json(state_path, state)
    logging.info("[LS_EXPORT_WORKER] Start export for project_id=%s", project_id)

    zip_path = os.path.join("/tmp", f"kittyhack_labelstudio_export_{project_id}_{int(started_at)}.zip")
    try:
        if os.path.exists(zip_path):
            os.remove(zip_path)
    except Exception:
        pass

    # Throttled so 6584+ tasks don't mean 6584+ disk writes - the UI only
    # needs to see progress move, not every single increment.
    last_write = [0.0]

    def on_progress(done: int, total: int) -> None:
        now = time.time()
        if now - last_write[0] < 0.5 and done != total - 1:
            return
        last_write[0] = now
        state["written"] = done
        state["total"] = total
        _merge_write_json(state_path, state)

    try:
        ok = export_labelstudio_project_as_zip(
            project_id,
            zip_path,
            host=host,
            port=port,
            token=ls_token,
            on_progress=on_progress,
        )
        if not ok or not os.path.exists(zip_path):
            state["status"] = "error"
            state["error"] = (
                "Échec de l'export du projet Label Studio - vérifie que le projet contient "
                "des tâches annotées avec des images accessibles."
            )
            state["finished_at"] = time.time()
            _merge_write_json(state_path, state)
            logging.error("[LS_EXPORT_WORKER] Export failed")
            return

        state["status"] = "submitting"
        _merge_write_json(state_path, state)
        logging.info("[LS_EXPORT_WORKER] Export done (%s bytes), submitting for training", os.path.getsize(zip_path))

        result = RemoteModelTrainer.enqueue_model_training(
            zip_path,
            model_name,
            user_name,
            email,
            yolo_model_variant=yolo_model_variant,
            image_size=image_size,
        )

        if is_valid_uuid4(result):
            state["status"] = "done"
            state["job_id"] = result
            state["finished_at"] = time.time()
            _merge_write_json(state_path, state)
            logging.info("[LS_EXPORT_WORKER] Submitted, job_id=%s", result)
            return

        # Same error_messages mapping as webui_api.py's own submit endpoint
        # (duplicated rather than imported - that file pulls in the full
        # FastAPI app, not worth importing into a standalone worker process
        # just for four strings).
        error_messages = {
            "invalid_file": "Le fichier n'est pas un ZIP de données d'entraînement valide.",
            "destination_unreachable": "Le serveur d'entraînement est injoignable. Réessaie plus tard.",
            "destination_not_found": "Le serveur d'entraînement est injoignable. Réessaie plus tard.",
            "file_too_large": "Le fichier est trop volumineux.",
        }
        state["status"] = "error"
        state["error"] = error_messages.get(result, f"Erreur : {result}")
        state["finished_at"] = time.time()
        _merge_write_json(state_path, state)
        logging.error("[LS_EXPORT_WORKER] Submit failed: %s", result)

    except Exception as e:
        state["status"] = "error"
        state["error"] = str(e)
        state["finished_at"] = time.time()
        _merge_write_json(state_path, state)
        logging.exception("[LS_EXPORT_WORKER] Failed")
    finally:
        try:
            if os.path.exists(zip_path):
                os.remove(zip_path)
        except Exception:
            pass


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-id", required=True, type=int)
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--ls-token", default="")
    parser.add_argument("--state-path", required=True)
    parser.add_argument("--model-name", default="")
    parser.add_argument("--user-name", default="")
    parser.add_argument("--email", default="")
    parser.add_argument("--yolo-model-variant", default="n")
    parser.add_argument("--image-size", required=True, type=int)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO)

    try:
        run_export_and_submit(
            project_id=args.project_id,
            host=args.host,
            port=args.port,
            ls_token=args.ls_token or None,
            state_path=args.state_path,
            model_name=args.model_name,
            user_name=args.user_name,
            email=args.email,
            yolo_model_variant=args.yolo_model_variant,
            image_size=args.image_size,
        )
        return 0
    except Exception:
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
