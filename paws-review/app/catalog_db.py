import sqlite3
from contextlib import contextmanager

from app.config import CATALOG_DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS captures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,              -- 'kittyflap' | 'reolink'
    source_event_id INTEGER NOT NULL,  -- events.id côté Kittyhack (ou équivalent Reolink)
    block_id INTEGER,
    created_at DATETIME NOT NULL,      -- horodatage de la détection d'origine
    ingested_at DATETIME NOT NULL DEFAULT (datetime('now')),

    cat_id INTEGER,
    cat_name TEXT,
    cat_source TEXT,                   -- 'rfid' | 'visual' | 'unknown'
    rfid TEXT,

    visual_label TEXT,                 -- ce que le modèle visuel pense (original_name/corrected_name)
    visual_confidence REAL,

    prey_probability REAL,             -- mouse_probability (0-100)
    own_cat_probability REAL,
    is_prey_candidate INTEGER NOT NULL DEFAULT 0,
    model_disagreement INTEGER NOT NULL DEFAULT 0,
    low_cat_confidence INTEGER NOT NULL DEFAULT 0,  -- chat peu/pas visible - négatif potentiel, jamais prioritaire

    original_path TEXT NOT NULL,
    thumbnail_path TEXT NOT NULL,
    phash TEXT,

    -- Propositions brutes du modèle au moment de la détection (events.event_text
    -- côté Kittyhack) : [{object_name, probability, x, y, width, height}, ...],
    -- coordonnées en % de l'image. Jamais modifié après ingestion - les
    -- corrections de Sid vivent dans box_annotations, pas ici.
    detected_objects_json TEXT,

    dedup_of_id INTEGER,               -- NULL si photo de référence, sinon pointe vers la représentative du cluster
    review_status TEXT NOT NULL DEFAULT 'pending',  -- pending | confirmed | corrected | rejected | skipped_dedup
    corrected_label TEXT,
    reviewed_at DATETIME,

    kept_for_training INTEGER NOT NULL DEFAULT 0,  -- exempte de la purge par âge

    UNIQUE(source, source_event_id)
);

CREATE INDEX IF NOT EXISTS idx_captures_review_queue
    ON captures (review_status, is_prey_candidate DESC, model_disagreement DESC, created_at);
CREATE INDEX IF NOT EXISTS idx_captures_cat_recent
    ON captures (cat_id, created_at);
CREATE INDEX IF NOT EXISTS idx_captures_created_at ON captures (created_at);

-- Une photo peut montrer plusieurs chats (double détection avec deux chats
-- différents, pas seulement une boîte dupliquée) - le RFID ne capte que celui
-- qui a déclenché l'entrée, pas forcément tous les chats visibles sur l'image.
-- Recalculé à chaque changement de boîtes (ingestion, correction, ajout,
-- suppression) à partir des boîtes effectives (modèle + corrections, hors
-- ignorées) - une photo apparaît sous l'onglet de CHAQUE chat qu'elle contient.
CREATE TABLE IF NOT EXISTS capture_cat_labels (
    capture_id INTEGER NOT NULL REFERENCES captures(id) ON DELETE CASCADE,
    cat_name TEXT NOT NULL,
    PRIMARY KEY (capture_id, cat_name)
);

CREATE TABLE IF NOT EXISTS ingest_state (
    source TEXT PRIMARY KEY,
    last_source_event_id INTEGER NOT NULL DEFAULT 0
);

-- Les corrections/ajouts explicites de Sid sur une capture. Vide tant qu'elle
-- n'a rien fait - detected_objects_json (sur captures) reste la seule source
-- tant qu'aucune ligne n'existe ici pour cette capture. object_index suit la
-- convention Kittyhack : index dans detected_objects pour confirmer/corriger
-- une boîte du modèle, ou un index au-delà de len(detected_objects) pour une
-- boîte dessinée à la main (source='manual' - cas "le modèle n'a rien vu").
CREATE TABLE IF NOT EXISTS box_annotations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    capture_id INTEGER NOT NULL REFERENCES captures(id) ON DELETE CASCADE,
    object_index INTEGER NOT NULL,
    source TEXT NOT NULL DEFAULT 'model',  -- 'model' | 'manual'
    label TEXT NOT NULL,
    x REAL NOT NULL,
    y REAL NOT NULL,
    width REAL NOT NULL,
    height REAL NOT NULL,
    confirmed INTEGER NOT NULL DEFAULT 1,
    ignored INTEGER NOT NULL DEFAULT 0,  -- détection en double/fausse écartée explicitement par Sid
    created_at DATETIME NOT NULL DEFAULT (datetime('now')),
    UNIQUE(capture_id, object_index)
);

-- 20.10, Sid ("le module de revérification des anciens modèles contre le
-- nouveau... je contrôle tout au même endroit") - portage de
-- src/model/retroactive_review.py côté Kittyhack : fait tourner un modèle
-- fraîchement entraîné sur les captures déjà confirmées/corrigées et
-- compare à la vérité terrain actuelle (effective_boxes, donc déjà avec
-- les corrections de Sid appliquées). Une capture scannée par un
-- model_version donné ne l'est plus jamais deux fois pour CE modèle (même
-- principe que ModelReviewRepo côté Kittyhack) - un second scan avance
-- naturellement sur les captures pas encore vues plutôt que de rejouer les
-- mêmes.
CREATE TABLE IF NOT EXISTS model_review_scanned (
    capture_id INTEGER NOT NULL,
    model_version TEXT NOT NULL,
    PRIMARY KEY (capture_id, model_version)
);

-- 20.10, Sid ("empêcher de recharger un truc que j'aurai déjà chargé une
-- fois") - un événement (event_key = block_id, ou "solo-<capture_id>" pour
-- les sources jamais groupées) déjà rendu dans la galerie de comparaison
-- pour CE couple de modèles précis ne doit plus jamais y revenir, même
-- d'une session à l'autre (pas juste le temps d'une page ouverte) - voir
-- model_compare.py run_recent_events.
CREATE TABLE IF NOT EXISTS model_compare_scanned (
    event_key TEXT NOT NULL,
    old_version TEXT NOT NULL,
    new_version TEXT NOT NULL,
    PRIMARY KEY (event_key, old_version, new_version)
);

-- 20.10, Sid ("il faut garder les filtres et corrélations RFID... pas que
-- j'en aie 60 000 à contrôler") - une ligne ici = un VRAI désaccord à
-- trancher à la main. Un désaccord sur une capture cat_source='rfid' (donc
-- déjà une vérité terrain fiable par puce, plus fiable qu'une inférence
-- fraîche) n'atterrit jamais ici sauf s'il touche Prey d'un côté ou de
-- l'autre - même principe de prudence que rfid_ground_truth.py côté
-- Kittyhack ("jamais une proie non détectée ne doit passer entre les
-- gouttes"). Voir model_review.py pour la logique de filtrage exacte.
CREATE TABLE IF NOT EXISTS model_review_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    capture_id INTEGER NOT NULL REFERENCES captures(id) ON DELETE CASCADE,  -- représentante du groupe (vignette affichée)
    model_version TEXT NOT NULL,
    old_label TEXT,
    old_probability REAL,
    new_label TEXT NOT NULL,
    new_probability REAL,
    new_x REAL NOT NULL,
    new_y REAL NOT NULL,
    new_width REAL NOT NULL,
    new_height REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',  -- pending | applied | dismissed
    created_at DATETIME NOT NULL DEFAULT (datetime('now')),
    resolved_at DATETIME
);
CREATE INDEX IF NOT EXISTS idx_model_review_queue_pending
    ON model_review_queue (status, created_at);

-- 20.10, Sid ("pas trop de vérifications sur des trucs identiques") - un
-- même désaccord (même bloc de mouvement, même ancienne/nouvelle
-- étiquette) regroupe toutes ses frames ici plutôt qu'une ligne
-- model_review_queue par frame - une seule tuile à trancher, appliquée à
-- tout le groupe d'un coup. object_index propre à CHAQUE capture (peut
-- différer d'une frame à l'autre même au sein du même groupe) : "appliquer"
-- corrige l'étiquette de la boîte déjà existante de chaque membre sans
-- toucher sa géométrie (jamais la géométrie de la représentante, qui
-- n'aurait aucun sens sur une autre image).
CREATE TABLE IF NOT EXISTS model_review_queue_members (
    queue_id INTEGER NOT NULL REFERENCES model_review_queue(id) ON DELETE CASCADE,
    capture_id INTEGER NOT NULL REFERENCES captures(id) ON DELETE CASCADE,
    object_index INTEGER,  -- NULL si cette frame-ci n'avait aucune boîte existante
    PRIMARY KEY (queue_id, capture_id)
);
"""


@contextmanager
def get_conn():
    # 18.10, Sid ("il a planté le paws review ?") - "database is locked"
    # vu 19x en 30 min dans les logs : plusieurs processus distincts
    # (web, ingest Kittyflap+Reolink, export) ouvrent chacun leurs propres
    # connexions courtes vers le MÊME fichier SQLite en concurrence. Le
    # timeout par défaut de sqlite3 (5s) est trop court une fois que
    # plusieurs écritures courtes se chevauchent vraiment. Deux correctifs
    # ensemble : WAL (lecteurs et écrivains ne se bloquent plus mutuellement,
    # contrairement au mode rollback-journal par défaut) + timeout élargi à
    # 30s en secours pour les vraies écritures concurrentes.
    conn = sqlite3.connect(CATALOG_DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)


def _migrate(conn: sqlite3.Connection):
    # 18.10, Sid ("un bouton qui lance l'export des photos validées") -
    # CREATE TABLE IF NOT EXISTS (dans SCHEMA) ne touche jamais une table
    # déjà existante, donc une colonne ajoutée après coup a besoin de son
    # propre ALTER TABLE, protégé pour rester sans danger à rejouer à
    # chaque démarrage (ajouté une fois pour toutes, puis no-op ensuite).
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(captures)")}
    if "exported_at" not in existing:
        conn.execute("ALTER TABLE captures ADD COLUMN exported_at DATETIME")
