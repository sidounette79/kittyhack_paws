"""
19.10, Sid ("tu peux faire tourner sur l'event de 5h10 hier matin et celui
de 9h32 ce matin ? Vue depuis kittyflap et reolink ?") - variante ciblée de
model_compare.py : au lieu d'un échantillon aléatoire de captures déjà
validées dans Paws Review, prend une liste EXPLICITE de fichiers (les deux
incidents réels examinés cette session) et fait tourner ancien/nouveau
modèle dessus, caméra par caméra. Pas de vérité terrain ici (frames brutes,
jamais passées par une revue) - seulement ancien vs nouveau, côte à côte.

Mêmes dépendances/contraintes que model_compare.py (ultralytics+torch,
jamais dans l'image paws-review - voir sa docstring pour comment
l'exécuter via un conteneur jetable).
"""

import json
import os
import time

from PIL import Image

from app.model_compare import COLOR_NEW, COLOR_OLD, _draw_boxes, _predict_boxes

OUT_DIR_DEFAULT = "/storage/event_comparisons"


def run(manifest_path: str, old_dir: str, new_dir: str, out_dir: str | None = None):
    from ultralytics import YOLO

    with open(manifest_path) as f:
        manifest = json.load(f)

    old_model = YOLO(os.path.join(old_dir, "model.pt"))
    new_model = YOLO(os.path.join(new_dir, "model.pt"))

    ts = time.strftime("%Y%m%d_%H%M%S")
    out_dir = out_dir or os.path.join(OUT_DIR_DEFAULT, ts)
    os.makedirs(out_dir, exist_ok=True)

    sections: dict[str, list[dict]] = {}

    for entry in manifest:
        path = entry["path"]
        if not os.path.isfile(path):
            print(f"MANQUANT: {path}")
            continue
        img = Image.open(path).convert("RGB")
        old_pred = _predict_boxes(old_model, path)
        new_pred = _predict_boxes(new_model, path)

        panel_old = _draw_boxes(img, old_pred, COLOR_OLD)
        panel_new = _draw_boxes(img, new_pred, COLOR_NEW)
        W, H = img.size
        combined = Image.new("RGB", (W * 2 + 10, H), (20, 20, 20))
        combined.paste(panel_old, (0, 0))
        combined.paste(panel_new, (W + 10, 0))

        fname = f"{entry['event']}_{entry['camera']}_{entry['ts']}.jpg".replace(":", "")
        combined.save(os.path.join(out_dir, fname), quality=90)

        section_key = f"{entry['event']} - {entry['camera']}"
        sections.setdefault(section_key, []).append({
            "filename": fname,
            "ts": entry["ts"],
            "old_classes": sorted({l for l, *_ in old_pred}),
            "new_classes": sorted({l for l, *_ in new_pred}),
        })
        print(f"{section_key} {entry['ts']} : ancien={[l for l,*_ in old_pred]} nouveau={[l for l,*_ in new_pred]}")

    _write_gallery(out_dir, sections, old_dir, new_dir)
    print(f"Terminé -> {out_dir}")
    return out_dir


def _write_gallery(out_dir: str, sections: dict, old_dir: str, new_dir: str):
    body = []
    for section, entries in sections.items():
        body.append(f"<h2>{section}</h2>")
        for e in entries:
            prey_old = any(c.lower() in ("prey", "beute") for c in e["old_classes"])
            prey_new = any(c.lower() in ("prey", "beute") for c in e["new_classes"])
            flag = ""
            if prey_new and not prey_old:
                flag = '<span class="tag win">proie rattrapée par le nouveau</span>'
            elif prey_old or prey_new:
                flag = '<span class="tag prey">proie détectée</span>'
            body.append(f"""
            <div class="row">
              <div class="meta">{e['ts']} {flag}</div>
              <img src="{e['filename']}" loading="lazy">
            </div>""")

    html = f"""<!doctype html><html><head><meta charset="utf-8">
<title>Comparaison événements réels</title>
<style>
body{{background:#111;color:#eee;font-family:system-ui;margin:0;padding:16px}}
h1{{font-size:16px}}
h2{{font-size:14px;color:#9cf;margin-top:32px}}
.legend{{font-size:13px;color:#aaa;margin-bottom:16px}}
.row{{margin-bottom:20px}}
.meta{{font-size:13px;margin-bottom:4px;color:#ccc}}
.tag{{padding:2px 8px;border-radius:4px;font-size:12px;margin-left:8px}}
.tag.win{{background:#27ae60}}
.tag.prey{{background:#c0392b}}
img{{max-width:100%;border:1px solid #333}}
</style></head><body>
<h1>Ancien modèle (orange, {os.path.basename(old_dir)}) / nouveau modèle (bleu, {os.path.basename(new_dir)})</h1>
<div class="legend">Frames brutes des deux incidents réels - pas de vérité terrain, comparaison directe des détections</div>
{''.join(body)}
</body></html>"""
    with open(os.path.join(out_dir, "index.html"), "w", encoding="utf-8") as f:
        f.write(html)


if __name__ == "__main__":
    import argparse
    import sys

    parser = argparse.ArgumentParser()
    parser.add_argument("manifest")
    parser.add_argument("--old", required=True)
    parser.add_argument("--new", required=True)
    parser.add_argument("--out", default=None)
    args = parser.parse_args()
    run(args.manifest, args.old, args.new, args.out)
