"""
Ingestion depuis la base Kittyhack (lecture seule) vers le catalogue propre à
Paws Review. Copie les fichiers image (jamais de lien/symlink - Kittyhack purge
son propre dossier au fil de l'eau, voir config.py) et applique :
  - résolution d'identité (RFID > visuel, le RFID ne ment jamais)
  - priorité proie (seuil de revue bas, volontairement sous le seuil d'action)
  - signal de désaccord de modèle (model_review_queue)
  - dédoublonnage des captures "chat seul" quasi-identiques (jamais appliqué
    aux proies suspectées ni aux désaccords de modèle)
"""

import json
import logging
import os
import shutil
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta

from app.catalog_db import get_conn, init_db
from app.cat_labels import recompute_cat_labels
from app.config import (
    DEDUP_HAMMING_THRESHOLD,
    DEDUP_WINDOW_MINUTES,
    MIN_OWN_CAT_PROBABILITY,
    ORIGINALS_DIR,
    PREY_REVIEW_THRESHOLD,
    SOURCE_DB_PATH,
    SOURCE_ORIGINALS_DIR,
    SOURCE_THUMBNAILS_DIR,
    THUMBNAILS_DIR,
)
from app.dedup import compute_phash, hamming_distance

logging.basicConfig(level=logging.INFO, format="%(asctime)s [ingest] %(message)s")
log = logging.getLogger(__name__)

PREY_KEYWORDS = ("prey", "proie", "maus", "mouse")


@contextmanager
def source_conn():
    conn = sqlite3.connect(f"file:{SOURCE_DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def _parse_detected_objects(event_text: str) -> list:
    """events.event_text -> [{object_name, probability, x, y, width, height}, ...]
    (coordonnées en % de l'image, convention Kittyhack - voir
    EventsRepo.read_event_from_json côté Kittyhack, même format reproduit ici
    sans dépendre de son code)."""
    if not event_text:
        return []
    try:
        data = json.loads(event_text)
        objs = []
        for obj in data.get("detected_objects", []):
            objs.append({
                "object_name": obj["object_name"],
                "probability": float(obj["probability"]),
                "x": float(obj["x"]),
                "y": float(obj["y"]),
                "width": float(obj["width"]),
                "height": float(obj["height"]),
            })
        return objs
    except Exception:
        return []


def _looks_like_prey(*names) -> bool:
    for name in names:
        if name and any(kw in name.lower() for kw in PREY_KEYWORDS):
            return True
    return False


def _load_cats_by_rfid(src: sqlite3.Connection) -> dict:
    rows = src.execute("SELECT id, name, rfid FROM cats WHERE rfid IS NOT NULL").fetchall()
    return {row["rfid"]: (row["id"], row["name"]) for row in rows}


def _get_checkpoint(catalog: sqlite3.Connection, source: str) -> int:
    row = catalog.execute(
        "SELECT last_source_event_id FROM ingest_state WHERE source = ?", (source,)
    ).fetchone()
    return row["last_source_event_id"] if row else 0


def _set_checkpoint(catalog: sqlite3.Connection, source: str, event_id: int):
    catalog.execute(
        """INSERT INTO ingest_state (source, last_source_event_id) VALUES (?, ?)
           ON CONFLICT(source) DO UPDATE SET last_source_event_id = excluded.last_source_event_id""",
        (source, event_id),
    )


def _find_dedup_match(catalog: sqlite3.Connection, cat_id, created_at: str, phash: str):
    if cat_id is None or phash is None:
        return None
    # sep=" " : les horodatages Kittyhack utilisent un espace, pas le "T" par
    # défaut d'isoformat() - sinon la comparaison de chaînes en SQL échoue
    # toujours (' ' < 'T' en ASCII, donc created_at >= window_start est
    # systématiquement faux malgré des horodatages réellement postérieurs).
    window_start = (
        datetime.fromisoformat(created_at) - timedelta(minutes=DEDUP_WINDOW_MINUTES)
    ).isoformat(sep=" ")
    candidates = catalog.execute(
        """SELECT id, phash FROM captures
           WHERE cat_id = ? AND created_at >= ? AND created_at <= ?
             AND dedup_of_id IS NULL AND phash IS NOT NULL
             AND is_prey_candidate = 0 AND model_disagreement = 0
           ORDER BY created_at DESC""",
        (cat_id, window_start, created_at),
    ).fetchall()
    for row in candidates:
        if hamming_distance(phash, row["phash"]) <= DEDUP_HAMMING_THRESHOLD:
            return row["id"]
    return None


def ingest_kittyflap_batch(limit: int = 2000) -> int:
    os.makedirs(ORIGINALS_DIR, exist_ok=True)
    os.makedirs(THUMBNAILS_DIR, exist_ok=True)
    init_db()

    ingested = 0
    with source_conn() as src, get_conn() as catalog:
        checkpoint = _get_checkpoint(catalog, "kittyflap")
        cats_by_rfid = _load_cats_by_rfid(src)
        known_names = {name.lower(): name for _, name in cats_by_rfid.values()}

        events = src.execute(
            """SELECT id, block_id, created_at, event_type, mouse_probability,
                      no_mouse_probability, own_cat_probability, rfid, rfid_is_real, event_text
               FROM events  -- own_cat_probability déjà sélectionnée ci-dessus
               WHERE id > ? AND deleted = 0
               ORDER BY id ASC
               LIMIT ?""",
            (checkpoint, limit),
        ).fetchall()

        last_id = checkpoint
        for event in events:
            last_id = event["id"]

            src_original = os.path.join(SOURCE_ORIGINALS_DIR, f"{event['id']}.jpg")
            src_thumb = os.path.join(SOURCE_THUMBNAILS_DIR, f"{event['id']}.jpg")
            if not (os.path.isfile(src_original) and os.path.isfile(src_thumb)):
                # Déjà purgé par la rotation interne de Kittyhack (~20000 photos) -
                # rien à récupérer, on avance quand même le checkpoint.
                continue

            feedback = src.execute(
                """SELECT original_name, corrected_name, original_probability
                   FROM detection_feedback WHERE photo_id = ? ORDER BY object_index LIMIT 1""",
                (event["id"],),
            ).fetchone()

            disagreement_row = src.execute(
                """SELECT old_name, new_name FROM model_review_queue
                   WHERE photo_id = ? AND status = 'pending' LIMIT 1""",
                (event["id"],),
            ).fetchone()
            model_disagreement = bool(
                disagreement_row and disagreement_row["old_name"] != disagreement_row["new_name"]
            )

            cat_id = cat_name = cat_source = None
            if event["rfid"] and event["rfid"] in cats_by_rfid:
                cat_id, cat_name = cats_by_rfid[event["rfid"]]
                cat_source = "rfid"
            elif feedback and (feedback["corrected_name"] or feedback["original_name"]):
                cat_name = feedback["corrected_name"] or feedback["original_name"]
                cat_source = "visual"

            visual_label = feedback["corrected_name"] if feedback else None
            visual_label = visual_label or (feedback["original_name"] if feedback else None)
            visual_confidence = feedback["original_probability"] if feedback else None

            detected_objects = _parse_detected_objects(event["event_text"])
            detected_names = [o["object_name"] for o in detected_objects]

            prey_probability = event["mouse_probability"]
            is_prey_candidate = bool(
                (prey_probability is not None and prey_probability >= PREY_REVIEW_THRESHOLD)
                or _looks_like_prey(
                    visual_label,
                    feedback["original_name"] if feedback else None,
                    *detected_names,
                )
            )

            # 24.10, Sid ("on peut suspendre l'envoi des photos taguées avec
            # boubou, tant que proie n'y est pas associé ou qu'il est pas
            # tout seul") - quota HK/Boubou atteint (200/200, voir
            # /app-api/rebalance/counts) : inutile d'en accumuler davantage
            # dans la file de revue. N'ignore QUE "Boubou seul, sans proie" -
            # une photo avec une proie ou un autre chat reste importée
            # normalement, ces cas-là restent utiles ailleurs. Vide ce set
            # pour reprendre l'import normal (aucune autre modification
            # nécessaire).
            # 25.10, Sid ("coupe l'arrivée des nouvelles photos pour pookie
            # et patoune, excepté si pas tout seul ou proie") - même
            # quota atteint (202/200 chacun), même mécanisme.
            _hk_ingest_paused_solo_cats = {"boubou", "nala", "pookie", "patoune"}
            if (
                not is_prey_candidate
                and cat_name
                and cat_name.lower() in _hk_ingest_paused_solo_cats
                and not any(
                    n.lower() != cat_name.lower() and n.lower() != "prey"
                    for n in detected_names
                )
            ):
                continue

            own_cat_probability = event["own_cat_probability"]
            # Chat peu/pas visible (occlusion, sortie momentanée du cadre, ou
            # carrément une fausse détection genre "bâche de jardin = Patoune") :
            # pas question de tout jeter (Sid, 17.10 - ça peut servir de négatif
            # explicite pour désapprendre ce genre de faux positif), mais pas
            # question non plus de garder 50 quasi-doublons par bloc. Même
            # traitement que les frames normales : dédoublonné, juste étiqueté à
            # part et jamais prioritaire dans la file de revue.
            low_cat_confidence = bool(
                not is_prey_candidate
                and own_cat_probability is not None
                and own_cat_probability < MIN_OWN_CAT_PROBABILITY
            )

            dst_original = os.path.join(ORIGINALS_DIR, f"kittyflap_{event['id']}.jpg")
            dst_thumb = os.path.join(THUMBNAILS_DIR, f"kittyflap_{event['id']}.jpg")
            shutil.copy2(src_original, dst_original)
            shutil.copy2(src_thumb, dst_thumb)

            review_status = "pending"
            dedup_of_id = None
            phash = None
            if is_prey_candidate or model_disagreement:
                pass  # jamais dédoublonné, toujours montré intégralement
            else:
                phash = compute_phash(dst_thumb)
                dedup_of_id = _find_dedup_match(catalog, cat_id, event["created_at"], phash)
                if dedup_of_id is not None:
                    review_status = "skipped_dedup"

            cursor = catalog.execute(
                """INSERT INTO captures (
                       source, source_event_id, block_id, created_at,
                       cat_id, cat_name, cat_source, rfid,
                       visual_label, visual_confidence,
                       prey_probability, own_cat_probability, is_prey_candidate, model_disagreement,
                       low_cat_confidence,
                       original_path, thumbnail_path, phash, detected_objects_json,
                       dedup_of_id, review_status
                   ) VALUES (?,?,?,?, ?,?,?,?, ?,?, ?,?,?,?, ?, ?,?,?,?, ?,?)""",
                (
                    "kittyflap", event["id"], event["block_id"], event["created_at"],
                    cat_id, cat_name, cat_source, event["rfid"],
                    visual_label, visual_confidence,
                    prey_probability, own_cat_probability, int(is_prey_candidate), int(model_disagreement),
                    int(low_cat_confidence),
                    dst_original, dst_thumb, phash, json.dumps(detected_objects),
                    dedup_of_id, review_status,
                ),
            )
            recompute_cat_labels(catalog, cursor.lastrowid, known_names)
            ingested += 1

        _set_checkpoint(catalog, "kittyflap", last_id)

    log.info("Ingestion terminée : %d nouvelles captures (checkpoint = %d)", ingested, last_id)
    return ingested


# 25.10, Sid ("j'ai que deux photos qui sont venues de pookie en HK... faut
# que je corrige la forme de la boîte. Donc une fonction de transfert vers
# paws review") - ingest_kittyflap_batch() ci-dessus lit event_text en
# séquence une seule fois (checkpoint jamais revisité) : une correction
# faite après coup dans la page Photos de Kittyhack (fausse détection
# écartée + boîte redessinée à la main) ne remonte jamais ici toute seule.
# Appelé depuis POST /app-api/ingest/kittyflap (voir main.py), lui-même
# appelé par kittyhack-remote (POST .../pictures/{pid}/send-to-paws-review)
# qui a déjà fait toute la reconstruction (boîtes effectives, RFID résolu)
# de son côté - jamais de dédoublonnage ici (elle choisit cette photo
# précise exprès), upsert sur (source='kittyflap', source_event_id) pour
# que renvoyer la même photo après une nouvelle correction rafraîchisse la
# capture existante plutôt que d'en créer un doublon.
def ingest_kittyflap_single(payload: dict) -> dict:
    event_id = int(payload["event_id"])
    block_id = payload.get("block_id")
    created_at = payload["created_at"]
    rfid = payload.get("rfid")
    cat_name = payload.get("cat_name")
    mouse_probability = payload.get("mouse_probability")
    own_cat_probability = payload.get("own_cat_probability")
    boxes = payload.get("boxes") or []

    os.makedirs(ORIGINALS_DIR, exist_ok=True)
    os.makedirs(THUMBNAILS_DIR, exist_ok=True)
    init_db()

    src_original = os.path.join(SOURCE_ORIGINALS_DIR, f"{event_id}.jpg")
    src_thumb = os.path.join(SOURCE_THUMBNAILS_DIR, f"{event_id}.jpg")
    if not (os.path.isfile(src_original) and os.path.isfile(src_thumb)):
        raise FileNotFoundError(f"photo Kittyhack {event_id} introuvable (déjà purgée ?)")

    dst_original = os.path.join(ORIGINALS_DIR, f"kittyflap_{event_id}.jpg")
    dst_thumb = os.path.join(THUMBNAILS_DIR, f"kittyflap_{event_id}.jpg")
    shutil.copy2(src_original, dst_original)
    shutil.copy2(src_thumb, dst_thumb)

    cat_source = "rfid" if (rfid and cat_name) else None
    visual_label = None
    visual_confidence = None
    for b in boxes:
        if not _looks_like_prey(b.get("object_name")):
            visual_label = b["object_name"]
            visual_confidence = b.get("probability")
            break
    if cat_source is None and visual_label:
        # Repli : aucune identité RFID déjà résolue côté kittyhack-remote -
        # premier chat visuel (jamais Prey) parmi les boîtes envoyées, même
        # logique que ingest_kittyflap_batch ci-dessus.
        cat_name = visual_label
        cat_source = "visual"

    detected_names = [b["object_name"] for b in boxes]
    is_prey_candidate = bool(
        (mouse_probability is not None and mouse_probability >= PREY_REVIEW_THRESHOLD)
        or _looks_like_prey(cat_name, visual_label, *detected_names)
    )
    low_cat_confidence = bool(
        not is_prey_candidate
        and own_cat_probability is not None
        and own_cat_probability < MIN_OWN_CAT_PROBABILITY
    )
    detected_objects_json = json.dumps(boxes)

    with get_conn() as catalog:
        existing = catalog.execute(
            "SELECT id, review_status FROM captures WHERE source = 'kittyflap' AND source_event_id = ?",
            (event_id,),
        ).fetchone()
        if existing is not None:
            capture_id = existing["id"]
            new_status = "pending" if existing["review_status"] == "skipped_dedup" else existing["review_status"]
            # Remplace detected_objects_json en entier (nouvelle liste de
            # boîtes, nouveaux indices) - toute correction déjà faite DANS
            # Paws Review (box_annotations, indexée sur l'ancien
            # detected_objects_json) pointerait sinon sur le mauvais objet
            # après coup. Cet envoi devient la nouvelle vérité de départ.
            catalog.execute("DELETE FROM box_annotations WHERE capture_id = ?", (capture_id,))
            catalog.execute(
                """UPDATE captures SET
                       cat_name = ?, cat_source = ?, rfid = ?,
                       visual_label = ?, visual_confidence = ?,
                       prey_probability = ?, own_cat_probability = ?,
                       is_prey_candidate = ?, low_cat_confidence = ?,
                       detected_objects_json = ?, dedup_of_id = NULL,
                       review_status = ?
                   WHERE id = ?""",
                (
                    cat_name, cat_source, rfid,
                    visual_label, visual_confidence,
                    mouse_probability, own_cat_probability,
                    int(is_prey_candidate), int(low_cat_confidence),
                    detected_objects_json, new_status,
                    capture_id,
                ),
            )
            action = "updated"
        else:
            cursor = catalog.execute(
                """INSERT INTO captures (
                       source, source_event_id, block_id, created_at,
                       cat_name, cat_source, rfid,
                       visual_label, visual_confidence,
                       prey_probability, own_cat_probability, is_prey_candidate, model_disagreement,
                       low_cat_confidence,
                       original_path, thumbnail_path, detected_objects_json,
                       review_status
                   ) VALUES (?,?,?,?, ?,?,?, ?,?, ?,?,?,?, ?, ?,?,?, ?)""",
                (
                    "kittyflap", event_id, block_id, created_at,
                    cat_name, cat_source, rfid,
                    visual_label, visual_confidence,
                    mouse_probability, own_cat_probability, int(is_prey_candidate), 0,
                    int(low_cat_confidence),
                    dst_original, dst_thumb, detected_objects_json,
                    "pending",
                ),
            )
            capture_id = cursor.lastrowid
            action = "inserted"
        recompute_cat_labels(catalog, capture_id, None)
    return {"ok": True, "capture_id": capture_id, "action": action}


if __name__ == "__main__":
    ingest_kittyflap_batch()
