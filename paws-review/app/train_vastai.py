"""
Orchestration d'un entraînement YOLO sur une machine GPU louée à l'heure
(vast.ai) plutôt que sur le serveur externe de Kittyhack
(kittyhack-models.fk-cloud.de) - 19.10, Sid : "vas-y lance le truc" suite
à un entraînement resté en file d'attente côté serveur Kittyhack sans
jamais démarrer.

Déroulé :
  1. Construit le zip complet d'entraînement (TOUTES les captures
     validées/corrigées, pas juste les nouvelles - voir
     export_training.build_full_training_zip) et le sert à une URL fixe
     que l'instance louée viendra chercher.
  2. Cherche une offre GPU pas chère sur vast.ai, loue une machine.
  3. La machine (script onstart) télécharge les données, installe
     ultralytics, entraîne, exporte en NCNN, empaquette exactement les 5
     fichiers que Kittyhack attend (voir app/main.py:REQUIRED_MODEL_FILES,
     vérifié le 19.10 sur un modèle déjà installé), et pousse le résultat
     vers /app-api/model/upload.
  4. Ce script-ci surveille l'arrivée du résultat (nouveau fichier dans
     TRAINED_MODELS_DIR), puis détruit la machine louée - jamais la clé
     API vast.ai elle-même n'est déposée sur la machine louée (moins
     fiable/moins sienne qu'un hôte à elle), toute la destruction se fait
     depuis ici.

Usage (clé API vast.ai en saisie interactive, jamais en argument) :
    docker exec -it paws-review python -m app.train_vastai
"""

import getpass
import json
import logging
import os
import sys
import threading
import time

import requests

from app.config import STORAGE_ROOT
from app.export_training import build_full_training_zip
from app.labels import CLASSES

logging.basicConfig(level=logging.INFO, format="%(asctime)s [train_vastai] %(message)s")
log = logging.getLogger(__name__)

VAST_BASE = "https://console.vast.ai/api/v0"
TRAINED_MODELS_DIR = os.path.join(STORAGE_ROOT, "trained_models")
TRAINING_DATA_PATH = os.path.join(STORAGE_ROOT, "vastai_training_data.zip")

# 19.10, Sid ("crée le bouton et on teste ensuite en live") - état persisté
# sur disque pour le bouton "Lancer un entraînement" de Paws Review, même
# pattern que export_training.py (_write_state/read_state) : survit à un
# rechargement de page, pollable par le frontend pendant les ~10-40 min que
# prend un run complet.
STATE_PATH = os.path.join(STORAGE_ROOT, "train_state.json")
_run_lock = threading.Lock()


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


def start_training_async(api_key: str) -> bool:
    """Lance l'entraînement en arrière-plan si aucun n'est déjà en cours.
    Renvoie False (no-op) si un run est déjà actif - jamais deux
    entraînements vast.ai en parallèle (double facturation, et le
    wait_for_result de l'un pourrait récupérer le résultat de l'autre)."""
    if not _run_lock.acquire(blocking=False):
        return False
    thread = threading.Thread(target=_run_training_locked, args=(api_key,), daemon=True)
    thread.start()
    return True


def _run_training_locked(api_key: str):
    try:
        run_training(api_key)
    except Exception as e:
        log.exception("Entraînement échoué")
        _write_state({"status": "error", "message": str(e)})
    finally:
        _run_lock.release()

# 19.10 : URL/jeton publics de Paws Review - l'instance louée (hors de mon
# propre réseau) doit pouvoir les joindre depuis internet.
PAWS_REVIEW_URL = os.environ.get("PAWS_REVIEW_PUBLIC_URL", "https://kittyreview.sidounette.ch")
UPLOAD_TOKEN = os.environ.get("MODEL_UPLOAD_TOKEN", "")

GPU_NAME = os.environ.get("VAST_GPU_NAME", "RTX 3090")
MAX_PRICE_PER_HOUR = float(os.environ.get("VAST_MAX_PRICE", "0.40"))
# 19.10 (nuit), Sid ("je te laisse tester tout seul") - vrai échec en
# direct, diagnostiqué par SSH sur l'instance : "OSError: [Errno 28] No
# space left on device" pendant `pip install ultralytics`. L'image de base
# utilise déjà 13 Go sur les 20 Go loués AVANT même ce pip install - le
# nouveau stack torch 2.14.1/CUDA13 qu'ultralytics réclame désormais pèse
# plus de 1.5 Go de roues (cudnn 553 Mo, cublas 423 Mo, cusolver 200 Mo,
# torch/torchvision eux-mêmes) et le cache pip double ce besoin pendant
# l'installation (roue téléchargée en cache + copie installée en même
# temps). 20 Go suffisait pour les runs 320/yolov8n d'avant ce soir, plus
# pour celui-ci. Porté à 50 Go - large marge plutôt qu'un calcul juste
# pile, le coût du disque supplémentaire est négligeable face au coût GPU.
DISK_GB = 50
TRAIN_EPOCHS = int(os.environ.get("VAST_EPOCHS", "100"))
# 19.10, Sid ("on peut passer en 640x640 ?") - mesuré en vrai sur OLDMEDIA :
# OpenVINO sur l'iGPU à 640 (79.6ms/image) est quasi aussi rapide que le
# CPU actuel à 320 (81.6ms/image), donc le gain de précision sur les
# petits objets (Prey) vient sans coût de vitesse perçu - voir
# requirements_remote.txt/compose.yaml de kittyhack-remote pour la bascule
# GPU qui va avec. Entraîner ET inférer à la même résolution, sinon le
# modèle n'apprend pas réellement plus de détail sur les petits objets.
IMAGE_SIZE = 640
# 19.10, Sid ("passer à yolo26 ?") - "YOLO26" (pas "YOLOv26" - Ultralytics a
# retiré le "v" sur cette version), nom de fichier exact "yolo26n.pt" (pas
# "yolov26n.pt" comme pour v8/v11) - probablement pourquoi une tentative
# précédente n'avait pas pris. Inclut Small-Target-Aware Label Assignment
# (STAL), pensé pour exactement le problème qu'on a sur Prey (petit objet,
# confiance basse). Nécessite ultralytics>=8.4.0 - confirmé 8.4.182 déjà en
# place sur les instances vast.ai utilisées ce soir.
YOLO_BASE_MODEL = os.environ.get("VAST_YOLO_BASE_MODEL", "yolo26n.pt")
POLL_INTERVAL_S = 30
# 19.10 (nuit), Sid ("je te laisse tester tout seul") - vrai échec en
# direct : run YOLO26+640 tué par ce timeout alors qu'il tournait TOUJOURS
# (gpu_util 14-30% jusqu'à la dernière seconde, epoch ~51/100 atteint
# après une relance manuelle ayant déjà consommé ~15min sur un `pip
# install ultralytics` anormalement lourd ce soir-là - torch/CUDA13 neufs,
# centaines de Mo de roues). 45 min ne couvre pas pip lent + 100 epochs à
# 640 (~20s/epoch ≈ 33min) + export NCNN + upload. Porté à 80 min - marge
# réelle plutôt qu'un chiffre arbitraire, voir le calcul ci-dessus.
TIMEOUT_S = 80 * 60  # au-delà, quelque chose s'est mal passé - on arrête les frais

# 19.10 : hôte 85323 vu défaillant 3 fois d'affilée sur des tentatives
# séparées (même erreur OCI/CDI à chaque fois - "unresolvable CDI devices...
# gpu=N: unknown") - exclu pour de bon, inutile de repayer le coût d'une
# création+détection à chaque fois pour un hôte structurellement incompatible
# avec cette image. exclude_host_ids (par run, en mémoire) s'ajoute à celle-ci.
# 19.10 : hôte 155385 (Yunnan, CN) vu mort 2 fois d'affilée - compteurs
# réseau et CPU figés dès le début, aucun log ni accès SSH possible malgré
# un statut "running" côté vast.ai. Passe le contrôle de santé (pas
# d'erreur OCI, juste injoignable) - exclu à la main, pas détectable
# automatiquement avec la logique actuelle.
PERMANENTLY_EXCLUDED_HOSTS = {85323, 155385}

# 19.10, Sid ("si on peut, par principe, exclure chine, russie, israel etc
# ca m'arrange") - préférence personnelle, pas une question de fiabilité :
# filtré sur le code pays ISO2 renvoyé dans le champ "geolocation" de
# chaque offre vast.ai (format observé "Ville, CC", ex. "Yunnan, CN").
EXCLUDED_COUNTRIES = {"CN", "RU", "IL"}

# 19.10, Sid ("on peut en choisir des favoris ?") - hôte 619828 (Australie)
# vu fonctionner correctement 2 fois (entraînement + export NCNN complets
# tous les deux). Pas un filtre vast.ai (leur API n'a pas cette notion) -
# juste une préférence appliquée ici : si une offre de cet hôte est
# disponible sous le prix plafond, on la prend même si une autre offre non
# testée est moins chère. Un hôte qui finit mal (OCI/mort) doit être retiré
# d'ici ET ajouté à PERMANENTLY_EXCLUDED_HOSTS.
#
# 19.10 (plus tard, même soirée), Sid ("faut qu'on teste avec d'autre") -
# vidé temporairement : les 2 runs YOLO26+640 de ce soir sur CET hôte ont
# tous les deux calé à gpu_util=0% sans jamais produire de résultat (voir
# _poll_instance_health, ajouté ce soir pour avoir un vrai journal la
# prochaine fois). Pas un échec de santé classique (actual_status=running,
# status_msg=success, le contrôle de santé passe) donc PAS ajouté à
# PERMANENTLY_EXCLUDED_HOSTS - rien ne prouve encore qu'il soit vraiment en
# cause plutôt que YOLO26+640 lui-même. Vider la préférence pour forcer un
# autre hôte au prochain run est le test direct de cette hypothèse. Remettre
# {619828} ici si un autre hôte échoue pareil (ça disculperait cet hôte).
PREFERRED_HOSTS: set[int] = set()


def _build_onstart_script() -> str:
    names_yaml = "[" + ", ".join(CLASSES) + "]"
    # 19.10 : clé API vast.ai jamais déposée ici - cette machine n'est pas
    # à moi, pas de raison qu'elle puisse s'auto-détruire ou agir en mon
    # nom sur mon compte. Elle pousse juste son résultat et s'arrête là ;
    # la destruction se fait depuis train_vastai.py, sur ma propre machine.
    return f"""#!/bin/bash
set -ex
cd /workspace
curl -sL -H "X-Upload-Token: {UPLOAD_TOKEN}" {PAWS_REVIEW_URL}/app-api/model/training-data -o data.zip
mkdir -p data && cd data && unzip -q ../data.zip && cd ..
pip install -q --no-cache-dir ultralytics
cat > data.yaml << 'YAML'
path: /workspace/data
train: images
val: images
names: {names_yaml}
YAML
yolo train data=data.yaml model={YOLO_BASE_MODEL} epochs={TRAIN_EPOCHS} imgsz={IMAGE_SIZE} project=/workspace/runs name=train device=0
yolo export model=/workspace/runs/train/weights/best.pt format=ncnn
mkdir -p /workspace/result/best_ncnn_model
cp /workspace/runs/train/weights/best.pt /workspace/result/model.pt
cp /workspace/data/classes.txt /workspace/result/labels.txt
# 19.10 : liste explicite plutôt qu'un glob '*' - un premier run a cassé ici
# en silence (set -ex + cp sans -r sur __pycache__/, présent dans
# best_ncnn_model/ depuis l'export NCNN, a tué le script avant le zip/upload,
# ~60 min de GPU payée pour rien). Seuls les fichiers listés dans
# REQUIRED_MODEL_FILES (main.py) ont besoin d'exister dans le zip final.
cp /workspace/runs/train/weights/best_ncnn_model/metadata.yaml \
   /workspace/runs/train/weights/best_ncnn_model/model.ncnn.bin \
   /workspace/runs/train/weights/best_ncnn_model/model.ncnn.param \
   /workspace/runs/train/weights/best_ncnn_model/model_ncnn.py \
   /workspace/result/best_ncnn_model/
python3 -c "
import json, datetime
json.dump({{
    'JOB_ID': 'vastai-' + str(int(datetime.datetime.utcnow().timestamp())),
    'USERNAME': '', 'MODEL_NAME': '',
    'TIMESTAMP_UTC': datetime.datetime.utcnow().isoformat() + 'Z',
    'VERSION': '1.0.0', 'MODEL_IMAGE_SIZE': '{IMAGE_SIZE}',
    'YOLO_MODEL_VARIANT': '{YOLO_BASE_MODEL}',
}}, open('/workspace/result/info.json', 'w'), indent=2)
"
cd /workspace/result && zip -qr ../result.zip .
curl -sL -H "X-Upload-Token: {UPLOAD_TOKEN}" -F "file=@/workspace/result.zip" {PAWS_REVIEW_URL}/app-api/model/upload
touch /workspace/TRAINING_DONE
"""


def _headers(api_key: str) -> dict:
    return {"Authorization": f"Bearer {api_key}"}


def find_cheapest_offer(api_key: str, exclude_host_ids: set | None = None) -> dict:
    # 19.10 : l'offre la moins chère reste souvent la MÊME d'une recherche
    # à l'autre (personne d'autre ne l'a louée entre temps) - détruire une
    # instance ratée puis relancer "la moins chère" relouait donc deux
    # fois de suite la même machine physique défaillante (confirmé en
    # vrai : host_id 85323 à chaque tentative). exclude_host_ids permet
    # de vraiment changer de machine, pas juste de numéro d'instance.
    exclude_host_ids = (exclude_host_ids or set()) | PERMANENTLY_EXCLUDED_HOSTS
    resp = requests.post(
        f"{VAST_BASE}/bundles/",
        headers=_headers(api_key),
        json={
            "gpu_name": {"in": [GPU_NAME, "RTX 4090"]},
            "num_gpus": {"eq": 1},
            "reliability2": {"gte": 0.98},
            "dph_total": {"lte": MAX_PRICE_PER_HOUR},
            "rentable": {"eq": True},
            "order": [["dph_total", "asc"]],
        },
        timeout=30,
    )
    resp.raise_for_status()
    offers = (resp.json() or {}).get("offers", [])
    offers = [o for o in offers if o.get("host_id") not in exclude_host_ids]
    offers = [
        o for o in offers
        if (o.get("geolocation") or "").split(",")[-1].strip().upper() not in EXCLUDED_COUNTRIES
    ]
    if not offers:
        raise RuntimeError(f"Aucune offre disponible sous {MAX_PRICE_PER_HOUR}$/h pour {GPU_NAME}/RTX 4090")

    preferred = [o for o in offers if o.get("host_id") in PREFERRED_HOSTS]
    best = preferred[0] if preferred else offers[0]
    log.info(
        "Offre retenue%s : %s à %.3f$/h (id %s, hôte %s)",
        " [hôte préféré]" if preferred else "",
        best.get("gpu_name"), best.get("dph_total"), best.get("id"), best.get("host_id"),
    )
    return best


def create_instance(api_key: str, offer_id: int) -> int:
    resp = requests.put(
        f"{VAST_BASE}/asks/{offer_id}/",
        headers=_headers(api_key),
        json={
            # 19.10 : l'image PyTorch générique (Docker Hub officiel)
            # échouait de façon identique sur 2 machines différentes -
            # "OCI runtime create failed... périphériques CDI non
            # résolvables" - pas un hasard de machine, l'image elle-même
            # n'est pas pensée pour l'injection GPU de vast.ai. Leur
            # propre image officielle (vastai/pytorch) est faite pour.
            "image": "vastai/pytorch:latest",
            "disk": DISK_GB,
            "onstart": _build_onstart_script(),
            "runtype": "ssh",
            # 19.10 : sans ça, l'instance reste créée mais à l'arrêt - vrai
            # bug trouvé en vrai (confirmé par Sid : "État prévu : arrêté"
            # sur sa première machine, 11 min sans rien faire tourner).
            "target_state": "running",
        },
        timeout=30,
    )
    resp.raise_for_status()
    body = resp.json()
    instance_id = body.get("new_contract") or body.get("id")
    if not instance_id:
        raise RuntimeError(f"Pas d'id d'instance dans la réponse : {body}")
    log.info("Instance créée : %s", instance_id)
    return instance_id


def check_instance_health(api_key: str, instance_id: int) -> tuple[bool, int | None, str]:
    """19.10 : un démarrage raté (image/GPU incompatible) se voit dans
    status_msg - pas la peine d'attendre le TIMEOUT_S complet (45 min) pour
    s'en rendre compte. Un seul coup à t+20s s'est avéré insuffisant en vrai
    (host 85323 : l'erreur OCI n'était pas encore remontée dans status_msg à
    t+20s pile, passée au travers, bloqué ~7min avant d'être repéré à la
    main) - plusieurs vérifications étalées sur ~40s au lieu d'un seul
    instantané."""
    host_id = None
    for _ in range(4):
        time.sleep(10)
        resp = requests.get(f"{VAST_BASE}/instances/{instance_id}/", headers=_headers(api_key), timeout=20)
        resp.raise_for_status()
        body = resp.json()
        inst = body.get("instances") or body
        status_msg = inst.get("status_msg") or ""
        host_id = inst.get("host_id")
        if "error response from daemon" in status_msg.lower() or "oci runtime create failed" in status_msg.lower():
            return False, host_id, status_msg
    return True, host_id, status_msg


def destroy_instance(api_key: str, instance_id: int) -> None:
    try:
        resp = requests.delete(f"{VAST_BASE}/instances/{instance_id}/", headers=_headers(api_key), timeout=30)
        log.info("Instance %s détruite (HTTP %s)", instance_id, resp.status_code)
    except Exception as e:
        log.error("Échec de la destruction de l'instance %s : %s - À VÉRIFIER MANUELLEMENT sur vast.ai !", instance_id, e)


def _poll_instance_health(api_key: str, instance_id: int) -> tuple[float | None, str]:
    """19.10, Sid ("tu fais la modif pour avoir un log en cas de bug ?"):
    before this, wait_for_result() only ever logged "en attente" with an
    elapsed counter - after 2 real failed runs tonight (both timed out at
    2700s with zero result), the only diagnostic was gpu_util checked by
    hand, outside this script, after the fact. This gets logged on every
    poll instead, so a 3rd failure leaves a real gpu_util timeline in the
    paws-review logs rather than nothing."""
    try:
        resp = requests.get(f"{VAST_BASE}/instances/{instance_id}/", headers=_headers(api_key), timeout=15)
        resp.raise_for_status()
        inst = resp.json().get("instances") or resp.json()
        if not inst:
            return None, "instance introuvable (détruite ?)"
        return inst.get("gpu_util"), str(inst.get("status_msg") or "")
    except Exception as e:
        return None, f"échec de lecture du statut : {e}"


def wait_for_result(
    started_after: float, api_key: str | None = None, instance_id: int | None = None, images: int | None = None,
) -> str | None:
    deadline = time.time() + TIMEOUT_S
    while time.time() < deadline:
        if os.path.isdir(TRAINED_MODELS_DIR):
            for fname in os.listdir(TRAINED_MODELS_DIR):
                fpath = os.path.join(TRAINED_MODELS_DIR, fname)
                if os.path.isfile(fpath) and os.path.getmtime(fpath) >= started_after:
                    return fpath
        elapsed = int(time.time() - started_after)
        if api_key and instance_id:
            gpu_util, status_msg = _poll_instance_health(api_key, instance_id)
            log.info(
                "En attente du résultat... (%ds écoulées, gpu_util=%s, status_msg=%s)",
                elapsed, gpu_util, status_msg[:150],
            )
            _write_state({
                "status": "running", "step": "training", "started_at": started_after,
                "instance_id": instance_id, "images": images, "last_gpu_util": gpu_util,
                "last_status_msg": status_msg[:150], "last_poll_at": time.time(),
            })
        else:
            log.info("En attente du résultat... (%ds écoulées)", elapsed)
        time.sleep(POLL_INTERVAL_S)
    return None


def run_training(api_key: str) -> dict:
    """Cœur partagé entre l'usage CLI (__main__ plus bas) et le bouton web
    (main.py:/app-api/train/start, via start_training_async) - écrit son
    état dans STATE_PATH à chaque étape pour que le frontend puisse suivre
    sans dépendre des logs serveur."""
    if not UPLOAD_TOKEN:
        msg = "MODEL_UPLOAD_TOKEN non configuré côté serveur, abandon."
        log.error(msg)
        _write_state({"status": "error", "message": msg})
        return {"status": "error", "message": msg}

    _write_state({"status": "running", "step": "export", "started_at": time.time()})
    log.info("Construction du zip d'entraînement complet...")
    stats = build_full_training_zip(TRAINING_DATA_PATH)
    log.info("Zip prêt : %s", stats)
    if stats["images"] == 0:
        msg = "Aucune capture validée/corrigée à entraîner, abandon."
        log.error(msg)
        _write_state({"status": "error", "message": msg})
        return {"status": "error", "message": msg}

    # 19.10 : certaines machines démarrent mais échouent tout de suite
    # (image/GPU incompatible côté hôte, vu en vrai 2x sur la même machine
    # relouée par erreur) - quelques tentatives sur des hôtes différents
    # avant d'abandonner pour de bon, plutôt qu'un seul essai qui peut
    # tomber sur une mauvaise machine au hasard.
    excluded_hosts: set = set()
    instance_id = None
    started_at = time.time()
    for attempt in range(1, 4):
        _write_state({
            "status": "running", "step": "searching_offer", "attempt": attempt,
            "started_at": started_at, "images": stats["images"],
        })
        offer = find_cheapest_offer(api_key, exclude_host_ids=excluded_hosts)
        try:
            instance_id = create_instance(api_key, offer["id"])
        except requests.HTTPError as e:
            # 19.10, Sid (vrai échec en live, bouton) : "no_such_ask... not
            # available" - une offre vue dans la recherche peut être louée
            # par quelqu'un d'autre une fraction de seconde avant notre
            # propre tentative (vraie course du marché, pas un bug). Avant
            # ce correctif, create_instance() levait et faisait tout
            # échouer d'un coup sans jamais retenter une autre offre -
            # maintenant traité comme n'importe quel hôte défaillant.
            log.warning("Tentative %d : offre %s indisponible (%s) - nouvel essai sur une autre offre.", attempt, offer["id"], e)
            continue
        _write_state({
            "status": "running", "step": "health_check", "attempt": attempt,
            "started_at": started_at, "images": stats["images"],
            "instance_id": instance_id, "host_id": offer.get("host_id"),
            "price": offer.get("dph_total"),
        })
        healthy, host_id, status_msg = check_instance_health(api_key, instance_id)
        if healthy:
            break
        log.warning("Tentative %d : hôte %s défaillant (%s) - nouvel essai sur un autre hôte.", attempt, host_id, status_msg[:150])
        destroy_instance(api_key, instance_id)
        if host_id is not None:
            excluded_hosts.add(host_id)
        instance_id = None
    else:
        msg = "3 hôtes défaillants d'affilée, abandon."
        log.error(msg)
        _write_state({"status": "error", "message": msg})
        return {"status": "error", "message": msg}

    try:
        log.info("Entraînement en cours sur l'instance %s, surveillance du résultat...", instance_id)
        _write_state({
            "status": "running", "step": "training", "started_at": started_at,
            "images": stats["images"], "instance_id": instance_id,
        })
        result_path = wait_for_result(started_at, api_key=api_key, instance_id=instance_id, images=stats["images"])
        if result_path:
            log.info("Résultat reçu : %s", result_path)
        else:
            log.error("Timeout (%ds) sans résultat reçu - vérifier l'instance manuellement avant de la perdre.", TIMEOUT_S)
    finally:
        destroy_instance(api_key, instance_id)

    if result_path:
        log.info("Terminé avec succès. Modèle installé dans le dossier Kittyhack.")
        _write_state({
            "status": "success", "finished_at": time.time(),
            "result_path": result_path, "images": stats["images"],
        })
        return {"status": "success", "result_path": result_path}
    else:
        msg = f"Timeout ({TIMEOUT_S}s) sans résultat reçu."
        _write_state({"status": "error", "message": msg})
        return {"status": "error", "message": msg}


def main():
    api_key = getpass.getpass("Clé API vast.ai (jamais affichée ni loggée) : ").strip()
    if not api_key:
        log.error("Clé vide, abandon.")
        sys.exit(1)
    result = run_training(api_key)
    if result["status"] != "success":
        sys.exit(1)


if __name__ == "__main__":
    main()
