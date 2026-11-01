"""On-disk cat-identity (Modèle 2, embeddings) metadata and delete helper.

Mirrors `yolo_model.py`'s `YoloModel` (filesystem helpers for installed
models under `models/yolo`), scoped to `models/identity` instead. No
image-size/variant concept here - an identity model directory is expected
to contain `model.onnx` (embedding backbone), `labels.txt` (known cat
names, one per line - informational, the real "classes" are the gallery
files), `info.json` and a `galleries/<cat_name>.npy` per known cat. See
`identity.py` for the runtime loader/inference side.
"""
import json
import logging
import os
import shutil
from datetime import datetime
from typing import Any

from src.paths import models_identity_root
from src.helper import DateTimeUtil


class IdentityModel:
    """Filesystem helpers for installed identity models under `models/identity`."""
    BASE_DIR = models_identity_root()

    @staticmethod
    def get_model_list() -> list[dict[str, Any]]:
        """List installed identity models with display name, path and metadata."""
        model_list: list[dict[str, Any]] = []
        if not os.path.exists(IdentityModel.BASE_DIR):
            return model_list

        for dir_name in os.listdir(IdentityModel.BASE_DIR):
            model_path = os.path.join(IdentityModel.BASE_DIR, dir_name)
            if not os.path.isdir(model_path):
                continue

            model_name = dir_name
            unique_id = None
            creation_date = ""
            try:
                creation_date = datetime.fromtimestamp(
                    os.path.getctime(model_path), DateTimeUtil.get_timezone()
                ).strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                pass

            info_json_path = os.path.join(model_path, "info.json")
            if os.path.exists(info_json_path):
                try:
                    with open(info_json_path, "r", encoding="utf-8") as f:
                        info_data = json.load(f)
                    model_name = info_data.get("MODEL_NAME", dir_name)
                    unique_id = info_data.get("JOB_ID")
                    try:
                        timestamp = datetime.fromisoformat(info_data["TIMESTAMP_UTC"])
                        creation_date = timestamp.astimezone(DateTimeUtil.get_timezone()).strftime("%Y-%m-%d %H:%M:%S")
                    except (ValueError, TypeError, KeyError) as e:
                        logging.warning(f"[IDENTITY] Invalid/missing TIMESTAMP_UTC for {dir_name}: {e}")
                except Exception as e:
                    logging.error(f"[IDENTITY] Could not parse info.json for {dir_name}: {e}")

            model_list.append({
                "display_name": model_name,
                "directory": dir_name,
                "creation_date": creation_date,
                "unique_id": unique_id,
                "full_display_name": f"{model_name} ({creation_date})",
            })
        return model_list

    @staticmethod
    def get_model_path(unique_id: str) -> str | None:
        """Return the model directory path for `unique_id` (JOB_ID), or None."""
        for model in IdentityModel.get_model_list():
            if model.get("unique_id") == unique_id:
                return os.path.join(IdentityModel.BASE_DIR, model["directory"])
        return None

    @staticmethod
    def delete_model(unique_id: str) -> bool:
        """Delete the identity model directory for `unique_id`; return True on success."""
        for model in IdentityModel.get_model_list():
            if model.get("unique_id") == unique_id:
                model_path = os.path.join(IdentityModel.BASE_DIR, model["directory"])
                try:
                    shutil.rmtree(model_path)
                    logging.info(f"[IDENTITY] Deleted model directory: {model_path}")
                    return True
                except Exception as e:
                    logging.error(f"[IDENTITY] Error deleting model directory: {e}")
                    return False
        return False
