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

# 29.10, plancher par chat x source introduit pour rééquilibrer un
# historique brut déséquilibré (Patoune avait ~2-4x plus de captures
# confirmées que les autres pour certaines classes) - RETIRÉ le 03.11,
# Sid ("supprime la limite de 200 aussi") : au moment où le vrai goulot
# d'étranglement est le volume absolu côté Reolink (155-249 crops/chat
# contre 1800/chat côté Kittyflap), plafonner artificiellement à 200
# jette justement les crops qui manquent le plus. `None` + slicing
# Python natif (`list[:None]` = liste entière) = aucun plafond, tout ce
# qui a été confirmé/corrigé par Sid est exporté.
TARGET_PER_SOURCE = None

# 01.11, Sid ("ça devrait reconnaître Boubou... il est quand même pas
# comme les autres") - diagnostic du soir : les confusions Boubou
# (Reolink) venaient de boîtes "Cat" minuscules (aire médiane 3,9% pour
# Boubou/Reolink contre 8-16% pour les 3 autres), d'où ce filtre à 2.0%.
# RETIRÉ le 03.11, Sid ("je pense plus trop" qu'il faille filtrer) après
# vérification visuelle de crops réellement écartés par ce seuil : à
# 1.6%-2.0% d'aire le chat reste parfaitement reconnaissable (silhouette
# et pelage nets), seul un cas extrême à 0.37% (34x34px) était vraiment
# illisible - ce filtre jetait donc surtout du signal exploitable, pas
# du bruit, alors que le volume Reolink est justement ce qui manque le
# plus. Le garde-fou contre un crop véritablement dégénéré reste plus
# bas (skipped_bad_crop, < 8x8px après recadrage) - suffisant à lui seul
# pour écarter les cas comme celui à 0.37%.


def build_identity_training_zip(output_path: str, target_per_source: int | None = TARGET_PER_SOURCE) -> dict:
    """Construit un zip de crops étiquetés par chat (`<cat_name>/capture_<id>_<bucket>.jpg`),
    optionnellement plafonné à `target_per_source` par chat x source
    (kittyflap vs reolink_manual) - `None` (défaut depuis le 03.11) = pas
    de plafond, tout le pool confirmé/corrigé est exporté.
    Synchrone, pensée pour être appelée depuis un futur train_vastai_identity.py
    (même principe que build_full_training_zip côté Modèle 1)."""
    init_db()
    tmp_dir = output_path + ".tmp"
    os.makedirs(tmp_dir, exist_ok=True)

    candidates: dict[tuple[str, str], list[tuple[int, str, dict]]] = {}
    skipped_prey_coexist = 0

    with get_conn() as conn:
        # 01.11, Sid ("le but c'est vraiment de la correction
        # d'apprentissage... apprends ça en plus") - une correction
        # enregistrée via la file de révision identité (identity_review.py)
        # fait désormais entrer une capture dans l'export même si elle n'a
        # jamais été formellement "confirmed"/"corrected" via le flux de
        # révision générique : corriger une prédiction du Modèle 2 EST en
        # soi la validation humaine qui compte ici.
        corrections = {
            (r["capture_id"], r["object_index"]): r["corrected_name"]
            for r in conn.execute(
                "SELECT capture_id, object_index, corrected_name FROM identity_corrections"
            ).fetchall()
        }
        corrected_capture_ids = {capture_id for capture_id, _ in corrections}

        rows = conn.execute(
            """SELECT id, source, original_path, detected_objects_json FROM captures
               WHERE review_status IN ('confirmed', 'corrected')"""
        ).fetchall()
        if corrected_capture_ids:
            extra_placeholders = ",".join("?" * len(corrected_capture_ids))
            rows += conn.execute(
                f"""SELECT id, source, original_path, detected_objects_json FROM captures
                    WHERE review_status NOT IN ('confirmed', 'corrected')
                      AND id IN ({extra_placeholders})""",
                tuple(corrected_capture_ids),
            ).fetchall()

        for row in rows:
            boxes = effective_boxes(conn, row["id"], row["detected_objects_json"])
            classes_here = {canonicalize(b.get("label")) for b in boxes} - {None}
            if "Prey" in classes_here:
                skipped_prey_coexist += 1
                continue
            bucket = "kittyflap" if row["source"] == "kittyflap" else "reolink_manual"
            for box in boxes:
                correction = corrections.get((row["id"], box["object_index"]))
                cat_name = correction or canonicalize(box.get("label"))
                if cat_name not in IDENTITY_CLASSES:
                    continue
                candidates.setdefault((cat_name, bucket), []).append(
                    (row["id"], row["original_path"], box, correction is not None)
                )

    counts: dict[str, int] = {}
    included = 0
    skipped_missing_file = 0
    skipped_bad_crop = 0
    exported_correction_keys: list[tuple[int, int]] = []

    for (cat_name, bucket), items in candidates.items():
        # 01.11 : les corrections passent toujours en premier (jamais
        # coupées par target_per_source) - une correction explicite de Sid
        # ne doit jamais se faire éliminer par le plafond au profit d'une
        # détection brute non revue. Id croissant ensuite, reproductible.
        selected = sorted(items, key=lambda t: (not t[3], t[0]))[:target_per_source]
        counts[f"{cat_name} ({bucket})"] = len(selected)
        cat_dir = os.path.join(tmp_dir, cat_name)
        os.makedirs(cat_dir, exist_ok=True)
        for capture_id, original_path, box, is_correction in selected:
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
                    if is_correction:
                        exported_correction_keys.append((capture_id, box["object_index"]))
            except Exception as e:
                log.warning("Crop échoué pour capture %s (%s): %s", capture_id, original_path, e)
                skipped_bad_crop += 1

    if exported_correction_keys:
        with get_conn() as conn:
            conn.executemany(
                """UPDATE identity_corrections SET exported_at = datetime('now')
                   WHERE capture_id = ? AND object_index = ?""",
                exported_correction_keys,
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
        "Export identité : %d crops (%d corrections de Sid incluses, %d captures Patoune+Prey exclues, %d fichiers manquants, %d crops invalides)",
        included, len(exported_correction_keys), skipped_prey_coexist, skipped_missing_file, skipped_bad_crop,
    )
    return {
        "included": included,
        "counts": counts,
        "corrections_included": len(exported_correction_keys),
        "skipped_prey_coexist": skipped_prey_coexist,
        "skipped_missing_file": skipped_missing_file,
        "skipped_bad_crop": skipped_bad_crop,
    }
