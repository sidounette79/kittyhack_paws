"""
19.10, Sid ("je construis [le test visuel] une fois que le modèle actuel a
fini d'entraîner") - compare visuellement deux modèles (l'ancien actif dans
Kittyhack vs le nouveau tout juste sorti de vast.ai) sur un échantillon de
photos déjà validées dans Paws Review, avec les boîtes superposées :
vérité terrain (vert, ce que Sid a validé/corrigé) / ancien modèle (orange)
/ nouveau modèle (bleu).

Volontairement PAS dans requirements.txt/Dockerfile de Paws Review -
ultralytics+torch sont lourds et ce script ne tourne qu'à la demande,
jamais en continu. Même logique que train_vastai.py (ultralytics installé
à la volée, jamais dans l'image de base) : à exécuter via un conteneur
jetable sur FUNMEDIA, ex.

    docker run --rm \
        -v /volume1/docker/paws-review/storage:/storage \
        -v /volume1/docker/kittyhack-remote/data/models/yolo:/models:ro \
        -w /work -v /volume1/docker/paws-review/app:/work/app \
        python:3.11-slim bash -c "
            pip install -q ultralytics pillow &&
            python -m app.model_compare --old /models/<ancien_ts> --new /models/<nouveau_ts>
        "

Sans --old/--new : prend automatiquement les deux dossiers de modèles les
plus récents sous /models (le nouveau et celui qu'il remplace).
"""

import argparse
import json
import os
import random
import sqlite3
import time

from PIL import Image, ImageDraw, ImageFont

from app.boxes import effective_boxes
from app.catalog_db import get_conn, init_db
from app.config import STORAGE_ROOT
from app.labels import CLASSES, canonicalize

COMPARISONS_DIR = os.path.join(STORAGE_ROOT, "model_comparisons")
DEFAULT_MODELS_DIR = os.environ.get("KITTYHACK_MODELS_DIR_RO", "/models")
SAMPLE_SIZE = 30

COLOR_GT = (46, 204, 113)      # vert - vérité terrain (Sid)
COLOR_OLD = (230, 126, 34)     # orange - ancien modèle
COLOR_NEW = (52, 152, 219)     # bleu - nouveau modèle


def _latest_model_dirs(models_root: str, n: int = 2) -> list[str]:
    entries = [
        os.path.join(models_root, d) for d in os.listdir(models_root)
        if os.path.isfile(os.path.join(models_root, d, "model.pt"))
    ]
    entries.sort(key=os.path.getmtime, reverse=True)
    if len(entries) < n:
        raise SystemExit(
            f"Seulement {len(entries)} dossier(s) de modèle trouvé(s) sous {models_root}, "
            f"{n} nécessaires. Passe --old/--new explicitement."
        )
    return entries[:n]


def _sample_captures(conn: sqlite3.Connection, n: int) -> list[sqlite3.Row]:
    # 19.10, Sid ("compare les proies mais aussi les chats, pour voir les
    # points d'amélioration") - répartition par classe, pas juste
    # proie/pas-proie : chaque classe entraînable (CLASSES) a sa part de
    # l'échantillon, dans la limite de ce qui existe réellement en base.
    # Une capture avec plusieurs chats compte pour chacun - un chat sur-
    # représenté dans les validations ne doit pas pour autant écraser les
    # autres dans l'échantillon.
    all_rows = conn.execute(
        """SELECT id, original_path, detected_objects_json FROM captures
           WHERE review_status IN ('confirmed', 'corrected')
             AND original_path IS NOT NULL"""
    ).fetchall()

    by_class: dict[str, list] = {c: [] for c in CLASSES}
    seen_ids = set()
    for row in all_rows:
        boxes = effective_boxes(conn, row["id"], row["detected_objects_json"])
        classes_here = {canonicalize(b["label"]) for b in boxes} - {None}
        for cls in classes_here:
            by_class[cls].append(row)

    per_class_budget = max(1, n // len(CLASSES))
    selected = []
    for cls in CLASSES:
        rows = by_class[cls]
        random.shuffle(rows)
        taken = 0
        for row in rows:
            if row["id"] in seen_ids:
                continue
            selected.append(row)
            seen_ids.add(row["id"])
            taken += 1
            if taken >= per_class_budget:
                break

    # Complète jusqu'à n avec d'autres validées si des classes étaient trop
    # rares pour remplir leur quota (ex: peu de Boubou validé jusqu'ici).
    if len(selected) < n:
        remaining = [r for r in all_rows if r["id"] not in seen_ids]
        random.shuffle(remaining)
        for row in remaining:
            if len(selected) >= n:
                break
            selected.append(row)
            seen_ids.add(row["id"])

    random.shuffle(selected)
    return selected[:n]


def _draw_boxes(img: Image.Image, boxes: list[tuple[str, float, float, float, float, float | None]], color) -> Image.Image:
    # boxes: (label, x, y, w, h, prob) en pourcentages (convention Paws Review)
    out = img.copy()
    draw = ImageDraw.Draw(out)
    W, H = out.size
    try:
        font = ImageFont.truetype("DejaVuSans-Bold.ttf", 14)
    except Exception:
        font = ImageFont.load_default()
    for label, x, y, w, h, prob in boxes:
        x1, y1 = x / 100.0 * W, y / 100.0 * H
        x2, y2 = (x + w) / 100.0 * W, (y + h) / 100.0 * H
        draw.rectangle([x1, y1, x2, y2], outline=color, width=3)
        text = f"{label} {prob:.0f}%" if prob is not None else label
        tw = draw.textlength(text, font=font)
        draw.rectangle([x1, max(0, y1 - 18), x1 + tw + 6, y1], fill=color)
        draw.text((x1 + 3, max(0, y1 - 18)), text, fill=(255, 255, 255), font=font)
    return out


def _predict_boxes(model, image_path: str) -> list[tuple[str, float, float, float, float, float]]:
    results = model.predict(image_path, verbose=False)
    out = []
    for r in results:
        boxes = getattr(r, "boxes", None)
        if boxes is None:
            continue
        names = r.names
        for xyxyn, conf, cls in zip(boxes.xyxyn, boxes.conf, boxes.cls):
            x1, y1, x2, y2 = (float(v) for v in xyxyn.tolist())
            label = names.get(int(cls), str(int(cls)))
            out.append((label, x1 * 100.0, y1 * 100.0, (x2 - x1) * 100.0, (y2 - y1) * 100.0, float(conf) * 100.0))
    return out


def run(old_dir: str, new_dir: str, sample_size: int, out_dir: str | None = None):
    from ultralytics import YOLO  # import ici : seul point du script qui a besoin de torch

    init_db()
    old_model = YOLO(os.path.join(old_dir, "model.pt"))
    new_model = YOLO(os.path.join(new_dir, "model.pt"))

    ts = time.strftime("%Y%m%d_%H%M%S")
    out_dir = out_dir or os.path.join(COMPARISONS_DIR, ts)
    os.makedirs(out_dir, exist_ok=True)

    with get_conn() as conn:
        rows = _sample_captures(conn, sample_size)
        gallery_entries = []

        for row in rows:
            if not os.path.isfile(row["original_path"]):
                continue
            img = Image.open(row["original_path"]).convert("RGB")

            gt_boxes = effective_boxes(conn, row["id"], row["detected_objects_json"])
            gt = [(b["label"], b["x"], b["y"], b["width"], b["height"], b["probability"]) for b in gt_boxes]
            old_pred = _predict_boxes(old_model, row["original_path"])
            new_pred = _predict_boxes(new_model, row["original_path"])

            panel_gt = _draw_boxes(img, gt, COLOR_GT)
            panel_old = _draw_boxes(img, old_pred, COLOR_OLD)
            panel_new = _draw_boxes(img, new_pred, COLOR_NEW)

            W, H = img.size
            combined = Image.new("RGB", (W * 3 + 20, H), (20, 20, 20))
            combined.paste(panel_gt, (0, 0))
            combined.paste(panel_old, (W + 10, 0))
            combined.paste(panel_new, (2 * W + 20, 0))

            # 19.10, Sid ("compare les proies mais aussi les chats") -
            # présence par classe (pas de matching IoU boîte à boîte, trop
            # fin pour un outil de lecture visuelle) : pour CHAQUE classe
            # présente en vérité terrain sur cette image, le modèle l'a-t-il
            # détectée quelque part dans l'image, oui ou non.
            gt_classes = {canonicalize(l) for l, *_ in gt} - {None}
            old_classes = {canonicalize(l) for l, *_ in old_pred} - {None}
            new_classes = {canonicalize(l) for l, *_ in new_pred} - {None}

            fname = f"capture_{row['id']}.jpg"
            combined.save(os.path.join(out_dir, fname), quality=90)
            gallery_entries.append({
                "filename": fname,
                "capture_id": row["id"],
                "gt_classes": sorted(gt_classes),
                "old_classes": sorted(old_classes),
                "new_classes": sorted(new_classes),
            })

    summary = {c: {"win": 0, "miss": 0, "regress": 0, "stable_ok": 0} for c in CLASSES}
    for e in gallery_entries:
        for cls in e["gt_classes"]:
            had_old = cls in e["old_classes"]
            had_new = cls in e["new_classes"]
            if not had_old and had_new:
                summary[cls]["win"] += 1
            elif not had_old and not had_new:
                summary[cls]["miss"] += 1
            elif had_old and not had_new:
                summary[cls]["regress"] += 1
            else:
                summary[cls]["stable_ok"] += 1

    # Les images avec le plus de changements (rattrapage/régression) en
    # premier - c'est elles qui montrent le plus sur les points d'amélioration.
    def _changes(e):
        return sum(
            1 for cls in e["gt_classes"]
            if (cls in e["old_classes"]) != (cls in e["new_classes"])
        )
    gallery_entries.sort(key=_changes, reverse=True)

    _write_gallery_html(out_dir, gallery_entries, old_dir, new_dir, summary)
    print(f"Comparaison terminée : {len(gallery_entries)} images -> {out_dir}")
    return out_dir


def _write_gallery_html(out_dir: str, entries: list[dict], old_dir: str, new_dir: str, summary: dict):
    summary_rows = "".join(
        f"""<tr><td>{cls}</td><td>{s['stable_ok']}</td>
            <td class="win">{s['win']}</td><td class="regress">{s['regress']}</td>
            <td class="miss">{s['miss']}</td></tr>"""
        for cls, s in summary.items()
        if sum(s.values()) > 0
    )
    summary_html = f"""
    <table class="summary">
      <tr><th>Classe</th><th>Stable (OK avant/après)</th>
          <th>Rattrapées</th><th>Régressions</th><th>Toujours loupées</th></tr>
      {summary_rows}
    </table>"""

    rows_html = []
    for e in entries:
        tags = []
        for cls in e["gt_classes"]:
            had_old = cls in e["old_classes"]
            had_new = cls in e["new_classes"]
            if not had_old and had_new:
                tags.append(f'<span class="tag win">{cls} rattrapé</span>')
            elif not had_old and not had_new:
                tags.append(f'<span class="tag miss">{cls} toujours loupé</span>')
            elif had_old and not had_new:
                tags.append(f'<span class="tag regress">{cls} régression</span>')
        rows_html.append(f"""
        <div class="row">
          <div class="meta">capture #{e['capture_id']} {''.join(tags)}</div>
          <img src="{e['filename']}" loading="lazy">
        </div>""")

    html = f"""<!doctype html><html><head><meta charset="utf-8">
<title>Comparaison modèles</title>
<style>
body{{background:#111;color:#eee;font-family:system-ui;margin:0;padding:16px}}
h1{{font-size:16px}}
.legend{{font-size:13px;color:#aaa;margin-bottom:16px}}
.legend span{{padding:2px 8px;border-radius:4px;margin-right:8px}}
.row{{margin-bottom:24px}}
.meta{{font-size:13px;margin-bottom:4px;color:#ccc}}
.tag{{padding:2px 8px;border-radius:4px;font-size:12px;margin-left:8px}}
.tag.win{{background:#27ae60}}
.tag.miss{{background:#c0392b}}
.tag.regress{{background:#8e44ad}}
img{{max-width:100%;border:1px solid #333}}
table.summary{{border-collapse:collapse;margin-bottom:20px;font-size:13px}}
table.summary td,table.summary th{{padding:4px 10px;border:1px solid #333;text-align:center}}
table.summary td.win{{background:#1e5a38}}
table.summary td.regress{{background:#4a2a5a}}
table.summary td.miss{{background:#5a2222}}
</style></head><body>
<h1>Vérité terrain (vert) / ancien modèle (orange, {os.path.basename(old_dir)}) / nouveau modèle (bleu, {os.path.basename(new_dir)})</h1>
<div class="legend">{len(entries)} images - les plus parlantes (changements old/new) en premier</div>
{summary_html}
{''.join(rows_html)}
</body></html>"""
    with open(os.path.join(out_dir, "index.html"), "w", encoding="utf-8") as f:
        f.write(html)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--old", help="Dossier contenant l'ancien model.pt")
    parser.add_argument("--new", help="Dossier contenant le nouveau model.pt")
    parser.add_argument("--sample", type=int, default=SAMPLE_SIZE)
    parser.add_argument("--models-dir", default=DEFAULT_MODELS_DIR)
    parser.add_argument("--out", default=None)
    args = parser.parse_args()

    old_dir, new_dir = args.old, args.new
    if not old_dir or not new_dir:
        latest_new, latest_old = _latest_model_dirs(args.models_dir, 2)
        old_dir = old_dir or latest_old
        new_dir = new_dir or latest_new

    run(old_dir, new_dir, args.sample, args.out)
