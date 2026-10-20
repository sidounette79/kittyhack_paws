"""
Purge par âge (1 an par défaut, confirmé par Sid le 17.10). Jamais de capture
'pending' purgée, quel que soit son âge - elle n'a pas encore été vue, donc pas
question de la perdre dans son dos. Les captures marquées kept_for_training sont
elles aussi exemptées (ex. proies confirmées, trop rares pour les jeter).
"""

import logging
import os
from datetime import datetime, timedelta, timezone

from app.catalog_db import get_conn, init_db
from app.config import RETENTION_DAYS

logging.basicConfig(level=logging.INFO, format="%(asctime)s [retention] %(message)s")
log = logging.getLogger(__name__)


def purge_old_captures() -> int:
    init_db()
    # sep=" " : même piège que le dédoublonnage (app/ingest.py) - created_at
    # utilise un espace, pas le "T" par défaut d'isoformat(), sinon la
    # comparaison de chaînes échoue silencieusement.
    cutoff = (datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)).isoformat(sep=" ")
    deleted = 0
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT id, original_path, thumbnail_path FROM captures
               WHERE created_at < ? AND review_status != 'pending' AND kept_for_training = 0""",
            (cutoff,),
        ).fetchall()
        for row in rows:
            for path in (row["original_path"], row["thumbnail_path"]):
                try:
                    os.remove(path)
                except FileNotFoundError:
                    pass
            conn.execute("DELETE FROM captures WHERE id = ?", (row["id"],))
            deleted += 1

    log.info("Purge terminée : %d captures supprimées (> %d jours)", deleted, RETENTION_DAYS)
    return deleted


if __name__ == "__main__":
    purge_old_captures()
