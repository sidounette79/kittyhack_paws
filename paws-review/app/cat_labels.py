"""
Détermine sous quel(s) onglet(s) chat une capture doit apparaître.

Ordre de confiance strict (Sid, 17.10 - "c'est pas toujours lui pour de vrai" :
la proposition visuelle brute du modèle sur une boîte n'est PAS une identité
fiable, contrairement au RFID) :
  1. RFID de l'événement - jamais contredit par une simple boîte non revue.
  2. Une correction/ajout EXPLICITE de Sid sur une boîte (box_annotations) -
     seule source capable d'ajouter un second chat sur une même photo.
  3. À défaut des deux (aucun RFID, rien d'annoté) : repli sur la proposition
     brute du modèle, uniquement pour ne pas perdre la photo de vue - c'est la
     source la moins fiable des trois, jamais utilisée si 1 ou 2 existe déjà.
Recalculé à chaque changement de boîtes effectives.
"""

import json
import sqlite3


def _known_cat_names(conn: sqlite3.Connection) -> dict:
    rows = conn.execute(
        "SELECT DISTINCT cat_name FROM captures WHERE cat_name IS NOT NULL"
    ).fetchall()
    return {r["cat_name"].lower(): r["cat_name"] for r in rows}


def recompute_cat_labels(conn: sqlite3.Connection, capture_id: int, known_names: dict | None = None):
    capture = conn.execute(
        "SELECT detected_objects_json, cat_name, cat_source FROM captures WHERE id = ?",
        (capture_id,),
    ).fetchone()
    if capture is None:
        return

    known = known_names if known_names is not None else _known_cat_names(conn)
    annotations = {
        a["object_index"]: dict(a)
        for a in conn.execute(
            "SELECT * FROM box_annotations WHERE capture_id = ?", (capture_id,)
        ).fetchall()
    }

    labels = set()

    # 1. RFID - jamais contredit par une boîte non revue.
    if capture["cat_name"] and capture["cat_source"] == "rfid":
        labels.add(capture["cat_name"])

    # 2. Corrections/ajouts explicites de Sid - seule source capable d'ajouter
    # un second chat sur une même photo (double détection réellement multi-chat).
    for ann in annotations.values():
        if ann["ignored"]:
            continue
        canon = known.get((ann["label"] or "").lower())
        if canon:
            labels.add(canon)

    # 3. Repli : ni RFID ni correction humaine - proposition brute du modèle,
    # juste pour ne pas perdre la photo de vue (jamais utilisé si 1 ou 2 existe).
    if not labels:
        if capture["cat_name"]:
            labels.add(capture["cat_name"])
        else:
            model_boxes = json.loads(capture["detected_objects_json"] or "[]")
            for mb in model_boxes:
                canon = known.get((mb["object_name"] or "").lower())
                if canon:
                    labels.add(canon)

    conn.execute("DELETE FROM capture_cat_labels WHERE capture_id = ?", (capture_id,))
    conn.executemany(
        "INSERT OR IGNORE INTO capture_cat_labels (capture_id, cat_name) VALUES (?, ?)",
        [(capture_id, label) for label in labels],
    )
