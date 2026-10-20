"""
Revérification d'un modèle fraîchement entraîné contre la vérité terrain
déjà validée dans Paws Review - portage de
src/model/retroactive_review.py côté Kittyhack (20.10, Sid : "je contrôle
tout au même endroit").

Fait tourner le modèle choisi sur les captures déjà confirmées/corrigées,
compare sa détection dominante à la vérité terrain actuelle (boîtes
effectives - voir app.boxes.effective_boxes, donc déjà avec les
corrections de Sid appliquées) et flague les désaccords dans
model_review_queue pour revue manuelle.

Filtrage volume (20.10, Sid : "pas que j'en aie 60 000 à contrôler") -
même principe que src/model/rfid_ground_truth.py côté Kittyhack : une
capture dont l'identité vient d'une lecture RFID confirmée
(cat_source='rfid') est une vérité terrain DÉJÀ plus fiable que n'importe
quelle inférence fraîche - un désaccord du nouveau modèle dessus est donc
auto-résolu en silence (la vérité terrain RFID l'emporte, rien n'est écrit,
rien n'est flagué) SAUF si Prey est impliqué d'un côté ou de l'autre :
"jamais une proie non détectée ne doit passer entre les gouttes" reste
vrai même avec une identité de chat certaine par puce.
"""

import json
import logging
import os
import threading
import time

import cv2
import numpy as np
import requests

from app.boxes import effective_boxes
from app.catalog_db import get_conn
from app.cat_labels import recompute_cat_labels
from app.config import KITTYHACK_API_TOKEN, KITTYHACK_API_URL, STORAGE_ROOT
from app.labels import CLASSES, canonicalize

log = logging.getLogger("model_review")

STATE_PATH = os.path.join(STORAGE_ROOT, "model_review_state.json")
MODELS_DIR = os.environ.get("KITTYHACK_MODELS_DIR", "/kittyhack_models")
_run_lock = threading.Lock()

# 20.10, Sid ("l'analyse chat/proie/ouverture de porte doit toujours être
# prioritaire sur tout le reste") - même garde-fou que
# src/model/retroactive_review.py._wait_for_live_loop_idle() côté Kittyhack,
# mais Paws Review tourne dans un conteneur séparé (ne peut pas importer
# l'état mémoire du handler live) donc poll de GET /api/v1/detection/status
# (voir src/api.py côté kittyhack-remote) à la place d'un import direct.
# Vérifié entre chaque image du scan, même cadence que le module d'origine.
_DETECTION_POLL_INTERVAL_S = 0.5


def _wait_for_detection_idle(cancel_check=None) -> bool:
    """Bloque pendant que la boucle de détection live (chat/proie/porte) est
    active, pour toujours lui céder le GPU partagé. Retourne False si annulé
    pendant l'attente, True dès que c'est sûr de continuer (y compris quand
    KITTYHACK_API_URL n'est pas configuré, ou que l'appel échoue - ne jamais
    bloquer indéfiniment le scan sur un statut manquant/cassé)."""
    if not KITTYHACK_API_URL:
        return True
    while True:
        try:
            r = requests.get(
                f"{KITTYHACK_API_URL}/api/v1/detection/status",
                headers={"Authorization": f"Bearer {KITTYHACK_API_TOKEN}"},
                timeout=3,
            )
            r.raise_for_status()
            busy = bool(r.json().get("busy"))
        except Exception as e:
            log.debug("detection/status indisponible, on ne bloque pas : %s", e)
            return True
        if not busy:
            return True
        if cancel_check is not None and cancel_check():
            return False
        time.sleep(_DETECTION_POLL_INTERVAL_S)


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


def list_model_dirs() -> list[str]:
    """Dossiers de modèles disponibles (basename), triés du plus récent au
    plus ancien - alimente le sélecteur du frontend."""
    if not os.path.isdir(MODELS_DIR):
        return []
    entries = [
        d for d in os.listdir(MODELS_DIR)
        if os.path.isfile(os.path.join(MODELS_DIR, d, "model.pt"))
    ]
    entries.sort(key=lambda d: os.path.getmtime(os.path.join(MODELS_DIR, d)), reverse=True)
    return entries


def _dominant_label(boxes: list[dict]) -> tuple[str | None, float, int | None, dict | None]:
    """Détection dominante parmi des boîtes effectives/prédites : Prey en
    priorité si présent (même logique de prudence que partout ailleurs ce
    soir - une proie ne doit jamais être éclipsée par un chat mieux noté),
    sinon la plus haute probabilité. Renvoie (label, probability,
    object_index_ou_None, box_dict_ou_None)."""
    best_prey = None
    best_other = None
    for b in boxes:
        label = canonicalize(b.get("label") or b.get("object_name"))
        if label is None:
            continue
        prob = float(b.get("probability") or 0.0)
        entry = (label, prob, b.get("object_index"), b)
        if label == "Prey":
            if best_prey is None or prob > best_prey[1]:
                best_prey = entry
        else:
            if best_other is None or prob > best_other[1]:
                best_other = entry
    chosen = best_prey or best_other
    if chosen is None:
        return None, 0.0, None, None
    return chosen[0], chosen[1], chosen[2], chosen[3]


def _predict_boxes(model, image_path: str) -> list[dict]:
    # 20.10, Sid ("je veux le GPU partout où c'est possible") - device=
    # explicite obligatoire, "AUTO" (le défaut si rien n'est passé) choisit
    # le CPU en silence sur ce matériel même quand le GPU est bien détecté
    # - vérifié en vrai cette nuit côté kittyhack-remote : 1759ms/image en
    # AUTO contre 65ms/image avec device='intel:gpu' forcé, même modèle,
    # même image.
    results = model(image_path, verbose=False, device="intel:gpu")
    r = results[0]
    h, w = r.orig_shape
    boxes = []
    names = r.names
    for box in r.boxes:
        x1, y1, x2, y2 = box.xyxy[0].tolist()
        boxes.append({
            "label": names[int(box.cls[0])],
            "probability": float(box.conf[0]) * 100.0,
            "x": x1 / w * 100.0, "y": y1 / h * 100.0,
            "width": (x2 - x1) / w * 100.0, "height": (y2 - y1) / h * 100.0,
        })
    return boxes


def _next_unscanned_captures(conn, model_version: str, limit: int) -> list:
    return conn.execute(
        """SELECT c.id, c.block_id, c.original_path, c.detected_objects_json, c.cat_source
           FROM captures c
           WHERE c.review_status IN ('confirmed', 'corrected')
             AND NOT EXISTS (
                 SELECT 1 FROM model_review_scanned s
                 WHERE s.capture_id = c.id AND s.model_version = ?
             )
           ORDER BY c.created_at ASC
           LIMIT ?""",
        (model_version, limit),
    ).fetchall()


def _ensure_openvino_export(model_path: str) -> str:
    """Exporte .pt vers OpenVINO IR si pas déjà fait (même logique que
    model_handler.py côté Kittyhack - voir sa fonction
    _ensure_openvino_model_export) - une fois par modèle, mis en cache à
    côté. 20.10, Sid ("tout ce qu'il faut pour soulager le CPU") : paws-
    review a maintenant le même accès GPU que kittyhack-remote (privileged
    + /dev/dri, voir compose.yaml), même piège déjà vécu cette nuit à
    éviter : NE JAMAIS laisser pip mettre à jour openvino au-delà de
    2023.3.0 sur ce matériel (Celeron J4125) - voir YOLO_AUTOINSTALL=False
    dans l'environnement du conteneur."""
    from ultralytics import YOLO

    openvino_dir = model_path.replace(".pt", "_openvino_model")
    if os.path.isdir(openvino_dir) and os.path.isfile(os.path.join(openvino_dir, "model.bin")):
        return openvino_dir
    log.info("Export OpenVINO manquant pour %s, export en cours...", model_path)
    model = YOLO(model_path)
    model.export(format="openvino")
    return openvino_dir


def run_model_review(model_version: str, num_captures: int = 200, cancel_event=None) -> dict:
    from ultralytics import YOLO

    model_path = os.path.join(MODELS_DIR, model_version, "model.pt")
    if not os.path.isfile(model_path):
        msg = f"Modèle introuvable : {model_path}"
        log.error(msg)
        _write_state({"status": "error", "message": msg})
        return {"status": "error", "message": msg}

    _write_state({"status": "running", "step": "loading_model", "model_version": model_version})
    openvino_dir = _ensure_openvino_export(model_path)
    # Jamais passer device= explicitement pour un modèle OpenVINO -
    # Ultralytics fige device_name="AUTO" en interne, qui choisit déjà le
    # GPU Intel s'il est disponible (voir model_handler.py côté Kittyhack,
    # même piège déjà rencontré cette nuit).
    model = YOLO(openvino_dir, task="detect")

    with get_conn() as conn:
        rows = _next_unscanned_captures(conn, model_version, num_captures)

    if not rows:
        _write_state({
            "status": "success", "finished_at": time.time(),
            "model_version": model_version, "scanned": 0, "flagged": 0, "all_scanned": True,
        })
        return {"scanned": 0, "flagged": 0, "all_scanned": True}

    scanned = 0
    flagged = 0  # groupes créés (= tuiles à trancher), pas le nombre de frames
    grouped_frames = 0
    auto_resolved = 0
    t0 = time.time()
    # 20.10, Sid ("pas trop de vérifications sur des trucs identiques") -
    # un même désaccord (même bloc de mouvement, même ancienne/nouvelle
    # étiquette) ne crée qu'UNE tuile (model_review_queue), toutes ses
    # frames deviennent membres de ce même groupe (model_review_queue_members).
    # Sans block_id (Reolink notamment, pas toujours groupé), chaque capture
    # reste son propre groupe plutôt que de risquer de fusionner des photos
    # sans rapport entre elles.
    group_queue_ids: dict = {}

    for row in rows:
        if cancel_event is not None and cancel_event.is_set():
            break

        _write_state({
            "status": "running", "step": "scanning", "model_version": model_version,
            "scanned": scanned, "flagged": flagged, "total": len(rows),
            "elapsed_s": time.time() - t0,
        })

        if not os.path.isfile(row["original_path"]):
            # Image manquante sur disque - on la marque scannée quand même,
            # pas la peine d'y revenir à chaque run tant que le fichier
            # n'est pas là.
            with get_conn() as conn:
                conn.execute(
                    "INSERT OR IGNORE INTO model_review_scanned (capture_id, model_version) VALUES (?, ?)",
                    (row["id"], model_version),
                )
            scanned += 1
            continue

        with get_conn() as conn:
            old_boxes = effective_boxes(conn, row["id"], row["detected_objects_json"])
        old_label, old_prob, old_idx, _ = _dominant_label(old_boxes)

        # 20.10, Sid ("les images vides où il n'y a pas de boîte, il ne
        # faut pas qu'elles ressortent") - aucune vérité terrain à comparer,
        # donc rien à "désaccorder" ici (que le nouveau modèle trouve
        # quelque chose ou rien n'a pas de sens sans référence) - marquée
        # scannée sans jamais tourner l'inférence, pas la peine du coût.
        if old_label is None:
            with get_conn() as conn:
                conn.execute(
                    "INSERT OR IGNORE INTO model_review_scanned (capture_id, model_version) VALUES (?, ?)",
                    (row["id"], model_version),
                )
            scanned += 1
            continue

        cancel_check = (lambda: cancel_event.is_set()) if cancel_event is not None else None
        if not _wait_for_detection_idle(cancel_check):
            break

        try:
            new_boxes = _predict_boxes(model, row["original_path"])
        except Exception as e:
            log.warning("Inférence échouée pour la capture %s : %s", row["id"], e)
            scanned += 1
            continue
        new_label, new_prob, _, new_box = _dominant_label(new_boxes)

        with get_conn() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO model_review_scanned (capture_id, model_version) VALUES (?, ?)",
                (row["id"], model_version),
            )
            scanned += 1

            if new_label == old_label:
                continue  # accord - rien à faire, rien à flaguer

            prey_involved = old_label == "Prey" or new_label == "Prey"
            if row["cat_source"] == "rfid" and not prey_involved:
                # Vérité terrain RFID plus fiable qu'une inférence fraîche -
                # désaccord ignoré en silence, jamais écrit, jamais flagué.
                auto_resolved += 1
                continue

            if new_box is None:
                # Le nouveau modèle ne voit plus rien du tout là où il y
                # avait une détection - pas assez d'info pour proposer une
                # boîte de remplacement, à vérifier visuellement par Sid
                # directement sur la capture plutôt que de deviner une
                # géométrie ici.
                continue

            group_key = (
                (row["block_id"], old_label, new_label) if row["block_id"] is not None
                else ("solo", row["id"])
            )
            queue_id = group_queue_ids.get(group_key)
            if queue_id is None:
                cur = conn.execute(
                    """INSERT INTO model_review_queue
                           (capture_id, model_version, old_label, old_probability,
                            new_label, new_probability, new_x, new_y, new_width, new_height)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (row["id"], model_version, old_label, old_prob,
                     new_label, new_prob, new_box["x"], new_box["y"], new_box["width"], new_box["height"]),
                )
                queue_id = cur.lastrowid
                group_queue_ids[group_key] = queue_id
                flagged += 1
            conn.execute(
                "INSERT INTO model_review_queue_members (queue_id, capture_id, object_index) VALUES (?,?,?)",
                (queue_id, row["id"], old_idx),
            )
            grouped_frames += 1

    _write_state({
        "status": "success", "finished_at": time.time(), "model_version": model_version,
        "scanned": scanned, "flagged": flagged, "grouped_frames": grouped_frames,
        "auto_resolved": auto_resolved, "all_scanned": len(rows) < num_captures,
    })
    return {"scanned": scanned, "flagged": flagged, "grouped_frames": grouped_frames, "auto_resolved": auto_resolved}


def start_model_review_async(model_version: str, num_captures: int = 200) -> bool:
    if not _run_lock.acquire(blocking=False):
        return False
    thread = threading.Thread(
        target=_run_model_review_locked, args=(model_version, num_captures), daemon=True,
    )
    thread.start()
    return True


def _run_model_review_locked(model_version: str, num_captures: int):
    try:
        run_model_review(model_version, num_captures)
    except Exception as e:
        log.exception("Revérification échouée")
        _write_state({"status": "error", "message": str(e)})
    finally:
        _run_lock.release()


def _member_box_geom(conn, capture_id: int, object_index: int | None) -> dict | None:
    """Géométrie ACTUELLE (propre à cette capture précise) de la boîte à
    corriger - jamais celle de la représentante du groupe, qui appartient à
    une autre image. None si cette capture n'avait aucune boîte à cet
    index (cas "nouvel objet jamais vu" - seule la représentante a une
    géométrie proposée utilisable dans ce cas)."""
    if object_index is None:
        return None
    capture = conn.execute(
        "SELECT detected_objects_json FROM captures WHERE id = ?", (capture_id,)
    ).fetchone()
    if capture is None:
        return None
    boxes = effective_boxes(conn, capture_id, capture["detected_objects_json"])
    for b in boxes:
        if b["object_index"] == object_index:
            return {"x": b["x"], "y": b["y"], "width": b["width"], "height": b["height"]}
    return None


def resolve_queue_items(ids: list[int], action: str) -> int:
    """action: 'apply' (corrige l'étiquette de chaque frame du groupe, en
    gardant la géométrie PROPRE à chaque frame - jamais celle de la
    représentante) ou 'dismiss' (garde la vérité terrain actuelle telle
    quelle, rien n'est écrit). Un groupe = une tuile = potentiellement
    plusieurs frames résolues d'un coup (voir model_review_queue_members)."""
    if action not in ("apply", "dismiss"):
        raise ValueError("action invalide")
    count = 0
    with get_conn() as conn:
        for qid in ids:
            row = conn.execute(
                "SELECT * FROM model_review_queue WHERE id = ? AND status = 'pending'", (qid,)
            ).fetchone()
            if row is None:
                continue
            members = conn.execute(
                "SELECT capture_id, object_index FROM model_review_queue_members WHERE queue_id = ?",
                (qid,),
            ).fetchall()
            if action == "apply":
                for m in members:
                    object_index = m["object_index"]
                    geom = _member_box_geom(conn, m["capture_id"], object_index)
                    if geom is None:
                        if object_index is not None:
                            continue  # boîte attendue mais introuvable - on ne devine pas sa géométrie
                        # Aucune boîte avant sur CETTE frame : seule
                        # géométrie disponible est celle de la représentante
                        # du groupe - approximation acceptée plutôt que de
                        # ne rien faire, mais seulement dans ce cas précis.
                        geom = {"x": row["new_x"], "y": row["new_y"], "width": row["new_width"], "height": row["new_height"]}
                        existing = conn.execute(
                            "SELECT COALESCE(MAX(object_index), -1) AS m FROM box_annotations WHERE capture_id = ?",
                            (m["capture_id"],),
                        ).fetchone()
                        model_boxes = conn.execute(
                            "SELECT detected_objects_json FROM captures WHERE id = ?", (m["capture_id"],)
                        ).fetchone()
                        base = len(json.loads(model_boxes["detected_objects_json"] or "[]"))
                        object_index = max(existing["m"] + 1, base)
                    conn.execute(
                        """INSERT INTO box_annotations
                               (capture_id, object_index, source, label, x, y, width, height, confirmed, ignored)
                           VALUES (?,?,?,?,?,?,?,?,1,0)
                           ON CONFLICT(capture_id, object_index) DO UPDATE SET
                               label = excluded.label, x = excluded.x, y = excluded.y,
                               width = excluded.width, height = excluded.height,
                               source = excluded.source, confirmed = 1, ignored = 0""",
                        (m["capture_id"], object_index, "model", row["new_label"],
                         geom["x"], geom["y"], geom["width"], geom["height"]),
                    )
                    recompute_cat_labels(conn, m["capture_id"])
            conn.execute(
                "UPDATE model_review_queue SET status = ?, resolved_at = datetime('now') WHERE id = ?",
                ("applied" if action == "apply" else "dismissed", qid),
            )
            count += 1
    return count
