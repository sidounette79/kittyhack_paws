// Native Web Push subscribe/unsubscribe flow for cat entry/exit notifications.
// Kept separate from app.js (which already owns the SW *registration* and the
// PWA install-prompt UI) since this is a distinct, optional feature a user
// opts into from the Configuration page.
//
// The Configuration tab's DOM is fully torn down and rebuilt by Shiny every
// time it's opened or saved, so binding once on DOMContentLoaded isn't
// enough - mirrors app.js's own pwaElementsObserver pattern (MutationObserver
// on document.body, kept alive rather than disconnected after first match).

(function () {
  function _urlBase64ToUint8Array(base64String) {
    const padding = '='.repeat((4 - (base64String.length % 4)) % 4);
    const base64 = (base64String + padding).replace(/-/g, '+').replace(/_/g, '/');
    const rawData = window.atob(base64);
    const outputArray = new Uint8Array(rawData.length);
    for (let i = 0; i < rawData.length; ++i) {
      outputArray[i] = rawData.charCodeAt(i);
    }
    return outputArray;
  }

  async function kittyhackWebpushStatus() {
    if (!('serviceWorker' in navigator) || !('PushManager' in window)) {
      return 'unsupported';
    }
    const registration = await navigator.serviceWorker.ready;
    const existing = await registration.pushManager.getSubscription();
    return existing ? 'subscribed' : 'unsubscribed';
  }

  async function enableKittyhackNotifications() {
    if (!('serviceWorker' in navigator) || !('PushManager' in window)) {
      alert("Les notifications ne sont pas supportées par ce navigateur.");
      return;
    }
    const permission = await Notification.requestPermission();
    if (permission !== 'granted') {
      alert("Permission refusée. Active les notifications pour ce site dans les paramètres du navigateur.");
      return;
    }
    const registration = await navigator.serviceWorker.ready;
    let subscription = await registration.pushManager.getSubscription();
    if (!subscription) {
      const keyResp = await fetch('/webpush/vapid-public-key');
      const { publicKey } = await keyResp.json();
      subscription = await registration.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: _urlBase64ToUint8Array(publicKey),
      });
    }
    await fetch('/webpush/subscribe', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(subscription),
    });
  }

  async function disableKittyhackNotifications() {
    const registration = await navigator.serviceWorker.ready;
    const subscription = await registration.pushManager.getSubscription();
    if (subscription) {
      await fetch('/webpush/unsubscribe', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ endpoint: subscription.endpoint }),
      });
      await subscription.unsubscribe();
    }
  }

  function bindWebpushControls(btn, status) {
    if (btn.dataset.khBound === '1') return;
    btn.dataset.khBound = '1';

    function refresh() {
      kittyhackWebpushStatus().then(state => {
        if (state === 'unsupported') {
          status.textContent = status.dataset.unsupported || 'Not supported by this browser.';
          btn.style.display = 'none';
          return;
        }
        const subscribed = state === 'subscribed';
        btn.dataset.khState = subscribed ? 'subscribed' : 'unsubscribed';
        status.textContent = subscribed
          ? (status.dataset.onLabel || 'Notifications enabled on this device.')
          : (status.dataset.offLabel || 'Notifications disabled on this device.');
        btn.textContent = subscribed
          ? (btn.dataset.disableLabel || 'Disable')
          : (btn.dataset.enableLabel || 'Enable');
      }).catch(e => {
        console.error('[Webpush] Status check failed:', e);
        status.textContent = 'Error: ' + e.message;
      });
    }

    btn.onclick = function () {
      btn.disabled = true;
      const action = btn.dataset.khState === 'subscribed'
        ? disableKittyhackNotifications()
        : enableKittyhackNotifications();
      action
        .catch(e => console.error('[Webpush] Toggle failed:', e))
        .finally(() => { btn.disabled = false; refresh(); });
    };

    refresh();
  }

  function scanForWebpushControls() {
    const btn = document.getElementById('kh_webpush_toggle_btn');
    const status = document.getElementById('kh_webpush_status');
    if (btn && status) bindWebpushControls(btn, status);
  }

  const observer = new MutationObserver(scanForWebpushControls);
  observer.observe(document.body, { childList: true, subtree: true });
  document.addEventListener('DOMContentLoaded', scanForWebpushControls);
  scanForWebpushControls();
})();
