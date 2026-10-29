"""
Orchestration d'un entraînement d'embeddings (Modèle 2 - identité par
ré-identification, voir le plan de scission détection/identité,
/volume1/docker/claude-home-data/plans/twinkly-percolating-wombat.md,
Phase 2) sur une machine GPU louée à l'heure (vast.ai) - sœur de
train_vastai.py (Modèle 1, détection YOLO), même orchestration de
location/surveillance/destruction, réutilisée ici par import plutôt que
dupliquée (find_cheapest_offer/check_instance_health/destroy_instance ne
contiennent rien de spécifique à YOLO).

Déroulé :
  1. Construit le zip de crops étiquetés par chat (voir
     export_identity_training.build_identity_training_zip) et le sert à
     une URL dédiée que l'instance louée viendra chercher.
  2. Loue une machine GPU (même recherche/tri que le Modèle 1).
  3. La machine (script onstart) télécharge les crops, installe torch/
     torchvision (déjà présents dans l'image vastai/pytorch de base),
     entraîne un petit backbone (MobileNetV3-Small) + tête ArcFace
     (margin loss, cf. recherche menée le 29.10 - standard pour la
     ré-identification/reconnaissance faciale à partir de peu
     d'exemples par identité), calcule une galerie d'embeddings de
     référence par chat, exporte le backbone (sans la tête ArcFace,
     inutile à l'inférence) en ONNX, empaquette le tout et pousse le
     résultat vers /app-api/identity-model/upload.
  4. Ce script-ci surveille l'arrivée du résultat puis détruit la
     machine louée - même garde-fou que le Modèle 1 (clé API vast.ai
     jamais déposée sur la machine louée).

**29.10 : écrit et relu, PAS ENCORE testé sur une vraie machine louée**
(contrairement à export_identity_training.py, validé en direct contre
les vraies données ce soir). Le script d'entraînement embarqué
(ArcFace/MobileNetV3) suit une architecture standard documentée, mais
le premier run réel sur Vast.ai est le vrai test - jalon de validation
obligatoire avant toute activation (voir Phase 6 du plan,
identity_compare.py, pas encore écrit).

Usage (clé API vast.ai en saisie interactive, jamais en argument) :
    docker exec -it paws-review python -m app.train_vastai_identity
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

logging.basicConfig(level=logging.INFO, format="%(asctime)s [train_vastai_identity] %(message)s")
log = logging.getLogger(__name__)

# Répertoires/état PROPRES au Modèle 2 - jamais partagés avec
# train_vastai.py (TRAINED_MODELS_DIR/STATE_PATH), pour que les deux
# entraînements ne puissent jamais se marcher dessus (wait_for_result
# du Modèle 1 ne doit jamais ramasser par erreur un résultat du Modèle 2,
# et réciproquement).
IDENTITY_TRAINED_MODELS_DIR = os.path.join(STORAGE_ROOT, "trained_identity_models")
IDENTITY_TRAINING_DATA_PATH = os.path.join(STORAGE_ROOT, "identity_training_data.zip")
STATE_PATH = os.path.join(STORAGE_ROOT, "train_identity_state.json")
_run_lock = threading.Lock()


def start_identity_training_async(api_key: str) -> bool:
    """Même patron que start_training_async() de train_vastai.py, verrou
    propre à ce module - n'empêche jamais un entraînement Modèle 1 en
    cours, seulement un deuxième entraînement identité simultané."""
    if not _run_lock.acquire(blocking=False):
        return False
    thread = threading.Thread(target=_run_identity_training_locked, args=(api_key,), daemon=True)
    thread.start()
    return True


def _run_identity_training_locked(api_key: str):
    try:
        run_identity_training(api_key)
    except Exception as e:
        log.exception("Entraînement identité échoué")
        _write_state({"status": "error", "message": str(e)})
    finally:
        _run_lock.release()

# 29.10, Sid ("fine-tuning réel de la tête d'embedding... pas une simple
# mise à jour de galerie") - même budget temps/prix que le Modèle 1 par
# défaut (un entraînement embeddings sur ~1600 crops est nettement plus
# léger qu'un YOLO sur plusieurs milliers d'images, mais pas la peine de
# complexifier la recherche d'offre pour ça tout de suite - la même
# enveloppe suffit largement et reste cohérente avec train_vastai.py).
TIMEOUT_S = 40 * 60
IDENTITY_EPOCHS = int(os.environ.get("VAST_IDENTITY_EPOCHS", "30"))
EMBED_DIM = 128


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


def _build_onstart_script() -> str:
    # 19.10 (même raison que train_vastai.py) : clé API vast.ai jamais
    # déposée ici - cette machine n'est pas à moi, elle pousse juste son
    # résultat et s'arrête là.
    return f"""#!/bin/bash
set -ex
cd /workspace
curl -sL -H "X-Upload-Token: {UPLOAD_TOKEN}" {PAWS_REVIEW_URL}/app-api/identity-model/training-data -o data.zip
mkdir -p data && cd data && unzip -q ../data.zip && cd ..
# 29.10, premier run réel : "ModuleNotFoundError: No module named 'torch'"
# sur l'image vastai/pytorch - torch/torchvision ne sont PAS garantis
# disponibles via le python3 par défaut malgré le nom de l'image (même
# piège que ultralytics côté Modèle 1, qui s'installe explicitement lui
# aussi - jamais supposer qu'une dépendance est déjà là).
pip install -q --no-cache-dir torch torchvision numpy onnx onnxscript
cat > train_identity.py << 'PYEOF'
import json, math, datetime, os
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.utils.data import DataLoader
from torchvision import datasets, transforms, models

EMBED_DIM = {EMBED_DIM}
EPOCHS = {IDENTITY_EPOCHS}
BATCH_SIZE = 32
LR = 1e-3
ARCFACE_MARGIN = 0.5
ARCFACE_SCALE = 30.0
DEVICE = "cuda" if torch.cuda.is_available() else "cpu"


class ArcMarginProduct(nn.Module):
    # Standard ArcFace (Deng et al. 2019) - marge angulaire additive,
    # le patron le plus courant pour la ré-identification/reconnaissance
    # faciale à partir de peu d'exemples par identité (voir la recherche
    # menée le 29.10 avec Sid).
    def __init__(self, in_features, out_features, s=ARCFACE_SCALE, m=ARCFACE_MARGIN):
        super().__init__()
        self.weight = nn.Parameter(torch.FloatTensor(out_features, in_features))
        nn.init.xavier_uniform_(self.weight)
        self.s = s
        self.m = m
        self.cos_m = math.cos(m)
        self.sin_m = math.sin(m)

    def forward(self, embeddings, labels):
        cosine = F.linear(F.normalize(embeddings), F.normalize(self.weight))
        sine = torch.sqrt((1.0 - torch.clamp(cosine ** 2, 0, 1)))
        phi = cosine * self.cos_m - sine * self.sin_m
        one_hot = torch.zeros_like(cosine)
        one_hot.scatter_(1, labels.view(-1, 1).long(), 1)
        output = one_hot * phi + (1.0 - one_hot) * cosine
        return output * self.s


class EmbeddingNet(nn.Module):
    # MobileNetV3-Small pré-entraîné (ImageNet) - léger, s'exporte en
    # OpenVINO/NCNN comme le Modèle 1 aujourd'hui, pas de 3e runtime
    # d'inférence à ajouter côté Kittyhack (voir Phase 3 du plan).
    def __init__(self, embed_dim=EMBED_DIM):
        super().__init__()
        backbone = models.mobilenet_v3_small(weights=models.MobileNet_V3_Small_Weights.DEFAULT)
        self.features = backbone.features
        self.pool = nn.AdaptiveAvgPool2d(1)
        self.fc = nn.Linear(576, embed_dim)

    def forward(self, x):
        x = self.features(x)
        x = self.pool(x).flatten(1)
        x = self.fc(x)
        return F.normalize(x, dim=1)


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
    dataset = datasets.ImageFolder(data_dir, transform=train_tf)
    class_names = dataset.classes
    print(f"Classes: {{class_names}} ({{len(dataset)}} crops)")
    loader = DataLoader(dataset, batch_size=BATCH_SIZE, shuffle=True, num_workers=4, drop_last=True)

    model = EmbeddingNet().to(DEVICE)
    arc = ArcMarginProduct(EMBED_DIM, len(class_names)).to(DEVICE)
    optimizer = torch.optim.Adam(list(model.parameters()) + list(arc.parameters()), lr=LR)

    model.train()
    for epoch in range(EPOCHS):
        total_loss = 0.0
        n_batches = 0
        for imgs, labels in loader:
            imgs, labels = imgs.to(DEVICE), labels.to(DEVICE)
            embeddings = model(imgs)
            logits = arc(embeddings, labels)
            loss = F.cross_entropy(logits, labels)
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()
            total_loss += loss.item()
            n_batches += 1
        print(f"epoch {{epoch+1}}/{{EPOCHS}} loss={{total_loss/max(n_batches,1):.4f}}")

    # Galeries de référence : embedding moyen par chat sur une passe
    # SANS augmentation (sinon la galerie elle-même serait bruitée).
    eval_tf = transforms.Compose([transforms.Resize((224, 224)), transforms.ToTensor(), norm])
    eval_dataset = datasets.ImageFolder(data_dir, transform=eval_tf)
    eval_loader = DataLoader(eval_dataset, batch_size=BATCH_SIZE, shuffle=False, num_workers=4)
    model.eval()
    sums = {{i: torch.zeros(EMBED_DIM) for i in range(len(class_names))}}
    counts = {{i: 0 for i in range(len(class_names))}}
    with torch.no_grad():
        for imgs, labels in eval_loader:
            embeddings = model(imgs.to(DEVICE)).cpu()
            for emb, lbl in zip(embeddings, labels):
                sums[lbl.item()] += emb
                counts[lbl.item()] += 1

    os.makedirs("/workspace/result/galleries", exist_ok=True)
    for i, name in enumerate(class_names):
        mean_emb = sums[i] / max(counts[i], 1)
        mean_emb = F.normalize(mean_emb.unsqueeze(0), dim=1).squeeze(0)
        np.save(f"/workspace/result/galleries/{{name}}.npy", mean_emb.numpy())
        print(f"Galerie {{name}}: {{counts[i]}} crops moyennés")

    with open("/workspace/result/identity_labels.txt", "w") as f:
        f.write("\\n".join(class_names) + "\\n")

    dummy = torch.randn(1, 3, 224, 224).to(DEVICE)
    torch.onnx.export(
        model, dummy, "/workspace/result/identity_model.onnx",
        input_names=["input"], output_names=["embedding"],
        dynamic_axes={{"input": {{0: "batch"}}, "embedding": {{0: "batch"}}}},
        opset_version=12,
    )

    json.dump({{
        "JOB_ID": "vastai-identity-" + str(int(datetime.datetime.utcnow().timestamp())),
        "TIMESTAMP_UTC": datetime.datetime.utcnow().isoformat() + "Z",
        "VERSION": "1.0.0",
        "EMBED_DIM": EMBED_DIM,
        "CLASSES": class_names,
        "BACKBONE": "mobilenet_v3_small",
        "EPOCHS": EPOCHS,
    }}, open("/workspace/result/info.json", "w"), indent=2)


if __name__ == "__main__":
    main()
PYEOF
mkdir -p /workspace/result
python3 train_identity.py
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
        raise RuntimeError(f"Pas d'id d'instance dans la réponse : {body}")
    log.info("Instance créée (entraînement identité) : %s", instance_id)
    return instance_id


def wait_for_result(started_after: float, api_key: str | None = None, instance_id: int | None = None) -> str | None:
    deadline = time.time() + TIMEOUT_S
    while time.time() < deadline:
        if os.path.isdir(IDENTITY_TRAINED_MODELS_DIR):
            for fname in os.listdir(IDENTITY_TRAINED_MODELS_DIR):
                fpath = os.path.join(IDENTITY_TRAINED_MODELS_DIR, fname)
                if os.path.isfile(fpath) and os.path.getmtime(fpath) >= started_after:
                    return fpath
        elapsed = int(time.time() - started_after)
        log.info("En attente du résultat (identité)... (%ds écoulées)", elapsed)
        _write_state({"status": "running", "step": "training", "started_at": started_after, "elapsed": elapsed})
        time.sleep(30)
    return None


def run_identity_training(api_key: str) -> dict:
    """Cœur partagé CLI/bouton web - voir run_training() de train_vastai.py
    pour le patron d'origine (Modèle 1), repris ici tel quel pour le
    Modèle 2 avec ses propres répertoires/état (jamais partagés)."""
    if not UPLOAD_TOKEN:
        msg = "MODEL_UPLOAD_TOKEN non configuré côté serveur, abandon."
        log.error(msg)
        _write_state({"status": "error", "message": msg})
        return {"status": "error", "message": msg}

    _write_state({"status": "running", "step": "export", "started_at": time.time()})
    log.info("Construction du zip de crops identité...")
    stats = build_identity_training_zip(IDENTITY_TRAINING_DATA_PATH)
    log.info("Zip prêt : %s", stats)
    if stats["included"] == 0:
        msg = "Aucun crop à entraîner, abandon."
        log.error(msg)
        _write_state({"status": "error", "message": msg})
        return {"status": "error", "message": msg}

    excluded_hosts: set = set()
    instance_id = None
    started_at = time.time()
    for attempt in range(1, 4):
        _write_state({
            "status": "running", "step": "searching_offer", "attempt": attempt,
            "started_at": started_at, "crops": stats["included"],
        })
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
        _write_state({"status": "error", "message": msg})
        return {"status": "error", "message": msg}

    try:
        result_path = wait_for_result(started_at, api_key=api_key, instance_id=instance_id)
    finally:
        destroy_instance(api_key, instance_id)

    if result_path is None:
        msg = f"Timeout ({TIMEOUT_S}s) sans résultat - voir les logs pour le diagnostic."
        log.error(msg)
        _write_state({"status": "error", "message": msg})
        return {"status": "error", "message": msg}

    state = {
        "status": "success", "finished_at": time.time(),
        "crops": stats["included"], "result_path": result_path,
    }
    _write_state(state)
    log.info("Entraînement identité terminé : %s", result_path)
    return state


def main():
    logging.getLogger().setLevel(logging.INFO)
    api_key = getpass.getpass("Clé API vast.ai (jamais affichée ni loggée) : ").strip()
    if not api_key:
        log.error("Clé vide, abandon.")
        sys.exit(1)
    result = run_identity_training(api_key)
    if result["status"] != "success":
        sys.exit(1)


if __name__ == "__main__":
    main()
