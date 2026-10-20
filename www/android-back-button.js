// 07.10, Sid: intercept the native Android back gesture/button so it
// navigates WITHIN Kittyhack's Shiny UI (no real URL history exists -
// it's a single reactive page) instead of closing the app outright.
// Silent no-op outside the native app (window.Capacitor absent/web),
// same pattern as fcm-native-client.js.
(function () {
  function isNativeApp() {
    return !!(window.Capacitor && window.Capacitor.isNativePlatform && window.Capacitor.isNativePlatform());
  }

  function isVisible(el) {
    return !!el && el.offsetParent !== null;
  }

  // Returns true if it found and triggered something to go back to,
  // false if there is nothing open (caller should fall back to the
  // platform's default back behavior, i.e. exit/minimize the app).
  function handleBack() {
    // 1. An open Shiny modal (event_modal.py) - its visible close button
    //    carries this real DOM id (Shiny always renders input_action_button
    //    ids verbatim), so clicking it does exactly what the on-screen
    //    close icon does.
    var modalClose = document.getElementById('btn_modal_cancel');
    if (isVisible(modalClose)) {
      modalClose.click();
      return true;
    }

    // 2. Any visible "<-" back arrow on a detail/stats/filter sub-view
    //    (presence.py: presence_detail_back, presence_events_filter_back,
    //    presence_stats_back all share this class). Only one can be
    //    visible at a time since they belong to different sub-views of
    //    the same reactive page.
    var backEls = document.querySelectorAll('.kh-presence-detail-back');
    for (var i = 0; i < backEls.length; i++) {
      if (isVisible(backEls[i])) {
        backEls[i].click();
        return true;
      }
    }

    // 3. No sub-view open, but we're not on the home tab (presence) either -
    //    e.g. Sid on Chronology/Journey/Pictures/etc. pressing back. A
    //    single-Activity app with no real URL history has nothing else to
    //    pop, so without this the OS default kicks in and just exits the
    //    whole app from any root tab - reported 07.10 ("je ne peux pas
    //    revenir en arrière ... je dois refermer complètement l'app").
    //    Go to the home tab first, exactly like a normal Android app's
    //    back stack would; only exit once already there.
    var activeLink = document.querySelector('#main_nav .nav-link.active[data-value]');
    if (activeLink && activeLink.getAttribute('data-value') !== 'presence') {
      var homeLink = document.querySelector('#main_nav .nav-link[data-value="presence"]');
      if (homeLink) {
        homeLink.click();
        return true;
      }
    }

    return false;
  }

  function setup() {
    if (!isNativeApp()) return;
    var AppPlugin = window.Capacitor.Plugins && window.Capacitor.Plugins.App;
    if (!AppPlugin) {
      console.warn('[BackButton] Running in the native app but the App plugin is unavailable.');
      return;
    }

    AppPlugin.addListener('backButton', function (data) {
      if (handleBack()) return;
      // Nothing open to back out of - we're already on a root tab, let
      // the platform do its normal thing (exit/minimize), no need to
      // block the user from leaving the app.
      if (data && data.canGoBack) {
        window.history.back();
      } else {
        AppPlugin.exitApp();
      }
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', setup);
  } else {
    setup();
  }
})();
