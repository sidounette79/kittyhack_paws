"""
Écarte en masse les captures importées depuis LabelStudio dont le nom de
fichier d'origine (côté LabelStudio - jamais conservé localement, voir
import_labelstudio.py) contient un mot-clé donné.

18.10, Sid : "un moyen de supprimer d'office celles qui viennent de
labelstudio et qui ont 'rain' dans leur nom de fichier ?" - marque
'rejected' (jamais 'supprimé' au sens base/fichiers - réversible, cohérent
avec le reste de l'outil), et seulement celles encore 'pending' : si Sid a
déjà validé une photo à la main, ce script ne revient pas dessus.

Usage (token PAT en saisie interactive, jamais en argument) :
    docker exec -it paws-review-ingest python -m app.bulk_reject_by_filename rain
"""

import getpass
import sys

import requests

from app.catalog_db import get_conn
from app.import_labelstudio import BASE_URL, get_access_token, resolve_image_url


def get_all_tasks_light(access_token: str, project_id: int) -> list[dict]:
    """Comme get_all_tasks (import_labelstudio.py) mais sans fields=all -
    seul `data` (qui contient le chemin/nom de fichier d'origine) est
    nécessaire ici, pas les annotations - plus léger."""
    tasks: list[dict] = []
    page, page_size, total = 1, 100, None
    headers = {"Authorization": f"Bearer {access_token}"}
    while True:
        resp = requests.get(
            f"{BASE_URL}/api/tasks",
            headers=headers,
            params={"project": project_id, "page": page, "page_size": page_size},
            timeout=60,
        )
        if resp.status_code != 200:
            print(f"Tasks API a renvoyé HTTP {resp.status_code} à la page {page}")
            break
        body = resp.json() if resp.text else {}
        chunk = body if isinstance(body, list) else body.get("tasks", body.get("results", []))
        if not chunk:
            break
        tasks.extend(chunk)
        if isinstance(body, dict):
            if total is None:
                total = body.get("total")
            if total is not None and len(tasks) >= total:
                break
            page += 1
        else:
            break
    return tasks


def main():
    if len(sys.argv) < 2:
        print("Usage : python -m app.bulk_reject_by_filename <mot-clé>")
        sys.exit(1)
    keyword = sys.argv[1].lower()

    pat = getpass.getpass("Personal Access Token LabelStudio (jamais affiché ni loggé) : ").strip()
    if not pat:
        print("Token vide, abandon.")
        sys.exit(1)
    access = get_access_token(pat)
    print("Authentifié.")

    project_id = int(input("ID du projet LabelStudio : ").strip())

    print(f"Récupération des tâches du projet {project_id}...")
    tasks = get_all_tasks_light(access, project_id)
    print(f"Total : {len(tasks)} tâches à examiner")

    matched = rejected = already_reviewed = not_found = 0
    with get_conn() as conn:
        for task in tasks:
            img_url = resolve_image_url(task) or ""
            if keyword not in img_url.lower():
                continue
            matched += 1

            row = conn.execute(
                "SELECT id, review_status FROM captures WHERE source = 'labelstudio' AND source_event_id = ?",
                (task["id"],),
            ).fetchone()
            if row is None:
                not_found += 1
                continue
            if row["review_status"] != "pending":
                already_reviewed += 1
                continue

            conn.execute(
                "UPDATE captures SET review_status = 'rejected', reviewed_at = datetime('now') WHERE id = ?",
                (row["id"],),
            )
            rejected += 1

    print(
        f"Terminé : {matched} fichiers correspondant à '{keyword}', "
        f"{rejected} écartées, {already_reviewed} déjà revues (laissées telles quelles), "
        f"{not_found} introuvables en base (jamais importées ou déjà purgées)"
    )


if __name__ == "__main__":
    main()
