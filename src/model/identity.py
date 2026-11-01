"""Cat-identity resolution (Modèle 2) - crops a 'Cat' detection box and
matches it against per-cat reference embeddings, instead of relying on
YOLO's own class name the way the single-model setup used to.

01.11, scission détection/identité (voir le plan) - Modèle 1 (YOLO,
`labels.txt` = ["Cat", "Prey"]) ne connaît plus les 4 chats par nom ; ce
module referme l'écart pour que `DetectedObject.object_name` continue de
porter un nom de chat exactement comme avant, invisible pour
`loop.py`/Parcours/le stockage.

Choix délibérés :
  - Backend d'inférence : 01.11, Sid ("il faut toujours, au mieux, que
    modèle 1 et modèle 2 tournent sur GPU") - OpenVINO quand
    `inference_device` l'indique (même convention que ModelHandler/
    watchdog._uses_openvino_backend : "gpu"/"intel:gpu"/"intel:cpu"/
    "intel:npu"), EXACTEMENT le même `openvino==2023.3.0` déjà épinglé et
    éprouvé pour le Modèle 1 - jamais une 2e dépendance GPU (pas
    onnxruntime-openvino, un package séparé avec son propre pinning, qui
    réintroduirait le genre d'enfer de drivers déjà documenté dans le
    Dockerfile pour un gain qu'on n'a même pas besoin de redémontrer).
    OpenVINO lit un .onnx directement (frontend ONNX natif depuis
    2022.x) - pas d'étape d'export IR séparée comme pour le Modèle 1
    (son .pt n'est pas un format qu'OpenVINO sait lire nativement, un
    .onnx l'est déjà). PIÈGE VÉRIFIÉ EN VRAI le 01.11 : ceci ne marche
    QUE si l'export ONNX a été fait avec `dynamo=False`
    (`train_vastai_identity.py`) - le nouvel exporteur "dynamo" par
    défaut de torch.onnx.export() produit un graphe qu'OpenVINO 2023.3.0
    refuse de lire (testé : même erreur avec et sans dynamic_axes, donc
    pas un souci de shape dynamique - un vrai conflit de version entre
    l'exporteur PyTorch et ce frontend ONNX précis). Un modèle entraîné
    AVANT ce correctif (ex. 20261029_221433) tombera sur l'exception
    ci-dessous et retombera proprement en CPU, pas de modèle refait pour
    lui - seuls les entraînements FUTURS en profitent. device_name="AUTO"
    (jamais forcé), même choix que
    model_handler.py pour Ultralytics - laisse OpenVINO retomber sur CPU
    tout seul si le GPU n'est pas dispo, pas une logique à dupliquer ici.
    onnxruntime (CPUExecutionProvider) reste le filet de secours : utilisé
    quand `inference_device` est "cpu" (target-mode Pi, ou remote-mode
    sans GPU), ET comme repli automatique si OpenVINO échoue pour
    N'IMPORTE quelle raison (modèle introuvable, driver, etc.) - ne
    jamais laisser un souci GPU rendre l'identité indisponible alors que
    le CPU suffit très bien pour ce volume (quelques crops par passage,
    pas un flux continu).
  - Métrique : similarité cosinus (naturelle pour un embedding ArcFace),
    PAS une distance - le seuil de config est donc "au-dessus duquel on
    accepte", pas "en-dessous" comme la première rédaction du plan le
    supposait.
  - Chat inconnu : libellé littéral "Chat inconnu" sous le seuil (recommandé
    par le plan lui-même, Phase 0.2) plutôt qu'un repli silencieux sur "Cat".

Contrat de dossier attendu (model_dir) - CE N'EST PAS une supposition : il
reprend exactement ce que `paws-review/app/train_vastai_identity.py` produit
et ce que `paws-review/app/main.py`'s `/app-api/identity-model/upload`
(REQUIRED_IDENTITY_MODEL_FILES) exige et déballe tel quel dans ce dossier :
    identity_model.onnx     - backbone, une image 224x224 -> un vecteur (EMBED_DIM)
    info.json                - {"JOB_ID", "TIMESTAMP_UTC", "EMBED_DIM", "CLASSES", ...}
    identity_labels.txt      - un nom de chat connu par ligne (informatif, pas lu ici -
                                 les vrais noms viennent des clés de `galleries/`)
    galleries/<cat_name>.npy - UN SEUL vecteur moyen (shape (EMBED_DIM,), pas une
                                 galerie à plusieurs poses - `np.atleast_2d` ci-dessous
                                 accepte aussi un futur format multi-vecteurs (N, EMBED_DIM)
                                 sans code à changer si cette simplification est levée plus tard)
"""
from __future__ import annotations

import json
import logging
import os

import cv2
import numpy as np

UNKNOWN_CAT_LABEL = "Chat inconnu"

# Même ensemble que ModelHandler._uses_openvino_backend() / watchdog.py's
# _uses_openvino_backend() - une seule source de vérité pour "ce device
# veut dire OpenVINO", dupliquée ici en constante plutôt qu'importée
# (model_handler.py importe déjà identity.py - import circulaire si
# l'inverse était tenté).
_OPENVINO_DEVICES = {"gpu", "intel:gpu", "intel:cpu", "intel:npu"}


class CatIdentity:
    """Loaded identity model: one embedding session (OpenVINO or
    onnxruntime CPU) + one gallery per known cat."""

    def __init__(self, model_dir: str, inference_device: str = "cpu"):
        self.model_dir = model_dir

        info_path = os.path.join(model_dir, "info.json")
        with open(info_path, "r", encoding="utf-8") as f:
            info = json.load(f)
        # train_vastai_identity.py entraîne sur du 224x224 fixe, jamais
        # stocké dans info.json (pas une vraie clé du contrat) - le défaut
        # ici EST la valeur réelle, pas une supposition.
        self.input_size = 224

        onnx_path = os.path.join(model_dir, "identity_model.onnx")
        self._backend = "cpu"
        self._ov_compiled = None
        self._session = None

        wants_gpu = str(inference_device or "cpu").strip().lower() in _OPENVINO_DEVICES
        if wants_gpu:
            try:
                from openvino.runtime import Core
                core = Core()
                ov_model = core.read_model(onnx_path)
                self._ov_compiled = core.compile_model(ov_model, "AUTO")
                self._ov_input = self._ov_compiled.input(0)
                self._ov_output = self._ov_compiled.output(0)
                self._backend = "openvino"
                logging.info(f"[IDENTITY] Loaded via OpenVINO (device=AUTO) from '{onnx_path}'.")
            except Exception as e:
                logging.warning(f"[IDENTITY] OpenVINO load failed ({e}), falling back to onnxruntime CPU.")

        if self._backend == "cpu":
            import onnxruntime as ort
            self._session = ort.InferenceSession(onnx_path, providers=["CPUExecutionProvider"])
            self._input_name = self._session.get_inputs()[0].name

        self.galleries: dict[str, np.ndarray] = {}
        galleries_dir = os.path.join(model_dir, "galleries")
        if os.path.isdir(galleries_dir):
            for fname in os.listdir(galleries_dir):
                if not fname.endswith(".npy"):
                    continue
                cat_name = fname[: -len(".npy")]
                # np.atleast_2d : le format réel actuel est UN vecteur
                # (EMBED_DIM,) par chat (moyenne, pas une galerie à
                # plusieurs poses) - accepte aussi (N, EMBED_DIM) sans
                # changement si ça évolue.
                vectors = np.atleast_2d(np.load(os.path.join(galleries_dir, fname)))
                # Normalisées une fois ici - la similarité cosinus devient un
                # simple produit scalaire à chaque identify_crop(), pas
                # recalculée (ni le coût, ni le risque d'incohérence) à
                # chaque frame.
                norms = np.linalg.norm(vectors, axis=1, keepdims=True)
                norms[norms == 0] = 1.0
                self.galleries[cat_name] = vectors / norms

        if not self.galleries:
            logging.warning(f"[IDENTITY] No gallery found under '{galleries_dir}' - all crops will resolve to '{UNKNOWN_CAT_LABEL}'.")

    def _embed(self, crop_bgr: np.ndarray) -> np.ndarray:
        rgb = cv2.cvtColor(crop_bgr, cv2.COLOR_BGR2RGB)
        resized = cv2.resize(rgb, (self.input_size, self.input_size))
        # ImageNet normalisation - hypothèse par défaut pour un backbone
        # pré-entraîné classique (MobileNetV3/EfficientNet-Lite) ; à revoir
        # si le script d'entraînement Vast.ai utilise une normalisation
        # différente une fois qu'il existe vraiment.
        normalized = (resized.astype(np.float32) / 255.0 - np.array([0.485, 0.456, 0.406], dtype=np.float32)) / np.array(
            [0.229, 0.224, 0.225], dtype=np.float32
        )
        tensor = np.transpose(normalized, (2, 0, 1))[np.newaxis, ...]
        if self._backend == "openvino":
            result = self._ov_compiled([tensor])[self._ov_output]
            embedding = np.asarray(result)
        else:
            (embedding,) = self._session.run(None, {self._input_name: tensor})
        vec = embedding[0].astype(np.float32)
        norm = np.linalg.norm(vec)
        return vec / norm if norm > 0 else vec

    def identify_crop(self, crop_bgr: np.ndarray, match_threshold: float) -> tuple[str, float]:
        """Return (nom_du_chat_ou_'Chat inconnu', score 0-100).

        `match_threshold` is on the same 0-100 scale as the rest of
        Kittyhack's confidence thresholds (CAT_THRESHOLD, MOUSE_THRESHOLD,
        ...), even though the underlying metric here is a cosine
        similarity (-1..1), rescaled to 0-100 for display/comparison.
        """
        if crop_bgr is None or crop_bgr.size == 0 or not self.galleries:
            return UNKNOWN_CAT_LABEL, 0.0

        embedding = self._embed(crop_bgr)

        best_name, best_sim = UNKNOWN_CAT_LABEL, -1.0
        for cat_name, vectors in self.galleries.items():
            # Plus proche voisin de la galerie, pas la moyenne - une galerie
            # couvre plusieurs poses/éclairages par chat, la moyenne
            # lisserait des clusters potentiellement éloignés les uns des
            # autres en un centroïde qui ne ressemble à aucune pose réelle.
            sims = vectors @ embedding
            sim = float(np.max(sims))
            if sim > best_sim:
                best_name, best_sim = cat_name, sim

        score = max(0.0, min(100.0, (best_sim + 1.0) / 2.0 * 100.0))
        if score >= match_threshold:
            return best_name, score
        return UNKNOWN_CAT_LABEL, score


def load_identity_model(model_dir: str | None, inference_device: str = "cpu") -> CatIdentity | None:
    """Load the identity model at `model_dir`, or None (logged) on any failure.

    `inference_device` : même chaîne déjà résolue par l'appelant
    (ModelHandler/watchdog.MouseDetector, post is_remote_mode()) - jamais
    recalculée ici, une seule source de vérité pour "ce device veut dire
    GPU/CPU" entre Modèle 1 et Modèle 2.

    Never raises - a broken/missing identity model must degrade to "no
    identity resolution" (boxes stay labelled 'Cat'), never take down the
    whole ModelHandler/MouseDetector it's loaded alongside.
    """
    if not model_dir:
        return None
    try:
        return CatIdentity(model_dir, inference_device=inference_device)
    except Exception as e:
        logging.error(f"[IDENTITY] Failed to load identity model from '{model_dir}': {e}")
        return None


def resolve_cat_identities(
    frame: np.ndarray,
    detected_objects: list[dict],
    identity: CatIdentity | None,
    cat_names: list[str],
    match_threshold: float,
) -> int:
    """Mutate each 'Cat' box in `detected_objects` (percent coords, Kittyhack
    convention: 'x'/'y'/'w'/'h'/'name'/'probability') in place, replacing
    'Cat' with the identified cat name or `UNKNOWN_CAT_LABEL`. 'Prey' boxes
    are left untouched.

    Returns the recomputed `own_cat_probability` (max identity-match score,
    0-100, among boxes resolved to a known cat) - this can NOT be inferred
    from the original YOLO confidence anymore (it was computed by matching
    the raw YOLO label against `cat_names`, which never matches once labels
    are collapsed to 'Cat'/'Prey' - see Phase 0.2 of the plan).

    If `identity` is None (not configured, or failed to load), every 'Cat'
    box is left exactly as YOLO produced it ('Cat', unmodified) and this
    returns 0 - the caller's own fallback (its own un-collapsed-model
    computation) is NOT redone here; this function is only ever meaningful
    once Model 1 is actually the active model.
    """
    if identity is None:
        return 0

    own_cat_probability = 0
    frame_h, frame_w = frame.shape[0], frame.shape[1]

    for obj in detected_objects:
        if str(obj.get("name", "")).strip().lower() != "cat":
            continue

        x1 = max(0, int(obj["x"] / 100.0 * frame_w))
        y1 = max(0, int(obj["y"] / 100.0 * frame_h))
        x2 = min(frame_w, int((obj["x"] + obj["w"]) / 100.0 * frame_w))
        y2 = min(frame_h, int((obj["y"] + obj["h"]) / 100.0 * frame_h))
        if x2 - x1 < 8 or y2 - y1 < 8:
            # Boîte dégénérée (quasi nulle) - jamais assez de signal pour une
            # identité fiable, inutile de solliciter le modèle pour ça.
            obj["name"] = UNKNOWN_CAT_LABEL
            obj["probability"] = 0.0
            continue

        name, score = identity.identify_crop(frame[y1:y2, x1:x2], match_threshold)
        obj["name"] = name
        # 01.11 : sans ça, "probability" restait la confiance "il y a un
        # chat ici" du Modèle 1 - avant la scission, ce nombre ÉTAIT la
        # confiance d'identité (un seul modèle faisait les deux) ; Parcours
        # et tout lecteur de detected_objects_json s'attendent à ce que ce
        # soit toujours le cas.
        obj["probability"] = round(score, 1)
        if name.lower() in cat_names:
            own_cat_probability = max(own_cat_probability, int(score))

    return own_cat_probability
