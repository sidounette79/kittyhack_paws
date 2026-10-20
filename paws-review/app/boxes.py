"""
Calcule les boîtes EFFECTIVES d'une capture (propositions du modèle +
corrections de Sid, boîtes ignorées exclues). Factorisé depuis
main.py:get_capture() le 18.10 pour que l'export d'entraînement
(export_training.py) utilise EXACTEMENT la même logique que ce que Sid
voit et corrige à l'écran - aucune divergence possible entre les deux.
"""

import json
import sqlite3


def effective_boxes(conn: sqlite3.Connection, capture_id: int, detected_objects_json: str | None) -> list[dict]:
    model_boxes = json.loads(detected_objects_json or "[]")
    annotations = {
        a["object_index"]: dict(a)
        for a in conn.execute(
            "SELECT * FROM box_annotations WHERE capture_id = ? ORDER BY object_index",
            (capture_id,),
        ).fetchall()
    }

    # Une annotation de Sid (object_index correspondant) remplace la
    # proposition du modèle pour l'affichage ; les boîtes ajoutées à la main
    # (object_index au-delà de len(model_boxes)) viennent s'ajouter. Une boîte
    # explicitement écartée (double détection, fausse boîte) disparaît ici -
    # elle ne sera jamais exportée pour l'entraînement.
    boxes = []
    for i, mb in enumerate(model_boxes):
        ann = annotations.pop(i, None)
        if ann and ann["ignored"]:
            continue
        boxes.append({
            "object_index": i,
            "source": "model",
            "label": ann["label"] if ann else mb["object_name"],
            "probability": mb["probability"],
            "x": ann["x"] if ann else mb["x"],
            "y": ann["y"] if ann else mb["y"],
            "width": ann["width"] if ann else mb["width"],
            "height": ann["height"] if ann else mb["height"],
            "confirmed": bool(ann["confirmed"]) if ann else False,
        })
    for idx, ann in sorted(annotations.items()):
        if ann["ignored"]:
            continue
        boxes.append({
            "object_index": idx,
            "source": "manual",
            "label": ann["label"],
            "probability": None,
            "x": ann["x"], "y": ann["y"], "width": ann["width"], "height": ann["height"],
            "confirmed": bool(ann["confirmed"]),
        })
    return boxes
