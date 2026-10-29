"""
Ingestion depuis les captures "training samples" du watchdog Reolink
(lecture seule, simples fichiers JPEG - pas de table SQL source comme pour
le Kittyflap, voir watchdog.py côté Kittyhack) vers le catalogue de Paws
Review.

18.10, Sid : "l'angle Reolink est complètement exclu de l'entraînement" -
watchdog.py sauvegarde déjà, en continu, un échantillon rare/filtré de
frames spécifiquement "pour Label Studio" (TRAINING_SAMPLES_DIR, voir son
propre commentaire côté Kittyhack : "séparé de EVENT_FRAMES_DIR exprès -
celui-là reste sparse/gated pour Label Studio") mais rien ne les reprenait
jusqu'ici. À l'origine (18.10), ces fichiers n'avaient effectivement AUCUNE
boîte pré-remplie. Depuis le 21.10 côté watchdog.py (_save_training_sample),
un sidecar JSON (même nom de fichier, extension .json) accompagne l'image
quand le modèle local de chatiere a vu quelque chose - jamais lu ici
jusqu'au 26.10 (Sid : "pourquoi les reolink ont pas les boîtes alors que
[dans Parcours] j'ai ?") : la doc ci-dessus n'avait jamais été mise à jour
avec le code, les boîtes existaient sur disque depuis une semaine sans
jamais être reprises. Clé "name" côté sidecar, traduite ici en "object_name"
pour matcher le schéma detected_objects_json attendu par effective_boxes()
(voir boxes.py) - même traduction que journeys.py fait pour son propre
import manuel, juste appliquée ici à l'import automatique.

Pas de table SQL source => pas d'ID numérique naturel à utiliser comme
checkpoint d'ingestion (contrairement à events.id côté Kittyflap) - on
s'appuie uniquement sur la contrainte UNIQUE(source, source_event_id) de
captures, avec un ID dérivé de façon stable du nom de fichier.
"""

import io
import json
import logging
import os
import re
import zlib
from datetime import datetime

from PIL import Image

from app.cat_labels import recompute_cat_labels
from app.catalog_db import get_conn, init_db
from app.config import ORIGINALS_DIR, SOURCE_REOLINK_SAMPLES_DIR, THUMBNAILS_DIR

logging.basicConfig(level=logging.INFO, format="%(asctime)s [ingest_reolink] %(message)s")
log = logging.getLogger(__name__)

# Même format que journeys.py côté Kittyhack (_SAMPLE_RE) - {cam}_{date}_{heure}.jpg
FILENAME_RE = re.compile(r"^(?P<cam>[a-z_]+)_(?P<date>\d{8})_(?P<time>\d{6})\.jpg$")
THUMB_SIZE = (320, 240)

# 18.10, Sid ("ne garder que la caméra chatière pour l'import/le tri") -
# watchdog.py surveille aussi jardin_japonais/entree/terrasse (mêmes
# fichiers plats, même dossier) mais seule "chatiere" (= "Chatière ext"
# côté Kittyhack, voir journeys.py _CAM_LABELS) a un intérêt pour
# l'entraînement proie/identité ici. Les captures déjà importées des 3
# autres ont été nettoyées via cleanup_reolink_cameras.py.
INCLUDED_CAMS = {"chatiere"}


def _stable_event_id(filename: str) -> int:
    """Dérivé du nom de fichier (date+heure+caméra, déjà unique en
    pratique) - 31 bits, compatible avec la colonne INTEGER existante,
    risque de collision négligeable à ce volume."""
    return zlib.crc32(filename.encode()) & 0x7FFFFFFF


def _parse_created_at(date_str: str, time_str: str) -> str:
    dt = datetime.strptime(date_str + time_str, "%Y%m%d%H%M%S")
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _make_thumbnail(image_bytes: bytes) -> bytes:
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    img.thumbnail(THUMB_SIZE)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return buf.getvalue()


def _load_sidecar_boxes(jpg_filename: str) -> str:
    """Lit le sidecar JSON de watchdog.py (_save_training_sample), s'il
    existe, et traduit sa clé "name" en "object_name" pour matcher le
    schéma attendu par effective_boxes() (boxes.py). '[]' si absent ou
    illisible - jamais une raison de faire échouer l'ingestion de la photo
    elle-même."""
    sidecar_path = os.path.join(SOURCE_REOLINK_SAMPLES_DIR, jpg_filename[:-4] + ".json")
    if not os.path.isfile(sidecar_path):
        return "[]"
    try:
        with open(sidecar_path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        boxes = [
            {
                "object_name": d["name"],
                "probability": d.get("probability", 0),
                "x": d["x"], "y": d["y"], "width": d["width"], "height": d["height"],
            }
            for d in raw
        ]
        return json.dumps(boxes)
    except Exception as e:
        log.warning("Sidecar illisible pour %s: %s", jpg_filename, e)
        return "[]"


def ingest_reolink_batch(limit: int = 2000) -> int:
    """Insère jusqu'à `limit` NOUVELLES captures par passage - un gros
    retard initial (confirmé le 18.10 : 18305 fichiers déjà accumulés
    depuis début septembre) se rattrape sur plusieurs cycles de 5 min
    plutôt que d'inonder la file de revue d'un coup, même logique que
    ingest.py côté Kittyflap."""
    os.makedirs(ORIGINALS_DIR, exist_ok=True)
    os.makedirs(THUMBNAILS_DIR, exist_ok=True)
    init_db()

    if not os.path.isdir(SOURCE_REOLINK_SAMPLES_DIR):
        log.warning("Dossier source introuvable : %s", SOURCE_REOLINK_SAMPLES_DIR)
        return 0

    filenames = sorted(
        f for f in os.listdir(SOURCE_REOLINK_SAMPLES_DIR)
        if (m := FILENAME_RE.match(f)) and m.group("cam") in INCLUDED_CAMS
    )

    ingested = 0
    for filename in filenames:
        if ingested >= limit:
            break
        m = FILENAME_RE.match(filename)
        source_event_id = _stable_event_id(filename)

        # 18.10, Sid ("ça rame, mes clics n'agissent pas") : CONFIRMÉ en
        # croisant ses horaires avec les logs - une seule connexion ouverte
        # pour tout le lot de 2000 tenait toute l'écriture (I/O fichier
        # comprise, interclassée avec chaque INSERT) dans UNE transaction
        # SQLite non commitée, bloquant ses propres écritures (Valider/
        # Écarter, chacune sa propre connexion courte) pendant toute la
        # durée du lot - plusieurs minutes. Une connexion courte par
        # fichier : l'I/O fichier se fait hors transaction, seule
        # l'écriture SQL elle-même tient un verrou, le temps d'un commit.
        with get_conn() as conn:
            exists = conn.execute(
                "SELECT 1 FROM captures WHERE source = 'reolink' AND source_event_id = ?",
                (source_event_id,),
            ).fetchone()
        if exists:
            continue

        src_path = os.path.join(SOURCE_REOLINK_SAMPLES_DIR, filename)
        try:
            with open(src_path, "rb") as f:
                img_bytes = f.read()
            thumb_bytes = _make_thumbnail(img_bytes)
        except Exception as e:
            log.warning("Image illisible pour %s: %s", filename, e)
            continue

        dst_original = os.path.join(ORIGINALS_DIR, f"reolink_{source_event_id}.jpg")
        dst_thumb = os.path.join(THUMBNAILS_DIR, f"reolink_{source_event_id}.jpg")
        with open(dst_original, "wb") as f:
            f.write(img_bytes)
        with open(dst_thumb, "wb") as f:
            f.write(thumb_bytes)

        created_at = _parse_created_at(m.group("date"), m.group("time"))
        detected_objects_json = _load_sidecar_boxes(filename)

        with get_conn() as conn:
            cursor = conn.execute(
                """INSERT INTO captures (
                       source, source_event_id, created_at,
                       detected_objects_json, original_path, thumbnail_path,
                       review_status
                   ) VALUES ('reolink', ?, ?, ?, ?, ?, 'pending')""",
                (source_event_id, created_at, detected_objects_json, dst_original, dst_thumb),
            )
            recompute_cat_labels(conn, cursor.lastrowid)
        ingested += 1

    log.info("Ingestion Reolink terminée : %d nouvelles captures", ingested)
    backfilled = _backfill_missing_boxes()
    if backfilled:
        log.info("Boîtes comblées rétroactivement pour %d capture(s) Reolink déjà importées", backfilled)
    return ingested


# 26.10, Sid ("pourquoi les reolink ont pas les boîtes") - les captures déjà
# importées AVANT ce correctif sont restées avec detected_objects_json='[]'
# pour de bon (l'ingestion normale ne revisite jamais un source_event_id
# déjà vu). Un passage ciblé à chaque cycle : seulement les captures encore
# review_status='pending' avec '[]' (jamais touché une capture déjà
# tranchée par Sid) ET un sidecar réellement présent maintenant.
def _backfill_missing_boxes() -> int:
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT id, source_event_id, created_at FROM captures
               WHERE source = 'reolink' AND review_status = 'pending'
                 AND (detected_objects_json IS NULL OR detected_objects_json = '[]')"""
        ).fetchall()
        backfilled = 0
        for row in rows:
            date_part, time_part = row["created_at"].split(" ")
            filename = f"chatiere_{date_part.replace('-', '')}_{time_part.replace(':', '')}.jpg"
            boxes_json = _load_sidecar_boxes(filename)
            if boxes_json != "[]":
                conn.execute(
                    "UPDATE captures SET detected_objects_json = ? WHERE id = ?",
                    (boxes_json, row["id"]),
                )
                recompute_cat_labels(conn, row["id"])
                backfilled += 1
        return backfilled


if __name__ == "__main__":
    ingest_reolink_batch()
