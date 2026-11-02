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

# 01.11, Sid ("ça devrait reconnaître Boubou... il est quand même pas
# comme les autres") - diagnostic en vrai ce soir : les confusions
# Boubou (Reolink) venaient de boîtes "Cat" minuscules (chat loin de la
# caméra extérieure), pas d'une histoire de couleur/pelage - aire
# médiane mesurée 3,9% pour Boubou/Reolink contre 8-16% pour les 3
# autres chats. Un crop de quelques pixels une fois agrandi à 224x224
# n'apporte quasiment aucun signal utile - pire, ça peut polluer
# l'apprentissage avec du bruit qui ressemble à n'importe quoi. Seuil
# vérifié sur les vraies données avant de le fixer : à 2.0%, il reste
# 155-202 crops par chat x source (sur 200 ciblés) - jamais un chat
# qui tombe à sec, juste les pires crops écartés.
MIN_BOX_AREA_PCT = 2.0


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
    skipped_too_small = 0

    with get_conn() as conn:
        # 01.11, Sid ("le but c'est vraiment de la correction
        # d'apprentissage... apprends ça en plus") - une capture corrigée
        # via la file de révision identité (identity_review.py, qui passe
        # par le même éditeur de boîtes que Photos/Révision -
        # /app-api/captures/{id}/boxes) entre dans l'export même si elle
        # n'a jamais été formellement "confirmed"/"corrected" dans
        # review_status : avoir une boîte explicitement revue par Sid EST
        # la validation humaine qui compte ici. box["confirmed"] (voir
        # boxes.py:effective_boxes) dit déjà, boîte par boîte, si une
        # ligne box_annotations existe pour elle - aucune table séparée à
        # tenir synchronisée.
        any_reviewed_box = {
            r["capture_id"]
            for r in conn.execute("SELECT DISTINCT capture_id FROM box_annotations").fetchall()
        }

        rows = conn.execute(
            """SELECT id, source, original_path, detected_objects_json FROM captures
               WHERE review_status IN ('confirmed', 'corrected')"""
        ).fetchall()
        if any_reviewed_box:
            placeholders = ",".join("?" * len(any_reviewed_box))
            rows += conn.execute(
                f"""SELECT id, source, original_path, detected_objects_json FROM captures
                    WHERE review_status NOT IN ('confirmed', 'corrected')
                      AND id IN ({placeholders})""",
                tuple(any_reviewed_box),
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
                if box["width"] * box["height"] / 100.0 < MIN_BOX_AREA_PCT:
                    skipped_too_small += 1
                    continue
                candidates.setdefault((cat_name, bucket), []).append(
                    (row["id"], row["original_path"], box, box["confirmed"])
                )

    counts: dict[str, int] = {}
    included = 0
    skipped_missing_file = 0
    skipped_bad_crop = 0
    reviewed_included = 0

    for (cat_name, bucket), items in candidates.items():
        # 01.11 : les boîtes explicitement revues par Sid (box["confirmed"],
        # qu'elles aient changé de nom ou juste été validées) passent
        # toujours en premier - jamais coupées par target_per_source au
        # profit d'une détection brute jamais revue. Id croissant ensuite,
        # reproductible.
        selected = sorted(items, key=lambda t: (not t[3], t[0]))[:target_per_source]
        counts[f"{cat_name} ({bucket})"] = len(selected)
        cat_dir = os.path.join(tmp_dir, cat_name)
        os.makedirs(cat_dir, exist_ok=True)
        for capture_id, original_path, box, is_reviewed in selected:
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
                    if is_reviewed:
                        reviewed_included += 1
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
        "Export identité : %d crops (%d boîtes explicitement revues par Sid, %d captures Patoune+Prey exclues, %d boîtes trop petites <%.1f%%, %d fichiers manquants, %d crops invalides)",
        included, reviewed_included, skipped_prey_coexist, skipped_too_small, MIN_BOX_AREA_PCT, skipped_missing_file, skipped_bad_crop,
    )
    return {
        "included": included,
        "counts": counts,
        "reviewed_included": reviewed_included,
        "skipped_prey_coexist": skipped_prey_coexist,
        "skipped_too_small": skipped_too_small,
        "skipped_missing_file": skipped_missing_file,
        "skipped_bad_crop": skipped_bad_crop,
    }
