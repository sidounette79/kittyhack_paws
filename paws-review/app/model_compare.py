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
import logging
import os
import random
import sqlite3
import threading
import time

from PIL import Image, ImageDraw, ImageFont

from app.boxes import effective_boxes
from app.catalog_db import get_conn, init_db
from app.config import STORAGE_ROOT
from app.model_review import _dominant_label
from app.labels import CLASSES, canonicalize

log = logging.getLogger("model_compare")

COMPARISONS_DIR = os.path.join(STORAGE_ROOT, "model_comparisons")
DEFAULT_MODELS_DIR = os.environ.get("KITTYHACK_MODELS_DIR_RO", "/models")
SAMPLE_SIZE = 30

# 20.10, Sid ("50 en asynchrone, ça me semble pas mal") - même mécanique
# que model_review.py (state JSON + verrou + thread daemon) : ~0,45s/image
# mesuré en vrai (25s pour 50), trop long pour un simple POST synchrone
# sans retour de progression pendant tout ce temps.
COMPARE_STATE_PATH = os.path.join(STORAGE_ROOT, "model_compare_recent_state.json")
_compare_run_lock = threading.Lock()


def _write_compare_state(state: dict):
    tmp = COMPARE_STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f)
    os.replace(tmp, COMPARE_STATE_PATH)


def read_compare_state() -> dict:
    if not os.path.isfile(COMPARE_STATE_PATH):
        return {"status": "idle"}
    try:
        with open(COMPARE_STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"status": "idle"}


def start_compare_recent_async(
    old_dir: str, new_dir: str, limit: int, class_filter: str | None, session_dir: str | None,
) -> bool:
    if not _compare_run_lock.acquire(blocking=False):
        return False
    thread = threading.Thread(
        target=_run_compare_recent_locked,
        args=(old_dir, new_dir, limit, class_filter, session_dir),
        daemon=True,
    )
    thread.start()
    return True


def _run_compare_recent_locked(old_dir, new_dir, limit, class_filter, session_dir):
    try:
        run_recent_events(old_dir, new_dir, limit, class_filter=class_filter, session_dir=session_dir)
    except Exception as e:
        log.exception("Comparaison récente échouée")
        _write_compare_state({"status": "error", "message": str(e)})
    finally:
        _compare_run_lock.release()

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
    # 20.10, Sid ("même le nez collé à l'écran je vois rien") - taille fixe
    # (14px) illisible une fois l'image affichée à sa vraie résolution (les
    # caméras Reolink/Kittyflap sont en 1440p+) - mise à l'échelle sur la
    # hauteur réelle de l'image plutôt qu'une constante.
    # 26.10, Sid ("c'est encore petit... mets 3 de plus") - texte gravé
    # dans l'image (pas un overlay CSS), donc un +3px se perdrait à peine
    # visible une fois l'image à 3 volets réduite à la largeur de l'écran -
    # augmentation réelle (H//24 -> H//16, planché 20 -> 28) plutôt qu'un
    # ajustement cosmétique.
    font_size = max(28, H // 16)
    line_width = max(3, H // 300)
    try:
        font = ImageFont.truetype("DejaVuSans-Bold.ttf", font_size)
    except Exception:
        font = ImageFont.load_default()
    label_h = font_size + 6
    for label, x, y, w, h, prob in boxes:
        x1, y1 = x / 100.0 * W, y / 100.0 * H
        x2, y2 = (x + w) / 100.0 * W, (y + h) / 100.0 * H
        draw.rectangle([x1, y1, x2, y2], outline=color, width=line_width)
        text = f"{label} {prob:.0f}%" if prob is not None else label
        tw = draw.textlength(text, font=font)
        # 26.10, Sid ("l'étiquette est beaucoup beaucoup trop petite") -
        # pas un souci de taille de police (déjà mise à l'échelle depuis le
        # 20.10), mais un vrai bug : max(0, y1 - label_h) ne préservait pas
        # label_h quand la boîte touche le haut de l'image (y1 petit) -
        # juste capé à 0, donc la bande d'étiquette se retrouvait haute de
        # y1 pixels seulement au lieu de label_h, écrasant le texte en un
        # éclat illisible. Même hauteur toujours garantie : au-dessus de la
        # boîte normalement, ou juste sous son bord haut si ça déborderait
        # de l'image.
        label_y1 = y1 - label_h if y1 - label_h >= 0 else y1
        label_y2 = label_y1 + label_h
        draw.rectangle([x1, label_y1, x1 + tw + 10, label_y2], fill=color)
        draw.text((x1 + 5, label_y1 + 2), text, fill=(255, 255, 255), font=font)
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


def _recent_event_groups(
    conn: sqlite3.Connection, limit: int, old_version: str, new_version: str, class_filter: str | None = None,
) -> tuple[list, list, int]:
    """20.10, Sid ("un bouton qui sort la comparaison... un clic lance les
    10 derniers événements, puis ceux d'avant") - un événement = un groupe
    de captures qui partagent le même block_id (un vrai passage), pas une
    frame isolée ; les sources jamais groupées (certaines captures Reolink)
    restent chacune leur propre événement. Un seul représentant par
    événement, choisi avec la même priorité Prey-d'abord que
    model_review._dominant_label - cohérent avec le reste de l'outil.

    class_filter (20.10, Sid "un filtre pour chacun" - plus seulement Prey,
    n'importe quelle classe, Prey comprise) - filtre AVANT sélection sur la
    vérité terrain ORIGINALE (effective_boxes, jamais les prédictions) : un
    événement est gardé si une de ses captures contenait réellement cette
    classe, confirmée/corrigée.

    20.10, Sid ("empêcher de recharger un truc que j'aurai déjà chargé une
    fois") - exclut aussi, de façon PERSISTANTE (model_compare_scanned,
    survit aux rechargements de page), tout événement déjà rendu pour CE
    couple de modèles précis - plus de notion d'offset, "les N derniers"
    veut dire "les N prochains jamais encore comparés pour ce couple".

    Retourne (lignes représentatives, event_key de chacune dans le même
    ordre, nombre total d'événements RESTANTS pour ce filtre+couple)."""
    rows = conn.execute(
        """SELECT id, block_id, original_path, detected_objects_json, created_at FROM captures
           WHERE review_status IN ('confirmed', 'corrected') AND original_path IS NOT NULL
           ORDER BY created_at DESC"""
    ).fetchall()

    groups: dict = {}
    for row in rows:
        key = str(row["block_id"]) if row["block_id"] is not None else f"solo-{row['id']}"
        groups.setdefault(key, []).append(row)

    scanned = {
        r["event_key"] for r in conn.execute(
            "SELECT event_key FROM model_compare_scanned WHERE old_version = ? AND new_version = ?",
            (old_version, new_version),
        ).fetchall()
    }

    ordered_keys = sorted(groups, key=lambda k: max(r["created_at"] for r in groups[k]), reverse=True)
    ordered_keys = [k for k in ordered_keys if k not in scanned]

    # 26.10, Sid ("un filtre sur négatif... on était souvent tagué comme
    # boubou au lieu de rien") - "Negatif" garde un groupe seulement si
    # AUCUNE de ses captures n'a de boîte classifiable (vérité terrain
    # réellement vide partout), pas juste "une boîte classe X" comme les
    # autres filtres.
    if class_filter == "Negatif":
        kept_keys = []
        for key in ordered_keys:
            if all(
                not any(canonicalize(b.get("label")) for b in effective_boxes(conn, row["id"], row["detected_objects_json"]))
                for row in groups[key]
            ):
                kept_keys.append(key)
        ordered_keys = kept_keys
    elif class_filter:
        kept_keys = []
        for key in ordered_keys:
            for row in groups[key]:
                boxes = effective_boxes(conn, row["id"], row["detected_objects_json"])
                if any(canonicalize(b.get("label")) == class_filter for b in boxes):
                    kept_keys.append(key)
                    break
        ordered_keys = kept_keys

    page_keys = ordered_keys[:limit]

    reps, rep_keys = [], []
    for key in page_keys:
        best_row, best_label, best_prob = None, None, -1.0
        for row in groups[key]:
            boxes = effective_boxes(conn, row["id"], row["detected_objects_json"])
            label, prob, _, _ = _dominant_label(boxes)
            if label is None:
                continue
            is_prey, was_prey = label == "Prey", best_label == "Prey"
            if best_row is None or (is_prey and not was_prey) or (is_prey == was_prey and prob > best_prob):
                best_row, best_label, best_prob = row, label, prob
        # 26.10, Sid ("ça me dit qu'il y en a mais ça me sort rien dans la
        # galerie") - un groupe réellement vide (filtre Negatif, par
        # définition aucune boîte nulle part) ne fixait jamais best_row
        # (boucle ci-dessus : "if label is None: continue" sur CHAQUE
        # ligne) - le groupe passait le filtre, comptait dans total_events,
        # puis disparaissait silencieusement ici, jamais ajouté à reps.
        # Première photo du groupe comme représentante dans ce cas.
        if best_row is None and groups[key]:
            best_row = groups[key][0]
        if best_row is not None:
            reps.append(best_row)
            rep_keys.append(key)

    return reps, rep_keys, len(ordered_keys)


def run_recent_events(
    old_dir: str, new_dir: str, limit: int,
    class_filter: str | None = None, session_dir: str | None = None,
) -> dict:
    """Variante chronologique de run() : au lieu d'un échantillon aléatoire
    équilibré par classe, prend les N PROCHAINS événements réels jamais
    encore comparés pour ce couple de modèles (voir _recent_event_groups),
    vérité terrain (vert) / ancien modèle (orange) / nouveau modèle (bleu)
    côte à côte.

    20.10, Sid ("si je clique 3x avant d'aller sur la galerie, j'en vois
    30 ?") - session_dir (reçu du frontend, c'est le out_dir renvoyé par
    l'appel précédent) fait que chaque clic ACCUMULE dans la MÊME galerie
    au lieu d'en recréer une nouvelle à chaque fois : le manifeste
    _entries.json porte la liste complète vue jusqu'ici, fusionnée (sans
    doublon par capture_id) et retriée avant de régénérer index.html en
    entier. Sans session_dir (premier clic, ou "recommencer"), une
    nouvelle galerie est créée - les événements déjà comparés avant
    restent exclus quand même (model_compare_scanned, pas lié à la
    galerie elle-même)."""
    from ultralytics import YOLO

    t0 = time.time()
    init_db()
    old_version, new_version = os.path.basename(old_dir), os.path.basename(new_dir)
    _write_compare_state({"status": "running", "step": "loading_model", "elapsed_s": time.time() - t0})
    old_model = YOLO(os.path.join(old_dir, "model.pt"))
    new_model = YOLO(os.path.join(new_dir, "model.pt"))

    if session_dir:
        out_dir = session_dir
    else:
        ts = time.strftime("%Y%m%d_%H%M%S")
        out_dir = os.path.join(COMPARISONS_DIR, f"{ts}_recent")
    os.makedirs(out_dir, exist_ok=True)

    manifest_path = os.path.join(out_dir, "_entries.json")
    existing_entries = []
    if os.path.isfile(manifest_path):
        with open(manifest_path) as f:
            existing_entries = json.load(f)

    with get_conn() as conn:
        rows, rep_keys, total_events = _recent_event_groups(conn, limit, old_version, new_version, class_filter)
        new_entries = []
        for i, (row, event_key) in enumerate(zip(rows, rep_keys)):
            _write_compare_state({
                "status": "running", "step": "comparing", "scanned": i, "total": len(rows),
                "elapsed_s": time.time() - t0,
            })
            # Scannée dans tous les cas, fichier trouvé ou pas - sinon un
            # fichier manquant reviendrait indéfiniment à chaque clic.
            conn.execute(
                "INSERT OR IGNORE INTO model_compare_scanned (event_key, old_version, new_version) VALUES (?, ?, ?)",
                (event_key, old_version, new_version),
            )
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

            fname = f"capture_{row['id']}.jpg"
            combined.save(os.path.join(out_dir, fname), quality=90)
            gt_set = {canonicalize(l) for l, *_ in gt} - {None}
            old_set = {canonicalize(l) for l, *_ in old_pred} - {None}
            new_set = {canonicalize(l) for l, *_ in new_pred} - {None}
            # 26.10, Sid ("un filtre sur négatif... on était souvent tagué
            # comme boubou au lieu de rien") - une vérité terrain réellement
            # vide devient sa propre pseudo-classe "Negatif", avec la même
            # logique rattrapé/identique/régression que les autres classes
            # (voir _write_recent_gallery_html) : un modèle qui invente
            # quelque chose sur du vide EST un faux positif, celui qui ne
            # prédit rien EST correct ici - réutilise la machinerie de tags/
            # résumé existante sans la dupliquer.
            if not gt_set:
                gt_set = {"Negatif"}
                if not old_set:
                    old_set = {"Negatif"}
                if not new_set:
                    new_set = {"Negatif"}
            new_entries.append({
                "filename": fname,
                "capture_id": row["id"],
                "created_at": row["created_at"],
                "gt_classes": sorted(gt_set),
                "old_classes": sorted(old_set),
                "new_classes": sorted(new_set),
            })
        conn.commit()

    seen_ids = {e["capture_id"] for e in existing_entries}
    merged = existing_entries + [e for e in new_entries if e["capture_id"] not in seen_ids]
    merged.sort(key=lambda e: e["created_at"], reverse=True)

    with open(manifest_path, "w") as f:
        json.dump(merged, f)

    _write_recent_gallery_html(out_dir, merged, old_dir, new_dir, total_events, class_filter)
    url = "/model-comparisons/" + os.path.basename(out_dir) + "/index.html"
    result = {
        "out_dir": out_dir, "url": url, "count": len(new_entries), "total_shown": len(merged),
        "total_events": total_events, "limit": limit, "class_filter": class_filter,
    }
    _write_compare_state({"status": "success", "finished_at": time.time(), **result})
    return result


# 26.10, Sid ("un résumé : éléments rattrapés, identiques et régression
# avec le nombre de chaque") - même logique que les tags par ligne
# juste en dessous (win/regress/"miss" ou rien), agrégée par classe vérité
# terrain. "Identiques" regroupe les deux cas sans tag (les deux modèles
# d'accord, qu'ils aient raison ou qu'ils loupent tous les deux) - elle n'a
# demandé que 3 catégories, pas une 4e séparant "toujours loupé" de "les
# deux ont raison".
def _compute_recent_summary(entries: list[dict]) -> dict:
    summary: dict[str, dict[str, int]] = {}
    for e in entries:
        for cls in e["gt_classes"]:
            had_old, had_new = cls in e["old_classes"], cls in e["new_classes"]
            s = summary.setdefault(cls, {"win": 0, "regress": 0, "identical": 0})
            if not had_old and had_new:
                s["win"] += 1
            elif had_old and not had_new:
                s["regress"] += 1
            else:
                s["identical"] += 1
    return summary


def _write_recent_gallery_html(out_dir: str, entries: list[dict], old_dir: str, new_dir: str, total_events: int, class_filter: str | None):
    summary = _compute_recent_summary(entries)
    total = {"win": 0, "regress": 0, "identical": 0}
    summary_rows = []
    for cls in sorted(summary):
        s = summary[cls]
        for k in total:
            total[k] += s[k]
        summary_rows.append(
            f'<tr><td>{cls}</td><td class="win">{s["win"]}</td>'
            f'<td class="regress">{s["regress"]}</td><td>{s["identical"]}</td></tr>'
        )
    summary_html = f"""
    <table class="summary">
      <thead><tr><th>Classe</th><th>Rattrapés</th><th>Régression</th><th>Identiques</th></tr></thead>
      <tbody>
        {''.join(summary_rows)}
        <tr class="total"><td>Total</td><td class="win">{total['win']}</td>
            <td class="regress">{total['regress']}</td><td>{total['identical']}</td></tr>
      </tbody>
    </table>"""

    rows_html = []
    for e in entries:
        tags = []
        for cls in e["gt_classes"]:
            had_old, had_new = cls in e["old_classes"], cls in e["new_classes"]
            if not had_old and had_new:
                tags.append(f'<span class="tag win">{cls} rattrapé</span>')
            elif had_old and not had_new:
                tags.append(f'<span class="tag regress">{cls} régression</span>')
            elif not had_old and not had_new:
                tags.append(f'<span class="tag miss">{cls} toujours loupé</span>')
        rows_html.append(f"""
        <div class="row">
          <div class="meta">{e['created_at']} - capture #{e['capture_id']} {''.join(tags)}</div>
          <img src="{e['filename']}" loading="lazy">
        </div>""")

    filter_note = f" - filtré sur les événements avec {class_filter} réel (vérité terrain)" if class_filter else ""
    html = f"""<!doctype html><html><head><meta charset="utf-8">
<title>Comparaison événements récents</title>
<style>
body{{background:#111;color:#eee;font-family:system-ui;margin:0;padding:16px}}
h1{{font-size:16px}}
.legend{{font-size:13px;color:#aaa;margin-bottom:16px}}
.row{{margin-bottom:24px}}
.meta{{font-size:13px;margin-bottom:4px;color:#ccc}}
.tag{{padding:2px 8px;border-radius:4px;font-size:12px;margin-left:8px}}
.tag.win{{background:#27ae60}}
.tag.miss{{background:#c0392b}}
.tag.regress{{background:#8e44ad}}
table.summary{{border-collapse:collapse;margin-bottom:20px;font-size:13px}}
table.summary td,table.summary th{{padding:4px 10px;border:1px solid #333;text-align:center}}
table.summary td.win{{background:#1e5a38}}
table.summary td.regress{{background:#4a2a5a}}
table.summary tr.total{{font-weight:bold}}
img{{max-width:100%;border:1px solid #333}}
</style></head><body>
<h1>Vérité terrain (vert) / ancien modèle (orange, {os.path.basename(old_dir)}) / nouveau modèle (bleu, {os.path.basename(new_dir)})</h1>
<div class="legend">{len(entries)} événements affichés sur {total_events} au total{filter_note} (les plus récents d'abord)</div>
{summary_html}
{''.join(rows_html)}
</body></html>"""
    with open(os.path.join(out_dir, "index.html"), "w", encoding="utf-8") as f:
        f.write(html)


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
