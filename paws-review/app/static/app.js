const CAT_COLORS = {
  Pookie: 'var(--cat-pookie)',
  Patoune: 'var(--cat-patoune)',
  Nala: 'var(--cat-nala)',
  Boubou: 'var(--cat-boubou)',
};
function catColor(name) { return CAT_COLORS[name] || 'var(--cat-unknown)'; }

let currentTab = null; // null = toutes, LABELSTUDIO_TAB = import LabelStudio, sinon un nom de chat
let currentQueue = [];
let currentIndex = 0;
let cats = [];
let drawing = null;
let lastStats = {};

// 18.10, Sid ("un onglet Reolink ?") - captures sans identité de chat (pas
// de RFID), importées en bloc par provenance plutôt que vues au fil de
// l'eau comme le Kittyflap (ingest_reolink.py, watchdog_training_samples).
// Onglet à part plutôt qu'un chat parmi d'autres : filtre par provenance,
// pas par identité.
// 20.10, Sid ("remplacer l'onglet labelstudio qui ne servira plus") -
// LabelStudio décommissionné (voir Paws Review lui-même, qui le
// remplace), son onglet source (~4800 captures, déjà toutes importées une
// fois pour toutes) retiré - plus aucune nouvelle capture n'arrivera par
// ce canal. Son emplacement dans la barre d'onglets réutilisé par
// COMPARE_TAB ci-dessous.
// 19.10, Sid ("un dossier dans docker dans lequel je peux ajouter
// manuellement des photos qui viendraient direct dans paws reviews") -
// storage/manual_drop, repris par ingest_manual.py à chaque cycle.
// 25.10, Sid ("on peut faire qu'un onglet pour manuel et reolink, les
// manuelles je les prends de l'app reolink de toute façon") - fusionnés :
// Reolink et Manuel étaient déjà le même angle caméra pour elle, la
// distinction de source n'apportait rien à la revue. 'reolink_or_manual'
// (IN ('reolink','manual') côté backend) plutôt que la valeur
// 'reolink_manual' déjà existante (utilisée par Rééquilibrage/stats, qui
// fait `source != 'kittyflap'` - regrouperait par erreur les 185 captures
// 'labelstudio' encore pending, jamais voulu ici).
const SOURCE_TABS = [
  { key: '__reolink__', label: 'Reolink', source: 'reolink_or_manual', statKey: 'reolink_or_manual_pending' },
];

// 18.10, Sid ("plus logique d'avoir un onglet avec les potentielles
// proies") - is_prey_candidate triait déjà en priorité dans chaque onglet
// (toujours en premier), mais rien ne permettait de ne voir QUE ça, toutes
// identités/provenances confondues - le cas le plus rare et le plus
// critique. Couleur danger (rouge), cohérente avec le surlignage des
// boîtes "Proie" déjà existant sur les photos.
const PREY_TAB = '__prey__';

// 18.10, Sid ("un onglet où je vois les validées et les écartées, au cas
// où je merde" - "il me faut deux onglets pas un") - filet de sécurité,
// deux listes séparées (pas un filtre par provenance comme SOURCE_TABS) :
// vérifier ses rejets et vérifier ses validations sont deux questions
// différentes. Triées par date de revue la plus récente côté API.
// Rouvrir une capture et recliquer Valider/Écarter change son statut
// normalement - aucun bouton "annuler" séparé nécessaire.
const REVIEWED_TABS = [
  { key: '__validated__', label: 'Validées', statusGroup: 'validated', statKey: 'validated_total' },
  { key: '__rejected__', label: 'Écartées', statusGroup: 'rejected', statKey: 'rejected_total', catFilterable: true },
  // 18.10, Sid ("fais un archives oui") - une fois exportée (export pour
  // entraînement), une capture sort de Validées/Écartées (la session est
  // close) mais reste accessible ici, réouvrable comme les autres, plutôt
  // que de devenir invisible partout.
  { key: '__archived__', label: 'Archivées', statusGroup: 'archived', statKey: 'archived_total', catFilterable: true },
];

// 25.10, Sid ("Archivées et écartées : mettre aussi le filtre par chat")
// - cat_name se combine déjà librement avec status_group côté backend
// (/app-api/queue), rien à changer là-bas. Réinitialisé à chaque
// changement d'onglet, même logique que selectedIds.clear() juste en
// dessous dans selectTab() - un filtre d'un autre onglet n'a plus de sens
// ici.
let reviewedCatFilter = null;

// 20.10, Sid ("le module de revérification... je contrôle tout au même
// endroit") - file à part (model_review_queue), pas un filtre sur
// captures comme les autres onglets - voir loadModelReviewQueue().
const MODEL_REVIEW_TAB = '__model_review__';

// 20.10, Sid ("un bouton qui sort aussi la comparaison... un clic lance
// les 10 derniers événements, puis ceux d'avant") - pas une file de
// revue (rien à valider/écarter ici), juste une action qui génère une
// galerie HTML (vérité terrain/ancien modèle/nouveau modèle) et un lien
// vers elle - voir startCompareRecent()/compareRecentMore().
const COMPARE_TAB = '__compare__';

// 21.10, Sid ("au lieu que je te demande tout le temps... un compteur du
// nombre de photos par chaque dans le modèle... la différence entre
// provient de KH vs provient de reolink et manuel") - lecture seule,
// aucune file, juste le tableau renvoyé par
// GET /app-api/stats/by-class-source.
const STATS_TAB = '__stats__';

// 22.10, Sid ("la page 200+200 c'est pas ce que je veux... il me faut un
// onglet spécifique") - remplace le chantier 200+200 par un flux plus
// simple : des captures explicitement remises en jeu (rebalance_bucket
// IS NOT NULL côté serveur, jamais mélangées à la vraie file "pending")
// qu'elle garde ('kept') ou écarte (bulkReview('rejected') existant,
// inchangé) au fil de l'eau, avec un bouton final qui envoie tout ce
// qui est 'kept' vers le vrai statut validé (voir rebalancePromote).
const REBALANCE_TAB = '__rebalance__';
let rebalanceFilters = { cat: null, source: null, prey: false, negative: false };
let rebalanceCounts = null;

async function api(path, opts) {
  const res = await fetch(path, opts);
  if (!res.ok) throw new Error(`${path}: ${res.status}`);
  const ct = res.headers.get('content-type') || '';
  return ct.includes('application/json') ? res.json() : res;
}

async function init() {
  const [catsResp, statsResp] = await Promise.all([
    api('/app-api/cats'),
    api('/app-api/stats'),
  ]);
  cats = catsResp;
  lastStats = statsResp;
  renderTabs();
  renderStats(statsResp);
  await loadQueue();
  await checkExportStatus();
  await checkTrainStatus();
  await refreshMrCount();
}

// 19.10, Sid ("crée le bouton et on teste ensuite en live") - même pattern
// que l'export : état lu au chargement, reposté toutes les 10s (un run
// prend 10-40 min, pas la peine de repoller aussi vite que l'export).
let trainPollTimer = null;

async function checkTrainStatus() {
  const state = await api('/app-api/train/status');
  renderTrainBar(state);
  if (state.status === 'running') {
    if (!trainPollTimer) trainPollTimer = setInterval(checkTrainStatus, 10000);
  } else if (trainPollTimer) {
    clearInterval(trainPollTimer);
    trainPollTimer = null;
  }
}

async function startTraining() {
  try {
    await api('/app-api/train/start', { method: 'POST' });
  } catch (e) {
    // 409 = déjà en cours - pas une vraie erreur, juste resynchroniser l'affichage
  }
  await checkTrainStatus();
}

const TRAIN_STEP_LABELS = {
  export: 'Préparation des données',
  searching_offer: 'Recherche d\'une machine GPU',
  health_check: 'Vérification de la machine',
  training: 'Entraînement en cours',
};

function renderTrainBar(state) {
  const bar = document.getElementById('train-bar');
  if (state.status === 'running') {
    const label = TRAIN_STEP_LABELS[state.step] || 'En cours';
    const price = state.price ? ` - ${state.price.toFixed(3)}$/h` : '';
    bar.innerHTML = `<span class="export-msg">🧠 ${label}${price}...</span>`;
  } else if (state.status === 'success') {
    const when = state.finished_at ? new Date(state.finished_at * 1000).toLocaleString('fr-CH') : '';
    bar.innerHTML = `
      <span class="export-msg">✓ Nouveau modèle entraîné (${state.images} images, ${when}) - sélectionne-le dans Kittyhack</span>
      <button onclick="startTraining()">Relancer un entraînement</button>`;
  } else if (state.status === 'error') {
    bar.innerHTML = `
      <span class="export-msg export-error">Échec de l'entraînement : ${state.message || 'erreur inconnue'}</span>
      <button onclick="startTraining()">Réessayer</button>`;
  } else {
    bar.innerHTML = `<button onclick="startTraining()">Lancer un entraînement</button>`;
  }
}

// 18.10, Sid ("un bouton qui lance l'export des photos validées... une
// notif qui dit que le zip est prêt") - état lu au chargement (survit à
// un rechargement de page/retour plus tard) et reposté toutes les 3s tant
// que l'export tourne encore côté serveur.
let exportPollTimer = null;

async function checkExportStatus() {
  const state = await api('/app-api/export/status');
  renderExportBar(state);
  if (state.status === 'running') {
    if (!exportPollTimer) exportPollTimer = setInterval(checkExportStatus, 3000);
  } else if (exportPollTimer) {
    clearInterval(exportPollTimer);
    exportPollTimer = null;
  }
}

async function startExport(full = false) {
  await api(`/app-api/export/start?full=${full ? '1' : '0'}`, { method: 'POST' });
  await checkExportStatus();
}

function renderExportBar(state) {
  const bar = document.getElementById('export-bar');
  if (state.status === 'running') {
    const progress = state.total ? ` (${state.done ?? 0}/${state.total})` : '';
    bar.innerHTML = `<span class="export-msg">Export en cours${progress}...</span>`;
  } else if (state.status === 'ready') {
    if (state.images === 0) {
      bar.innerHTML = `
        <span class="export-msg">${state.message || 'Rien de nouveau à exporter.'}</span>
        <button onclick="startExport()">Exporter pour entraînement</button>`;
    } else {
      bar.innerHTML = `
        <span class="export-msg">✓ Zip prêt : ${state.images} images (${state.positives} avec boîte, ${state.negatives} négatifs)</span>
        <a href="/app-api/export/download" class="btn-download">Télécharger</a>
        <button onclick="startExport()">Nouvel export</button>`;
    }
  } else if (state.status === 'error') {
    bar.innerHTML = `
      <span class="export-msg export-error">Échec de l'export : ${state.message || 'erreur inconnue'}</span>
      <button onclick="startExport()">Réessayer</button>`;
  } else {
    bar.innerHTML = `<button onclick="startExport()">Exporter pour entraînement</button>`;
  }
}

function renderStats(stats) {
  document.getElementById('stats').innerHTML = `
    <span><b>${stats.pending}</b> à réviser</span>
    <span><b>${stats.prey_candidates}</b> proies suspectées</span>
    <span><b>${stats.model_disagreements}</b> désaccords modèle</span>
    <span><b>${stats.deduped}</b> dédoublonnées</span>
  `;
}

function renderTabs() {
  const tabsEl = document.getElementById('tabs');
  const allTab = `<div class="tab ${currentTab === null ? 'active' : ''}" style="${currentTab === null ? 'background:var(--accent)' : ''}" onclick="selectTab(null)">Toutes</div>`;
  const catTabs = cats.map(c => `
    <div class="tab ${currentTab === c.name ? 'active' : ''}"
         style="${currentTab === c.name ? `background:${catColor(c.name)}` : `border-color:${catColor(c.name)}44`}"
         onclick="selectTab('${c.name}')">
      ${c.name}<span class="count">${c.pending}</span>
    </div>`).join('');
  const sourceTabs = SOURCE_TABS.map(t => `
    <div class="tab ${currentTab === t.key ? 'active' : ''}"
         style="${currentTab === t.key ? 'background:var(--accent)' : 'border-color:var(--accent)44'}"
         onclick="selectTab('${t.key}')">
      ${t.label}<span class="count">${lastStats[t.statKey] ?? 0}</span>
    </div>`).join('');
  const preyTab = `
    <div class="tab ${currentTab === PREY_TAB ? 'active' : ''}"
         style="${currentTab === PREY_TAB ? 'background:var(--danger)' : 'border-color:var(--danger)'}"
         onclick="selectTab('${PREY_TAB}')">
      Proies suspectées<span class="count">${lastStats.prey_pending ?? 0}</span>
    </div>`;
  // 18.10, Sid : "Validées"/"Écartées" - couleur neutre (ni accent ni
  // couleur de chat) pour bien les distinguer visuellement des onglets
  // "à faire" (ce ne sont pas des files d'attente).
  const reviewedTabs = REVIEWED_TABS.map(t => `
    <div class="tab ${currentTab === t.key ? 'active' : ''}"
         style="${currentTab === t.key ? 'background:var(--text-dim);color:var(--bg)' : 'border-color:var(--text-dim)'}"
         onclick="selectTab('${t.key}')">
      ${t.label}<span class="count">${lastStats[t.statKey] ?? 0}</span>
    </div>`).join('');
  const modelReviewTab = `
    <div class="tab ${currentTab === MODEL_REVIEW_TAB ? 'active' : ''}"
         style="${currentTab === MODEL_REVIEW_TAB ? 'background:var(--accent)' : 'border-color:var(--accent)'}"
         onclick="selectTab('${MODEL_REVIEW_TAB}')">
      Revérification<span class="count">${mrCount}</span>
    </div>`;
  const compareTab = `
    <div class="tab ${currentTab === COMPARE_TAB ? 'active' : ''}"
         style="${currentTab === COMPARE_TAB ? 'background:var(--accent)' : 'border-color:var(--accent)'}"
         onclick="selectTab('${COMPARE_TAB}')">
      Comparaison</div>`;
  const statsTab = `
    <div class="tab ${currentTab === STATS_TAB ? 'active' : ''}"
         style="${currentTab === STATS_TAB ? 'background:var(--accent)' : 'border-color:var(--accent)'}"
         onclick="selectTab('${STATS_TAB}')">
      Stats</div>`;
  const rebalanceTab = `
    <div class="tab ${currentTab === REBALANCE_TAB ? 'active' : ''}"
         style="${currentTab === REBALANCE_TAB ? 'background:var(--accent)' : 'border-color:var(--accent)'}"
         onclick="selectTab('${REBALANCE_TAB}')">
      Rééquilibrage</div>`;
  tabsEl.innerHTML = allTab + catTabs + sourceTabs + preyTab + reviewedTabs + modelReviewTab + compareTab + statsTab + rebalanceTab;
}

function selectTab(name) {
  currentTab = name;
  selectedIds.clear(); // une sélection d'un autre onglet n'a plus de sens ici
  reviewedCatFilter = null;
  renderTabs();
  document.getElementById('model-review-bar').hidden = currentTab !== MODEL_REVIEW_TAB;
  document.getElementById('compare-bar').hidden = currentTab !== COMPARE_TAB;
  document.getElementById('stats-bar').hidden = currentTab !== STATS_TAB;
  document.getElementById('rebalance-bar').hidden = currentTab !== REBALANCE_TAB;
  const reviewedTabForFilter = REVIEWED_TABS.find(t => t.key === currentTab && t.catFilterable);
  document.getElementById('reviewed-filter-bar').hidden = !reviewedTabForFilter;
  if (reviewedTabForFilter) renderReviewedFilterBar();
  if (currentTab === MODEL_REVIEW_TAB) {
    checkModelReviewStatus();
    loadModelReviewQueue();
  } else if (currentTab === COMPARE_TAB) {
    // 21.10 : ces deux onglets n'ont pas de file - sans ça, #queue garde
    // les vignettes du dernier onglet visité en dessous de la barre.
    document.getElementById('queue').innerHTML = '';
    document.getElementById('empty').hidden = true;
    checkCompareStatus();
  } else if (currentTab === STATS_TAB) {
    document.getElementById('queue').innerHTML = '';
    document.getElementById('empty').hidden = true;
    loadStatsTab();
  } else if (currentTab === REBALANCE_TAB) {
    loadRebalanceCounts();
    loadRebalanceQueue();
  } else {
    loadQueue();
  }
}

async function loadQueue() {
  // 25.10, Sid ("je sois pas limitée à 200") - même raisonnement que
  // loadRebalanceQueue (21.10) : une limite basse
  // tronquait silencieusement dès qu'un onglet dépassait ce chiffre,
  // rendant impossible de voir/trier le lot complet. Relevée largement
  // au-dessus de n'importe quelle combinaison réaliste plutôt que
  // supprimée (filet de sécurité).
  const params = new URLSearchParams({ limit: 10000 });
  const sourceTab = SOURCE_TABS.find(t => t.key === currentTab);
  const reviewedTab = REVIEWED_TABS.find(t => t.key === currentTab);
  if (reviewedTab) {
    params.set('status_group', reviewedTab.statusGroup);
    if (reviewedTab.catFilterable && reviewedCatFilter) params.set('cat_name', reviewedCatFilter);
  } else if (currentTab === PREY_TAB) {
    params.set('prey_only', '1');
  } else if (sourceTab) {
    params.set('source', sourceTab.source);
  } else if (currentTab) {
    params.set('cat_name', currentTab);
  }
  currentQueue = await api(`/app-api/queue?${params}`);
  renderQueue();
}

function renderReviewedFilterBar() {
  const bar = document.getElementById('reviewed-filter-bar');
  // 25.10 : même ajout que renderRebalanceBar() - "Prey" filtré via
  // filter_prey_label côté /app-api/queue, absent de `cats` (identités
  // RFID seulement).
  const catOptions = ['', ...cats.map(c => c.name), 'Prey'].map(n =>
    `<option value="${n}" ${reviewedCatFilter === (n || null) ? 'selected' : ''}>${n || 'Tous les chats'}</option>`
  ).join('');
  bar.innerHTML = `<select onchange="reviewedSetCatFilter(this.value || null)">${catOptions}</select>`;
}

function reviewedSetCatFilter(value) {
  reviewedCatFilter = value;
  loadQueue();
}

// 18.10, Sid ("un moyen d'en sélectionner plusieurs d'un coup pour les
// écarter en une fois ?") - la grille Reolink en particulier enchaîne
// beaucoup de vignettes vides côte à côte (captures périodiques sans
// rien dessus). Set d'ids plutôt qu'un flag par tuile - survit à un
// renderQueue() (changement d'onglet mis à part, voir selectTab/
// loadQueue qui le vide explicitement).
let selectedIds = new Set();

function renderQueue() {
  if (currentTab === MODEL_REVIEW_TAB) { renderMrQueue(); return; }
  const grid = document.getElementById('queue');
  const empty = document.getElementById('empty');
  if (currentQueue.length === 0) {
    grid.innerHTML = '';
    empty.hidden = false;
    renderBulkBar();
    return;
  }
  empty.hidden = true;
  grid.innerHTML = currentQueue.map((c, i) => `
    <div class="queue-tile ${c.is_prey_candidate ? 'prey' : ''} ${c.model_disagreement ? 'disagreement' : ''} ${selectedIds.has(c.id) ? 'selected' : ''}"
         onclick="openDetail(${i})">
      <img src="/app-api/captures/${c.id}/image?size=thumbnail" loading="lazy">
      <input type="checkbox" class="tile-check" ${selectedIds.has(c.id) ? 'checked' : ''}
             onclick="event.stopPropagation(); toggleSelect(${c.id})">
      ${c.is_prey_candidate ? '<div class="flag">proie ?</div>' : ''}
      ${c.model_disagreement && !c.is_prey_candidate ? '<div class="flag">modèle ?</div>' : ''}
    </div>
  `).join('');
  renderBulkBar();
}

function toggleSelect(id) {
  if (selectedIds.has(id)) selectedIds.delete(id); else selectedIds.add(id);
  renderQueue();
}

function selectAllVisible() {
  const visible = currentTab === MODEL_REVIEW_TAB ? mrQueue : currentQueue;
  visible.forEach(c => selectedIds.add(c.id));
  renderQueue();
}

function clearSelection() {
  selectedIds.clear();
  renderQueue();
}

function renderBulkBar() {
  const bar = document.getElementById('bulk-bar');
  const count = selectedIds.size;
  bar.hidden = count === 0;
  if (count === 0) return;
  const label = `${count} sélectionnée${count > 1 ? 's' : ''}`;
  if (currentTab === MODEL_REVIEW_TAB) {
    bar.innerHTML = `
      <span>${label}</span>
      <button onclick="selectAllVisible()">Tout sélectionner</button>
      <button onclick="clearSelection()">Annuler</button>
      <button class="btn-reject" onclick="bulkResolveMr('dismiss')">Garder l'ancien (sélection)</button>
      <button class="btn-confirm" onclick="bulkResolveMr('apply')">Appliquer le nouveau (sélection)</button>`;
  } else if (currentTab === REBALANCE_TAB) {
    bar.innerHTML = `
      <span>${label}</span>
      <button onclick="selectAllVisible()">Tout sélectionner</button>
      <button onclick="clearSelection()">Annuler</button>
      <button class="btn-reject" onclick="bulkReview('rejected')">Écarter la sélection</button>
      <button class="btn-confirm" onclick="rebalanceKeepSelection()">Garder la sélection</button>`;
  } else {
    bar.innerHTML = `
      <span>${label}</span>
      <button onclick="selectAllVisible()">Tout sélectionner</button>
      <button onclick="clearSelection()">Annuler</button>
      <button class="btn-reject" onclick="bulkReview('rejected')">Écarter la sélection</button>`;
  }
}

async function bulkReview(status) {
  const ids = [...selectedIds];
  if (ids.length === 0) return;
  await api('/app-api/captures/bulk-review', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ids, status }),
  });
  selectedIds.clear();
  await reloadCurrentTab();
  const statsResp = await api('/app-api/stats');
  lastStats = statsResp;
  renderStats(statsResp);
  const catsResp = await api('/app-api/cats');
  cats = catsResp;
  renderTabs();
}

// 20.10, Sid ("le module de revérification... je contrôle tout au même
// endroit") - file séparée (model_review_queue : un groupe = une tuile,
// potentiellement plusieurs frames résolues d'un coup, voir
// model_review.py), mais même mécanique de sélection multiple
// (selectedIds) que la file normale.
let mrQueue = [];
let mrCount = 0;
let mrPollTimer = null;
let mrModels = [];
let mrIndex = 0;

async function refreshMrCount() {
  mrQueue = await api('/app-api/model-review/queue');
  mrCount = mrQueue.length;
}

async function checkModelReviewStatus() {
  const state = await api('/app-api/model-review/status');
  renderModelReviewBar(state);
  if (state.status === 'running') {
    if (!mrPollTimer) mrPollTimer = setInterval(checkModelReviewStatus, 3000);
  } else if (mrPollTimer) {
    clearInterval(mrPollTimer);
    mrPollTimer = null;
    await loadModelReviewQueue();
    renderTabs();
  }
}

async function startModelReview() {
  mrModels = await api('/app-api/model-review/models');
  if (mrModels.length === 0) return;
  try {
    // Le plus récent (list_model_dirs() trie déjà du plus récent au plus
    // ancien) - celui qu'on vient d'entraîner, le cas d'usage courant.
    await api('/app-api/model-review/start', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ model_version: mrModels[0], num_captures: 300 }),
    });
  } catch (e) { /* 409 = déjà en cours - pas une vraie erreur */ }
  await checkModelReviewStatus();
}

const MR_STEP_LABELS = { loading_model: 'Chargement du modèle', scanning: 'Analyse des captures' };

function renderModelReviewBar(state) {
  const bar = document.getElementById('model-review-bar');
  if (state.status === 'running') {
    const label = MR_STEP_LABELS[state.step] || 'En cours';
    const progress = state.total ? ` (${state.scanned ?? 0}/${state.total})` : '';
    bar.innerHTML = `<span class="export-msg">Revérification : ${label}${progress}...</span>`;
  } else if (state.status === 'success') {
    const when = state.finished_at ? new Date(state.finished_at * 1000).toLocaleString('fr-CH') : '';
    bar.innerHTML = `
      <span class="export-msg">✓ ${state.flagged ?? 0} désaccord(s) à trancher (${state.scanned ?? 0} captures scannées, ${when})</span>
      <button onclick="startModelReview()">Relancer le scan</button>
      <button onclick="resetModelReview()" title="Vide la file de désaccords en attente, pour repartir d'une file propre après un nouvel entraînement">Réinitialiser</button>`;
  } else if (state.status === 'error') {
    bar.innerHTML = `
      <span class="export-msg export-error">Échec : ${state.message || 'erreur inconnue'}</span>
      <button onclick="startModelReview()">Réessayer</button>`;
  } else {
    bar.innerHTML = `
      <button onclick="startModelReview()">Lancer le scan (dernier modèle)</button>
      <button onclick="resetModelReview()" title="Vide la file de désaccords en attente, pour repartir d'une file propre après un nouvel entraînement">Réinitialiser</button>`;
  }
}

// 25.10, Sid ("Faire un Reset de rectification. Je ferais une fois le
// nouvel entraînement effectué") - vide model_review_queue côté serveur,
// jamais lancé sans confirmation (irréversible : les désaccords non
// tranchés sont perdus, pas juste masqués).
async function resetModelReview() {
  if (!confirm("Réinitialiser la revérification : vide la file de désaccords en attente (non tranchés = perdus). Continuer ?")) return;
  await api('/app-api/model-review/reset', { method: 'POST' });
  await checkModelReviewStatus();
  await loadModelReviewQueue();
  renderTabs();
}

async function loadModelReviewQueue() {
  mrQueue = await api('/app-api/model-review/queue');
  mrCount = mrQueue.length;
  renderMrQueue();
}

function renderMrQueue() {
  const grid = document.getElementById('queue');
  const empty = document.getElementById('empty');
  if (mrQueue.length === 0) {
    grid.innerHTML = '';
    empty.hidden = false;
    renderBulkBar();
    return;
  }
  empty.hidden = true;
  grid.innerHTML = mrQueue.map((q, i) => `
    <div class="queue-tile ${q.new_label === 'Prey' || q.old_label === 'Prey' ? 'prey' : 'disagreement'} ${selectedIds.has(q.id) ? 'selected' : ''}"
         onclick="openMrDetail(${i})">
      <img src="/app-api/captures/${q.capture_id}/image?size=thumbnail" loading="lazy">
      <input type="checkbox" class="tile-check" ${selectedIds.has(q.id) ? 'checked' : ''}
             onclick="event.stopPropagation(); toggleSelect(${q.id})">
      <div class="flag">${q.old_label || '?'} → ${q.new_label}</div>
    </div>
  `).join('');
  renderBulkBar();
}

async function bulkResolveMr(action) {
  const ids = [...selectedIds];
  if (ids.length === 0) return;
  await api('/app-api/model-review/resolve', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ids, action }),
  });
  selectedIds.clear();
  await loadModelReviewQueue();
  renderTabs();
}

async function openMrDetail(index) {
  mrIndex = index;
  await renderMrDetailView();
  document.getElementById('mr-detail').classList.add('open');
}

function closeMrDetail() {
  document.getElementById('mr-detail').classList.remove('open');
}

async function navigateMrDetail(delta) {
  const newIndex = mrIndex + delta;
  if (newIndex < 0 || newIndex >= mrQueue.length) return;
  mrIndex = newIndex;
  await renderMrDetailView();
}

function toggleMrZoom() {
  document.getElementById('mr-image-wrap').classList.toggle('zoomed');
}

async function renderMrDetailView() {
  document.getElementById('mr-image-wrap').classList.remove('zoomed');
  const q = mrQueue[mrIndex];
  document.getElementById('mr-detail-meta').textContent =
    `capture #${q.capture_id} · chat : ${q.cat_name || '?'} (${q.cat_source || '?'})`;
  const img = document.getElementById('mr-detail-img');
  img.src = `/app-api/captures/${q.capture_id}/image?size=original`;
  const wrap = document.getElementById('mr-image-wrap');
  wrap.querySelectorAll('.box').forEach(b => b.remove());
  // Boîte ancienne : pas de géométrie stockée sur la ligne de groupe elle-
  // même (chaque frame a la sienne, voir model_review.py) - on va la
  // chercher sur la capture représentante, celle affichée ici.
  const full = await api(`/app-api/captures/${q.capture_id}`);
  const oldBox = full.boxes.find(b => (b.label || '').toLowerCase() === (q.old_label || '').toLowerCase());
  if (oldBox) {
    const el = document.createElement('div');
    el.className = 'box mr-old';
    el.style.left = oldBox.x + '%'; el.style.top = oldBox.y + '%';
    el.style.width = oldBox.width + '%'; el.style.height = oldBox.height + '%';
    el.innerHTML = `<div class="box-label">ancien : ${q.old_label}</div>`;
    wrap.appendChild(el);
  }
  const newEl = document.createElement('div');
  newEl.className = 'box mr-new';
  newEl.style.left = q.new_x + '%'; newEl.style.top = q.new_y + '%';
  newEl.style.width = q.new_width + '%'; newEl.style.height = q.new_height + '%';
  newEl.innerHTML = `<div class="box-label">nouveau : ${q.new_label} ${q.new_probability.toFixed(0)}%</div>`;
  wrap.appendChild(newEl);

  document.getElementById('mr-old-chip').textContent = `ancien : ${q.old_label || '(rien)'}`;
  document.getElementById('mr-new-chip').textContent = `nouveau : ${q.new_label} (${q.new_probability.toFixed(0)}%)`;
}

async function resolveMrCurrent(action) {
  const q = mrQueue[mrIndex];
  await api('/app-api/model-review/resolve', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ids: [q.id], action }),
  });
  mrQueue.splice(mrIndex, 1);
  mrCount = mrQueue.length;
  renderTabs();
  if (mrQueue.length === 0) {
    closeMrDetail();
    renderMrQueue();
    return;
  }
  if (mrIndex >= mrQueue.length) mrIndex = mrQueue.length - 1;
  await renderMrDetailView();
}

// 20.10, Sid ("un bouton qui sort aussi la comparaison... un clic lance
// les 10 derniers événements, puis ceux d'avant", puis "par coup de 20 ?
// un filtre pour chacun ? empêcher de recharger un truc déjà chargé ?",
// puis "50 en asynchrone, ça me semble pas mal") - pas une file de revue,
// une seule action qui génère une galerie HTML (voir model_compare.py
// run_recent_events) et un lien vers elle. Plus de notion d'offset côté
// client : le backend exclut lui-même, de façon PERSISTANTE
// (model_compare_scanned), tout événement déjà rendu pour ce couple de
// modèles - "suivants" redemande juste le prochain lot de 50 jamais vus,
// même après un rechargement de page ou un autre jour. Asynchrone (même
// mécanique state+polling que la revérification) : ~0,45s/image mesuré en
// vrai, 50 images = ~25s, trop long pour un simple POST sans progression.
const COMPARE_BATCH = 50;
let compareResult = null;
let comparePollTimer = null;
// Filtre par classe (chat ou proie), sur la vérité terrain originale, pas
// les prédictions. Changer de filtre recommence une galerie neuve : mélanger
// des événements filtrés et non filtrés n'aurait pas de sens dans une même
// galerie accumulée.
let compareClassFilter = null;

function renderCompareBar(state) {
  const bar = document.getElementById('compare-bar');
  const options = ['', 'Prey', ...Object.keys(CAT_COLORS)]
    .map(c => `<option value="${c}" ${compareClassFilter === (c || null) ? 'selected' : ''}>${c === '' ? 'Tous' : (c === 'Prey' ? 'Proies' : c)}</option>`)
    .join('');
  const filterSelect = `
    <select onchange="setCompareClassFilter(this.value)" style="margin-right:8px">${options}</select>`;
  if (state && state.status === 'running') {
    const label = state.step === 'loading_model' ? 'Chargement des modèles' : 'Comparaison';
    const progress = state.total ? ` (${state.scanned ?? 0}/${state.total})` : '';
    bar.innerHTML = filterSelect + `<span class="export-msg">${label}${progress}...</span>`;
    return;
  }
  if (state && state.status === 'error') {
    bar.innerHTML = filterSelect + `
      <span class="export-msg export-error">Échec : ${state.message || 'erreur inconnue'}</span>
      <button onclick="startCompareRecent()">Réessayer</button>`;
    return;
  }
  if (!compareResult) {
    bar.innerHTML = filterSelect + `<button onclick="startCompareRecent()">Comparer les ${COMPARE_BATCH} derniers événements</button>`;
    return;
  }
  const r = compareResult;
  const hasMore = r.total_events > 0;
  bar.innerHTML = filterSelect + `
    <span class="export-msg">✓ ${r.total_shown} événement(s) accumulé(s) dans la galerie${r.class_filter ? ` (filtre : ${r.class_filter})` : ''}${hasMore ? `, ${r.total_events} restant(s)` : ', plus aucun restant'}</span>
    <a href="${r.url}" target="_blank" onclick="markCompareGalleryOpened()"><button>Ouvrir la galerie</button></a>
    ${hasMore ? `<button onclick="compareRecentMore()">${COMPARE_BATCH} événements précédents</button>` : ''}
    <button onclick="startCompareRecent()">Recommencer (nouvelle galerie)</button>`;
}

// 20.10, Sid ("je clique 4x sur 50... je reclique 4x sur 50 pour avoir les
// 200 d'après dans une deuxième galerie mais ça me génère une galerie de
// 400") - rien ne marquait la session comme "consommée" une fois la
// galerie effectivement ouverte, donc chaque clic suivant continuait
// d'accumuler indéfiniment. Ouvrir le lien marque la session terminée :
// le prochain clic sur le bouton de lot repart sur une galerie neuve
// (session_dir redevient null), sans perdre le fait que les événements
// déjà montrés restent exclus pour de bon (model_compare_scanned, pas lié
// à la galerie elle-même).
function markCompareGalleryOpened() {
  compareResult = null;
  renderCompareBar();
}

function setCompareClassFilter(value) {
  compareClassFilter = value || null;
  compareResult = null; // nouveau filtre = nouvelle galerie, pas de mélange
  renderCompareBar();
}

async function checkCompareStatus() {
  const state = await api('/app-api/model-compare/status');
  renderCompareBar(state);
  if (state.status === 'running') {
    if (!comparePollTimer) comparePollTimer = setInterval(checkCompareStatus, 2000);
  } else {
    if (comparePollTimer) { clearInterval(comparePollTimer); comparePollTimer = null; }
    if (state.status === 'success') compareResult = state;
  }
}

async function startCompareRecent() {
  try {
    await api('/app-api/model-compare/recent', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ limit: COMPARE_BATCH, class_filter: compareClassFilter }),
    });
  } catch (e) { /* 409 = déjà en cours - pas une vraie erreur */ }
  await checkCompareStatus();
}

async function compareRecentMore() {
  const sessionDir = compareResult ? compareResult.out_dir.split('/').pop() : null;
  try {
    await api('/app-api/model-compare/recent', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ limit: COMPARE_BATCH, class_filter: compareClassFilter, session_dir: sessionDir }),
    });
  } catch (e) { /* 409 = déjà en cours - pas une vraie erreur */ }
  await checkCompareStatus();
}

// 21.10, Sid ("un compteur du nombre de photos par chaque dans le
// modèle... la différence entre provient de KH vs provient de reolink et
// manuel... combien en rajouter pour chacun pour garder un équilibre chat
// par chat et KH vs reolink") - lecture seule, un seul appel au chargement
// de l'onglet (pas de polling, les comptes ne bougent pas pendant qu'on
// regarde).
async function loadStatsTab() {
  const bar = document.getElementById('stats-bar');
  bar.innerHTML = `<span class="export-msg">Chargement...</span>`;
  let data;
  try {
    data = await api('/app-api/stats/by-class-source');
  } catch (e) {
    bar.innerHTML = `<span class="export-msg export-error">Échec du chargement des stats.</span>`;
    return;
  }
  const classes = Object.keys(data);
  // 21.10, Sid ("autre ancien = principalement du KH. Considère 70% KH et
  // 30% reolink. Splitter autre sur cette base et rajoute la proportion à
  // chaque colonne") - "Autre (ancien)" (import LabelStudio, angle caméra
  // jamais enregistré) n'est plus montré à part : réparti 70/30 et ajouté
  // directement dans KH et Reolink+Manuel, qui deviennent des estimations
  // plutôt que des comptes bruts exacts - toutes les comparaisons
  // (3 dimensions, voir plus haut) tournent maintenant sur ces totaux
  // estimés, cohérent partout plutôt que de mélanger brut et estimé.
  const OTHER_KH_SHARE = 0.7;
  const withEstimate = classes.map(c => {
    const { kittyflap, reolink_manual, other } = data[c];
    return {
      cls: c,
      khEst: kittyflap + Math.round(other * OTHER_KH_SHARE),
      reolinkEst: reolink_manual + Math.round(other * (1 - OTHER_KH_SHARE)),
    };
  });
  const maxKittyflap = Math.max(...withEstimate.map(c => c.khEst));
  const maxReolinkManual = Math.max(...withEstimate.map(c => c.reolinkEst));
  const withGap = withEstimate.map(c => ({
    ...c,
    gapVsBestKh: maxKittyflap - c.khEst,
    gapVsBestReolink: maxReolinkManual - c.reolinkEst,
    gapKhReolink: c.khEst - c.reolinkEst,
  })).sort((a, b) => b.gapKhReolink - a.gapKhReolink);
  const rows = withGap.map(({ cls, khEst, reolinkEst, gapVsBestKh, gapVsBestReolink, gapKhReolink }) => `<tr>
      <td>${cls}</td>
      <td>${khEst}</td>
      <td>${reolinkEst}</td>
      <td>${khEst + reolinkEst}</td>
      <td class="${gapVsBestKh > 0 ? 'export-error' : ''}">${gapVsBestKh > 0 ? `+${gapVsBestKh}` : '-'}</td>
      <td class="${gapVsBestReolink > 0 ? 'export-error' : ''}">${gapVsBestReolink > 0 ? `+${gapVsBestReolink}` : '-'}</td>
      <td class="${gapKhReolink > 0 ? 'export-error' : ''}">${gapKhReolink > 0 ? `+${gapKhReolink}` : 'équilibré/en avance'}</td>
    </tr>`).join('');
  bar.innerHTML = `
    <table class="stats-table">
      <thead><tr>
        <th>Classe</th><th>KH (estimé)</th><th>Reolink+Manuel (estimé)</th><th>Total</th>
        <th>vs meilleur KH</th><th>vs meilleur Reolink</th><th>Écart KH−Reolink</th>
      </tr></thead>
      <tbody>${rows}</tbody>
    </table>
    <div class="hint" style="margin-top:8px">"Autre (ancien)" (import LabelStudio, angle caméra jamais enregistré) réparti 70% KH / 30% Reolink et ajouté dans les deux colonnes ci-dessus - ce sont des estimations, pas des comptes exacts.</div>`;
}

// 21.10, Sid ("j'ai sélectionné pookie + manuel, j'en ai écarté 7 et ça
// a tout vidé ? Quand je les valide à nouveau elles vont où ? J'arrive
// pas à voir le suivi") - bulkReview()/submitReview() rafraîchissaient
// après action via loadQueue() (générique), qui ne connaît pas l'onglet
// courant et traitait son nom comme un nom de chat, donc zéro résultat -
// la liste semblait "tout vidée" après une action. Les compteurs de
// progression n'étaient non plus jamais réactualisés après une action.
async function reloadCurrentTab() {
  if (currentTab === REBALANCE_TAB) {
    await loadRebalanceQueue();
    await loadRebalanceCounts();
  } else {
    await loadQueue();
  }
}

const REBALANCE_TARGET_PER_SOURCE = 200;

async function loadRebalanceCounts() {
  rebalanceCounts = await api('/app-api/rebalance/counts');
  renderRebalanceBar();
}

function rebalanceSetFilter(key, value) {
  rebalanceFilters[key] = value;
  loadRebalanceQueue();
  renderRebalanceBar();
}

async function rebalanceKeepSelection() {
  const ids = [...selectedIds];
  if (ids.length === 0) return;
  await api('/app-api/rebalance/keep', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ids }),
  });
  selectedIds.clear();
  await reloadCurrentTab();
}

async function rebalancePromote() {
  if (!confirm("Envoyer tout le lot 'gardé' vers le vrai statut validé (export-éligible) ? Elles sortiront de cet onglet et redeviendront des captures normales.")) return;
  const result = await api('/app-api/rebalance/promote', { method: 'POST' });
  alert(`${result.count} capture(s) envoyée(s) vers le statut validé.`);
  await reloadCurrentTab();
}

function renderRebalanceBar() {
  const bar = document.getElementById('rebalance-bar');
  // 25.10, Sid ("j'ai pas proie dans le menu déroulant") - "Prey" n'est
  // jamais un chat enregistré (cats vient de /app-api/cats, les
  // identités RFID), donc absent de cette liste par défaut. Ajouté à la
  // main : filtre sur l'étiquette effective "Prey" (voir filter_prey_label
  // côté /app-api/queue), distinct de la case "Proies suspectées"
  // ci-dessous (is_prey_candidate, un indice de priorité de revue, pas
  // une classification confirmée).
  const catOptions = ['', ...cats.map(c => c.name), 'Prey'].map(n =>
    `<option value="${n}" ${rebalanceFilters.cat === (n || null) ? 'selected' : ''}>${n || 'Tous les chats'}</option>`
  ).join('');
  const controls = `
    <select onchange="rebalanceSetFilter('cat', this.value || null)">${catOptions}</select>
    <select onchange="rebalanceSetFilter('source', this.value || null)">
      <option value="" ${!rebalanceFilters.source ? 'selected' : ''}>Toute source</option>
      <option value="kittyflap" ${rebalanceFilters.source === 'kittyflap' ? 'selected' : ''}>HK</option>
      <option value="reolink_manual" ${rebalanceFilters.source === 'reolink_manual' ? 'selected' : ''}>Reolink+Manuel</option>
    </select>
    <label style="margin-left:8px"><input type="checkbox" ${rebalanceFilters.prey ? 'checked' : ''}
      onchange="rebalanceSetFilter('prey', this.checked)"> Proies suspectées seulement</label>
    <label style="margin-left:8px"><input type="checkbox" ${rebalanceFilters.negative ? 'checked' : ''}
      onchange="rebalanceSetFilter('negative', this.checked)"> Négatif seulement (ni chat ni proie)</label>
    <button style="margin-left:8px" class="btn-confirm" onclick="rebalancePromote()" title="Envoie tout ce qui a été gardé ('Garder la sélection') vers le vrai statut validé, export-éligible">
      Envoyer le lot gardé vers Validé
    </button>
  `;
  let table = '';
  if (rebalanceCounts) {
    const rows = Object.entries(rebalanceCounts).map(([cls, g]) => `<tr>
        <td>${cls}</td>
        <td class="${g.kittyflap >= REBALANCE_TARGET_PER_SOURCE ? '' : 'export-error'}">${g.kittyflap} / ${REBALANCE_TARGET_PER_SOURCE}</td>
        <td class="${g.reolink_manual >= REBALANCE_TARGET_PER_SOURCE ? '' : 'export-error'}">${g.reolink_manual} / ${REBALANCE_TARGET_PER_SOURCE}</td>
        <td>${g.kittyflap + g.reolink_manual}</td>
      </tr>`).join('');
    table = `
      <table class="stats-table" style="margin-top:8px">
        <thead><tr><th>Classe</th><th>HK gardées</th><th>Reolink+Manuel gardées</th><th>Total</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>`;
  }
  bar.innerHTML = controls + table;
}

async function loadRebalanceQueue() {
  // Même filet de sécurité que Tri 200+200 (voir 21.10 ci-dessus) - voir
  // tout le lot remis en jeu sur une seule page, pas tronqué.
  const params = new URLSearchParams({ limit: 10000, rebalance: '1' });
  if (rebalanceFilters.cat) params.set('cat_name', rebalanceFilters.cat);
  if (rebalanceFilters.source) params.set('source', rebalanceFilters.source);
  if (rebalanceFilters.prey) params.set('prey_only', '1');
  if (rebalanceFilters.negative) params.set('negative_only', '1');
  currentQueue = await api(`/app-api/queue?${params}`);
  renderQueue();
}

async function openDetail(index) {
  currentIndex = index;
  await renderDetail();
  document.getElementById('detail').classList.add('open');
}

function closeDetail() {
  document.getElementById('detail').classList.remove('open');
}

// 18.10, Sid ("écarter une série à la suite, et quand y'en a une à
// retraiter je fais suivant sans la traiter") - déplace juste currentIndex
// et réaffiche, sans rien soumettre au serveur (contrairement à
// submitReview). La photo zappée reste 'pending' à sa place dans la
// queue, pour y revenir plus tard dans ce même lot.
async function navigateDetail(delta) {
  const newIndex = currentIndex + delta;
  if (newIndex < 0 || newIndex >= currentQueue.length) return;
  currentIndex = newIndex;
  await renderDetail();
}

async function renderDetail() {
  const capture = currentQueue[currentIndex];
  const full = await api(`/app-api/captures/${capture.id}`);
  document.getElementById('detail-meta').textContent =
    `#${full.id} · ${full.created_at} · chat: ${full.cat_name || '?'} (${full.cat_source || '?'}) · proie: ${full.prey_probability ?? '?'}%`;

  const img = document.getElementById('detail-img');
  img.src = `/app-api/captures/${capture.id}/image?size=original`;

  const wrap = document.getElementById('image-wrap');
  wrap.querySelectorAll('.box').forEach(b => b.remove());

  // 18.10, Sid ("j'arrive pas à sélectionner la petite boîte" - une
  // grosse boîte "Patoune" couvrant tout le visage, une petite "Prey"
  // imbriquée dedans) : une boîte plus grande dessinée après recouvre
  // tout ce qu'elle contient, qui devient impossible à attraper au clic/
  // glissé. Dessiner les plus petites en dernier (donc au-dessus dans la
  // pile - uniquement pour le rendu visuel, la liste de dropdowns plus
  // bas garde l'ordre d'origine) : une boîte contenue dans une autre
  // gagne toujours le clic.
  const byAreaDesc = [...full.boxes].sort((a, b) => (b.width * b.height) - (a.width * a.height));
  byAreaDesc.forEach(box => renderBox(wrap, full.id, box));
  renderBoxList(full);
  wireDrawing(wrap, full.id, full.boxes.length);
}

// 18.10, Sid ("j'ai 2x Patoune, je sais pas laquelle je supprime") -
// identifiant visuel partagé entre une boîte sur l'image et sa ligne dans
// la liste en dessous (object_index, stable, jamais réutilisé pour deux
// boîtes différentes d'une même capture). Une couleur par index, pas liée
// au label ni au statut confirmé/proie (ces couleurs-là restent sur la
// bordure, inchangées) - juste de quoi faire correspondre les deux.
const INDEX_COLORS = ['#e8a33d', '#5fb87a', '#5a9fd4', '#c77dd4', '#e35d5d', '#4fc4c4'];
function indexColor(i) { return INDEX_COLORS[i % INDEX_COLORS.length]; }

function renderBox(wrap, captureId, box) {
  const el = document.createElement('div');
  el.className = 'box' + (box.confirmed ? '' : ' unconfirmed') +
    (/prey|proie|maus|mouse/i.test(box.label) ? ' prey' : '');
  el.style.left = box.x + '%';
  el.style.top = box.y + '%';
  el.style.width = box.width + '%';
  el.style.height = box.height + '%';
  const probTxt = box.probability != null ? ` ${box.probability.toFixed(0)}%` : '';
  el.innerHTML = `<div class="box-label">${box.label}${probTxt}</div><div class="box-handle"></div>` +
    `<div class="index-badge" style="background:${indexColor(box.object_index)}">${box.object_index}</div>`;
  wrap.appendChild(el);
  wireBoxResize(wrap, el, captureId, box);
}

// 18.10, Sid ("un moyen pour que je puisse reproportionner une boîte
// existante ?") : glisser le corps de la boîte la déplace, glisser la
// poignée (coin bas-droit) la redimensionne. Mêmes coordonnées % que le
// traçage d'une nouvelle boîte (wireDrawing) - wrap est le même conteneur
// position:relative. box.{x,y,width,height} viennent de l'état serveur
// juste chargé (full.boxes) - jamais mutés ici, seulement lus comme point
// de départ de chaque geste, pour que deux glissés d'affilée restent
// cohérents même si le premier n'a pas encore fini de se sauvegarder.
function wireBoxResize(wrap, el, captureId, box) {
  function toPercent(clientX, clientY) {
    const rect = wrap.getBoundingClientRect();
    return {
      x: Math.max(0, Math.min(100, ((clientX - rect.left) / rect.width) * 100)),
      y: Math.max(0, Math.min(100, ((clientY - rect.top) / rect.height) * 100)),
    };
  }
  function applyGeom(geom) {
    el.style.left = geom.x + '%'; el.style.top = geom.y + '%';
    el.style.width = geom.w + '%'; el.style.height = geom.h + '%';
  }

  el.onpointerdown = (e) => {
    if (e.target.closest('.box-handle')) return; // la poignée gère son propre geste, voir plus bas
    e.stopPropagation();
    e.preventDefault();
    const start = toPercent(e.clientX, e.clientY);
    const orig = { x: box.x, y: box.y, w: box.width, h: box.height };
    let geom = orig;
    el.setPointerCapture(e.pointerId);
    el.onpointermove = (ev) => {
      const p = toPercent(ev.clientX, ev.clientY);
      geom = {
        x: Math.max(0, Math.min(100 - orig.w, orig.x + (p.x - start.x))),
        y: Math.max(0, Math.min(100 - orig.h, orig.y + (p.y - start.y))),
        w: orig.w, h: orig.h,
      };
      applyGeom(geom);
    };
    el.onpointerup = () => {
      el.onpointermove = null; el.onpointerup = null;
      if (geom.x !== orig.x || geom.y !== orig.y) {
        correctBox(captureId, box.object_index, box.label, geom.x, geom.y, geom.w, geom.h, box.source);
      }
    };
  };

  const handle = el.querySelector('.box-handle');
  handle.onpointerdown = (e) => {
    e.stopPropagation();
    e.preventDefault();
    const orig = { x: box.x, y: box.y, w: box.width, h: box.height };
    let geom = orig;
    handle.setPointerCapture(e.pointerId);
    handle.onpointermove = (ev) => {
      const p = toPercent(ev.clientX, ev.clientY);
      geom = {
        x: orig.x, y: orig.y,
        w: Math.max(2, Math.min(100 - orig.x, p.x - orig.x)),
        h: Math.max(2, Math.min(100 - orig.y, p.y - orig.y)),
      };
      applyGeom(geom);
    };
    handle.onpointerup = () => {
      handle.onpointermove = null; handle.onpointerup = null;
      if (geom.w !== orig.w || geom.h !== orig.h) {
        correctBox(captureId, box.object_index, box.label, geom.x, geom.y, geom.w, geom.h, box.source);
      }
    };
  };
}

const KNOWN_LABELS = ['Pookie', 'Patoune', 'Nala', 'Boubou', 'Proie', 'Autre'];
// La brute du modèle peut dire "Prey"/"Maus"/"mouse" (anglais/allemand, selon
// la génération du modèle) - sans ça, aucune option ne matchait "Prey" et le
// <select> retombait silencieusement sur la première option (Pookie),
// donnant l'impression fausse que la boîte était déjà étiquetée "Pookie".
const LABEL_SYNONYMS = { prey: 'Proie', maus: 'Proie', mouse: 'Proie' };
function canonicalLabel(raw) {
  const lower = (raw || '').toLowerCase();
  if (LABEL_SYNONYMS[lower]) return LABEL_SYNONYMS[lower];
  const known = KNOWN_LABELS.find(l => l.toLowerCase() === lower);
  return known || raw;
}

function renderBoxList(full) {
  const list = document.getElementById('box-list');
  list.innerHTML = full.boxes.map(box => {
    const canon = canonicalLabel(box.label);
    // Une étiquette brute jamais vue (ni dans KNOWN_LABELS ni un synonyme
    // connu) reste visible telle quelle plutôt que de disparaître dans un
    // mauvais choix par défaut - mieux vaut un intitulé inattendu que faux.
    const options = KNOWN_LABELS.includes(canon) ? KNOWN_LABELS : [canon, ...KNOWN_LABELS];
    return `
    <div class="box-row">
      <div class="index-badge" style="background:${indexColor(box.object_index)}">${box.object_index}</div>
      <select onchange="correctBox(${full.id}, ${box.object_index}, this.value, ${box.x}, ${box.y}, ${box.width}, ${box.height}, '${box.source}')">
        ${options.map(l => `<option value="${l}" ${l === canon ? 'selected' : ''}>${l}</option>`).join('')}
      </select>
      <span class="prob">${box.source === 'manual' ? 'ajoutée' : (box.probability != null ? box.probability.toFixed(0) + '%' : '')}</span>
      <button onclick="discardBox(${full.id}, ${box.object_index})">supprimer</button>
    </div>
  `;
  }).join('') || '<div class="hint">Aucune boîte détectée - trace un rectangle sur l\'image si quelque chose a été manqué.</div>';
}

async function correctBox(captureId, objectIndex, label, x, y, width, height, source = 'model') {
  // 18.10 : source était figé à 'model' ici, même pour une boîte ajoutée à
  // la main (source 'manual') - la relabelliser ou la redimensionner
  // aurait silencieusement changé sa source en base, et discardBox()
  // l'aurait ensuite cherchée dans detected_objects_json (où elle n'existe
  // pas, étant manuelle) au lieu de la supprimer franchement. Repasser la
  // vraie source du box affiché évite ça.
  await api(`/app-api/captures/${captureId}/boxes`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ object_index: objectIndex, label, x, y, width, height, source }),
  });
  await renderDetail();
}

async function discardBox(captureId, objectIndex) {
  await api(`/app-api/captures/${captureId}/boxes/${objectIndex}`, { method: 'DELETE' });
  await renderDetail();
}

// Petit <select> flottant posé sur la boîte qu'on vient de tracer à la main
// (voir wireDrawing) - remplace l'ancien prompt() texte libre. Résout une
// promesse avec le label choisi, ou null si elle ferme sans choisir.
function pickLabel(wrap, boxGeom) {
  return new Promise((resolve) => {
    const sel = document.createElement('select');
    sel.className = 'label-picker';
    sel.style.left = boxGeom.x + '%';
    sel.style.top = boxGeom.y + '%';
    sel.innerHTML =
      `<option value="" disabled selected>Chat ou proie ?</option>` +
      KNOWN_LABELS.map(l => `<option value="${l}">${l}</option>`).join('');
    wrap.appendChild(sel);

    let settled = false;
    function finish(value) {
      if (settled) return;
      settled = true;
      sel.remove();
      resolve(value || null);
    }
    sel.onchange = () => finish(sel.value);
    // Fermée sans choix (tape à côté, touche Échap) : pas de boîte créée.
    sel.onblur = () => setTimeout(() => finish(null), 150);

    sel.focus();
    if (sel.showPicker) {
      try { sel.showPicker(); } catch (e) { /* pas supporté partout */ }
    }
  });
}

function wireDrawing(wrap, captureId, nextIndex) {
  let startX, startY, tempEl, current;

  function toPercent(clientX, clientY) {
    const rect = wrap.getBoundingClientRect();
    const x = Math.max(0, Math.min(100, ((clientX - rect.left) / rect.width) * 100));
    const y = Math.max(0, Math.min(100, ((clientY - rect.top) / rect.height) * 100));
    return { x, y };
  }

  wrap.onpointerdown = (e) => {
    if (e.target.closest('.box')) return; // clic sur une boîte existante = pas un nouveau dessin
    const p = toPercent(e.clientX, e.clientY);
    startX = p.x; startY = p.y;
    current = { x: startX, y: startY, w: 0, h: 0 };
    tempEl = document.createElement('div');
    tempEl.className = 'box drawing';
    wrap.appendChild(tempEl);
    wrap.setPointerCapture(e.pointerId);
  };
  wrap.onpointermove = (e) => {
    if (!tempEl) return;
    const p = toPercent(e.clientX, e.clientY);
    current = {
      x: Math.min(startX, p.x), y: Math.min(startY, p.y),
      w: Math.abs(p.x - startX), h: Math.abs(p.y - startY),
    };
    tempEl.style.left = current.x + '%'; tempEl.style.top = current.y + '%';
    tempEl.style.width = current.w + '%'; tempEl.style.height = current.h + '%';
  };
  wrap.onpointerup = async () => {
    if (!tempEl) return;
    tempEl.remove(); tempEl = null;
    if (!current || current.w < 2 || current.h < 2) return; // trop petit, probablement un simple clic
    // 18.10, Sid ("je dois taper" - un prompt() texte libre, pas pratique
    // au clavier tactile et sujet aux fautes de frappe) : un <select> avec
    // les mêmes options que la liste de boîtes en dessous, positionné sur
    // la boîte qu'on vient de tracer.
    const boxGeom = { x: current.x, y: current.y, w: current.w, h: current.h };
    const label = await pickLabel(wrap, boxGeom);
    if (!label) return;
    await api(`/app-api/captures/${captureId}/boxes`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        object_index: nextIndex, label,
        x: boxGeom.x, y: boxGeom.y, width: boxGeom.w, height: boxGeom.h,
        source: 'manual',
      }),
    });
    await renderDetail();
  };
}

let reviewInFlight = false;

async function submitReview(status) {
  // 18.10, Sid ("ça m'arrive 3x de suite, une boîte que j'ai pas eu le
  // temps de corriger n'est plus dans la liste") - vraie course de
  // concurrence, pas un souci tactile : un second clic pendant que le
  // premier submitReview() attendait encore sa réponse serveur lisait le
  // MÊME currentIndex/capture (le splice du premier n'avait pas encore
  // eu lieu), et les deux appels finissaient chacun par spliced un
  // élément - le second jamais réellement envoyé au serveur, juste perdu
  // de la liste sans avoir été revu. Un verrou simple empêche tout
  // chevauchement ; les boutons sont aussi désactivés pendant l'appel
  // pour que le second clic n'ait même plus de cible.
  if (reviewInFlight) return;
  reviewInFlight = true;
  const actionButtons = document.querySelectorAll('.actions button');
  actionButtons.forEach(b => b.disabled = true);
  try {
    const capture = currentQueue[currentIndex];
    // 22.10, Sid ("j'en ai validé 3 de boubou... les trois se sont mises
    // automatiquement dans validé, le validé habituel") - le bouton
    // "Valider" de cette vue détail appelait toujours l'endpoint de
    // revue normal (review_status='confirmed' direct), qui ignore
    // complètement rebalance_bucket - les photos filaient tout droit
    // dans "Validées" au lieu de rester dans le lot 'kept' en cours de
    // constitution. Dans l'onglet Rééquilibrage, "Valider" doit passer
    // par le même mécanisme que "Garder la sélection" (bulk), pas
    // confirmer directement.
    if (currentTab === REBALANCE_TAB && status === 'confirmed') {
      await api('/app-api/rebalance/keep', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ids: [capture.id] }),
      });
    } else {
      await api(`/app-api/captures/${capture.id}/review?status=${status}`, { method: 'POST' });
    }
    currentQueue.splice(currentIndex, 1);
    renderQueue();
    if (currentTab === REBALANCE_TAB) {
      await loadRebalanceCounts();
    }
    const statsResp = await api('/app-api/stats');
    lastStats = statsResp;
    renderStats(statsResp);
    const catsResp = await api('/app-api/cats');
    cats = catsResp;
    renderTabs();

    // 18.10, Sid ("ça pourrait passer direct à la suivante ?") - revue d'un
    // gros lot de faux positifs similaires : fermer le détail à chaque
    // Valider/Écarter pour revenir à la grille, retrouver la vignette,
    // rouvrir... était un aller-retour inutile. La photo suivante prend la
    // même place dans la queue (on vient de spliced l'actuelle) - on reste
    // ouvert et on l'affiche directement.
    if (currentQueue.length === 0) {
      // 18.10, Sid ("pourquoi je vois pas les 4700 photos ?") - loadQueue()
      // ne charge que 200 captures à la fois (voir plus bas) ; les traiter
      // toutes les 200 affichait "vide" même s'il en restait des milliers en
      // attente, sans jamais recharger le lot suivant - il fallait recliquer
      // l'onglet à la main pour continuer. On retente un chargement avant de
      // conclure que c'est vraiment vide.
      await reloadCurrentTab();
      if (currentQueue.length === 0) {
        closeDetail();
        return;
      }
      currentIndex = 0;
      await renderDetail();
      return;
    }
    if (currentIndex >= currentQueue.length) {
      currentIndex = currentQueue.length - 1;
    }
    await renderDetail();
  } finally {
    reviewInFlight = false;
    actionButtons.forEach(b => b.disabled = false);
  }
}

init();

if ('serviceWorker' in navigator) {
  navigator.serviceWorker.register('/sw.js').catch(() => {});
}
