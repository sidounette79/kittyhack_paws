"""
Mesure, pas production : entraîne un classifieur softmax direct à 4
sorties (MobileNetV3-Small + tête linéaire, cross-entropy) comme
architecture ALTERNATIVE à l'approche embeddings/ArcFace actuelle
(train_vastai_identity.py), pour comparer les deux contre le même jeu de
test figé (vérité RFID réelle, voir
/volume1/docker/kittyhack-remote/data/identity_eval_scripts/).

02.11, Sid ("il me faut un taux de au moins 90%... on fait les deux et on
regarde le mieux") - 4 chats fixes, connus d'avance, jamais de
ré-identification ouverte à prévoir (confirmé par Sid : "va pas y en
avoir un de sitôt"), donc un classifieur direct est un problème
structurellement plus simple que la ré-id par embeddings/distance.

Réutilise l'orchestration Vast.ai existante telle quelle
(find_cheapest_offer/check_instance_health/destroy_instance de
train_vastai.py) et le même dataset (build_identity_training_zip) - ne
duplique QUE le script d'entraînement embarqué (onstart), qui diffère par
l'architecture de tête et ajoute un vrai split train/validation (absent
de train_vastai_identity.py, cause probable de l'instabilité déjà
mesurée là-bas: 46.6%/49.2%/58.4% selon le run).

Usage (clé API vast.ai lue depuis l'environnement VAST_API_KEY, jamais en
argument - même convention que la prod) :
    docker exec paws-review python -m app.train_vastai_classifier
"""

import json
import logging
import os
import sys
import time

import requests

from app.config import STORAGE_ROOT
from app.export_identity_training import build_identity_training_zip
from app.train_vastai import (
    DISK_GB,
    GPU_NAME,
    PAWS_REVIEW_URL,
    UPLOAD_TOKEN,
    VAST_BASE,
    _headers,
    check_instance_health,
    destroy_instance,
    find_cheapest_offer,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [train_vastai_classifier] %(message)s")
log = logging.getLogger(__name__)

# Répertoires propres à cette mesure - ne touche JAMAIS
# IDENTITY_TRAINED_MODELS_DIR/IDENTITY_MODELS_DIR (train_vastai_identity.py
# tourne peut-être en parallèle ce soir, même serveur) pour éviter toute
# course entre les deux résultats. On réutilise quand même
# /app-api/identity-model/upload côté serveur (infra déjà testée, pas de
# nouveau code serveur) mais on isole le RÉSULTAT dès réception via le
# marqueur CLASSIFIER_MARKER.json plutôt que de laisser le classifieur
# se mélanger dans le dossier des modèles d'identité "officiels".
# 02.11 - servie via le mount statique DÉJÀ ACTIF /model-comparisons
# (main.py, app.mount sur MODEL_COMPARISONS_DIR) plutôt qu'un nouvel
# endpoint - évite tout risque de toucher main.py pendant que
# train_vastai_identity.py tourne peut-être en parallèle ce soir sur le
# même serveur. Pas de jeton sur ce mount (juste des crops de chat, pas
# sensible) - acceptable pour une mesure ponctuelle, pas un nouveau canal
# permanent.
MODEL_COMPARISONS_DIR = os.path.join(STORAGE_ROOT, "model_comparisons")
CLASSIFIER_TRAINING_DATA_PATH = os.path.join(MODEL_COMPARISONS_DIR, "classifier_training_data.zip")
CLASSIFIER_RESULTS_DIR = "/volume1/docker/kittyhack-remote/data/classifier_models"
# Côté serveur (/app-api/identity-model/upload), le zip uploadé brut est
# gardé ici AVANT extraction - c'est LÀ qu'on va chercher le nôtre via le
# marqueur, pas dans IDENTITY_MODELS_DIR (le dossier des modèles
# officiellement activables, qu'on ne veut pas polluer avec un format
# incompatible - ce classifieur n'a pas de galleries/, CatIdentity ne
# saurait pas le charger).
IDENTITY_TRAINED_MODELS_DIR = os.path.join(STORAGE_ROOT, "trained_identity_models")

TIMEOUT_S = 40 * 60
CLASSIFIER_EPOCHS = int(os.environ.get("VAST_CLASSIFIER_EPOCHS", "40"))
MARKER = "CLASSIFIER_MARKER_v1"


def _build_onstart_script() -> str:
    # 19.10/29.10 (même raison que train_vastai.py/train_vastai_identity.py) :
    # clé API vast.ai jamais déposée ici - cette machine n'est pas à moi.
    return f"""#!/bin/bash
set -ex
cd /workspace
curl -sL {PAWS_REVIEW_URL}/model-comparisons/classifier_training_data.zip -o data.zip
mkdir -p data && cd data && unzip -q ../data.zip && cd ..
pip install -q --no-cache-dir torch torchvision numpy onnx onnxscript scikit-learn
cat > train_classifier.py << 'PYEOF'
import json, datetime, os, random
import numpy as np
import torch
import torch.nn as nn
from torch.utils.data import DataLoader, Subset
from torchvision import datasets, transforms, models

EPOCHS = {CLASSIFIER_EPOCHS}
BATCH_SIZE = 32
LR = 1e-3
VAL_FRACTION = 0.15
SEED = 20261102
DEVICE = "cuda" if torch.cuda.is_available() else "cpu"

random.seed(SEED)
torch.manual_seed(SEED)


def _capture_id_from_path(path):
    # Filenames are "<cat_name>/capture_<id>_<bucket>.jpg" (see
    # export_identity_training.py) - ids from the same passage are
    # created moments apart, almost consecutive (confirmed on real data:
    # 266,267,268,270 all Nala, same capture burst).
    base = os.path.basename(path)
    try:
        return int(base.split("_")[1])
    except (IndexError, ValueError):
        return 0


def stratified_split(dataset, val_fraction, seed):
    # 02.11, round 2 - premiere version shuffle-puis-coupe (random par
    # classe) laissait des frames quasi-identiques du MEME passage (ids
    # consecutifs, meme chat, meme lumiere) tomber des deux cotes du
    # split - le val_acc mesure alors en partie "ai-je deja vu une frame
    # presque identique a l'entrainement", pas une vraie generalisation.
    # Confirme en pratique : premier run val_acc=91.3% en interne, mais
    # seulement 63.2% sur le jeu de test independant (RFID reel, jamais
    # vu). Trie maintenant par capture_id et prend un BLOC CONTIGU en
    # validation par classe - les passages entiers restent groupes d'un
    # seul cote, le signal de validation redevient honnete.
    by_class = {{}}
    for idx, (path, label) in enumerate(dataset.samples):
        by_class.setdefault(label, []).append((idx, _capture_id_from_path(path)))
    train_idx, val_idx = [], []
    for label, idx_ids in by_class.items():
        idx_ids = sorted(idx_ids, key=lambda t: t[1])
        idxs = [i for i, _ in idx_ids]
        n_val = max(1, int(len(idxs) * val_fraction))
        # Bloc du MILIEU plutot que la fin - evite que la validation
        # porte systematiquement sur les passages les plus RECENTS d'un
        # chat (distribution temporelle potentiellement differente du
        # reste, ex: saison/lumiere), un echantillon du milieu est plus
        # representatif de l'ensemble de la plage chronologique.
        start = (len(idxs) - n_val) // 2
        val_idx.extend(idxs[start:start + n_val])
        train_idx.extend(idxs[:start] + idxs[start + n_val:])
    return train_idx, val_idx


def main():
    data_dir = "/workspace/data"
    norm = transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225])
    train_tf = transforms.Compose([
        transforms.Resize((224, 224)),
        transforms.RandomHorizontalFlip(),
        transforms.ColorJitter(0.2, 0.2, 0.2),
        transforms.ToTensor(),
        norm,
    ])
    eval_tf = transforms.Compose([transforms.Resize((224, 224)), transforms.ToTensor(), norm])

    full_train_view = datasets.ImageFolder(data_dir, transform=train_tf)
    full_eval_view = datasets.ImageFolder(data_dir, transform=eval_tf)
    class_names = full_train_view.classes
    print(f"Classes: {{class_names}} ({{len(full_train_view)}} crops)")

    train_idx, val_idx = stratified_split(full_train_view, VAL_FRACTION, SEED)
    print(f"Train: {{len(train_idx)}} crops, Val: {{len(val_idx)}} crops")

    train_loader = DataLoader(Subset(full_train_view, train_idx), batch_size=BATCH_SIZE, shuffle=True, num_workers=4, drop_last=True)
    val_loader = DataLoader(Subset(full_eval_view, val_idx), batch_size=BATCH_SIZE, shuffle=False, num_workers=4)

    backbone = models.mobilenet_v3_small(weights=models.MobileNet_V3_Small_Weights.DEFAULT)
    backbone.classifier = nn.Sequential(
        nn.Linear(576, 128),
        nn.Hardswish(),
        nn.Dropout(0.3),
        nn.Linear(128, len(class_names)),
    )
    model = backbone.to(DEVICE)
    criterion = nn.CrossEntropyLoss()
    optimizer = torch.optim.Adam(model.parameters(), lr=LR)
    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=EPOCHS)

    best_val_acc = -1.0
    best_state = None
    best_epoch = -1

    for epoch in range(EPOCHS):
        model.train()
        total_loss, n_batches = 0.0, 0
        for imgs, labels in train_loader:
            imgs, labels = imgs.to(DEVICE), labels.to(DEVICE)
            logits = model(imgs)
            loss = criterion(logits, labels)
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()
            total_loss += loss.item()
            n_batches += 1
        scheduler.step()

        model.eval()
        correct, total = 0, 0
        with torch.no_grad():
            for imgs, labels in val_loader:
                imgs, labels = imgs.to(DEVICE), labels.to(DEVICE)
                preds = model(imgs).argmax(dim=1)
                correct += (preds == labels).sum().item()
                total += labels.size(0)
        val_acc = correct / max(total, 1)
        print(f"epoch {{epoch+1}}/{{EPOCHS}} loss={{total_loss/max(n_batches,1):.4f}} val_acc={{val_acc:.4f}}")

        if val_acc > best_val_acc:
            best_val_acc = val_acc
            best_epoch = epoch + 1
            best_state = {{k: v.cpu().clone() for k, v in model.state_dict().items()}}

    print(f"Best checkpoint: epoch {{best_epoch}}, val_acc={{best_val_acc:.4f}}")
    model.load_state_dict(best_state)
    model.eval()

    os.makedirs("/workspace/result", exist_ok=True)
    with open("/workspace/result/identity_labels.txt", "w") as f:
        f.write("\\n".join(class_names) + "\\n")

    dummy = torch.randn(1, 3, 224, 224).to(DEVICE)
    # Meme piege documente dans train_vastai_identity.py : l'exporteur
    # "dynamo" par defaut casse la lecture par OpenVINO 2023.3.0 (epingle
    # pour le GPU Intel de production) - dynamo=False (ancien exporteur
    # TorchScript) confirme la-bas, reutilise ici a l'identique.
    torch.onnx.export(
        model, dummy, "/workspace/result/identity_model.onnx",
        input_names=["input"], output_names=["logits"],
        dynamic_axes={{"input": {{0: "batch"}}, "logits": {{0: "batch"}}}},
        opset_version=12,
        dynamo=False,
    )

    json.dump({{
        "JOB_ID": "vastai-classifier-" + str(int(datetime.datetime.utcnow().timestamp())),
        "TIMESTAMP_UTC": datetime.datetime.utcnow().isoformat() + "Z",
        "VERSION": "1.0.0",
        "CLASSES": class_names,
        "BACKBONE": "mobilenet_v3_small",
        "ARCHITECTURE": "direct_softmax_classifier",
        "EPOCHS": EPOCHS,
        "BEST_EPOCH": best_epoch,
        "BEST_VAL_ACC": best_val_acc,
    }}, open("/workspace/result/info.json", "w"), indent=2)

    with open("/workspace/result/{MARKER}.json", "w") as f:
        json.dump({{"marker": "{MARKER}", "best_val_acc": best_val_acc}}, f)


if __name__ == "__main__":
    main()
PYEOF
mkdir -p /workspace/result
python3 train_classifier.py
cd /workspace/result && zip -qr ../classifier_result.zip .
curl -sL -H "X-Upload-Token: {UPLOAD_TOKEN}" -F "file=@/workspace/classifier_result.zip" {PAWS_REVIEW_URL}/app-api/identity-model/upload
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
        raise RuntimeError(f"Pas d'id d'instance dans la réponse : {body}")
    log.info("Instance créée (entraînement classifieur) : %s", instance_id)
    return instance_id


def wait_for_marked_result(started_after: float) -> str | None:
    """Même patron que wait_for_result() de train_vastai_identity.py,
    mais filtre sur le marqueur CLASSIFIER_MARKER_v1 dans le zip plutôt
    que juste la date - train_vastai_identity.py tourne peut-être en
    parallèle ce soir sur le MÊME serveur/dossier IDENTITY_TRAINED_MODELS_DIR,
    la date seule ne suffirait pas à distinguer les deux résultats."""
    import zipfile

    deadline = time.time() + TIMEOUT_S
    seen = set()
    while time.time() < deadline:
        if os.path.isdir(IDENTITY_TRAINED_MODELS_DIR):
            for fname in os.listdir(IDENTITY_TRAINED_MODELS_DIR):
                fpath = os.path.join(IDENTITY_TRAINED_MODELS_DIR, fname)
                if fpath in seen or not os.path.isfile(fpath):
                    continue
                if os.path.getmtime(fpath) < started_after:
                    continue
                try:
                    with zipfile.ZipFile(fpath) as zf:
                        if any(MARKER in n for n in zf.namelist()):
                            return fpath
                except zipfile.BadZipFile:
                    pass
                seen.add(fpath)
        elapsed = int(time.time() - started_after)
        log.info("En attente du résultat (classifieur)... (%ds écoulées)", elapsed)
        time.sleep(30)
    return None


def run_classifier_training(api_key: str) -> dict:
    if not UPLOAD_TOKEN:
        msg = "MODEL_UPLOAD_TOKEN non configuré côté serveur, abandon."
        log.error(msg)
        return {"status": "error", "message": msg}

    os.makedirs(MODEL_COMPARISONS_DIR, exist_ok=True)
    log.info("Construction du zip de crops identité (réutilisé pour le classifieur)...")
    stats = build_identity_training_zip(CLASSIFIER_TRAINING_DATA_PATH)
    log.info("Zip prêt : %s", stats)
    if stats["included"] == 0:
        msg = "Aucun crop à entraîner, abandon."
        log.error(msg)
        return {"status": "error", "message": msg}

    excluded_hosts: set = set()
    instance_id = None
    started_at = time.time()
    for attempt in range(1, 4):
        offer = find_cheapest_offer(api_key, exclude_host_ids=excluded_hosts)
        try:
            instance_id = create_instance(api_key, offer["id"])
        except requests.HTTPError as e:
            log.warning("Échec de création d'instance (tentative %d) : %s", attempt, e)
            excluded_hosts.add(offer.get("host_id"))
            continue

        healthy, host_id, status_msg = check_instance_health(api_key, instance_id)
        if not healthy:
            log.warning("Instance %s en échec immédiat (%s), destruction et nouvel essai.", instance_id, status_msg[:150])
            destroy_instance(api_key, instance_id)
            if host_id:
                excluded_hosts.add(host_id)
            instance_id = None
            continue
        break

    if instance_id is None:
        msg = "Impossible de louer une machine GPU fonctionnelle après 3 tentatives."
        log.error(msg)
        return {"status": "error", "message": msg}

    try:
        result_path = wait_for_marked_result(started_at)
    finally:
        destroy_instance(api_key, instance_id)

    if result_path is None:
        msg = f"Timeout ({TIMEOUT_S}s) sans résultat marqué - voir les logs pour le diagnostic."
        log.error(msg)
        return {"status": "error", "message": msg}

    # Extraction manuelle dans NOTRE propre dossier (pas IDENTITY_MODELS_DIR,
    # pour ne pas polluer la liste des modèles d'identité officiels avec un
    # format que CatIdentity/galleries ne sait pas charger).
    import zipfile
    ts = time.strftime("%Y%m%d_%H%M%S")
    target_dir = os.path.join(CLASSIFIER_RESULTS_DIR, ts)
    os.makedirs(target_dir, exist_ok=True)
    with zipfile.ZipFile(result_path) as zf:
        zf.extractall(target_dir)
    log.info("Résultat classifieur extrait vers : %s", target_dir)

    return {"status": "success", "finished_at": time.time(), "crops": stats["included"], "result_dir": target_dir}


def main():
    api_key = os.environ.get("VAST_API_KEY", "")
    if not api_key:
        log.error("VAST_API_KEY non défini dans l'environnement, abandon.")
        sys.exit(1)
    result = run_classifier_training(api_key)
    print(json.dumps(result, indent=2))
    if result["status"] != "success":
        sys.exit(1)


if __name__ == "__main__":
    main()
