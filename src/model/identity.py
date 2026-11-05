"""Cat-identity resolution (Modèle 2) - crops a 'Cat' detection box and
resolves its identity, instead of relying on YOLO's own class name the
way the single-model setup used to.

02.11, deuxième génération de Modèle 2 - deux formats de model_dir
coexistent, distingués par la présence de `YOLO_CLASSIFIER_MARKER.txt` :
  - "classifier" (nouveau, actif) : `train_vastai_yolo_identity.py` sur un
    lot de 1800 crops/chat tiré directement des 161k vrais événements RFID
    de production (pas les ~400/chat curés à la main utilisés jusqu'ici) -
    92.8% sur le jeu de test figé (`/data/identity_eval_set`), contre 58.4%
    pour le format embeddings ci-dessous. Chargé via `ultralytics.YOLO`
    directement sur le `.pt` (PAS un export ONNX + reimplémentation du
    prétraitement à la main) - Sid rigueur : le script qui a mesuré 92.8%
    (`eval_yolo_cls_large_rfid.py`) appelle `YOLO(...).predict(crop,
    imgsz=224)`, et Ultralytics ne documente pas publiquement le détail
    exact de son prétraitement classify (letterbox vs resize, etc.) ; le
    seul moyen de garantir qu'on retrouve bien 92.8% en prod plutôt qu'un
    score dégradé silencieusement est d'appeler EXACTEMENT le même chemin
    de code, pas une réimplémentation. `ultralytics` est déjà une
    dépendance du projet (Modèle 1) - aucun poids ajouté.
  - "embedding" (ancien, conservé pour compatibilité) : voir ci-dessous.

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

from src.baseconfig import CONFIG

UNKNOWN_CAT_LABEL = "Chat inconnu"

# 02.11, vrai bug en prod la nuit suivant l'activation (Sid : "la vidéo
# enregistre qu'un bout de queue prise pour Boubou") - Nala sort du cadre,
# own_cat_probability retombe à 0 sur toutes les frames suivantes (plus
# aucun chat visible), SAUF une : un bout de queue a franchi le plancher
# de 8 PIXELS absolus (beaucoup trop permissif) et obtenu une identité
# confiante ("Boubou") au lieu de "Chat inconnu" - comme c'était la
# DERNIÈRE identité vue avant que le bloc ne se termine, elle est restée
# affichée comme titre du bloc entier (loop.py:winning_tag, voir son
# commentaire - reprend la dernière valeur connue, pas une vraie
# ré-ouverture de vote). Même seuil que MIN_BOX_AREA_PCT côté
# export_identity_training.py (paws-review) - cohérent : un crop trop
# petit pour servir de bonne donnée d'entraînement n'est pas non plus
# assez d'information pour une identité fiable à l'inférence. % de l'aire
# totale de l'image, pas un nombre de pixels absolu (une image HD a
# beaucoup plus de pixels "libres" qu'un flux bas débit pour la même
# vraie taille de sujet). 05.11, Sid ("2% c'est trop, il faut baisser à
# 1%... mais ptet que je reviendrais à 2%") - rendu configurable
# (CONFIG["IDENTITY_MIN_CROP_AREA_PCT"], baseconfig.py SETTINGS_SCHEMA,
# réglable depuis Configuration) plutôt qu'une constante figée ; lu
# directement à chaque appel ci-dessous et dans watchdog.py, pas de
# mécanisme de reload séparé à maintenir (CONFIG est déjà le dict partagé
# mis à jour en place par load_config()).

# Même ensemble que ModelHandler._uses_openvino_backend() / watchdog.py's
# _uses_openvino_backend() - une seule source de vérité pour "ce device
# veut dire OpenVINO", dupliquée ici en constante plutôt qu'importée
# (model_handler.py importe déjà identity.py - import circulaire si
# l'inverse était tenté).
_OPENVINO_DEVICES = {"gpu", "intel:gpu", "intel:cpu", "intel:npu"}


class CatIdentity:
    """Loaded identity model: either a YOLO classifier (new, 92.8%) or an
    embedding session (OpenVINO/onnxruntime CPU) + one gallery per known
    cat (old, 58.4%, kept for compatibility)."""

    def __init__(self, model_dir: str, inference_device: str = "cpu"):
        self.model_dir = model_dir
        self._mode = "classifier" if os.path.exists(
            os.path.join(model_dir, "YOLO_CLASSIFIER_MARKER.txt")
        ) else "embedding"

        if self._mode == "classifier":
            from ultralytics import YOLO
            pt_path = os.path.join(model_dir, "best.pt")
            # 03.11, bug réel en prod (RuntimeError: Invalid device string:
            # 'intel:gpu', a cassé le flux caméra des deux côtés ~23h00-04h30
            # la nuit de l'activation) - la syntaxe "intel:gpu"/"intel:cpu"/
            # "intel:npu" n'est comprise par Ultralytics QUE quand le modèle
            # chargé est déjà au format OpenVINO exporté (comme le Modèle 1,
            # `model_openvino_model`), jamais pour un `.pt` PyTorch brut (son
            # backend appelle `weight.to(device)` en PyTorch natif, qui ne
            # connaît que "cpu"/"cuda"/"mps"). Sid (01.11) : "il faut
            # toujours, au mieux, que modèle 1 et modèle 2 tournent sur GPU"
            # - donc on exporte ce `.pt` en OpenVINO nous-mêmes (une fois,
            # mis en cache à côté du .pt pour les chargements suivants et
            # les futurs modèles entraînés pareil) plutôt que de rester sur
            # CPU indéfiniment. PIÈGE VÉRIFIÉ : charger un dossier OpenVINO
            # exporté SANS `task="classify"` explicite fait qu'Ultralytics
            # devine "detect" par défaut (pas de métadonnée de tâche fiable
            # dans le dossier seul) et casse tout le post-traitement
            # (IndexError dans le NMS) - jamais omettre `task=`.
            # Chaque étape a son repli : export échoue → reste sur le .pt
            # brut en CPU (jamais une tentative GPU avec un device string
            # invalide comme cette nuit) ; chargement du dossier exporté
            # échoue → même repli. Jamais une exception qui remonte d'ici.
            load_path = pt_path
            load_kwargs: dict = {}
            self._yolo_device = "cpu"
            wants_gpu = str(inference_device or "cpu").strip().lower() in _OPENVINO_DEVICES
            if wants_gpu:
                ov_dir = os.path.join(model_dir, "best_openvino_model")
                try:
                    if not os.path.isdir(ov_dir):
                        YOLO(pt_path).export(format="openvino", imgsz=224)
                    if os.path.isdir(ov_dir):
                        load_path = ov_dir
                        load_kwargs = {"task": "classify"}
                        self._yolo_device = str(inference_device).strip().lower()
                except Exception as e:
                    logging.warning(
                        f"[IDENTITY] OpenVINO export failed ({e}), falling back to .pt on CPU."
                    )
                    load_path = pt_path
                    load_kwargs = {}
                    self._yolo_device = "cpu"
            try:
                self._yolo = YOLO(load_path, **load_kwargs)
            except Exception as e:
                logging.warning(
                    f"[IDENTITY] Failed to load '{load_path}' ({e}), falling back to raw .pt on CPU."
                )
                self._yolo = YOLO(pt_path)
                self._yolo_device = "cpu"
            logging.info(
                f"[IDENTITY] Loaded YOLO classifier (92.8% generation) from '{load_path}' "
                f"(device={self._yolo_device})."
            )
            return

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

    def identify_crop(self, crop_bgr: np.ndarray) -> tuple[str, float]:
        """Return (nom_du_chat_le_plus_proche, score 0-100).

        01.11, Sid ("le seul chat étranger qui pourrissait ma vie est
        mort, ça fait un an qu'aucun chat inconnu ne s'est approché de la
        chatière") - plus de scénario réel à protéger en retombant sur
        `UNKNOWN_CAT_LABEL` à partir d'un seuil de confiance arbitraire :
        toujours le chat connu le plus proche, jamais "Chat inconnu" en
        dessous d'un pourcentage. `UNKNOWN_CAT_LABEL` reste utilisé,
        ailleurs, pour les cas structurellement dégénérés (aucune galerie
        chargée, boîte quasi nulle) - jamais pour "pas assez confiant".
        """
        if crop_bgr is None or crop_bgr.size == 0:
            return UNKNOWN_CAT_LABEL, 0.0

        if self._mode == "classifier":
            return self._identify_crop_classifier(crop_bgr)

        if not self.galleries:
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
        return best_name, score

    def _identify_crop_classifier(self, crop_bgr: np.ndarray) -> tuple[str, float]:
        # Même appel, mêmes kwargs que eval_yolo_cls_large_rfid.py (le
        # script qui a mesuré 92.8%) - imgsz=224, device explicite si pas
        # cpu (sinon Ultralytics retombe sur CPU malgré un GPU disponible,
        # cf. bug GPU du Modèle 1 corrigé plus tôt ce soir, même piège).
        predict_kwargs: dict = dict(imgsz=224, verbose=False)
        if self._yolo_device != "cpu":
            predict_kwargs["device"] = self._yolo_device
        results = self._yolo.predict(crop_bgr, **predict_kwargs)
        probs = results[0].probs
        idx = int(probs.top1)
        name = self._yolo.names[idx]
        score = float(probs.top1conf) * 100.0
        return name, score


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
        area_pct = obj["w"] * obj["h"] / 100.0
        if x2 - x1 < 8 or y2 - y1 < 8 or area_pct < float(CONFIG.get("IDENTITY_MIN_CROP_AREA_PCT", 2.0)):
            # Boîte trop petite (quasi nulle, ou juste un bout de queue/
            # patte) - jamais assez de signal pour une identité fiable,
            # inutile de solliciter le modèle pour ça.
            obj["name"] = UNKNOWN_CAT_LABEL
            obj["probability"] = 0.0
            continue

        # 03.11, incident de prod (voir commentaire plus haut sur
        # `device="intel:gpu"` invalide pour un .pt brut) : une exception
        # ICI remontait sans aucun filet jusqu'à `ModelHandler.run()` et
        # tuait le thread entier de traitement du modèle - plus aucune
        # frame traitée nulle part, plus aucune détection de mouvement,
        # porte restée verrouillée 5h30 (deuxième occurrence du même
        # symptôme structurel que l'incident du 30.10, cause différente -
        # voir [[project_kittyhack_backend_freeze_30_10]]). Un module
        # d'identité qui plante ne doit JAMAIS pouvoir arrêter la
        # détection de mouvement elle-même, qui ne dépend pas de lui -
        # même principe déjà affirmé pour le CHARGEMENT du modèle
        # (`load_identity_model`, "never raises"), manquant ici pour
        # l'INFÉRENCE elle-même.
        try:
            name, score = identity.identify_crop(frame[y1:y2, x1:x2])
        except Exception as e:
            logging.error(f"[IDENTITY] identify_crop() failed, box left as '{UNKNOWN_CAT_LABEL}': {e}")
            name, score = UNKNOWN_CAT_LABEL, 0.0
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
