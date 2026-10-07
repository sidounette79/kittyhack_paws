// Periodically refresh the watchdog camera preview thumbnails on the
// Presence page. Same "keep re-scanning the DOM" pattern as
// webpush-client.js - Shiny tears down and rebuilds this panel's DOM on
// every re-render, so a one-time DOMContentLoaded bind isn't enough.

(function () {
  var CAMS = ['chatiere', 'terrasse', 'jardin_japonais', 'entree'];
  var REFRESH_MS = 4000;

  function refreshOne(img) {
    img.src = img.dataset.base + '?t=' + Date.now();
  }

  function bindAll() {
    CAMS.forEach(function (name) {
      var img = document.getElementById('wd-cam-' + name);
      if (img && img.dataset.khBound !== '1') {
        img.dataset.khBound = '1';
        img.dataset.base = '/watchdog-frame/' + name + '.jpg';
        img.onerror = function () {
          img.classList.add('kh-wdcam-error');
        };
        img.onload = function () {
          img.classList.remove('kh-wdcam-error');
        };
        refreshOne(img);
        setInterval(function () { refreshOne(img); }, REFRESH_MS);
      }
    });
  }

  var observer = new MutationObserver(bindAll);
  observer.observe(document.body, { childList: true, subtree: true });
  document.addEventListener('DOMContentLoaded', bindAll);
  bindAll();
})();
