// 07.10, Sid: native FCM registration, only relevant when this site is
// loaded inside the Capacitor Android app (android-app/, server.url points
// here) - a plain browser tab never has window.Capacitor, so this is a
// silent no-op everywhere else, including the PWA/webpush-client.js path
// which stays fully independent (she keeps normal browser access too).
// Capacitor injects window.Capacitor into any page it loads, even a
// REMOTE site like this one - no bundler/import needed, the plugin is
// reachable as window.Capacitor.Plugins.PushNotifications directly.
(function () {
  function isNativeApp() {
    return !!(window.Capacitor && window.Capacitor.isNativePlatform && window.Capacitor.isNativePlatform());
  }

  async function registerNativePush() {
    if (!isNativeApp()) return;
    const PushNotifications = window.Capacitor.Plugins && window.Capacitor.Plugins.PushNotifications;
    if (!PushNotifications) {
      console.warn('[FCM] Running in the native app but PushNotifications plugin is unavailable.');
      return;
    }

    try {
      const perm = await PushNotifications.requestPermissions();
      if (perm.receive !== 'granted') {
        console.warn('[FCM] Push permission not granted:', perm.receive);
        return;
      }

      PushNotifications.addListener('registration', async (tokenData) => {
        try {
          await fetch('/fcm/register', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ token: tokenData.value }),
          });
          console.log('[FCM] Device token registered.');
        } catch (e) {
          console.warn('[FCM] Failed to send token to server:', e);
        }
      });

      PushNotifications.addListener('registrationError', (err) => {
        console.warn('[FCM] Registration error:', err);
      });

      // Opening a notification from the tray while the app is in the
      // background - same "focus/open the app" intent as the PWA service
      // worker's own notificationclick handler.
      PushNotifications.addListener('pushNotificationActionPerformed', (action) => {
        const url = action.notification && action.notification.data && action.notification.data.url;
        if (url && url !== window.location.pathname) {
          window.location.href = url;
        }
      });

      await PushNotifications.register();
    } catch (e) {
      console.warn('[FCM] Native push registration failed:', e);
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', registerNativePush);
  } else {
    registerNativePush();
  }
})();
