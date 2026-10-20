"""
Import ponctuel : récupère toutes les tâches + annotations déjà présentes
dans LabelStudio (la vérité-terrain actuellement utilisée pour entraîner le
modèle, ~4800 captures) et les insère dans le catalogue de Paws Review pour
que Sid puisse les trier (garder/corriger/écarter) avant que le futur
conteneur d'export les reprenne pour un nouveau zip d'entraînement.

Lecture seule côté LabelStudio - rien n'y est jamais modifié ni supprimé.

18.10, Sid : "me faire parvenir, dans paws review, les 4800 et quelques
photos qui sont dans labelstudio... je trie, et après tu attaques le
nouveau container qui récupérera les photos triées."

Usage (depuis l'intérieur du conteneur paws-review-ingest, token PAT
jamais passé en argument ni en variable d'environnement - saisie
interactive uniquement, pour ne pas le laisser traîner dans l'historique
shell ou `docker inspect`) :
    docker exec -it paws-review-ingest python -m app.import_labelstudio
"""

import base64
import getpass
import io
import json
import logging
import os
import sys
from datetime import datetime

import requests
from PIL import Image

from app.cat_labels import recompute_cat_labels
from app.catalog_db import get_conn, init_db
from app.config import ORIGINALS_DIR, THUMBNAILS_DIR

logging.basicConfig(level=logging.INFO, format="%(asctime)s [import_ls] %(message)s")
log = logging.getLogger(__name__)

LS_HOST = os.environ.get("LABELSTUDIO_HOST", "192.168.178.127")
LS_PORT = os.environ.get("LABELSTUDIO_PORT", "8081")
BASE_URL = f"http://{LS_HOST}:{LS_PORT}"

THUMB_SIZE = (320, 240)


def get_access_token(refresh_token: str) -> str:
    resp = requests.post(f"{BASE_URL}/api/token/refresh", json={"refresh": refresh_token}, timeout=10)
    resp.raise_for_status()
    access = (resp.json() or {}).get("access")
    if not access:
        raise RuntimeError("Pas d'access token dans la réponse - PAT invalide ou expiré ?")
    return access


def list_projects(access_token: str) -> list[dict]:
    resp = requests.get(
        f"{BASE_URL}/api/projects/", headers={"Authorization": f"Bearer {access_token}"}, timeout=15
    )
    resp.raise_for_status()
    body = resp.json()
    return body.get("results", body) if isinstance(body, dict) else body


def get_all_tasks(access_token: str, project_id: int) -> list[dict]:
    """Même pagination que labelstudio_api.py côté Kittyhack
    (_get_all_tasks_with_annotations) - fields=all pour avoir les boîtes,
    pas juste le compte."""
    tasks: list[dict] = []
    page, page_size, total = 1, 100, None
    headers = {"Authorization": f"Bearer {access_token}"}
    while True:
        resp = requests.get(
            f"{BASE_URL}/api/tasks",
            headers=headers,
            params={"project": project_id, "page": page, "page_size": page_size, "fields": "all"},
            timeout=60,
        )
        if resp.status_code != 200:
            log.warning("Tasks API a renvoyé HTTP %s à la page %s", resp.status_code, page)
            break
        body = resp.json() if resp.text else {}
        chunk = body if isinstance(body, list) else body.get("tasks", body.get("results", []))
        if not chunk:
            break
        tasks.extend(chunk)
        if len(tasks) % 500 < page_size:
            log.info("... %d tâches récupérées", len(tasks))
        if isinstance(body, dict):
            if total is None:
                total = body.get("total")
            if total is not None and len(tasks) >= total:
                break
            page += 1
        else:
            break
    return tasks


def resolve_image_url(task: dict) -> str | None:
    """Même logique que labelstudio_api.py (_resolve_image_url) - certaines
    tâches stockent leur image en data URI base64 plutôt qu'en fichier
    uploadé (confirmé côté Kittyhack le 10.10 : 4223 tâches dans ce cas)."""
    data = task.get("data") or {}
    val = ""
    for key in ("image", "img", "url", "photo", "picture"):
        val = data.get(key, "")
        if val:
            break
    if not val:
        for v in data.values():
            if isinstance(v, str) and ("/" in v or v.lower().endswith((".jpg", ".jpeg", ".png", ".bmp", ".webp"))):
                val = v
                break
    if not val:
        return None
    if val.startswith("/"):
        return f"{BASE_URL}{val}"
    if val.startswith(("http://", "https://", "data:")):
        return val
    return None


def download_image(url: str, access_token: str) -> bytes | None:
    if url.startswith("data:"):
        try:
            return base64.b64decode(url.split(",", 1)[1])
        except Exception as e:
            log.debug("Data URI illisible: %s", e)
            return None
    try:
        resp = requests.get(url, headers={"Authorization": f"Bearer {access_token}"}, timeout=30)
        if resp.status_code == 200 and resp.content:
            return resp.content
    except Exception as e:
        log.debug("Téléchargement échoué pour %s: %s", url, e)
    return None


def boxes_from_task(task: dict) -> list[dict]:
    """Toutes les boîtes des annotations non annulées, au format attendu
    par captures.detected_objects_json (voir main.py get_capture - probability
    DOIT être une clé présente, même à null, sinon KeyError à l'affichage).

    10.10, Sid (via Kittyhack) : ne JAMAIS filtrer sur ground_truth - Label
    Studio le met à true par défaut sur un /import en masse, ce qui avait
    fait disparaître 4223 corrections réelles la première fois que ça a été
    tenté. was_cancelled reste un vrai motif d'exclusion (annotation
    explicitement annulée)."""
    boxes = []
    for ann in task.get("annotations") or []:
        if ann.get("was_cancelled"):
            continue
        for r in ann.get("result") or []:
            val = r.get("value") or {}
            labs = val.get("rectanglelabels") or []
            if not labs:
                continue
            x, y, w, h = val.get("x"), val.get("y"), val.get("width"), val.get("height")
            if x is None or y is None or w is None or h is None:
                continue
            boxes.append(
                {"object_name": labs[0], "probability": None, "x": x, "y": y, "width": w, "height": h}
            )
    return boxes


def normalize_created_at(raw) -> str:
    """Les tâches LS utilisent l'ISO avec 'T' (et parfois 'Z') - reconverti
    au format 'espace' utilisé partout ailleurs dans cette base. Sans ça,
    les comparaisons de chaînes de ingest.py (dedup) et retention.py
    (purge) échouent silencieusement face à ce format, piège déjà rencontré
    deux fois ce soir sur ce même projet."""
    if raw:
        try:
            dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            return dt.strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            pass
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")


def make_thumbnail(image_bytes: bytes) -> bytes:
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    img.thumbnail(THUMB_SIZE)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return buf.getvalue()


def main():
    os.makedirs(ORIGINALS_DIR, exist_ok=True)
    os.makedirs(THUMBNAILS_DIR, exist_ok=True)
    init_db()

    pat = getpass.getpass("Personal Access Token LabelStudio (jamais affiché ni loggé) : ").strip()
    if not pat:
        log.error("Token vide, abandon.")
        sys.exit(1)
    access = get_access_token(pat)
    log.info("Authentifié.")

    projects = list_projects(access)
    print("\nProjets LabelStudio :")
    for p in projects:
        print(f"  {p.get('id')}  ·  {p.get('title')}  ·  {p.get('task_number')} tâches")
    project_id = int(input("\nID du projet à importer : ").strip())

    log.info("Récupération des tâches du projet %d...", project_id)
    tasks = get_all_tasks(access, project_id)
    log.info("Total : %d tâches à traiter", len(tasks))

    imported = skipped_existing = skipped_no_boxes = skipped_no_image = 0
    with get_conn() as conn:
        known_names = {
            r["cat_name"].lower(): r["cat_name"]
            for r in conn.execute(
                "SELECT DISTINCT cat_name FROM captures WHERE cat_name IS NOT NULL"
            ).fetchall()
        }
        for i, task in enumerate(tasks):
            if i and i % 200 == 0:
                log.info("... %d/%d traitées (%d importées)", i, len(tasks), imported)

            exists = conn.execute(
                "SELECT id FROM captures WHERE source = 'labelstudio' AND source_event_id = ?",
                (task["id"],),
            ).fetchone()
            if exists:
                skipped_existing += 1
                continue

            boxes = boxes_from_task(task)
            if not boxes:
                skipped_no_boxes += 1
                continue

            img_url = resolve_image_url(task)
            img_bytes = download_image(img_url, access) if img_url else None
            if not img_bytes:
                skipped_no_image += 1
                continue

            dst_original = os.path.join(ORIGINALS_DIR, f"labelstudio_{task['id']}.jpg")
            dst_thumb = os.path.join(THUMBNAILS_DIR, f"labelstudio_{task['id']}.jpg")
            try:
                thumb_bytes = make_thumbnail(img_bytes)
            except Exception as e:
                log.warning("Image illisible pour la tâche %s: %s", task["id"], e)
                skipped_no_image += 1
                continue
            with open(dst_original, "wb") as f:
                f.write(img_bytes)
            with open(dst_thumb, "wb") as f:
                f.write(thumb_bytes)

            created_at = normalize_created_at(task.get("created_at"))

            cursor = conn.execute(
                """INSERT INTO captures (
                       source, source_event_id, created_at,
                       detected_objects_json, original_path, thumbnail_path,
                       review_status
                   ) VALUES ('labelstudio', ?, ?, ?, ?, ?, 'pending')""",
                (task["id"], created_at, json.dumps(boxes), dst_original, dst_thumb),
            )
            recompute_cat_labels(conn, cursor.lastrowid, known_names)
            imported += 1

    log.info(
        "Terminé : %d importées, %d déjà présentes, %d sans boîte exploitable, %d image indisponible",
        imported, skipped_existing, skipped_no_boxes, skipped_no_image,
    )


if __name__ == "__main__":
    main()
