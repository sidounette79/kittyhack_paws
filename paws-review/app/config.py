import os

# Chemins côté Kittyhack (lecture seule, jamais d'écriture sur ces fichiers)
SOURCE_DB_PATH = os.environ.get("SOURCE_DB_PATH", "/source/kittyhack.db")
SOURCE_ORIGINALS_DIR = os.environ.get("SOURCE_ORIGINALS_DIR", "/source/pictures/original_images")
SOURCE_THUMBNAILS_DIR = os.environ.get("SOURCE_THUMBNAILS_DIR", "/source/pictures/thumbnails")

# 18.10, Sid ("l'angle Reolink est complètement exclu de l'entraînement") :
# fichiers plats écrits en continu par watchdog.py côté Kittyhack
# (TRAINING_SAMPLES_DIR - déjà gated/sparse, explicitement "pour Label
# Studio" dans son propre commentaire) - pas de table SQL source, pas de
# détection pré-remplie, voir ingest_reolink.py.
SOURCE_REOLINK_SAMPLES_DIR = os.environ.get(
    "SOURCE_REOLINK_SAMPLES_DIR", "/source/reolink_samples"
)

# Stockage propre à Paws Review, découplé du plafond interne de Kittyhack (~20000 photos)
STORAGE_ROOT = os.environ.get("STORAGE_ROOT", "/storage")
ORIGINALS_DIR = os.path.join(STORAGE_ROOT, "originals")
THUMBNAILS_DIR = os.path.join(STORAGE_ROOT, "thumbnails")
CATALOG_DB_PATH = os.environ.get("CATALOG_DB_PATH", os.path.join(STORAGE_ROOT, "paws_review.db"))

# Seuil de REVUE pour une proie suspectée (volontairement bas : on veut surtout
# rattraper les faux négatifs - une détection écartée à tort par le modèle - pas
# seulement confirmer les cas déjà sûrs. Le seuil d'ACTION de Kittyhack (blocage de
# sortie) reste à 72.0 dans son propre config.ini ; ce seuil-ci ne le remplace pas,
# il sert uniquement à décider quoi montrer à Sid en priorité.
PREY_REVIEW_THRESHOLD = float(os.environ.get("PREY_REVIEW_THRESHOLD", "15.0"))

# Distance de Hamming entre deux pHash en dessous de laquelle deux photos "chat
# seul" du même chat sont considérées quasi-identiques (même entrée filmée deux
# fois) et la seconde n'est pas proposée à la revue. Ne s'applique jamais aux
# proies suspectées ni aux désaccords de modèle : ceux-là sont toujours gardés
# intégralement.
DEDUP_HAMMING_THRESHOLD = int(os.environ.get("DEDUP_HAMMING_THRESHOLD", "10"))
DEDUP_WINDOW_MINUTES = int(os.environ.get("DEDUP_WINDOW_MINUTES", "10"))

# Un bloc de détection va jusqu'à 200 frames (observé sur données réelles,
# ~30 secondes de rafale) - certaines, au sein même du bloc, n'ont pas le chat
# bien visible (occlusion, sortie momentanée du cadre), pas seulement en
# début/fin. own_cat_probability (déjà calculé par Kittyhack, par frame) sert de
# filtre : sous ce seuil, la frame n'est même pas gardée pour revue/entraînement -
# sauf si elle est candidate proie, jamais filtrée dans ce cas.
MIN_OWN_CAT_PROBABILITY = float(os.environ.get("MIN_OWN_CAT_PROBABILITY", "30.0"))

# Fenêtre de rétention : 1 an confirmé par Sid le 17.10 (sur les volumes réels
# observés, ~60 Ko/photo, 1 an ≈ 25 Go - un plafond Go n'a donc pas besoin d'être
# strict, la purge se fait par âge).
RETENTION_DAYS = int(os.environ.get("RETENTION_DAYS", "365"))
