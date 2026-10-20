"""
Construit un zip YOLO (images/ + labels/ + classes.txt) à partir des
captures confirmées/corrigées de Paws Review, pour upload manuel dans la
page "AI Training" de Kittyhack (qui accepte déjà un zip arbitraire en
plus de l'export LabelStudio - confirmé le 18.10 dans
server_ui/ai_training.py, input.model_training_data()).

18.10, Sid : "un bouton qui lance l'export des photos validées vers le
nouveau container... et une notif dans Paws Review qui dit que le zip est
prêt." Tourne en arrière-plan (thread, voir main.py) - un vrai 502 déjà
vécu côté Kittyhack sur ce même genre d'export bloquant (13.10,
labelstudio_api.py) a appris à ne jamais faire ça en synchrone dans une
requête HTTP.

Négatifs explicites (18.10, validé avec Sid) : une capture confirmée dont
TOUTES les boîtes sont exclues (aucune, ou seulement "Autre"/un label
inconnu) part quand même dans le zip, avec un fichier label VIDE - sert de
négatif explicite ("rien ici"), contrairement à l'export LabelStudio
historique qui jetait purement et simplement ces cas
(skipped_no_boxes, voir labelstudio_api.py).
"""

import json
import logging
import os
import threading
import time
import zipfile
from datetime import datetime, timezone

from app.boxes import effective_boxes
from app.catalog_db import get_conn, init_db
from app.config import STORAGE_ROOT
from app.labels import CLASSES, canonicalize

logging.basicConfig(level=logging.INFO, format="%(asctime)s [export_training] %(message)s")
log = logging.getLogger(__name__)

EXPORTS_DIR = os.path.join(STORAGE_ROOT, "exports")
STATE_PATH = os.path.join(STORAGE_ROOT, "export_state.json")

_lock = threading.Lock()


def _write_state(state: dict):
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f)
    os.replace(tmp, STATE_PATH)


def read_state() -> dict:
    if not os.path.isfile(STATE_PATH):
        return {"status": "idle"}
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"status": "idle"}


def start_export_async():
    """Lance l'export en arrière-plan si aucun n'est déjà en cours ;
    no-op sinon (évite deux exports concurrents qui se marcheraient
    dessus sur les mêmes captures/exported_at)."""
    if not _lock.acquire(blocking=False):
        log.info("Export déjà en cours, requête ignorée")
        return
    thread = threading.Thread(target=_run_export_locked, daemon=True)
    thread.start()


def _run_export_locked():
    try:
        _run_export()
    except Exception as e:
        log.exception("Export échoué")
        _write_state({"status": "error", "message": str(e)})
    finally:
        _lock.release()


def _run_export():
    started_at = datetime.now(timezone.utc).isoformat()
    _write_state({"status": "running", "started_at": started_at, "done": 0})
    log.info("Export démarré")

    os.makedirs(EXPORTS_DIR, exist_ok=True)
    init_db()

    with get_conn() as conn:
        rows = conn.execute(
            """SELECT id, original_path, detected_objects_json FROM captures
               WHERE review_status IN ('confirmed', 'corrected') AND exported_at IS NULL"""
        ).fetchall()
        # 18.10, Sid ("quand j'exporte, les deux [onglets] se vident") -
        # les écartées ne vont jamais dans le zip (rien à y gagner), mais
        # sortent quand même du filet de sécurité "Écartées" au même
        # moment : exporter marque la session de revue comme close, dans
        # les deux sens. Marquées à part, tout de suite, puisqu'elles ne
        # passent par aucune des étapes fichier/zip plus bas.
        rejected_ids = [
            r["id"] for r in conn.execute(
                "SELECT id FROM captures WHERE review_status = 'rejected' AND exported_at IS NULL"
            ).fetchall()
        ]
        if rejected_ids:
            conn.executemany(
                "UPDATE captures SET exported_at = datetime('now') WHERE id = ?",
                [(cid,) for cid in rejected_ids],
            )

    total = len(rows)
    if total == 0:
        _write_state({
            "status": "ready", "started_at": started_at,
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "images": 0, "positives": 0, "negatives": 0, "zip_filename": None,
            "message": "Rien à exporter - aucune nouvelle capture validée/corrigée.",
        })
        log.info("Rien à exporter (%d écartées archivées au passage)", len(rejected_ids))
        return

    ts = time.strftime("%Y%m%d_%H%M%S")
    zip_filename = f"training_{ts}.zip"
    zip_path = os.path.join(EXPORTS_DIR, zip_filename)
    tmp_dir = os.path.join(EXPORTS_DIR, f".tmp_{ts}")
    images_dir = os.path.join(tmp_dir, "images")
    labels_dir = os.path.join(tmp_dir, "labels")
    os.makedirs(images_dir, exist_ok=True)
    os.makedirs(labels_dir, exist_ok=True)

    positives = negatives = 0
    skipped_boxes_by_class: dict[str, int] = {}
    exported_ids = []

    with get_conn() as conn:
        for i, row in enumerate(rows):
            if i % 200 == 0:
                _write_state({"status": "running", "started_at": started_at, "done": i, "total": total})

            boxes = effective_boxes(conn, row["id"], row["detected_objects_json"])
            lines = []
            has_excluded = False
            for box in boxes:
                cls = canonicalize(box["label"])
                if cls is None:
                    has_excluded = True
                    skipped_boxes_by_class[box["label"]] = skipped_boxes_by_class.get(box["label"], 0) + 1
                    continue
                cls_idx = CLASSES.index(cls)
                x_center = (box["x"] + box["width"] / 2.0) / 100.0
                y_center = (box["y"] + box["height"] / 2.0) / 100.0
                w = box["width"] / 100.0
                h = box["height"] / 100.0
                lines.append(f"{cls_idx} {x_center:.6f} {y_center:.6f} {w:.6f} {h:.6f}")

            if not os.path.isfile(row["original_path"]):
                continue  # fichier déjà purgé entre temps - rien à exporter pour celle-ci

            base_name = f"capture_{row['id']}"
            ext = os.path.splitext(row["original_path"])[1] or ".jpg"
            with open(row["original_path"], "rb") as src, \
                 open(os.path.join(images_dir, base_name + ext), "wb") as dst:
                dst.write(src.read())
            with open(os.path.join(labels_dir, base_name + ".txt"), "w") as f:
                f.write("\n".join(lines) + ("\n" if lines else ""))

            if lines:
                positives += 1
            else:
                negatives += 1
            exported_ids.append(row["id"])

        with open(os.path.join(tmp_dir, "classes.txt"), "w") as f:
            f.write("\n".join(CLASSES) + "\n")

        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for root, _dirs, files in os.walk(tmp_dir):
                for fname in files:
                    abs_path = os.path.join(root, fname)
                    arc_name = os.path.relpath(abs_path, tmp_dir)
                    zf.write(abs_path, arc_name)

        # 18.10 : marquées exportées seulement maintenant, APRÈS un zip
        # écrit avec succès - une erreur plus haut (disque plein, etc.)
        # laisse exported_at à NULL, donc rien n'est perdu, le prochain
        # export les reprend normalement.
        conn.executemany(
            "UPDATE captures SET exported_at = datetime('now') WHERE id = ?",
            [(cid,) for cid in exported_ids],
        )

    for root, _dirs, files in os.walk(tmp_dir, topdown=False):
        for fname in files:
            os.remove(os.path.join(root, fname))
        os.rmdir(root)

    if skipped_boxes_by_class:
        log.info("Labels exclus de l'export (hors classes du modèle) : %s", skipped_boxes_by_class)

    _write_state({
        "status": "ready",
        "started_at": started_at,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "images": len(exported_ids),
        "positives": positives,
        "negatives": negatives,
        "skipped_boxes_by_class": skipped_boxes_by_class,
        "zip_filename": zip_filename,
    })
    log.info(
        "Export terminé : %d images (%d positifs, %d négatifs) -> %s",
        len(exported_ids), positives, negatives, zip_filename,
    )


def build_full_training_zip(output_path: str) -> dict:
    """19.10, Sid ("on renvoie chaque fois le tout... pour repartir des
    poids du modèle précédent mais sans risque d'oubli") - contrairement à
    _run_export() ci-dessus (lié au bouton de l'UI, qui ne reprend que les
    captures jamais encore exportées et les marque ensuite), celle-ci
    reprend TOUT ce qui est validé/corrigé à chaque appel, sans jamais
    rien marquer - pensée pour un entraînement vast.ai répété qui doit
    toujours voir l'intégralité du jeu de données, pas juste le delta.
    Synchrone (appelée depuis train_vastai.py, un script, pas une requête
    HTTP) - la même construction que _run_export() mais sans thread/état
    partagé, inutiles ici.

    19.10, Sid ("équilibrer la provenance des deux caméras" + "augmenter
    mon taux [de Prey] à un niveau identique aux chats") - Prey reste la
    classe la plus rare et la plus dure (petit objet, souvent caché par le
    chat - voir la discussion sur la résolution d'entraînement), ET très
    déséquilibrée entre kittyflap et reolink. Suréchantillonnage en deux
    temps, jamais de fabrication d'image (juste des copies des VRAIES
    photos déjà validées, avec un nom de fichier distinct à chaque copie -
    les autres augmentations Ultralytics actives par défaut, mosaïque
    incluse, évitent que le modèle voie littéralement deux fois la même
    chose) :
      1. kittyflap vs reolink ramenés à parité entre eux (la source la
         moins fournie est dupliquée jusqu'à égaler l'autre).
      2. Le total Prey (toutes provenances) est ensuite remonté au niveau
         moyen des 4 classes de chats, en recyclant les mêmes photos."""
    init_db()
    tmp_dir = output_path + ".tmp"
    images_dir = os.path.join(tmp_dir, "images")
    labels_dir = os.path.join(tmp_dir, "labels")
    os.makedirs(images_dir, exist_ok=True)
    os.makedirs(labels_dir, exist_ok=True)

    positives = negatives = 0
    skipped_boxes_by_class: dict[str, int] = {}
    included = 0
    cat_class_counts: dict[str, int] = {c: 0 for c in CLASSES if c != "Prey"}
    prey_rows_by_source: dict[str, list[str]] = {}  # base_name -> source, grouped

    def _write_sample(base_name: str, img_bytes: bytes, lines: list[str]):
        with open(os.path.join(images_dir, base_name + ".jpg"), "wb") as f:
            f.write(img_bytes)
        with open(os.path.join(labels_dir, base_name + ".txt"), "w") as f:
            f.write("\n".join(lines) + ("\n" if lines else ""))

    with get_conn() as conn:
        rows = conn.execute(
            """SELECT id, source, original_path, detected_objects_json FROM captures
               WHERE review_status IN ('confirmed', 'corrected')"""
        ).fetchall()

        prey_originals: dict[str, list[tuple[str, bytes, list[str]]]] = {}  # source -> [(base_name, img_bytes, lines)]

        for row in rows:
            boxes = effective_boxes(conn, row["id"], row["detected_objects_json"])
            lines = []
            classes_here = set()
            for box in boxes:
                cls = canonicalize(box["label"])
                if cls is None:
                    skipped_boxes_by_class[box["label"]] = skipped_boxes_by_class.get(box["label"], 0) + 1
                    continue
                classes_here.add(cls)
                cls_idx = CLASSES.index(cls)
                x_center = (box["x"] + box["width"] / 2.0) / 100.0
                y_center = (box["y"] + box["height"] / 2.0) / 100.0
                w = box["width"] / 100.0
                h = box["height"] / 100.0
                lines.append(f"{cls_idx} {x_center:.6f} {y_center:.6f} {w:.6f} {h:.6f}")

            if not os.path.isfile(row["original_path"]):
                continue

            base_name = f"capture_{row['id']}"
            with open(row["original_path"], "rb") as src:
                img_bytes = src.read()
            _write_sample(base_name, img_bytes, lines)

            for cls in classes_here:
                if cls in cat_class_counts:
                    cat_class_counts[cls] += 1
            if "Prey" in classes_here:
                prey_originals.setdefault(row["source"], []).append((base_name, img_bytes, lines))

            if lines:
                positives += 1
            else:
                negatives += 1
            included += 1

    # --- Suréchantillonnage Prey : 1) parité kittyflap/reolink ---
    kf = prey_originals.get("kittyflap", [])
    rl = prey_originals.get("reolink", [])
    prey_total_now = sum(len(v) for v in prey_originals.values())
    target_each = max(len(kf), len(rl))
    for name, samples in (("kittyflap", kf), ("reolink", rl)):
        if not samples:
            continue
        dup_i = 0
        while len(samples) + dup_i < target_each:
            base_name, img_bytes, lines = samples[dup_i % len(samples)]
            dup_name = f"{base_name}_dup{name}{dup_i}"
            _write_sample(dup_name, img_bytes, lines)
            included += 1
            positives += 1
            prey_total_now += 1
            dup_i += 1

    # --- 2) total Prey remonté au niveau moyen des classes de chats ---
    all_prey_samples = [s for src_list in prey_originals.values() for s in src_list]
    cat_counts = [n for n in cat_class_counts.values() if n > 0]
    target_prey_total = int(sum(cat_counts) / len(cat_counts)) if cat_counts else 0

    if all_prey_samples and prey_total_now < target_prey_total:
        missing = target_prey_total - prey_total_now
        for i in range(missing):
            base_name, img_bytes, lines = all_prey_samples[i % len(all_prey_samples)]
            dup_name = f"{base_name}_dupall{i}"
            _write_sample(dup_name, img_bytes, lines)
            included += 1
            positives += 1

    with open(os.path.join(tmp_dir, "classes.txt"), "w") as f:
        f.write("\n".join(CLASSES) + "\n")

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
        "Export complet (vast.ai) : %d images (%d positifs, %d négatifs)",
        included, positives, negatives,
    )
    return {"images": included, "positives": positives, "negatives": negatives,
            "skipped_boxes_by_class": skipped_boxes_by_class}
