"""
Ingestion depuis un dossier de dépôt manuel - Sid y copie elle-même des
photos (téléchargées depuis l'appli Reolink, un export, peu importe la
source) qui arrivent directement dans Paws Review pour être corrigées et
ajoutées à l'entraînement, sans passer par aucun pipeline automatique.

19.10, Sid : "il me faut avoir un dossier dans docker dans lequel je peux
ajouter manuellement des photos qui viendraient direct dans paws reviews...
et je peux déjà combler les trous avec les quelques photos que j'ai en
plus" - répond en particulier au trou de 13 min du 18.10 (l'épisode de la
souris) : les photos Reolink avec la souris visible existent dans son
NVR/appli, mais n'arrivaient nulle part automatiquement. Ce dossier est le
pont manuel.

Aucune boîte pré-remplie pour un vrai dépôt manuel (Sid les dessine à la
main) - sauf si un sidecar JSON du même nom de base accompagne l'image
(21.10 : le bouton "envoyer" de Parcours en dépose un quand chatière a
déjà fait tourner son propre modèle sur ce frame, voir journeys.py's
_detections_sidecar_for_paws_review / manual_drop_upload côté main.py).
Dossier déjà monté (STORAGE_ROOT/manual_drop, sur le même volume partagé
que originals/thumbnails/paws_review.db) - aucun nouveau montage docker
nécessaire, elle y dépose des fichiers directement via scp/un gestionnaire
de fichiers sur le NAS.
"""

import io
import json
import logging
import os
import zlib
from datetime import datetime

from PIL import Image

from app.cat_labels import recompute_cat_labels
from app.catalog_db import get_conn, init_db
from app.config import ORIGINALS_DIR, STORAGE_ROOT, THUMBNAILS_DIR

logging.basicConfig(level=logging.INFO, format="%(asctime)s [ingest_manual] %(message)s")
log = logging.getLogger(__name__)

MANUAL_DROP_DIR = os.path.join(STORAGE_ROOT, "manual_drop")
# 19.10 : déplacées ici une fois importées - confirme visuellement à Sid ce
# qui a déjà été repris (le dossier de dépôt se vide), sans dépendre
# uniquement d'une vérification en base pour éviter un doublon.
IMPORTED_SUBDIR = "_imported"

IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png")
THUMB_SIZE = (320, 240)


def _stable_event_id(filename: str) -> int:
    return zlib.crc32(filename.encode()) & 0x7FFFFFFF


def _make_thumbnail(image_bytes: bytes) -> bytes:
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    img.thumbnail(THUMB_SIZE)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return buf.getvalue()


def ingest_manual_batch() -> int:
    os.makedirs(ORIGINALS_DIR, exist_ok=True)
    os.makedirs(THUMBNAILS_DIR, exist_ok=True)
    os.makedirs(MANUAL_DROP_DIR, exist_ok=True)
    imported_dir = os.path.join(MANUAL_DROP_DIR, IMPORTED_SUBDIR)
    os.makedirs(imported_dir, exist_ok=True)
    init_db()

    filenames = sorted(
        f for f in os.listdir(MANUAL_DROP_DIR)
        if f.lower().endswith(IMAGE_EXTENSIONS) and os.path.isfile(os.path.join(MANUAL_DROP_DIR, f))
    )

    ingested = 0
    for filename in filenames:
        source_event_id = _stable_event_id(filename)
        src_path = os.path.join(MANUAL_DROP_DIR, filename)

        with get_conn() as conn:
            exists = conn.execute(
                "SELECT 1 FROM captures WHERE source = 'manual' AND source_event_id = ?",
                (source_event_id,),
            ).fetchone()
        if exists:
            # Déjà en base (ex: réessai après un déplacement qui avait
            # échoué) - on la pousse quand même vers _imported pour que le
            # dossier de dépôt reste cohérent avec ce qui est réellement en
            # attente.
            os.replace(src_path, os.path.join(imported_dir, filename))
            sidecar_leftover = src_path[:-4] + ".json" if src_path.lower().endswith((".jpg", ".jpeg")) else None
            if sidecar_leftover and os.path.isfile(sidecar_leftover):
                os.replace(sidecar_leftover, os.path.join(imported_dir, os.path.basename(sidecar_leftover)))
            continue

        try:
            with open(src_path, "rb") as f:
                img_bytes = f.read()
            thumb_bytes = _make_thumbnail(img_bytes)
        except Exception as e:
            log.warning("Image illisible pour %s: %s", filename, e)
            continue

        # 21.10: sidecar optionnel déposé par manual_drop_upload quand la
        # source avait déjà des boîtes (Parcours/chatière) - sinon '[]'
        # inchangé, Sid les dessine à la main comme avant.
        detected_objects_json = "[]"
        sidecar_path = src_path[:-4] + ".json" if src_path.lower().endswith((".jpg", ".jpeg")) else None
        if sidecar_path and os.path.isfile(sidecar_path):
            try:
                with open(sidecar_path, "r", encoding="utf-8") as f:
                    boxes = json.load(f)
                detected_objects_json = json.dumps(boxes)
            except Exception as e:
                log.warning("Sidecar de détections illisible pour %s: %s", filename, e)

        dst_original = os.path.join(ORIGINALS_DIR, f"manual_{source_event_id}.jpg")
        dst_thumb = os.path.join(THUMBNAILS_DIR, f"manual_{source_event_id}.jpg")
        with open(dst_original, "wb") as f:
            f.write(img_bytes)
        with open(dst_thumb, "wb") as f:
            f.write(thumb_bytes)

        # Pas de date exploitable dans le nom de fichier (contrairement à
        # Reolink) - mtime du fichier déposé, à défaut "maintenant".
        try:
            mtime = os.path.getmtime(src_path)
            created_at = datetime.utcfromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            created_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

        with get_conn() as conn:
            cursor = conn.execute(
                """INSERT INTO captures (
                       source, source_event_id, created_at,
                       detected_objects_json, original_path, thumbnail_path,
                       review_status
                   ) VALUES ('manual', ?, ?, ?, ?, ?, 'pending')""",
                (source_event_id, created_at, detected_objects_json, dst_original, dst_thumb),
            )
            recompute_cat_labels(conn, cursor.lastrowid)

        os.replace(src_path, os.path.join(imported_dir, filename))
        if sidecar_path and os.path.isfile(sidecar_path):
            os.replace(sidecar_path, os.path.join(imported_dir, os.path.basename(sidecar_path)))
        ingested += 1

    if ingested:
        log.info("Ingestion manuelle terminée : %d nouvelles captures", ingested)
    return ingested


if __name__ == "__main__":
    ingest_manual_batch()
