"""File de correction d'identité (Modèle 2) - Phase 5 du plan de scission
détection/identité. Sid ("le but c'est vraiment de la correction
d'apprentissage, lui dire là tu as mis ça, tu t'es trompé, apprends ça en
plus") - montre les boîtes "Cat" déjà résolues par le Modèle 2 (nom +
score, voir identity.py/resolve_cat_identities côté kittyhack-remote) pas
encore corrigées, triées par score croissant (les moins sûres d'abord -
c'est elles qui ont le plus de chances d'être fausses). Une correction
enregistrée ici est reprise par export_identity_training.py au prochain
entraînement, à la place du label brut détecté.
"""
import sqlite3

from app.boxes import effective_boxes
from app.catalog_db import get_conn, init_db
from app.labels import IDENTITY_CLASSES, canonicalize

# Fenêtre de scan : les captures les plus récentes seulement - pas tout
# l'historique (des dizaines de milliers de lignes), ce qu'on veut
# corriger c'est le Modèle 2 fraîchement activé, pas rejuger indéfiniment
# le passé.
SCAN_WINDOW = 3000


def get_identity_review_queue(conn: sqlite3.Connection, limit: int = 100) -> list[dict]:
    already_corrected = {
        (r["capture_id"], r["object_index"])
        for r in conn.execute("SELECT capture_id, object_index FROM identity_corrections").fetchall()
    }

    rows = conn.execute(
        """SELECT id, source, original_path, thumbnail_path, detected_objects_json, created_at
           FROM captures
           ORDER BY id DESC
           LIMIT ?""",
        (SCAN_WINDOW,),
    ).fetchall()

    candidates = []
    for row in rows:
        boxes = effective_boxes(conn, row["id"], row["detected_objects_json"])
        for box in boxes:
            cat_name = canonicalize(box.get("label"))
            if cat_name not in IDENTITY_CLASSES:
                continue
            if (row["id"], box["object_index"]) in already_corrected:
                continue
            score = box.get("probability")
            if score is None:
                continue
            candidates.append({
                "capture_id": row["id"],
                "object_index": box["object_index"],
                "source": row["source"],
                "thumbnail_path": row["thumbnail_path"],
                "predicted_name": cat_name,
                "predicted_score": score,
                "created_at": row["created_at"],
            })

    candidates.sort(key=lambda c: c["predicted_score"])
    return candidates[:limit]


def record_identity_correction(
    conn: sqlite3.Connection, capture_id: int, object_index: int,
    predicted_name: str, predicted_score: float | None, corrected_name: str,
) -> None:
    conn.execute(
        """INSERT INTO identity_corrections
               (capture_id, object_index, predicted_name, predicted_score, corrected_name)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(capture_id, object_index) DO UPDATE SET
               predicted_name=excluded.predicted_name,
               predicted_score=excluded.predicted_score,
               corrected_name=excluded.corrected_name,
               created_at=datetime('now'),
               exported_at=NULL""",
        (capture_id, object_index, predicted_name, predicted_score, corrected_name),
    )


def pending_corrections_count(conn: sqlite3.Connection) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM identity_corrections WHERE exported_at IS NULL"
    ).fetchone()[0]
