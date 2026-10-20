"""
19.10, Sid ("tu peux tester ces deux points? et faire tes calculs pour
passer en 640x640?") - banc d'essai isolé (jamais le service réel) pour
comparer, sur le même modèle et les mêmes photos réelles :
  1. NCNN CPU à 320 (ce qui tourne en prod aujourd'hui)
  2. NCNN CPU à 640 (le gain de précision proposé, coût CPU réel mesuré au
     lieu d'estimé)
  3. OpenVINO sur l'iGPU Intel (intel:gpu) - déjà codé dans
     model_handler.py côté Kittyhack mais jamais activé ni testé

Usage (conteneur jetable, voir model_compare.py pour le pattern complet) :
    python -m app.inference_bench --model /models/<ts>/model.pt --images /sample_images --n 30
"""

import argparse
import glob
import os
import time


def _bench(predict_fn, image_paths: list[str], label: str, warmup: int = 2) -> dict:
    for p in image_paths[:warmup]:
        predict_fn(p)
    t0 = time.perf_counter()
    for p in image_paths:
        predict_fn(p)
    elapsed = time.perf_counter() - t0
    per_image_ms = (elapsed / len(image_paths)) * 1000
    print(f"{label} : {per_image_ms:.1f} ms/image ({len(image_paths)} images, {elapsed:.2f}s total)")
    return {"label": label, "ms_per_image": per_image_ms, "n": len(image_paths)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True, help="Chemin vers model.pt")
    parser.add_argument("--images", required=True, help="Dossier d'images de test (.jpg)")
    parser.add_argument("--n", type=int, default=30)
    args = parser.parse_args()

    from ultralytics import YOLO

    image_paths = sorted(glob.glob(os.path.join(args.images, "*.jpg")))[: args.n]
    if not image_paths:
        raise SystemExit(f"Aucune image .jpg trouvée dans {args.images}")
    print(f"{len(image_paths)} images de test\n")

    results = []

    # 1. NCNN CPU @320 - baseline actuelle en prod
    ncnn320_dir = args.model.replace("model.pt", "best_ncnn_model")
    if os.path.isdir(ncnn320_dir):
        model_ncnn320 = YOLO(ncnn320_dir, task="detect")
        results.append(_bench(
            lambda p: model_ncnn320.predict(p, imgsz=320, device="cpu", verbose=False),
            image_paths, "NCNN CPU @320 (actuel en prod)",
        ))
    else:
        print(f"ATTENTION : {ncnn320_dir} introuvable, étape 1 ignorée")

    # 2. NCNN CPU @640 - même modèle, export à la nouvelle résolution.
    # /models est monté en lecture seule (sécurité) - export() écrit par
    # défaut à côté du .pt source, donc on travaille sur une copie locale
    # inscriptible plutôt que de changer le montage.
    import shutil
    local_pt = "/tmp/model.pt"
    shutil.copy2(args.model, local_pt)
    print("\nExport NCNN @640...")
    base_model = YOLO(local_pt, task="detect")
    export_path_640 = base_model.export(format="ncnn", imgsz=640)
    model_ncnn640 = YOLO(export_path_640, task="detect")
    results.append(_bench(
        lambda p: model_ncnn640.predict(p, imgsz=640, device="cpu", verbose=False),
        image_paths, "NCNN CPU @640 (proposition)",
    ))

    # 3. OpenVINO sur l'iGPU Intel - jamais testé, voir model_handler.py
    try:
        print("\nExport OpenVINO...")
        export_path_ov = base_model.export(format="openvino", imgsz=320)
        model_ov = YOLO(export_path_ov, task="detect")
        results.append(_bench(
            lambda p: model_ov.predict(p, imgsz=320, device="intel:gpu", verbose=False),
            image_paths, "OpenVINO iGPU @320",
        ))
    except Exception as e:
        print(f"\nOpenVINO iGPU a échoué : {e}")

    print("\n=== Résumé ===")
    baseline = next((r for r in results if "320" in r["label"] and "NCNN" in r["label"]), None)
    for r in results:
        factor = f" ({r['ms_per_image'] / baseline['ms_per_image']:.2f}x)" if baseline else ""
        print(f"  {r['label']} : {r['ms_per_image']:.1f} ms/image{factor}")


if __name__ == "__main__":
    main()
