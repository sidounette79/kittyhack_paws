"""
Construit un zip de crops identité (Modèle 2) tirés directement des vrais
événements RFID de production Kittyhack (rfid_is_real=1), PAS du petit lot
curé "confirmed/corrected" de Paws Review (voir export_identity_training.py
pour ce dernier, utilisé par l'ancienne approche embeddings/ArcFace,
train_vastai_identity.py). C'est CE dataset (volume massif, pas l'approche)
qui a fait passer le classificateur YOLO26-cls de 62.8% (petit lot) à
92.8% (ce lot) - voir la mémoire project_kittyhack_identity_model2_work.md
pour l'historique complet de cette découverte, 02.11.

Recréé le 05.11 - le script original (exécuté une fois à la main depuis un
fork/scratchpad le 02.11, jamais committé dans ce repo) a été perdu. Sid,
05.11 : "reconstruire le vrai pipeline" pour que le bouton web "Modèle 2"
de Paws Review reproduise vraiment ce résultat, au lieu de lancer
l'ancienne approche abandonnée par erreur (voir identity_train_start()
dans main.py, qui pointait sur train_vastai_identity.py avant ce correctif).

Décisions reproduites depuis la mémoire du run original (pas redevinées) :
- Source : kittyhack.db en lecture seule (SOURCE_DB_PATH, même pattern que
  ingest.py), events avec rfid_is_real=1 - le tag RFID est la vérité
  terrain, jamais une prédiction du modèle YOLO lui-même (qui peut se
  tromper, c'est justement ce qu'on veut corriger).
- Volume cible : jusqu'à `target_per_cat` crops/chat (1800 par défaut, le
  chiffre du run original), cap adaptatif par bloc/visite
  (ceil(target/n_blocks) par block_id distinct) pour maximiser la
  diversité réelle (124-218 visites distinctes par chat le 02.11) plutôt
  que sur-échantillonner une même visite.
- Exclusion de contamination : tout event_id présent dans
  identity_eval_set_event_ids.json (les 800 event_ids du jeu de test
  figé, utilisé pour TOUTES les mesures de précision depuis le 02.11,
  jamais à modifier) est exclu d'office - vérifié deux fois comme le run
  original (filtré à la sélection ET revérifié sur le résultat final,
  RuntimeError si jamais une fuite passait au travers).
- Exclut aussi toute capture où une boîte Prey coexiste dans la même
  frame (même logique que export_identity_training.py - évite que le
  modèle apprenne une corrélation parasite proie-présente -> "c'est
  Patoune", 99,9% des proies rapportées venant de lui).
- Recadrage sur la boîte non-Prey la plus probable de la frame (le RFID
  dit QUI, cette boîte dit juste OÙ - peu importe quel nom le modèle
  avait prédit dessus).
"""

import json
import logging
import math
import os
import random
import sqlite3
import zipfile
from collections import defaultdict
from contextlib import contextmanager

from PIL import Image

from app.config import SOURCE_DB_PATH, SOURCE_ORIGINALS_DIR
from app.labels import canonicalize

logging.basicConfig(level=logging.INFO, format="%(asctime)s [build_large_rfid_training_set] %(message)s")
log = logging.getLogger(__name__)

_EXCLUDED_EVENT_IDS_PATH = os.path.join(os.path.dirname(__file__), "identity_eval_set_event_ids.json")


def _load_excluded_event_ids() -> set[int]:
    with open(_EXCLUDED_EVENT_IDS_PATH, encoding="utf-8") as f:
        return set(json.load(f))


@contextmanager
def _source_conn():
    # Même pattern que ingest.py's source_conn() - lecture seule, jamais
    # d'écriture dans la base Kittyhack depuis Paws Review.
    conn = sqlite3.connect(f"file:{SOURCE_DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def _parse_detected_objects(event_text: str) -> list[dict]:
    if not event_text:
        return []
    try:
        data = json.loads(event_text)
        return [
            {
                "object_name": obj["object_name"],
                "probability": float(obj["probability"]),
                "x": float(obj["x"]), "y": float(obj["y"]),
                "width": float(obj["width"]), "height": float(obj["height"]),
            }
            for obj in data.get("detected_objects", [])
        ]
    except Exception:
        return []


def build_large_rfid_training_zip(
    output_path: str, target_per_cat: int = 1800, seed: int = 20261105,
) -> dict:
    excluded_ids = _load_excluded_event_ids()
    rng = random.Random(seed)

    with _source_conn() as conn:
        cats_by_rfid = {
            row["rfid"]: row["name"]
            for row in conn.execute("SELECT name, rfid FROM cats WHERE rfid IS NOT NULL").fetchall()
        }
        rows = conn.execute(
            """SELECT id, block_id, rfid, event_text FROM events
               WHERE rfid_is_real = 1 AND media_purged = 0 AND deleted = 0"""
        ).fetchall()

    # cat_name -> block_id -> [(event_id, box), ...]
    by_cat_block: dict[str, dict[int, list]] = defaultdict(lambda: defaultdict(list))
    skipped_excluded = 0
    skipped_unknown_rfid = 0
    skipped_prey_coexist = 0
    skipped_no_box = 0

    for row in rows:
        if row["id"] in excluded_ids:
            skipped_excluded += 1
            continue
        cat_name = cats_by_rfid.get(row["rfid"])
        if not cat_name:
            skipped_unknown_rfid += 1
            continue
        boxes = _parse_detected_objects(row["event_text"])
        if any(canonicalize(b["object_name"]) == "Prey" for b in boxes):
            skipped_prey_coexist += 1
            continue
        non_prey_boxes = [b for b in boxes if canonicalize(b["object_name"]) != "Prey"]
        if not non_prey_boxes:
            skipped_no_box += 1
            continue
        best_box = max(non_prey_boxes, key=lambda b: b["probability"])
        by_cat_block[cat_name][row["block_id"]].append((row["id"], best_box))

    # Cap adaptatif par bloc : ceil(target/n_blocks) par block_id distinct
    # plutôt qu'un plafond global aveugle - une seule longue visite ne peut
    # jamais monopoliser le pool d'un chat, même si elle contient des
    # milliers de frames.
    selected: dict[str, list[tuple[int, dict]]] = {}
    for cat_name, blocks in by_cat_block.items():
        n_blocks = len(blocks)
        per_block_cap = math.ceil(target_per_cat / n_blocks) if n_blocks else 0
        items = []
        for block_id in sorted(blocks):
            block_items = sorted(blocks[block_id], key=lambda t: t[0])  # id croissant, reproductible
            items.extend(block_items[:per_block_cap])
        rng.shuffle(items)
        selected[cat_name] = items[:target_per_cat]

    tmp_dir = output_path + ".tmp"
    os.makedirs(tmp_dir, exist_ok=True)
    counts: dict[str, int] = {}
    included = 0
    skipped_missing_file = 0
    skipped_bad_crop = 0
    contamination_check_ids: set[int] = set()

    for cat_name, items in selected.items():
        cat_dir = os.path.join(tmp_dir, cat_name)
        os.makedirs(cat_dir, exist_ok=True)
        cat_included = 0
        for event_id, box in items:
            contamination_check_ids.add(event_id)
            src_path = os.path.join(SOURCE_ORIGINALS_DIR, f"{event_id}.jpg")
            if not os.path.isfile(src_path):
                skipped_missing_file += 1
                continue
            try:
                with Image.open(src_path) as img:
                    w, h = img.size
                    x0 = max(0, int(box["x"] / 100.0 * w))
                    y0 = max(0, int(box["y"] / 100.0 * h))
                    x1 = min(w, int((box["x"] + box["width"]) / 100.0 * w))
                    y1 = min(h, int((box["y"] + box["height"]) / 100.0 * h))
                    if x1 - x0 < 8 or y1 - y0 < 8:
                        skipped_bad_crop += 1
                        continue
                    crop = img.convert("RGB").crop((x0, y0, x1, y1))
                    crop.save(os.path.join(cat_dir, f"capture_{event_id}.jpg"), format="JPEG", quality=90)
                    included += 1
                    cat_included += 1
            except Exception as e:
                log.warning("Crop échoué pour event %s: %s", event_id, e)
                skipped_bad_crop += 1
        counts[cat_name] = cat_included

    # Double vérification de non-contamination, comme le run original du
    # 02.11 (filtré à la sélection ci-dessus ET revérifié ici sur le
    # résultat final) - jamais silencieux en cas de fuite.
    contamination = contamination_check_ids & excluded_ids
    if contamination:
        raise RuntimeError(
            f"Contamination détectée : {len(contamination)} event_ids du jeu de "
            f"test figé inclus par erreur : {sorted(contamination)[:10]}"
        )

    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, _dirs, files in os.walk(tmp_dir):
            for fname in files:
                abs_path = os.path.join(root, fname)
                zf.write(abs_path, os.path.relpath(abs_path, tmp_dir))

    for root, _dirs, files in os.walk(tmp_dir, topdown=False):
        for fname in files:
            os.remove(os.path.join(root, fname))
        os.rmdir(root)

    log.info(
        "Export RFID large : %d crops inclus (%d exclus jeu de test, %d rfid inconnu, "
        "%d proie co-présente, %d sans boîte utilisable, %d fichiers manquants, %d crops invalides)",
        included, skipped_excluded, skipped_unknown_rfid, skipped_prey_coexist,
        skipped_no_box, skipped_missing_file, skipped_bad_crop,
    )
    return {
        "included": included,
        "counts": counts,
        "skipped_excluded_eval_set": skipped_excluded,
        "skipped_unknown_rfid": skipped_unknown_rfid,
        "skipped_prey_coexist": skipped_prey_coexist,
        "skipped_no_box": skipped_no_box,
        "skipped_missing_file": skipped_missing_file,
        "skipped_bad_crop": skipped_bad_crop,
    }
