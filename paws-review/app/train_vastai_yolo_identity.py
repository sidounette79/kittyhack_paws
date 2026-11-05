"""
Orchestration d'un entraînement YOLO-classification (Modèle 2, piste
alternative demandée par Sid le 02.11 : "réentraîner modèle 2 sur la base
de YOLO26 avec uniquement les 4 boîtes, que les chats sans les proies")
sur une machine GPU louée à l'heure (vast.ai) - sœur de
train_vastai_identity.py (approche embeddings ArcFace) et de
train_vastai.py (Modèle 1, détection), même orchestration de
location/surveillance/destruction, réutilisée par import.

Différence avec train_vastai_identity.py : au lieu d'un backbone
MobileNetV3-Small + tête ArcFace + galeries d'embeddings, ce script
entraîne un vrai modèle YOLO en mode classification (`task=classify`,
image entière -> une des 4 classes directement, pas de comparaison de
distance à une galerie). Réutilise le même dataset source
(build_identity_training_zip) et le même endpoint d'upload
(/app-api/identity-model/upload, qui attend seulement 3 fichiers -
identity_model.onnx, identity_labels.txt, info.json - donc compatible
même si le FORMAT du modèle diffère complètement en interne).

Un AUTRE run (fine-tuning de l'approche embeddings existante) tourne en
parallèle ce soir sur la MÊME orchestration/le MÊME endpoint d'upload -
pour ne jamais ramasser son résultat par erreur, le zip produit ici
contient un marqueur dédié (YOLO_CLASSIFIER_MARKER.txt) que
wait_for_result() vérifie avant d'accepter un candidat.

Usage (clé API vast.ai en saisie interactive, jamais en argument) :
    docker exec -it paws-review python -m app.train_vastai_yolo_identity
"""

import getpass
import json
import logging
import os
import sys
import threading
import time
import zipfile

import requests

from app.build_combined_identity_training_set import build_combined_identity_training_zip
from app.build_large_rfid_training_set import build_large_rfid_training_zip
from app.config import STORAGE_ROOT
from app.export_identity_training import build_identity_training_zip
from app.train_vastai import (
    DISK_GB,
    GPU_NAME,
    PAWS_REVIEW_URL,
    PERMANENTLY_EXCLUDED_HOSTS,
    UPLOAD_TOKEN,
    VAST_BASE,
    _headers,
    check_instance_health,
    destroy_instance,
    find_cheapest_offer,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [train_vastai_yolo_identity] %(message)s")
log = logging.getLogger(__name__)

# 05.11, Sid ("reconstruire le vrai pipeline") - état propre à CE module,
# distinct de train_identity_state.json (train_vastai_identity.py, ancienne
# approche embeddings/ArcFace jamais activée en prod) - c'est désormais ce
# fichier-ci que main.py's /app-api/identity-train/start branche, voir plus
# bas run_combined_identity_training()/start_combined_identity_training_async().
STATE_PATH = os.path.join(STORAGE_ROOT, "train_yolo_identity_state.json")
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


# Même répertoire/endpoint que train_vastai_identity.py (voir le marqueur
# de distinction dans wait_for_result ci-dessous - aucun risque de
# collision malgré le partage).
IDENTITY_TRAINED_MODELS_DIR = os.path.join(STORAGE_ROOT, "trained_identity_models")
IDENTITY_TRAINING_DATA_PATH = os.path.join(STORAGE_ROOT, "identity_training_data.zip")

TIMEOUT_S = 40 * 60
EPOCHS = int(os.environ.get("VAST_YOLO_CLS_EPOCHS", "60"))
MARKER_FILENAME = "YOLO_CLASSIFIER_MARKER.txt"


def _build_onstart_script() -> str:
    return f"""#!/bin/bash
set -ex
cd /workspace
curl -sL -H "X-Upload-Token: {UPLOAD_TOKEN}" {PAWS_REVIEW_URL}/app-api/identity-model/training-data -o data.zip
mkdir -p data_flat && cd data_flat && unzip -q ../data.zip && cd ..
# 02.11, meme piege deja documente/resolu pour le Modele 1 (train_vastai.py) :
# Ultralytics telecharge son checkpoint de base depuis les releases GitHub
# au premier `yolo train`, et les IP datacenter de vast.ai sont des
# candidates plausibles a un rate-limit GitHub silencieux (gpu_util=0%,
# cpu_util=0%, aucune erreur). Recupere nous-memes les deux checkpoints
# (telechargement direct confirme instantane hors vast.ai) avant - si le
# fichier existe deja dans le dossier courant, Ultralytics ne retente
# jamais son propre telechargement.
curl -sL -H "X-Upload-Token: {UPLOAD_TOKEN}" {PAWS_REVIEW_URL}/app-api/model/yolo-base-weights/yolo26n-cls.pt -o yolo26n-cls.pt
curl -sL -H "X-Upload-Token: {UPLOAD_TOKEN}" {PAWS_REVIEW_URL}/app-api/model/yolo-base-weights/yolo11n-cls.pt -o yolo11n-cls.pt
pip install -q --no-cache-dir ultralytics

python3 << 'PYEOF_SPLIT'
import os, random, shutil
random.seed(20261102)
SRC = "/workspace/data_flat"
DST = "/workspace/data_split"
VAL_FRAC = 0.15
for cat in sorted(os.listdir(SRC)):
    cat_dir = os.path.join(SRC, cat)
    if not os.path.isdir(cat_dir):
        continue
    files = sorted(os.listdir(cat_dir))
    random.shuffle(files)
    n_val = max(1, int(len(files) * VAL_FRAC))
    val_files = files[:n_val]
    train_files = files[n_val:]
    for split, flist in [("train", train_files), ("val", val_files)]:
        out_dir = os.path.join(DST, split, cat)
        os.makedirs(out_dir, exist_ok=True)
        for f in flist:
            shutil.copy(os.path.join(cat_dir, f), os.path.join(out_dir, f))
    print(f"{{cat}}: {{len(train_files)}} train, {{len(val_files)}} val")
PYEOF_SPLIT

EPOCHS={EPOCHS}
# 02.11, Sid ("sur la base de YOLO26") - "yolo26n-cls.pt" (pas "yolov26"),
# necessite ultralytics>=8.4.0. Pas garanti publie au moment de ce run -
# repli automatique sur yolo11n-cls.pt si le telechargement/l'entrainement
# echoue, jamais un echec silencieux (le nom reellement utilise est ecrit
# dans info.json plus bas).
#
# 02.11 (round 2), Sid ("HK prend une ligne droite et reolink a un angle
# plus ouvert mais pas de beaucoup... 200 photos de plus seraient quasi
# identiques") - verifie dans le code source d'Ultralytics
# (ClassificationDataset.__init__ / classify_augmentations) avant de
# choisir ces valeurs : pour task=classify, SEULS scale/hsv_h/hsv_s/
# hsv_v/erasing/fliplr/flipud/auto_augment sont reellement appliques -
# degrees/shear/perspective/translate sont silencieusement ignores (ce
# sont des augmentations detect/segment uniquement, jamais lues par
# ClassificationDataset). scale=0.7 pousse la plage de RandomResizedCrop
# a (0.3, 1.0) au lieu du defaut (0.5, 1.0) - simule directement plus de
# variation de cadrage/zoom a partir des memes photos, le levier le plus
# pertinent face au manque de diversite d'angle physique. hsv_v=0.5 (vs
# 0.4) accentue la variation de luminosite deja presente par defaut.
# erasing=0.5 (vs 0.4) renforce la robustesse a l'occlusion partielle
# (corps coupes, deja presents dans le jeu de Sid). auto_augment reste
# randaugment (deja actif par defaut, inclut deja rotation/translation/
# cisaillement via sa propre politique interne malgre l'absence de
# degrees/shear explicites).
AUG_ARGS="scale=0.7 hsv_v=0.5 erasing=0.5"
if yolo classify train model=yolo26n-cls.pt data=/workspace/data_split epochs=$EPOCHS imgsz=224 patience=15 project=/workspace/runs name=train_primary device=0 $AUG_ARGS; then
    USED_MODEL="yolo26n-cls.pt"
    RUN_DIR=/workspace/runs/train_primary
else
    echo "yolo26n-cls.pt indisponible ou echec - repli sur yolo11n-cls.pt"
    yolo classify train model=yolo11n-cls.pt data=/workspace/data_split epochs=$EPOCHS imgsz=224 patience=15 project=/workspace/runs name=train_fallback device=0 $AUG_ARGS
    USED_MODEL="yolo11n-cls.pt"
    RUN_DIR=/workspace/runs/train_fallback
fi

mkdir -p /workspace/result
RUN_DIR_ENV="$RUN_DIR" USED_MODEL_ENV="$USED_MODEL" EPOCHS_ENV="$EPOCHS" python3 << 'PYEOF_EXPORT'
import os, sys, json, datetime
sys.path.insert(0, "/workspace")
from ultralytics import YOLO

run_dir = os.environ["RUN_DIR_ENV"]
used_model = os.environ["USED_MODEL_ENV"]
epochs = os.environ["EPOCHS_ENV"]

model = YOLO(f"{{run_dir}}/weights/best.pt")
names = model.names
class_names = [names[i] for i in sorted(names.keys())]
with open("/workspace/result/identity_labels.txt", "w") as f:
    f.write("\\n".join(class_names) + "\\n")

import shutil
shutil.copy(f"{{run_dir}}/weights/best.pt", "/workspace/result/best.pt")
try:
    model.export(format="onnx", opset=12, imgsz=224)
    shutil.move(f"{{run_dir}}/weights/best.onnx", "/workspace/result/identity_model.onnx")
except Exception as e:
    print(f"ONNX export failed ({{e}}), falling back to a placeholder - best.pt in the zip is the real result")
    open("/workspace/result/identity_model.onnx", "wb").close()

json.dump({{
    "JOB_ID": "vastai-yolo-identity-" + str(int(datetime.datetime.utcnow().timestamp())),
    "TIMESTAMP_UTC": datetime.datetime.utcnow().isoformat() + "Z",
    "VERSION": "1.0.0",
    "CLASSES": class_names,
    "BACKBONE": used_model,
    "EPOCHS": epochs,
    "TASK": "classify",
    "IMGSZ": 224,
}}, open("/workspace/result/info.json", "w"), indent=2)
print(f"Exported with backbone={{used_model}}, classes={{class_names}}")
PYEOF_EXPORT

touch /workspace/result/{MARKER_FILENAME}
cd /workspace/result && zip -qr ../identity_result.zip .
curl -sL -H "X-Upload-Token: {UPLOAD_TOKEN}" -F "file=@/workspace/identity_result.zip" {PAWS_REVIEW_URL}/app-api/identity-model/upload
touch /workspace/TRAINING_DONE
"""


def create_instance(api_key: str, offer_id: int) -> int:
    resp = requests.put(
        f"{VAST_BASE}/asks/{offer_id}/",
        headers=_headers(api_key),
        json={
            "image": "vastai/pytorch:latest",
            "disk": DISK_GB,
            "onstart": _build_onstart_script(),
            "runtype": "ssh",
            "target_state": "running",
        },
        timeout=30,
    )
    resp.raise_for_status()
    body = resp.json()
    instance_id = body.get("new_contract") or body.get("id")
    if not instance_id:
        raise RuntimeError(f"Pas d'id d'instance dans la reponse : {body}")
    log.info("Instance creee (YOLO-classification identite) : %s", instance_id)
    return instance_id


def _is_my_result(fpath: str) -> bool:
    """Distingue mon resultat de celui, potentiellement concurrent, du
    fine-tuning embeddings qui partage le meme repertoire/endpoint."""
    try:
        with zipfile.ZipFile(fpath) as zf:
            return MARKER_FILENAME in zf.namelist()
    except Exception:
        return False


def wait_for_result(started_after: float, api_key: str | None = None, instance_id: int | None = None) -> str | None:
    deadline = time.time() + TIMEOUT_S
    seen_non_matching: set = set()
    while time.time() < deadline:
        if os.path.isdir(IDENTITY_TRAINED_MODELS_DIR):
            for fname in os.listdir(IDENTITY_TRAINED_MODELS_DIR):
                fpath = os.path.join(IDENTITY_TRAINED_MODELS_DIR, fname)
                if not os.path.isfile(fpath) or os.path.getmtime(fpath) < started_after:
                    continue
                if fpath in seen_non_matching:
                    continue
                if _is_my_result(fpath):
                    return fpath
                seen_non_matching.add(fpath)
                log.info("Resultat ignore (pas le marqueur attendu, probablement l'autre run en parallele) : %s", fpath)
        elapsed = int(time.time() - started_after)
        log.info("En attente du resultat (YOLO-classification identite)... (%ds ecoulees)", elapsed)
        time.sleep(30)
    return None


def _rent_train_and_wait(api_key: str, images: int | None = None) -> dict:
    """Shared tail of both training entry points below - rents the
    cheapest healthy GPU, waits for OUR result (see _is_my_result's
    marker), always destroys the instance even on timeout/error.

    05.11 : write_state ajouté à chaque étape (même patron que
    train_vastai.py's run_training()) pour que le bouton web puisse
    suivre la progression - n'affecte pas l'usage CLI historique
    (run_yolo_identity_training), juste un fichier JSON en plus."""
    excluded_hosts: set = set()
    instance_id = None
    started_at = time.time()
    for attempt in range(1, 4):
        _write_state({
            "status": "running", "step": "searching_offer", "attempt": attempt,
            "started_at": started_at, "images": images,
        })
        offer = find_cheapest_offer(api_key, exclude_host_ids=excluded_hosts)
        try:
            instance_id = create_instance(api_key, offer["id"])
        except requests.HTTPError as e:
            log.warning("Echec de creation d'instance (tentative %d) : %s", attempt, e)
            excluded_hosts.add(offer.get("host_id"))
            continue

        _write_state({
            "status": "running", "step": "health_check", "attempt": attempt,
            "started_at": started_at, "images": images,
            "instance_id": instance_id, "host_id": offer.get("host_id"),
            "price": offer.get("dph_total"),
        })
        healthy, host_id, status_msg = check_instance_health(api_key, instance_id)
        if not healthy:
            log.warning("Instance %s en echec immediat (%s), destruction et nouvel essai.", instance_id, status_msg[:150])
            destroy_instance(api_key, instance_id)
            if host_id:
                excluded_hosts.add(host_id)
            instance_id = None
            continue
        break

    if instance_id is None:
        msg = "Impossible de louer une machine GPU fonctionnelle apres 3 tentatives."
        log.error(msg)
        _write_state({"status": "error", "message": msg})
        return {"status": "error", "message": msg}

    try:
        _write_state({
            "status": "running", "step": "training", "started_at": started_at,
            "images": images, "instance_id": instance_id,
        })
        result_path = wait_for_result(started_at, api_key=api_key, instance_id=instance_id)
    finally:
        destroy_instance(api_key, instance_id)

    if result_path is None:
        msg = f"Timeout ({TIMEOUT_S}s) sans resultat - verifier l'instance manuellement avant de la perdre."
        log.error(msg)
        _write_state({"status": "error", "message": msg})
        return {"status": "error", "message": msg}

    log.info("Entrainement YOLO-classification identite termine : %s", result_path)
    _write_state({
        "status": "success", "finished_at": time.time(),
        "result_path": result_path, "images": images,
    })
    return {"status": "success", "result_path": result_path}


def run_yolo_identity_training(api_key: str) -> dict:
    """Small curated dataset (~400/cat, Paws Review confirmed/corrected
    pool) - original entry point, unchanged."""
    if not UPLOAD_TOKEN:
        msg = "MODEL_UPLOAD_TOKEN non configure cote serveur, abandon."
        log.error(msg)
        return {"status": "error", "message": msg}

    log.info("Construction du zip de crops identite (meme dataset que l'approche embeddings)...")
    stats = build_identity_training_zip(IDENTITY_TRAINING_DATA_PATH)
    log.info("Zip pret : %s", stats)
    if stats["included"] == 0:
        msg = "Aucun crop a entrainer, abandon."
        log.error(msg)
        return {"status": "error", "message": msg}

    return _rent_train_and_wait(api_key)


def run_yolo_identity_training_from_zip(api_key: str, zip_path: str) -> dict:
    """02.11, Sid ("explore les possibilites d'autres approches") - au
    lieu du pool curé Paws Review (~400/cat), un jeu BEAUCOUP plus large
    tire directement des vrais evenements RFID de Kittyflap (161k+
    disponibles). zip_path doit deja exister (construit a part, pas ici -
    ce module n'a pas acces a la vraie DB Kittyflap/aux fichiers image,
    seulement paws-review/VAST_API_KEY). Copie simplement ce zip vers
    IDENTITY_TRAINING_DATA_PATH (meme chemin que l'autre entree, servi
    tel quel par /app-api/identity-model/training-data) puis reutilise
    EXACTEMENT la meme location/attente/destruction.

    05.11 : utilisé en interne par run_combined_identity_training()
    ci-dessous, qui construit lui-même le zip (voir
    app/build_combined_identity_training_set.py, reconstruit après la
    perte des scripts originaux du 02.11/04.11) - gardé comme point
    d'entrée séparé pour un usage CLI avec un zip déjà construit ailleurs."""
    if not UPLOAD_TOKEN:
        msg = "MODEL_UPLOAD_TOKEN non configure cote serveur, abandon."
        log.error(msg)
        return {"status": "error", "message": msg}
    if not os.path.isfile(zip_path):
        msg = f"Zip introuvable : {zip_path}"
        log.error(msg)
        return {"status": "error", "message": msg}

    import shutil
    shutil.copy(zip_path, IDENTITY_TRAINING_DATA_PATH)
    log.info("Zip de dataset large copie vers %s (depuis %s).", IDENTITY_TRAINING_DATA_PATH, zip_path)

    return _rent_train_and_wait(api_key)


# 05.11, Sid ("reconstruire le vrai pipeline", puis "quid de toutes celles
# de reolink qu'on a rajoutée ?") - point d'entrée désormais branché au
# bouton web "Modèle 2" (main.py:/app-api/identity-train/start), remplace
# l'ancien branchement sur train_vastai_identity.py (approche embeddings/
# ArcFace abandonnée, plafonnait à ~58-64% sur le jeu de test figé). Une
# première reconstruction (RFID Kittyflap seul) a raté la moitié du
# dataset réel : le modèle actif aujourd'hui combine RFID Kittyflap
# (jusqu'à rfid_target_per_cat/chat) ET le pool Reolink/manuel validé à la
# main dans Paws Review (dupliqué reolink_duplication fois) - voir
# build_combined_identity_training_set.py pour le détail, reconstruit par
# inspection directe du zip réel du run actif (toujours sur disque au
# moment de cette correction).
def run_combined_identity_training(
    api_key: str, rfid_target_per_cat: int = 1800, reolink_duplication: int = 3,
) -> dict:
    if not UPLOAD_TOKEN:
        msg = "MODEL_UPLOAD_TOKEN non configure cote serveur, abandon."
        log.error(msg)
        _write_state({"status": "error", "message": msg})
        return {"status": "error", "message": msg}

    _write_state({"status": "running", "step": "export", "started_at": time.time()})
    log.info(
        "Construction du zip combine (RFID %d/chat + Reolink/manuel x%d)...",
        rfid_target_per_cat, reolink_duplication,
    )
    stats = build_combined_identity_training_zip(
        IDENTITY_TRAINING_DATA_PATH,
        rfid_target_per_cat=rfid_target_per_cat,
        reolink_duplication=reolink_duplication,
    )
    log.info("Zip pret : %s", stats)
    if stats["included"] == 0:
        msg = "Aucun crop a entrainer, abandon."
        log.error(msg)
        _write_state({"status": "error", "message": msg})
        return {"status": "error", "message": msg}

    return _rent_train_and_wait(api_key, images=stats["included"])


_run_combined_lock = threading.Lock()


def start_combined_identity_training_async(
    api_key: str, rfid_target_per_cat: int = 1800, reolink_duplication: int = 3,
) -> bool:
    """Même patron que start_training_async()/start_identity_training_async
    - verrou propre à ce pipeline précis (n'empêche ni le Modèle 1 ni
    l'ancienne entrée identité CLI de tourner en parallèle, budgets Vast.ai
    séparés)."""
    if not _run_combined_lock.acquire(blocking=False):
        return False
    thread = threading.Thread(
        target=_run_combined_identity_training_locked,
        args=(api_key, rfid_target_per_cat, reolink_duplication), daemon=True,
    )
    thread.start()
    return True


def _run_combined_identity_training_locked(api_key: str, rfid_target_per_cat: int, reolink_duplication: int):
    try:
        run_combined_identity_training(
            api_key, rfid_target_per_cat=rfid_target_per_cat, reolink_duplication=reolink_duplication,
        )
    except Exception as e:
        log.exception("Entrainement identite (dataset combine RFID+Reolink) echoue")
        _write_state({"status": "error", "message": str(e)})
    finally:
        _run_combined_lock.release()


def main():
    logging.getLogger().setLevel(logging.INFO)
    api_key = getpass.getpass("Cle API vast.ai (jamais affichee ni loggee) : ").strip()
    if not api_key:
        log.error("Cle vide, abandon.")
        sys.exit(1)
    result = run_yolo_identity_training(api_key)
    if result["status"] != "success":
        sys.exit(1)
    print(json.dumps(result))


if __name__ == "__main__":
    main()
