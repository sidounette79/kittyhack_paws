"""
Construit le VRAI zip d'entraînement du Modèle 2 tel qu'utilisé pour le
modèle actuellement actif en production (vastai-yolo-identity-1793772422,
04.11) - combine deux sources, PAS une seule :

1. RFID réel Kittyflap (build_large_rfid_training_set.py), jusqu'à
   rfid_target_per_cat crops/chat.
2. Pool Reolink/manuel validé à la main dans Paws Review
   (export_identity_training.build_identity_training_zip, bucket
   "reolink_manual" uniquement - le bucket "kittyflap" de ce même pool est
   ignoré ici, bien plus petit (~900 captures) que le RFID direct qui le
   couvre déjà largement), dupliqué `reolink_duplication` fois.

05.11, Sid ("quid de toutes celles de reolink qu'on a rajoutée ? J'ai
passé des heures à rajouter des photos sous l'angle reolink pour en avoir
500 par chat") - ma première reconstruction du pipeline (voir
build_large_rfid_training_set.py) ne couvrait QUE la source 1, oubliant
complètement la source 2. Reconstruit ici après avoir retrouvé et
inspecté le zip réel du run qui a produit le modèle actif
(/storage/large_combined_training_data_v4_x3.zip, encore sur disque au
moment de cette reconstruction) : 7200 fichiers au format RFID (noms
numériques, event_id) + 6192 fichiers au format
`capture_<id>_reolink_manual_dupN.jpg` (N=1..3) - confirmant ~516-520
crops/chat avant duplication ×3, le chiffre que Sid a cité de mémoire.

Un script de corrélation temporelle automatique RFID↔Reolink
(build_reolink_rfid_correlated_set.py, dans l'historique de cette
session) avait aussi été exploré mais Sid, 05.11 : "celui-là n'avait rien
donné" - abandonné, aucune trace de son format de nom de fichier dans le
zip réel inspecté. Pas reconstruit ici, volontairement.

Duplication x3, pas x1 ni x12/x4 (autres volumes testés dans
l'historique, voir project_kittyhack_identity_model2_work.md) : c'est
précisément le ratio du zip réellement utilisé pour le modèle actif
aujourd'hui.
"""

import logging
import os
import zipfile

from app.build_large_rfid_training_set import build_large_rfid_training_zip
from app.export_identity_training import build_identity_training_zip

logging.basicConfig(level=logging.INFO, format="%(asctime)s [build_combined_identity_training_set] %(message)s")
log = logging.getLogger(__name__)

REOLINK_BUCKET_SUFFIX = "_reolink_manual.jpg"


def build_combined_identity_training_zip(
    output_path: str, rfid_target_per_cat: int = 1800, reolink_duplication: int = 3,
) -> dict:
    tmp_rfid_zip = output_path + ".rfid.tmp.zip"
    tmp_reolink_zip = output_path + ".reolink.tmp.zip"
    tmp_dir = output_path + ".tmp"
    os.makedirs(tmp_dir, exist_ok=True)

    log.info("Construction de la part RFID Kittyflap (cible %d/chat)...", rfid_target_per_cat)
    rfid_stats = build_large_rfid_training_zip(tmp_rfid_zip, target_per_cat=rfid_target_per_cat)
    log.info("RFID : %s", rfid_stats)

    log.info("Construction de la part Reolink/manuel (tout le pool validé)...")
    reolink_pool_stats = build_identity_training_zip(tmp_reolink_zip)
    log.info("Pool Reolink/manuel+Kittyflap curé (avant filtrage bucket) : %s", reolink_pool_stats)

    with zipfile.ZipFile(tmp_rfid_zip) as zf:
        zf.extractall(tmp_dir)
    os.remove(tmp_rfid_zip)

    reolink_unique = 0
    reolink_duplicated = 0
    reolink_counts: dict[str, int] = {}
    with zipfile.ZipFile(tmp_reolink_zip) as zf:
        for name in zf.namelist():
            if not name.endswith(REOLINK_BUCKET_SUFFIX):
                continue  # bucket "kittyflap" de ce pool curé ignoré - le RFID direct le couvre déjà bien plus largement
            cat_name, fname = name.split("/", 1)
            base = fname[: -len(".jpg")]
            data = zf.read(name)
            reolink_unique += 1
            reolink_counts[cat_name] = reolink_counts.get(cat_name, 0) + 1
            for dup in range(1, reolink_duplication + 1):
                out_dir = os.path.join(tmp_dir, cat_name)
                os.makedirs(out_dir, exist_ok=True)
                with open(os.path.join(out_dir, f"{base}_dup{dup}.jpg"), "wb") as f:
                    f.write(data)
                reolink_duplicated += 1
    os.remove(tmp_reolink_zip)

    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, _dirs, files in os.walk(tmp_dir):
            for fname in files:
                abs_path = os.path.join(root, fname)
                zf.write(abs_path, os.path.relpath(abs_path, tmp_dir))

    for root, _dirs, files in os.walk(tmp_dir, topdown=False):
        for fname in files:
            os.remove(os.path.join(root, fname))
        os.rmdir(root)

    total_included = rfid_stats["included"] + reolink_duplicated
    log.info(
        "Zip combiné : %d total (%d RFID Kittyflap + %d Reolink/manuel x%d dup = %d)",
        total_included, rfid_stats["included"], reolink_unique, reolink_duplication, reolink_duplicated,
    )
    return {
        "included": total_included,
        "rfid": rfid_stats,
        "reolink_unique": reolink_unique,
        "reolink_duplicated": reolink_duplicated,
        "reolink_duplication": reolink_duplication,
        "reolink_counts_unique": reolink_counts,
    }
