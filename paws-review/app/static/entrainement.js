// 26.10, Sid ("Validée ça va avec entrainement") - page "Entraînement" :
// Export + Entraînement (anciennement dans le header de toutes les pages,
// maintenant 100% ici), et 2 vraies sous-pages pour les données
// d'entraînement elles-mêmes : /entrainement = Validées (pas encore
// exportées), /entrainement/exportees = déjà dans un zip (confirmed/
// corrected uniquement - voir status_group="exported" côté serveur,
// distinct d'"archived" qui mélange aussi les écartées). Stats par classe/
// source retiré le même soir ("tu peux enlever la partie des stats").

const SUBPAGES = [
  { slug: '', label: 'Validées', statusGroup: 'validated', href: '/entrainement' },
  { slug: 'exportees', label: 'Exportées (dans le zip)', statusGroup: 'exported', href: '/entrainement/exportees' },
];

function currentSub() {
  const rest = location.pathname.replace(/^\/entrainement\/?/, '');
  return SUBPAGES.find(s => s.slug === rest) || SUBPAGES[0];
}

let cats = [];
let currentQueue = [];
let currentIndex = 0;
let reviewedCatFilter = null;
let reviewedSourceFilter = null;
let reviewedPreyFilter = false;

function renderSubNav() {
  const sub = currentSub();
  document.getElementById('sub-nav').innerHTML = SUBPAGES.map(s => {
    const active = s.slug === sub.slug;
    return `<a class="tab ${active ? 'active' : ''}" style="${active ? 'background:var(--text-dim);color:var(--bg)' : 'border-color:var(--text-dim)'}" href="${s.href}">${s.label}</a>`;
  }).join('');
}

async function init() {
  cats = await api('/app-api/cats');
  renderSubNav();
  renderReviewedFilterBar();
  await loadQueue();
  await checkExportStatus();
  await checkTrainStatus();
  await checkIdentityTrainStatus();
}

// --- Export ----------------------------------------------------------
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
    bar.innerHTML = `<span class="export-msg">Export en cours${progress}...</span>
      <button onclick="resetStuckState('export', checkExportStatus)" title="Si ça semble bloqué après un redéploiement">Réinitialiser</button>`;
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

// --- Entraînement ------------------------------------------------------
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

async function startTraining(collapseIdentity = false) {
  try {
    await api(`/app-api/train/start?collapse_identity=${collapseIdentity ? '1' : '0'}`, { method: 'POST' });
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

// 29.10, plan de scission détection/identité - train_vastai.py gardait
// deux modes (collapse_identity=0|1) le temps que le Modèle 2 existe
// vraiment. 05.11, Sid ("retire-le") - le Modèle 2 est en prod depuis le
// 02.11, le mode "modèle combiné 5 classes" (collapse_identity=0) n'a
// plus de raison d'être appelé depuis l'UI - seul le bouton a été retiré,
// `startTraining(false)`/`/app-api/train/start?collapse_identity=0` côté
// serveur restent inchangés (pas de régression à risquer pour un bouton
// qui n'existe plus).
function renderTrainBar(state) {
  const bar = document.getElementById('train-bar');
  const bothButtons = `
    <button onclick="startTraining(true)" title="Classes réduites à Cat+Prey - voir le plan de scission détection/identité">Lancer un entraînement Modèle 1 (détection Cat+Prey)</button>`;
  if (state.status === 'running') {
    const label = TRAIN_STEP_LABELS[state.step] || 'En cours';
    const price = state.price ? ` - ${state.price.toFixed(3)}$/h` : '';
    bar.innerHTML = `<span class="export-msg">🧠 ${label}${price}...</span>
      <button onclick="resetStuckState('train', checkTrainStatus)" title="Si ça semble bloqué après un redéploiement">Réinitialiser</button>`;
  } else if (state.status === 'success') {
    const when = state.finished_at ? new Date(state.finished_at * 1000).toLocaleString('fr-CH') : '';
    bar.innerHTML = `
      <span class="export-msg">✓ Nouveau modèle entraîné (${state.images} images, ${when}) - sélectionne-le dans Kittyhack</span>
      ${bothButtons}`;
  } else if (state.status === 'error') {
    bar.innerHTML = `
      <span class="export-msg export-error">Échec de l'entraînement : ${state.message || 'erreur inconnue'}</span>
      ${bothButtons}`;
  } else {
    bar.innerHTML = bothButtons;
  }
}

// --- Entraînement identité (Modèle 2) -----------------------------------
// 29.10, plan de scission détection/identité (Phase 2) - entraînement
// complètement indépendant du Modèle 1 (état séparé côté serveur, voir
// train_vastai_yolo_identity.py), peut tourner en même temps ou pas.
// 05.11, Sid ("reconstruire le vrai pipeline", puis "quid de toutes
// celles de reolink qu'on a rajoutée ?") - ce bouton pointait avant sur
// l'ancienne approche embeddings/ArcFace (train_vastai_identity.py,
// jamais activée en prod, plafonnait à ~58-64%). Rebranché sur le vrai
// pipeline (train_vastai_yolo_identity.py +
// build_combined_identity_training_set.py) : le modèle actif (92.8%)
// combine le RFID réel Kittyflap (jusqu'à 1800/chat) ET le pool
// Reolink/manuel validé à la main dans Paws Review (dupliqué x3) - une
// première version de ce rebranchement oubliait cette seconde moitié.
let identityTrainPollTimer = null;

const IDENTITY_TRAIN_STEP_LABELS = {
  export: 'Construction du dataset (RFID réel + Reolink/manuel validé)',
  searching_offer: 'Recherche d\'une machine GPU',
  health_check: 'Vérification de la machine',
  training: 'Entraînement en cours',
};

async function checkIdentityTrainStatus() {
  const state = await api('/app-api/identity-train/status');
  renderIdentityTrainBar(state);
  if (state.status === 'running') {
    if (!identityTrainPollTimer) identityTrainPollTimer = setInterval(checkIdentityTrainStatus, 10000);
  } else if (identityTrainPollTimer) {
    clearInterval(identityTrainPollTimer);
    identityTrainPollTimer = null;
  }
}

async function startIdentityTraining() {
  try {
    await api('/app-api/identity-train/start', { method: 'POST' });
  } catch (e) {
    // 409 = déjà en cours - pas une vraie erreur, juste resynchroniser l'affichage
  }
  await checkIdentityTrainStatus();
}

function renderIdentityTrainBar(state) {
  const bar = document.getElementById('identity-train-bar');
  const button = `<button onclick="startIdentityTraining()" title="Classificateur YOLO26 (task=classify) sur RFID réel Kittyflap (jusqu'à 1800 crops/chat) + pool Reolink/manuel validé dans Paws Review (dupliqué x3) - c'est ce pipeline qui a produit le modèle à 92.8% actif aujourd'hui">Lancer un entraînement Modèle 2 (identité)</button>`;
  if (state.status === 'running') {
    const label = IDENTITY_TRAIN_STEP_LABELS[state.step] || 'En cours';
    const price = state.price ? ` - ${state.price.toFixed(3)}$/h` : '';
    bar.innerHTML = `<span class="export-msg">🧠 ${label}${price}...</span>
      <button onclick="resetStuckState('identity_train', checkIdentityTrainStatus)" title="Si ça semble bloqué après un redéploiement">Réinitialiser</button>`;
  } else if (state.status === 'success') {
    const when = state.finished_at ? new Date(state.finished_at * 1000).toLocaleString('fr-CH') : '';
    bar.innerHTML = `
      <span class="export-msg">✓ Modèle identité entraîné (${state.images} images, ${when}) - sélectionne-le dans Kittyhack</span>
      ${button}`;
  } else if (state.status === 'error') {
    bar.innerHTML = `
      <span class="export-msg export-error">Échec de l'entraînement identité : ${state.message || 'erreur inconnue'}</span>
      ${button}`;
  } else {
    bar.innerHTML = button;
  }
}

// --- Validées (file + filtres) ------------------------------------------
function renderReviewedFilterBar() {
  const bar = document.getElementById('reviewed-filter-bar');
  const catOptions = ['', ...cats.map(c => c.name), 'Prey'].map(n =>
    `<option value="${n}" ${reviewedCatFilter === (n || null) ? 'selected' : ''}>${n || 'Tous les chats'}</option>`
  ).join('');
  bar.innerHTML = `
    <span class="export-msg">${currentSub().label}</span>
    <select onchange="reviewedSetFilter('cat', this.value || null)">${catOptions}</select>
    <select onchange="reviewedSetFilter('source', this.value || null)">
      <option value="" ${!reviewedSourceFilter ? 'selected' : ''}>Toute source</option>
      <option value="kittyflap" ${reviewedSourceFilter === 'kittyflap' ? 'selected' : ''}>HK</option>
      <option value="reolink_or_manual" ${reviewedSourceFilter === 'reolink_or_manual' ? 'selected' : ''}>Reolink+Manuel</option>
    </select>
    <label style="margin-left:8px"><input type="checkbox" ${reviewedPreyFilter ? 'checked' : ''}
      onchange="reviewedSetFilter('prey', this.checked)"> Proies suspectées seulement</label>
  `;
}

function reviewedSetFilter(key, value) {
  if (key === 'cat') reviewedCatFilter = value;
  else if (key === 'source') reviewedSourceFilter = value;
  else if (key === 'prey') reviewedPreyFilter = value;
  loadQueue();
}

async function loadQueue() {
  const params = new URLSearchParams({ limit: 10000, status_group: currentSub().statusGroup });
  if (reviewedCatFilter) params.set('cat_name', reviewedCatFilter);
  if (reviewedSourceFilter) params.set('source', reviewedSourceFilter);
  if (reviewedPreyFilter) params.set('prey_only', '1');
  currentQueue = await api(`/app-api/queue?${params}`);
  renderQueue();
}

function renderQueue() {
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
    <div class="queue-tile ${c.is_prey_candidate ? 'prey' : ''} ${selectedIds.has(c.id) ? 'selected' : ''}"
         onclick="openDetail(${i})">
      <img src="/app-api/captures/${c.id}/image?size=thumbnail" loading="lazy">
      <input type="checkbox" class="tile-check" ${selectedIds.has(c.id) ? 'checked' : ''}
             onclick="event.stopPropagation(); toggleSelect(${c.id}, renderQueue)">
      ${c.is_prey_candidate ? '<div class="flag">proie ?</div>' : ''}
    </div>
  `).join('');
  renderBulkBar();
}

function renderBulkBar() {
  const bar = document.getElementById('bulk-bar');
  const count = selectedIds.size;
  bar.hidden = count === 0;
  if (count === 0) return;
  const label = `${count} sélectionnée${count > 1 ? 's' : ''}`;
  bar.innerHTML = `
    <span>${label}</span>
    <button onclick="selectAllVisible(currentQueue, renderQueue)">Tout sélectionner</button>
    <button onclick="clearSelection(renderQueue)">Annuler</button>
    <button class="btn-reject" onclick="bulkReview('rejected')">Écarter la sélection</button>`;
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
  await loadQueue();
}

async function openDetail(index) {
  currentIndex = index;
  await renderCurrentDetail();
  document.getElementById('detail').classList.add('open');
}

async function navigateDetail(delta) {
  const newIndex = currentIndex + delta;
  if (newIndex < 0 || newIndex >= currentQueue.length) return;
  currentIndex = newIndex;
  await renderCurrentDetail();
}

async function renderCurrentDetail() {
  const capture = currentQueue[currentIndex];
  await renderDetail(capture.id, null, renderCurrentDetail);
}

let reviewInFlight = false;

async function submitReview(status) {
  if (reviewInFlight) return;
  reviewInFlight = true;
  const actionButtons = document.querySelectorAll('.actions button');
  actionButtons.forEach(b => b.disabled = true);
  try {
    const capture = currentQueue[currentIndex];
    await api(`/app-api/captures/${capture.id}/review?status=${status}`, { method: 'POST' });
    currentQueue.splice(currentIndex, 1);
    renderQueue();
    if (currentQueue.length === 0) {
      await loadQueue();
      if (currentQueue.length === 0) {
        closeDetail();
        return;
      }
      currentIndex = 0;
      await renderCurrentDetail();
      return;
    }
    if (currentIndex >= currentQueue.length) {
      currentIndex = currentQueue.length - 1;
    }
    await renderCurrentDetail();
  } finally {
    reviewInFlight = false;
    actionButtons.forEach(b => b.disabled = false);
  }
}

init();
