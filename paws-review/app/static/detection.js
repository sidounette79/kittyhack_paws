// 01.11, Sid ("il me faudrait la même page mais pour chat/proie") - même
// mécanique qu'identite.js (voir ses commentaires), pour le Modèle 1
// (détection Cat/Prey) au lieu du Modèle 2 (identité). Les boîtes Proie
// restent triables par confiance de détection (jamais réécrite par la
// résolution d'identité, contrairement aux boîtes de chat - voir
// identity_review.get_detection_review_queue côté serveur).

let currentQueue = [];
let currentIndex = 0;

async function init() {
  await loadQueue();
}

async function loadQueue() {
  const res = await api('/app-api/detection/review-queue?limit=150');
  currentQueue = res.items;
  renderBar();
  renderQueue();
}

function renderBar() {
  document.getElementById('review-bar').innerHTML =
    `<span class="export-msg">${currentQueue.length} boîte(s) Proie à revoir, les moins sûres d'abord</span>`;
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
    <div class="queue-tile prey" onclick="openDetail(${i})">
      <img src="/app-api/captures/${item.capture_id}/image?size=thumbnail" loading="lazy">
      <div class="flag">${item.predicted_name} · ${item.predicted_score.toFixed(0)}%</div>
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
  const meta = `détecté par le Modèle 1 : ${item.predicted_name} (${item.predicted_score.toFixed(0)}%)`;
  await renderDetail(item.capture_id, meta, renderCurrentDetail);
}

async function backToQueue() {
  closeDetail();
  await loadQueue();
}

init();
