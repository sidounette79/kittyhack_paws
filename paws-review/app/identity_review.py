"""File de correction d'identité (Modèle 2) - Phase 5 du plan de scission
détection/identité. Sid ("le but c'est vraiment de la correction
d'apprentissage, lui dire là tu as mis ça, tu t'es trompé, apprends ça en
plus") - montre les boîtes de chat déjà résolues par le Modèle 2 (voir
identity.py/resolve_cat_identities côté kittyhack-remote), triées par
score croissant (les moins sûres d'abord).

01.11, REVERT d'une 1ère version avec sa propre table identity_corrections
- inutile : effective_boxes()/box_annotations (voir boxes.py) est DÉJÀ le
mécanisme de vérité terrain qu'export_training.py ET
export_identity_training.py consultent tous les deux. "Déjà revue" = une
ligne box_annotations existe pour cette boîte précise (confirmée OU
écartée, peu importe) - pas de table séparée à synchroniser. La
correction elle-même passe par l'éditeur de boîtes déjà existant
(/app-api/captures/{id}/boxes, common.js) : changer le nom = corrige
l'identité ; écarter = "ce n'est pas un chat" ; dessiner une boîte = "un
chat manqué". Cette file n'est qu'un point d'entrée filtré/trié vers cet
éditeur, pas un système parallèle.
"""
import sqlite3

from app.boxes import effective_boxes
from app.labels import canonicalize

SCAN_WINDOW = 3000


def _already_reviewed(conn: sqlite3.Connection) -> set[tuple[int, int]]:
    return {
        (r["capture_id"], r["object_index"])
        for r in conn.execute("SELECT capture_id, object_index FROM box_annotations").fetchall()
    }


def _low_confidence_queue(conn: sqlite3.Connection, allowed_labels: set[str], limit: int) -> list[dict]:
    reviewed = _already_reviewed(conn)
    rows = conn.execute(
        """SELECT id, source, detected_objects_json, created_at
           FROM captures
           ORDER BY id DESC
           LIMIT ?""",
        (SCAN_WINDOW,),
    ).fetchall()

    candidates = []
    for row in rows:
        boxes = effective_boxes(conn, row["id"], row["detected_objects_json"])
        for box in boxes:
            label = canonicalize(box.get("label"))
            if label not in allowed_labels:
                continue
            if (row["id"], box["object_index"]) in reviewed:
                continue
            score = box.get("probability")
            if score is None:
                continue
            candidates.append({
                "capture_id": row["id"],
                "object_index": box["object_index"],
                "source": row["source"],
                "predicted_name": label,
                "predicted_score": score,
                "created_at": row["created_at"],
            })

    candidates.sort(key=lambda c: c["predicted_score"])
    return candidates[:limit]


def get_identity_review_queue(conn: sqlite3.Connection, limit: int = 100) -> list[dict]:
    """Boîtes de chat (Pookie/Patoune/Nala/Boubou) résolues par le Modèle 2,
    pas encore revues, score croissant."""
    return _low_confidence_queue(conn, {"Pookie", "Patoune", "Nala", "Boubou"}, limit)


def get_detection_review_queue(conn: sqlite3.Connection, limit: int = 100) -> list[dict]:
    """01.11, Sid ("il me faudrait la même page mais pour chat/proie") -
    même principe, mais pour les boîtes Prey (jamais touchées par la
    résolution d'identité, leur probability est toujours celle du Modèle
    1 - contrairement aux boîtes de chat, dont probability est devenue le
    score d'identité, voir identity.py). Pas de tri par confiance possible
    côté chat ici (cette info n'existe plus une fois le nom résolu) -
    seules les boîtes Prey sont vraiment triables par confiance de
    détection ; incluses quand même pour laisser revoir les captures
    récentes qui n'ont jamais été ouvertes."""
    return _low_confidence_queue(conn, {"Prey"}, limit)
