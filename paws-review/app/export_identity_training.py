"""
Export des crops recadrés sur les boîtes de chat pour l'entraînement du
Modèle 2 (identité par embeddings) - voir le plan de scission détection/
identité (/volume1/docker/claude-home-data/plans/twinkly-percolating-wombat.md,
Phase 2).

Patron direct de export_training.py (mêmes captures source, même
mécanisme effective_boxes()/canonicalize()), mais produit des IMAGES
RECADRÉES étiquetées par chat (`<cat_name>/capture_<id>_<bucket>.jpg`)
au lieu d'un zip YOLO - le format standard pour un entraînement de
ré-identification/metric learning (un dossier par identité), pas des
fichiers de labels avec coordonnées.

29.10, Sid ("99.9% du temps c'est Patoune qui rapporte des proies") -
toute capture où une boîte Prey coexiste est EXCLUE ici (contrairement à
export_training.py, qui les garde pour le Modèle 1) - évite que le
Modèle 2 apprenne une corrélation parasite (proie à proximité → "c'est
Patoune"). Chiffres réels mesurés ce soir-là : Reolink+Manuel = 116
Patoune seul / 156 Patoune+Prey ; Kittyflap = 199 Patoune seul / 3
Patoune+Prey - gap réel à combler côté Reolink avant un premier
entraînement propre, voir l'onglet Photos (filtre "Exclure les
proies").

29.10, tranché avec Sid : crop sur la boîte "Cat" ENTIÈRE (corps + tête),
pas resserré sur le seul museau - la silhouette/pelage porte aussi un
signal d'identité utile. Même boîte utilisée à l'entraînement et à
l'inférence (celle que produira le Modèle 1), pas de logique de
recadrage séparée à maintenir.
"""

import logging
import os
import zipfile

from PIL import Image

from app.boxes import effective_boxes
from app.catalog_db import get_conn, init_db
from app.config import STORAGE_ROOT
from app.labels import IDENTITY_CLASSES, canonicalize

logging.basicConfig(level=logging.INFO, format="%(asctime)s [export_identity_training] %(message)s")
log = logging.getLogger(__name__)

IDENTITY_EXPORTS_DIR = os.path.join(STORAGE_ROOT, "identity_exports")

# 29.10, plan de scission - plancher par chat x source déjà en place côté
# UI ("Tri 200+200"/onglet Équilibrage) : reprend la même cible ici plutôt
# que d'exporter tout l'historique brut (déséquilibré - Patoune a ~2-4x
# plus de captures confirmées que les autres chats pour certaines
# classes). Au-delà de la cible, sélection déterministe (id croissant) -
# pas aléatoire, pour que deux exports successifs sur les mêmes données
# produisent le même lot (reproductible, auditable).
TARGET_PER_SOURCE = 200


def build_identity_training_zip(output_path: str, target_per_source: int = TARGET_PER_SOURCE) -> dict:
    """Construit un zip de crops étiquetés par chat (`<cat_name>/capture_<id>_<bucket>.jpg`),
    plafonné à `target_per_source` par chat x source (kittyflap vs reolink_manual).
    Synchrone, pensée pour être appelée depuis un futur train_vastai_identity.py
    (même principe que build_full_training_zip côté Modèle 1)."""
    init_db()
    tmp_dir = output_path + ".tmp"
    os.makedirs(tmp_dir, exist_ok=True)

    candidates: dict[tuple[str, str], list[tuple[int, str, dict]]] = {}
    skipped_prey_coexist = 0

    with get_conn() as conn:
        rows = conn.execute(
            """SELECT id, source, original_path, detected_objects_json FROM captures
               WHERE review_status IN ('confirmed', 'corrected')"""
        ).fetchall()

        for row in rows:
            boxes = effective_boxes(conn, row["id"], row["detected_objects_json"])
            classes_here = {canonicalize(b.get("label")) for b in boxes} - {None}
            if "Prey" in classes_here:
                skipped_prey_coexist += 1
                continue
            bucket = "kittyflap" if row["source"] == "kittyflap" else "reolink_manual"
            for box in boxes:
                cat_name = canonicalize(box.get("label"))
                if cat_name not in IDENTITY_CLASSES:
                    continue
                candidates.setdefault((cat_name, bucket), []).append(
                    (row["id"], row["original_path"], box)
                )

    counts: dict[str, int] = {}
    included = 0
    skipped_missing_file = 0
    skipped_bad_crop = 0

    for (cat_name, bucket), items in candidates.items():
        # Id croissant = ordre de capture, reproductible (pas de tri aléatoire).
        selected = sorted(items, key=lambda t: t[0])[:target_per_source]
        counts[f"{cat_name} ({bucket})"] = len(selected)
        cat_dir = os.path.join(tmp_dir, cat_name)
        os.makedirs(cat_dir, exist_ok=True)
        for capture_id, original_path, box in selected:
            if not os.path.isfile(original_path):
                skipped_missing_file += 1
                continue
            try:
                with Image.open(original_path) as img:
                    w, h = img.size
                    x0 = max(0, int(box["x"] / 100.0 * w))
                    y0 = max(0, int(box["y"] / 100.0 * h))
                    x1 = min(w, int((box["x"] + box["width"]) / 100.0 * w))
                    y1 = min(h, int((box["y"] + box["height"]) / 100.0 * h))
                    if x1 - x0 < 8 or y1 - y0 < 8:
                        skipped_bad_crop += 1
                        continue
                    crop = img.convert("RGB").crop((x0, y0, x1, y1))
                    crop.save(
                        os.path.join(cat_dir, f"capture_{capture_id}_{bucket}.jpg"),
                        format="JPEG", quality=90,
                    )
                    included += 1
            except Exception as e:
                log.warning("Crop échoué pour capture %s (%s): %s", capture_id, original_path, e)
                skipped_bad_crop += 1

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
        "Export identité : %d crops (%d captures Patoune+Prey exclues, %d fichiers manquants, %d crops invalides)",
        included, skipped_prey_coexist, skipped_missing_file, skipped_bad_crop,
    )
    return {
        "included": included,
        "counts": counts,
        "skipped_prey_coexist": skipped_prey_coexist,
        "skipped_missing_file": skipped_missing_file,
        "skipped_bad_crop": skipped_bad_crop,
    }
