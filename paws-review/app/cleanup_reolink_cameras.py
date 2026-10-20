"""
Supprime (pour de vrai : lignes + fichiers originaux/vignettes - pas un
simple 'rejected', contrairement à bulk_reject_by_filename.py) les
captures Reolink déjà importées depuis des caméras autres que "chatiere"
(= "Chatière ext" côté Kittyhack, voir journeys.py _CAM_LABELS).

18.10, Sid : "il faut supprimer celles qui viennent de jardin japonais,
entrée et terrasse. Ne garder que la caméra chatière." - ingest_reolink.py
importait initialement toutes les caméras du watchdog sans distinction ;
corrigé en parallèle pour ne plus jamais réimporter les 3 autres (voir
EXCLUDED_CAMS dans ce fichier), mais les ~8000 déjà en base avant ce
correctif doivent être nettoyées explicitement.

Usage : python -m app.cleanup_reolink_cameras
"""

import logging
import os
import re
import zlib

from app.catalog_db import get_conn
from app.config import SOURCE_REOLINK_SAMPLES_DIR

logging.basicConfig(level=logging.INFO, format="%(asctime)s [cleanup_reolink] %(message)s")
log = logging.getLogger(__name__)

FILENAME_RE = re.compile(r"^(?P<cam>[a-z_]+)_(?P<date>\d{8})_(?P<time>\d{6})\.jpg$")
EXCLUDED_CAMS = {"entree", "jardin_japonais", "terrasse"}


def _stable_event_id(filename: str) -> int:
    return zlib.crc32(filename.encode()) & 0x7FFFFFFF


def main():
    if not os.path.isdir(SOURCE_REOLINK_SAMPLES_DIR):
        log.error("Dossier source introuvable : %s", SOURCE_REOLINK_SAMPLES_DIR)
        return

    to_check = [
        f for f in os.listdir(SOURCE_REOLINK_SAMPLES_DIR)
        if (m := FILENAME_RE.match(f)) and m.group("cam") in EXCLUDED_CAMS
    ]
    log.info("%d fichiers des caméras exclues (%s) à vérifier", len(to_check), ", ".join(EXCLUDED_CAMS))

    deleted = checked = 0
    with get_conn() as conn:
        for filename in to_check:
            checked += 1
            source_event_id = _stable_event_id(filename)
            row = conn.execute(
                "SELECT id, original_path, thumbnail_path FROM captures "
                "WHERE source = 'reolink' AND source_event_id = ?",
                (source_event_id,),
            ).fetchone()
            if row is None:
                continue
            for path in (row["original_path"], row["thumbnail_path"]):
                try:
                    os.remove(path)
                except FileNotFoundError:
                    pass
            conn.execute("DELETE FROM captures WHERE id = ?", (row["id"],))
            deleted += 1
            if deleted % 1000 == 0:
                log.info("... %d supprimées", deleted)

    log.info("Terminé : %d fichiers vérifiés, %d captures supprimées", checked, deleted)


if __name__ == "__main__":
    main()
