import json
import logging
import os
import time
import zipfile

from fastapi import Body, FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.types import Scope

from app.boxes import effective_boxes
from app.cat_labels import recompute_cat_labels
from app.catalog_db import get_conn, init_db
from app.config import RETENTION_DAYS, STORAGE_ROOT
from app.export_training import EXPORTS_DIR, read_state, start_export_async
from app.labels import CLASSES, canonicalize
from app.model_review import list_model_dirs, resolve_queue_items, start_model_review_async
from app.model_review import read_state as read_model_review_state
from app.train_vastai import read_state as read_training_state
from app.train_vastai import start_training_async

app = FastAPI(title="Paws Review")

init_db()


@app.get("/app-api/cats")
def list_cats():
    # Une photo compte pour CHAQUE chat qu'elle contient réellement (boîtes
    # effectives, voir cat_labels.py) - pas seulement celui identifié par RFID,
    # pour ne pas perdre une double détection avec deux chats différents.
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT ccl.cat_name AS name, COUNT(*) AS total,
                      SUM(c.review_status = 'pending') AS pending
               FROM capture_cat_labels ccl
               JOIN captures c ON c.id = ccl.capture_id
               GROUP BY ccl.cat_name
               ORDER BY ccl.cat_name"""
        ).fetchall()
        return [dict(r) for r in rows]


# 21.10, Sid ("je repars encore une fois à zéro... 200 tag reolink et 200
# tag HK par chat... trier reo vs HK pour pouvoir faire du différent de
# tous les côtés possibles") - les 1819 captures réellement utilisées pour
# entraîner le modèle actuellement actif (pas 1874 : ce chiffre inclut 55
# doublons volontaires "_dupkittyflapN" pour suréchantillonner certaines
# images HK à l'entraînement - jamais de nouvelles images). Lu directement
# depuis le zip envoyé à vast.ai à chaque appel plutôt que figé en base -
# reste juste tout seul si un nouvel entraînement écrase ce même fichier.
def _current_model_capture_ids() -> set[int]:
    import re
    if not os.path.isfile(TRAINING_DATA_PATH):
        return set()
    try:
        with zipfile.ZipFile(TRAINING_DATA_PATH) as z:
            ids = set()
            for name in z.namelist():
                m = re.match(r"images/capture_(\d+)", name)
                if m:
                    ids.add(int(m.group(1)))
            return ids
    except Exception as e:
        logging.warning(f"Impossible de lire {TRAINING_DATA_PATH}: {e}")
        return set()


@app.get("/app-api/training-targets/counts")
def training_targets_counts():
    """Pour chaque chat (+ Prey, + Negatif), le compte HK/Reolink+Manuel,
    séparément pour le modèle actuel (1819) et le nouveau depuis (pas
    encore exporté) - alimente la page de tri 200+200 par chat."""
    current_ids = _current_model_capture_ids()
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT c.id, c.source, c.detected_objects_json
               FROM captures c
               WHERE c.review_status IN ('confirmed', 'corrected')"""
        ).fetchall()
        groups = {c: {"current": {"kittyflap": 0, "reolink_manual": 0}, "new": {"kittyflap": 0, "reolink_manual": 0}}
                  for c in list(CLASSES) + ["Negatif"]}
        for row in rows:
            boxes = effective_boxes(conn, row["id"], row["detected_objects_json"])
            classes_here = {canonicalize(b.get("label")) for b in boxes} - {None}
            if not classes_here:
                classes_here = {"Negatif"}
            bucket = "kittyflap" if row["source"] == "kittyflap" else "reolink_manual"
            group = "current" if row["id"] in current_ids else "new"
            for cls in classes_here:
                if cls in groups:
                    groups[cls][group][bucket] += 1
    return groups


@app.get("/app-api/rebalance/counts")
def rebalance_counts():
    """Pour chaque chat (+ Prey, + Negatif), le compte de captures déjà
    'kept' dans le chantier de rééquilibrage en cours, HK vs Reolink+Manuel
    séparément - alimente la barre de progression de l'onglet dédié."""
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT c.id, c.source, c.detected_objects_json
               FROM captures c
               WHERE c.review_status = 'pending' AND c.rebalance_bucket = 'kept'"""
        ).fetchall()
        groups = {c: {"kittyflap": 0, "reolink_manual": 0} for c in list(CLASSES) + ["Negatif"]}
        for row in rows:
            boxes = effective_boxes(conn, row["id"], row["detected_objects_json"])
            classes_here = {canonicalize(b.get("label")) for b in boxes} - {None}
            if not classes_here:
                classes_here = {"Negatif"}
            bucket = "kittyflap" if row["source"] == "kittyflap" else "reolink_manual"
            for cls in classes_here:
                if cls in groups:
                    groups[cls][bucket] += 1
    return groups


@app.post("/app-api/rebalance/keep")
def rebalance_keep(payload: dict = Body(...)):
    """Marque une sélection comme 'gardée' dans le lot de rééquilibrage
    en cours - review_status reste 'pending' (pas encore export-éligible),
    voir /rebalance/promote pour l'envoi final."""
    ids = payload.get("ids") or []
    if not ids:
        return {"ok": True, "count": 0}
    with get_conn() as conn:
        conn.executemany(
            "UPDATE captures SET rebalance_bucket = 'kept' WHERE id = ? AND review_status = 'pending'",
            [(cid,) for cid in ids],
        )
    return {"ok": True, "count": len(ids)}


@app.post("/app-api/rebalance/promote")
def rebalance_promote():
    """Envoie tout le lot 'kept' vers le vrai statut validé (confirmed,
    export-éligible) et vide rebalance_bucket - elles redeviennent des
    captures normales, visibles dans "Validées" comme n'importe quelle
    autre, prêtes pour le bouton d'export habituel (exported_at déjà NULL
    depuis la remise en jeu initiale)."""
    with get_conn() as conn:
        result = conn.execute(
            """UPDATE captures SET review_status = 'confirmed', rebalance_bucket = NULL,
                                    reviewed_at = datetime('now')
               WHERE review_status = 'pending' AND rebalance_bucket = 'kept'"""
        )
    return {"ok": True, "count": result.rowcount}


@app.get("/app-api/queue")
def review_queue(
    cat_name: str | None = None, source: str | None = None,
    status_group: str | None = None, prey_only: bool = False,
    negative_only: bool = False, in_model: str | None = None,
    training_mode: bool = False, rebalance: bool = False, limit: int = 100,
):
    query = """SELECT c.* FROM captures c"""
    params: list = []
    if cat_name is not None:
        query += """ JOIN capture_cat_labels ccl
                        ON ccl.capture_id = c.id AND ccl.cat_name = ?"""
        params.append(cat_name)
    # 18.10, Sid ("un onglet où je vois les validées et les écartées, au
    # cas où je merde... il me faut deux onglets pas un") - filet de
    # sécurité, deux listes séparées plutôt qu'un seul "déjà revues" -
    # vérifier ses rejets et vérifier ses validations sont deux
    # questions différentes. Triées par date de revue la plus récente
    # (ce qu'elle vient de faire en premier), pas par date de capture.
    # 18.10, Sid ("fais un archives oui") - une capture exportée (validée
    # ou écartée) disparaît de "Validées"/"Écartées" (voir leurs
    # commentaires) mais reste accessible ici si besoin de rouvrir/
    # corriger après coup, plutôt que de devenir invisible partout.
    if status_group == "validated":
        query += " WHERE c.review_status IN ('confirmed', 'corrected') AND c.exported_at IS NULL"
    elif status_group == "rejected":
        query += " WHERE c.review_status = 'rejected' AND c.exported_at IS NULL"
    elif status_group == "archived":
        query += " WHERE c.exported_at IS NOT NULL"
    elif training_mode:
        # 21.10, Sid ("je repars encore une fois à zéro... trier reo vs
        # HK") - l'onglet "Tri 200+200" porte toujours sur des captures
        # déjà confirmées/corrigées, même avec in_model vide ("les deux"
        # groupes à la fois) - peu importe si elles ont déjà été
        # exportées ou pas (contrairement à "validated"/"archived"
        # ci-dessus, qui restent des filets de sécurité séparés).
        query += " WHERE c.review_status IN ('confirmed', 'corrected')"
    elif rebalance:
        # 22.10, Sid ("la page 200+200 c'est pas ce que je veux... il me
        # faut un onglet spécifique que je puisse savoir lesquels c'est")
        # - ne reprend QUE les captures explicitement remises en jeu pour
        # ce chantier, jamais mélangé avec la vraie file "pending"
        # (nouvelles captures jamais revues) - c'est précisément ce qui
        # manquait à l'onglet Tri 200+200.
        # 22.10 (même soir) : filtré sur 'queued' seulement, pas "IS NOT
        # NULL" (donc PAS 'kept') - Sid ("j'en ai validé 3 de boubou...
        # comment faire pour qu'elles apparaissent dans la colonne de
        # boubou") voulait qu'une capture gardée disparaisse de la file
        # de travail comme partout ailleurs dans l'appli (une fois
        # décidée, plus la peine de la revoir) - le compte, lui, vient de
        # /app-api/rebalance/counts (scope 'kept'), pas de cette file.
        query += " WHERE c.review_status = 'pending' AND c.rebalance_bucket = 'queued'"
    else:
        query += " WHERE c.review_status = 'pending' AND c.rebalance_bucket IS NULL"
    if in_model is not None:
        current_ids = _current_model_capture_ids()
        if current_ids:
            placeholders = ",".join("?" * len(current_ids))
            query += f" AND c.id {'IN' if in_model == 'current' else 'NOT IN'} ({placeholders})"
            params.extend(sorted(current_ids))
        elif in_model == "current":
            query += " AND 0"
    # 18.10, Sid : onglet dédié aux ~4800 captures importées depuis
    # LabelStudio (voir import_labelstudio.py) - à trier avant le futur
    # export vers le nouveau conteneur d'entraînement.
    # 21.10, Sid ("j'en vois 68 alors qu'il y en a 230") : le tableau de
    # comptage (training_targets_counts ci-dessus) regroupe tout ce qui
    # n'est pas "kittyflap" sous une seule colonne "Reolink+Manuel" - un
    # filtre strict source='reolink' ne matchait donc jamais les captures
    # 'manual', créant un écart entre le chiffre affiché et ce qui
    # s'affichait réellement. "reolink_manual" reproduit exactement le
    # même regroupement que le tableau ; "reolink"/"manual" restent
    # disponibles séparément pour qui veut les distinguer.
    if source == "reolink_manual":
        query += " AND c.source != 'kittyflap'"
    elif source is not None:
        query += " AND c.source = ?"
        params.append(source)
    # 18.10, Sid ("plus logique d'avoir un onglet avec les potentielles
    # proies") - is_prey_candidate était déjà utilisé pour trier en
    # priorité DANS chaque onglet, mais rien ne permettait de ne voir QUE
    # ça, toutes identités/provenances confondues - le cas le plus rare et
    # le plus critique à réviser.
    if prey_only:
        query += " AND c.is_prey_candidate = 1"
    # 21.10 : "négatif" (ni chat ni proie après revue) ne se calcule pas
    # en SQL pur - les corrections vivent dans box_annotations, pas
    # directement sur c.detected_objects_json (voir effective_boxes). Pas
    # de LIMIT SQL dans ce cas, filtré puis tronqué côté Python plus bas.
    if status_group == "archived":
        query += " ORDER BY c.exported_at DESC"
    elif status_group or training_mode:
        query += " ORDER BY c.reviewed_at DESC"
    else:
        query += """ ORDER BY c.is_prey_candidate DESC, c.model_disagreement DESC,
                          c.low_cat_confidence ASC, c.created_at ASC"""
    # 18.10 : LIMIT ? doit être ajouté dans TOUS les cas, pas seulement le
    # tri par défaut - un bug introduit en ajoutant les tris spécifiques
    # ci-dessus faisait planter "Validées"/"Écartées"/"Archivées" en 500
    # (params.append(limit) tournait quand même, sans "?" correspondant
    # dans la requête, confirmé en testant "Archivées" juste après coup).
    if not negative_only:
        query += " LIMIT ?"
        params.append(limit)
    with get_conn() as conn:
        rows = conn.execute(query, params).fetchall()
        if not negative_only:
            return [dict(r) for r in rows]
        result = []
        for row in rows:
            boxes = effective_boxes(conn, row["id"], row["detected_objects_json"])
            classes_here = {canonicalize(b.get("label")) for b in boxes} - {None}
            if not classes_here:
                result.append(dict(row))
                if len(result) >= limit:
                    break
        return result


@app.get("/app-api/captures/{capture_id}")
def get_capture(capture_id: int):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM captures WHERE id = ?", (capture_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "capture introuvable")
        capture = dict(row)
        capture["boxes"] = effective_boxes(conn, capture_id, capture["detected_objects_json"])
        del capture["detected_objects_json"]
        return capture


@app.post("/app-api/captures/{capture_id}/boxes")
def upsert_box(capture_id: int, box: dict = Body(...)):
    required = {"object_index", "label", "x", "y", "width", "height"}
    if not required.issubset(box):
        raise HTTPException(400, f"champs manquants, requis : {sorted(required)}")
    source = box.get("source", "manual")
    with get_conn() as conn:
        exists = conn.execute(
            "SELECT 1 FROM captures WHERE id = ?", (capture_id,)
        ).fetchone()
        if not exists:
            raise HTTPException(404, "capture introuvable")
        conn.execute(
            """INSERT INTO box_annotations
                   (capture_id, object_index, source, label, x, y, width, height, confirmed, ignored)
               VALUES (?,?,?,?,?,?,?,?,1,0)
               ON CONFLICT(capture_id, object_index) DO UPDATE SET
                   label = excluded.label, x = excluded.x, y = excluded.y,
                   width = excluded.width, height = excluded.height,
                   source = excluded.source, confirmed = 1, ignored = 0""",
            (capture_id, box["object_index"], source, box["label"],
             box["x"], box["y"], box["width"], box["height"]),
        )
        recompute_cat_labels(conn, capture_id)
    return {"ok": True}


@app.delete("/app-api/captures/{capture_id}/boxes/{object_index}")
def discard_box(capture_id: int, object_index: int):
    """Écarte une boîte (double détection, fausse détection) - elle ne sera
    plus jamais montrée ni exportée pour l'entraînement. Une boîte ajoutée à la
    main (jamais proposée par le modèle) est carrément effacée ; une boîte du
    modèle est marquée ignorée (sa proposition d'origine, dans
    detected_objects_json, reste immuable - voir catalog_db.py)."""
    with get_conn() as conn:
        existing = conn.execute(
            "SELECT source FROM box_annotations WHERE capture_id = ? AND object_index = ?",
            (capture_id, object_index),
        ).fetchone()
        if existing and existing["source"] == "manual":
            conn.execute(
                "DELETE FROM box_annotations WHERE capture_id = ? AND object_index = ?",
                (capture_id, object_index),
            )
        else:
            capture = conn.execute(
                "SELECT detected_objects_json FROM captures WHERE id = ?", (capture_id,)
            ).fetchone()
            if capture is None:
                raise HTTPException(404, "capture introuvable")
            model_boxes = json.loads(capture["detected_objects_json"] or "[]")
            if object_index >= len(model_boxes):
                raise HTTPException(404, "boîte introuvable")
            mb = model_boxes[object_index]
            conn.execute(
                """INSERT INTO box_annotations
                       (capture_id, object_index, source, label, x, y, width, height, confirmed, ignored)
                   VALUES (?,?,'model',?,?,?,?,?,1,1)
                   ON CONFLICT(capture_id, object_index) DO UPDATE SET ignored = 1""",
                (capture_id, object_index, mb["object_name"], mb["x"], mb["y"], mb["width"], mb["height"]),
            )
        recompute_cat_labels(conn, capture_id)
    return {"ok": True}


@app.get("/app-api/captures/{capture_id}/image")
def get_image(capture_id: int, size: str = "thumbnail"):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT original_path, thumbnail_path FROM captures WHERE id = ?", (capture_id,)
        ).fetchone()
    if row is None:
        raise HTTPException(404, "capture introuvable")
    path = row["original_path"] if size == "original" else row["thumbnail_path"]
    if not os.path.isfile(path):
        raise HTTPException(404, "fichier image manquant sur disque")
    return FileResponse(path)


@app.post("/app-api/captures/{capture_id}/review")
def review_capture(capture_id: int, status: str, corrected_label: str | None = None):
    if status not in ("confirmed", "corrected", "rejected"):
        raise HTTPException(400, "status invalide")
    with get_conn() as conn:
        result = conn.execute(
            """UPDATE captures SET review_status = ?, corrected_label = ?,
                                    reviewed_at = datetime('now')
               WHERE id = ?""",
            (status, corrected_label, capture_id),
        )
        if result.rowcount == 0:
            raise HTTPException(404, "capture introuvable")
    return {"ok": True}


@app.post("/app-api/captures/bulk-review")
def bulk_review_captures(payload: dict = Body(...)):
    # 18.10, Sid ("un moyen d'en sélectionner plusieurs d'un coup pour les
    # écarter en une fois ?") - la grille Reolink en particulier contient
    # beaucoup de cases vides côte à côte (captures périodiques sans rien
    # dessus) ; un seul round-trip pour tout le lot plutôt qu'un par photo.
    ids = payload.get("ids") or []
    status = payload.get("status")
    if status not in ("confirmed", "corrected", "rejected"):
        raise HTTPException(400, "status invalide")
    if not ids:
        return {"ok": True, "count": 0}
    with get_conn() as conn:
        conn.executemany(
            "UPDATE captures SET review_status = ?, reviewed_at = datetime('now') WHERE id = ?",
            [(status, cid) for cid in ids],
        )
    return {"ok": True, "count": len(ids)}


@app.get("/app-api/stats")
def stats():
    with get_conn() as conn:
        row = conn.execute(
            """SELECT
                   COUNT(*) AS total,
                   SUM(review_status = 'pending') AS pending,
                   SUM(is_prey_candidate) AS prey_candidates,
                   SUM(model_disagreement) AS model_disagreements,
                   SUM(review_status = 'skipped_dedup') AS deduped,
                   SUM(review_status = 'pending' AND source = 'labelstudio') AS labelstudio_pending,
                   SUM(review_status = 'pending' AND source = 'reolink') AS reolink_pending,
                   SUM(review_status = 'pending' AND source = 'manual') AS manual_pending,
                   SUM(review_status = 'pending' AND is_prey_candidate = 1) AS prey_pending,
                   SUM(review_status IN ('confirmed', 'corrected') AND exported_at IS NULL) AS validated_total,
                   SUM(review_status = 'rejected' AND exported_at IS NULL) AS rejected_total,
                   SUM(exported_at IS NOT NULL) AS archived_total
               FROM captures"""
        ).fetchone()
        return {**dict(row), "retention_days": RETENTION_DAYS}


# 21.10, Sid ("au lieu que je te demande tout le temps... un compteur du
# nombre de photos par chaque dans le modèle... la différence entre
# provient de KH vs provient de reolink et manuel") - les chats de face
# (KH) sont déjà bien couverts, l'angle Reolink (reolink + manuel, elle
# ajoute "manuel" en tirant depuis l'appli Reolink elle-même, même angle)
# a encore des trous - ce compteur lui dit directement combien ajouter
# pour chacun, par source, sans avoir à me redemander. labelstudio = vieil
# import historique, angle caméra inconnu, montré à part plutôt que
# mélangé à tort dans l'un des deux autres groupes.
@app.get("/app-api/stats/by-class-source")
def stats_by_class_source():
    buckets = {c: {"kittyflap": 0, "reolink_manual": 0, "other": 0} for c in CLASSES}
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT id, source, detected_objects_json FROM captures
               WHERE review_status IN ('confirmed', 'corrected')"""
        ).fetchall()
        for row in rows:
            boxes = effective_boxes(conn, row["id"], row["detected_objects_json"])
            classes_here = {canonicalize(b.get("label")) for b in boxes} - {None}
            source = row["source"]
            bucket = "kittyflap" if source == "kittyflap" else (
                "reolink_manual" if source in ("reolink", "manual") else "other"
            )
            for cls in classes_here:
                if cls in buckets:
                    buckets[cls][bucket] += 1
    return buckets


@app.post("/app-api/export/start")
def export_start(full: bool = False):
    # 18.10, Sid ("un bouton qui lance l'export... je choisis à quel
    # moment je lance un zip") - en arrière-plan (voir export_training.py
    # pour pourquoi jamais en synchrone), cette route rend immédiatement.
    # 21.10 : full=True (bouton dédié dans l'onglet Tri 200+200) - voir
    # start_export_async pour le détail de ce que ça change.
    start_export_async(full=full)
    return {"ok": True}


@app.get("/app-api/export/status")
def export_status():
    return read_state()


@app.get("/app-api/export/download")
def export_download():
    state = read_state()
    filename = state.get("zip_filename")
    if not filename:
        raise HTTPException(404, "Aucun export prêt")
    path = os.path.join(EXPORTS_DIR, filename)
    if not os.path.isfile(path):
        raise HTTPException(404, "Fichier d'export introuvable sur disque")
    return FileResponse(path, filename=filename, media_type="application/zip")


# 19.10, Sid ("vas-y lance le truc") - l'instance vast.ai (éphémère, moins
# fiable qu'un hôte à moi) pousse ici le zip du modèle entraîné une fois
# prêt. Jeton partagé simple en protection - cet endpoint écrit
# directement dans le dossier de modèles de Kittyhack (voir
# KITTYHACK_MODELS_DIR), pas question de le laisser ouvert à qui trouve
# l'URL.
TRAINED_MODELS_DIR = os.path.join(STORAGE_ROOT, "trained_models")
KITTYHACK_MODELS_DIR = os.environ.get("KITTYHACK_MODELS_DIR", "/kittyhack_models")
MODEL_UPLOAD_TOKEN = os.environ.get("MODEL_UPLOAD_TOKEN", "")
TRAINING_DATA_PATH = os.path.join(STORAGE_ROOT, "vastai_training_data.zip")
REQUIRED_MODEL_FILES = [
    "model.pt", "labels.txt", "info.json",
    "best_ncnn_model/model.ncnn.bin", "best_ncnn_model/model.ncnn.param",
]


@app.get("/app-api/model/training-data")
def model_training_data(x_upload_token: str = Header(default="")):
    # 19.10 : même jeton que l'upload - l'instance louée télécharge ses
    # données d'entraînement par là (voir train_vastai.py).
    if MODEL_UPLOAD_TOKEN and x_upload_token != MODEL_UPLOAD_TOKEN:
        raise HTTPException(403, "jeton invalide")
    if not os.path.isfile(TRAINING_DATA_PATH):
        raise HTTPException(404, "Pas de zip d'entraînement prêt")
    return FileResponse(TRAINING_DATA_PATH, filename="training_data.zip", media_type="application/zip")


@app.post("/app-api/model/upload")
async def model_upload(file: UploadFile = File(...), x_upload_token: str = Header(default="")):
    if MODEL_UPLOAD_TOKEN and x_upload_token != MODEL_UPLOAD_TOKEN:
        raise HTTPException(403, "jeton invalide")

    os.makedirs(TRAINED_MODELS_DIR, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    zip_path = os.path.join(TRAINED_MODELS_DIR, f"model_{ts}.zip")
    with open(zip_path, "wb") as f:
        f.write(await file.read())

    try:
        with zipfile.ZipFile(zip_path) as zf:
            names = zf.namelist()
            missing = [r for r in REQUIRED_MODEL_FILES if r not in names]
            if missing:
                raise HTTPException(400, f"fichiers manquants dans le zip : {missing}")
            os.makedirs(KITTYHACK_MODELS_DIR, exist_ok=True)
            target_dir = os.path.join(KITTYHACK_MODELS_DIR, ts)
            os.makedirs(target_dir, exist_ok=True)
            zf.extractall(target_dir)
    except zipfile.BadZipFile:
        raise HTTPException(400, "zip invalide")

    return {"ok": True, "installed_as": ts}


# 20.10, Sid ("je peux toujours faire une modif en live sur la vidéo...
# qu'elle parte dans paws review") - LabelStudio (décommissionné ce soir)
# était la destination de l'action "send" de la page Parcours (Kittyhack),
# maintenant cassée (service arrêté). Même jeton que /model/upload (même
# frontière de confiance - Kittyhack poussant quelque chose chez lui).
# Dépose simplement dans MANUAL_DROP_DIR, repris tel quel par le cycle de
# 5 min existant (paws-review-ingest, app/ingest_manual.py) - aucun
# nouveau montage docker, aucune nouvelle logique d'ingestion.
MANUAL_DROP_DIR = os.path.join(STORAGE_ROOT, "manual_drop")


@app.post("/app-api/manual-drop/upload")
async def manual_drop_upload(
    file: UploadFile = File(...),
    detections: str = Form(default=""),
    x_upload_token: str = Header(default=""),
):
    if MODEL_UPLOAD_TOKEN and x_upload_token != MODEL_UPLOAD_TOKEN:
        raise HTTPException(403, "jeton invalide")
    os.makedirs(MANUAL_DROP_DIR, exist_ok=True)
    safe_name = os.path.basename(file.filename or "")
    if not safe_name or not safe_name.lower().endswith((".jpg", ".jpeg", ".png")):
        raise HTTPException(400, "nom de fichier invalide, extension .jpg/.jpeg/.png requise")
    ts = time.strftime("%Y%m%d_%H%M%S")
    dst = os.path.join(MANUAL_DROP_DIR, f"journeys_{ts}_{safe_name}")
    with open(dst, "wb") as f:
        f.write(await file.read())
    # 21.10, Sid ("j'ai des tags dans 'video parcours' et celles-là je les
    # ai pas") - quand Parcours a déjà fait tourner son modèle sur cette
    # image (chatière only, voir journeys.py's _detections_sidecar_for_
    # paws_review), le sidecar suit le même nom de base que l'image, repris
    # tel quel par ingest_manual_batch au prochain cycle - jamais écrasé par
    # un drop manuel normal (celui-ci n'envoie jamais ce champ).
    if detections:
        try:
            parsed = json.loads(detections)
            with open(dst[:-4] + ".json", "w", encoding="utf-8") as f:
                json.dump(parsed, f)
        except Exception as e:
            logging.warning(f"Detections sidecar invalide pour {dst}: {e}")
    return {"ok": True}


# 19.10, Sid ("crée le bouton et on teste ensuite en live") - la clé API
# vast.ai vit ici en variable d'env (même frontière de confiance que
# MODEL_UPLOAD_TOKEN, même conteneur) plutôt que saisie à la main à chaque
# clic - jamais déposée sur la machine louée (voir train_vastai.py), jamais
# renvoyée au frontend.
VAST_API_KEY = os.environ.get("VAST_API_KEY", "")


@app.post("/app-api/train/start")
def train_start():
    if not VAST_API_KEY:
        raise HTTPException(500, "VAST_API_KEY non configurée côté serveur")
    started = start_training_async(VAST_API_KEY)
    if not started:
        raise HTTPException(409, "Un entraînement est déjà en cours")
    return {"ok": True}


@app.get("/app-api/train/status")
def train_status():
    return read_training_state()


# 20.10, Sid ("le module de revérification... je contrôle tout au même
# endroit") - portage de src/model/retroactive_review.py côté Kittyhack,
# voir app/model_review.py pour la logique complète (filtrage RFID inclus).
@app.get("/app-api/model-review/models")
def model_review_models():
    return list_model_dirs()


@app.post("/app-api/model-review/start")
def model_review_start(payload: dict = Body(...)):
    model_version = payload.get("model_version")
    if not model_version:
        raise HTTPException(400, "model_version requis")
    num_captures = int(payload.get("num_captures") or 200)
    started = start_model_review_async(model_version, num_captures)
    if not started:
        raise HTTPException(409, "Une revérification est déjà en cours")
    return {"ok": True}


@app.get("/app-api/model-review/status")
def model_review_status():
    return read_model_review_state()


# 20.10, Sid ("un bouton qui sort aussi la comparaison... un clic lance
# les 10 derniers événements, puis ceux d'avant", puis "50 en asynchrone,
# ça me semble pas mal") - model_compare.py tournait jusqu'ici à la demande
# dans un conteneur jetable (ultralytics/torch volontairement hors de
# l'image) ; depuis l'ajout du scan GPU de model_review.py, ces mêmes
# dépendances sont déjà dans l'image, donc ce bouton peut tourner
# directement ici. Même dossier de modèles que la revérification
# (MODELS_DIR, pas le /models en lecture seule que model_compare.py
# suppose en usage CLI) - ancien/nouveau = les deux modèles les plus
# récents, auto-sélectionnés. Asynchrone (même mécanique state+thread que
# /model-review/start) : ~0,45s/image mesuré en vrai, 50 images = ~25s,
# trop long pour un simple POST sans retour de progression.
@app.post("/app-api/model-compare/recent")
def model_compare_recent(payload: dict = Body(...)):
    from app.labels import CLASSES
    from app.model_compare import COMPARISONS_DIR, start_compare_recent_async
    from app.model_review import MODELS_DIR

    limit = int(payload.get("limit") or 20)
    class_filter = payload.get("class_filter") or None
    if class_filter and class_filter not in CLASSES:
        raise HTTPException(400, f"class_filter invalide : {class_filter}")
    # session_dir : uniquement une valeur qu'on a nous-mêmes renvoyée sur un
    # appel précédent (voir "out_dir" ci-dessous) - jamais un chemin arbitraire
    # du client, d'où la vérification qu'il reste bien sous COMPARISONS_DIR.
    session_dir = payload.get("session_dir")
    if session_dir:
        session_dir = os.path.join(COMPARISONS_DIR, os.path.basename(session_dir))
        if not os.path.isdir(session_dir):
            session_dir = None
    dirs = list_model_dirs()
    if len(dirs) < 2:
        raise HTTPException(409, f"Seulement {len(dirs)} modèle(s) disponible(s), 2 nécessaires")
    new_dir = os.path.join(MODELS_DIR, dirs[0])
    old_dir = os.path.join(MODELS_DIR, dirs[1])
    started = start_compare_recent_async(old_dir, new_dir, limit, class_filter, session_dir)
    if not started:
        raise HTTPException(409, "Une comparaison est déjà en cours")
    return {"ok": True}


@app.get("/app-api/model-compare/status")
def model_compare_status():
    from app.model_compare import read_compare_state
    return read_compare_state()


@app.get("/app-api/model-review/queue")
def model_review_queue(limit: int = 200):
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT q.*, c.thumbnail_path, c.cat_name, c.cat_source
               FROM model_review_queue q
               JOIN captures c ON c.id = q.capture_id
               WHERE q.status = 'pending'
               ORDER BY q.created_at ASC
               LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


@app.post("/app-api/model-review/resolve")
def model_review_resolve(payload: dict = Body(...)):
    ids = payload.get("ids") or []
    action = payload.get("action")
    if not ids:
        return {"ok": True, "count": 0}
    count = resolve_queue_items(ids, action)
    return {"ok": True, "count": count}


class NoCacheStaticFiles(StaticFiles):
    # 18.10, Sid ("ça affiche toujours pookie au lieu de proie" - a fix
    # already live server-side, confirmed via curl, just not what her
    # browser was showing): StaticFiles sets no Cache-Control at all by
    # default, so browsers fall back to their own heuristic caching and can
    # keep serving a stale app.js for a long-lived tab. This is a tool that
    # gets edited live while she's actively using it - force revalidation
    # on every request (still cheap: ETag/Last-Modified means an unchanged
    # file is just a 304, not a full re-download) instead of relying on a
    # hard refresh every time something changes.
    async def get_response(self, path: str, scope: Scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


# 19.10 : résultats de app/model_compare.py (lancé à la demande, voir sa
# docstring) - juste une galerie HTML statique pré-générée par le script,
# rien à exposer côté API, servie telle quelle pour consultation.
MODEL_COMPARISONS_DIR = os.path.join(STORAGE_ROOT, "model_comparisons")
os.makedirs(MODEL_COMPARISONS_DIR, exist_ok=True)
app.mount(
    "/model-comparisons",
    NoCacheStaticFiles(directory=MODEL_COMPARISONS_DIR, html=True),
    name="model_comparisons",
)

# 20.10 : même principe pour app/event_compare.py (galerie HTML statique
# ciblée sur des événements réels précis, pas un échantillon aléatoire) -
# jamais monté jusqu'ici, donc jamais consultable que par accès disque direct.
EVENT_COMPARISONS_DIR = os.path.join(STORAGE_ROOT, "event_comparisons")
os.makedirs(EVENT_COMPARISONS_DIR, exist_ok=True)
app.mount(
    "/event-comparisons",
    NoCacheStaticFiles(directory=EVENT_COMPARISONS_DIR, html=True),
    name="event_comparisons",
)

app.mount("/", NoCacheStaticFiles(directory="app/static", html=True), name="static")
