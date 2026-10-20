"""
Canonicalisation des labels de boîtes pour l'export d'entraînement -
portage Python de canonicalLabel()/LABEL_SYNONYMS (app/static/app.js),
pour que les deux restent cohérents.

18.10, Sid : "ça doit être toujours majuscule, identique aux noms inscrits
dans kittyhack. Y a eu un bug à un moment donné où les 4 chats avaient des
boîtes majuscules et minuscules." - confirmé dans le classes.txt réel de
LabelStudio ("Patoune" ET "patoune" comme deux classes distinctes). La
comparaison ici est insensible à la casse, mais le résultat exporté est
TOUJOURS la forme canonique en majuscule - jamais la variante bugguée.

CLASSES est la liste figée des classes réellement entraînables par le
modèle actuel (lue depuis le classes.txt réel de LabelStudio le 18.10,
"patoune" en minuscule exclu comme bug, pas une vraie 6e classe). "Autre"
n'en fait pas partie : c'est une étiquette de triage côté Paws Review, pas
une classe du modèle - une boîte "Autre" est exclue de l'export (ni
positif pour une classe qui n'existe pas, ni négatif).
"""

CLASSES = ["Pookie", "Patoune", "Nala", "Boubou", "Prey"]

_SYNONYMS = {
    "prey": "Prey",
    "proie": "Prey",
    "maus": "Prey",
    "mouse": "Prey",
}
_CANONICAL_BY_LOWER = {c.lower(): c for c in CLASSES}


def canonicalize(raw_label: str | None) -> str | None:
    """Renvoie la classe canonique (une valeur de CLASSES), ou None si le
    label ne correspond à aucune classe entraînable (ex: "Autre", ou un
    label inconnu/vide) - l'appelant doit alors exclure cette boîte de
    l'export plutôt que d'inventer une classe."""
    if not raw_label:
        return None
    lower = raw_label.strip().lower()
    if lower in _SYNONYMS:
        return _SYNONYMS[lower]
    return _CANONICAL_BY_LOWER.get(lower)
