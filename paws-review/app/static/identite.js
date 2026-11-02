// 01.11, Sid ("le but c'est vraiment de la correction d'apprentissage,
// lui dire là tu as mis ça, tu t'es trompé, apprends ça en plus") puis,
// après avoir vu la 1ère version ("je ne vois pas les boîtes... si la
// photo comporte une boîte alors qu'en fait il n'y a aucun chat je fais
// quoi ? Si la photo comporte deux chats alors qu'il n'y a qu'une boîte
// je fais quoi ?") - Phase 5 : simple point d'entrée filtré (score
// croissant - les moins sûres d'abord) + trié vers l'éditeur de boîtes
// déjà existant (common.js:renderDetail/renderBox/correctBox/discardBox/
// wireDrawing, le même que Photos/Révision) - jamais de système de
// correction maison en parallèle.

let currentQueue = [];
let currentIndex = 0;

async function init() {
  await loadQueue();
}

async function loadQueue() {
  const res = await api('/app-api/identity/review-queue?limit=150');
  currentQueue = res.items;
  renderBar();
  renderQueue();
}

function renderBar() {
  document.getElementById('review-bar').innerHTML =
    `<span class="export-msg">${currentQueue.length} boîte(s) de chat à revoir, les moins sûres d'abord</span>`;
}

function renderQueue() {
  const grid = document.getElementById('queue');
  const empty = document.getElementById('empty');
  if (currentQueue.length === 0) {
    grid.innerHTML = '';
    empty.hidden = false;
    return;
  }
  empty.hidden = true;
  grid.innerHTML = currentQueue.map((item, i) => `
    <div class="queue-tile" onclick="openDetail(${i})">
      <img src="/app-api/captures/${item.capture_id}/image?size=thumbnail" loading="lazy">
      <div class="flag" style="color:${catColor(item.predicted_name)}">${item.predicted_name} · ${item.predicted_score.toFixed(0)}%</div>
    </div>
  `).join('');
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
  const item = currentQueue[currentIndex];
  const meta = `prédit par le Modèle 2 : ${item.predicted_name} (${item.predicted_score.toFixed(0)}%)`;
  // onChanged : juste se redessiner soi-même - une capture peut avoir
  // besoin de PLUSIEURS actions (corriger l'identité ET ajouter un 2e
  // chat manqué) avant de passer à la suivante ; jamais d'avancée
  // automatique après une seule boîte touchée. "Suivant"/"retour"
  // recharge la vraie file depuis le serveur (voir navigateDetail/
  // backToQueue) - c'est à ce moment-là qu'une capture désormais revue
  // (box_annotations) disparaît naturellement.
  await renderDetail(item.capture_id, meta, renderCurrentDetail);
}

async function backToQueue() {
  closeDetail();
  await loadQueue();
}

init();
