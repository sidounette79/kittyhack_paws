import { useEffect, useRef } from "react";

// 07.10, Sid ("si je scroll gauche après avoir cliqué sur une photo, j'ai
// un écran noir"): shared version of the fix first written directly in
// Chronology.tsx - every migrated page is standalone (no www/android-back-
// button.js loaded), so each one has to register its own native back
// handling or a left swipe/gesture falls through to Capacitor's default
// WebView history.back() with nothing in this page's own state to catch it.
//
// `onBack` returns true if it consumed the event (closed a lightbox/sub-
// view) - the native back is swallowed, nothing else happens. Returning
// false means "nothing open here", and `fallback` runs instead (usually a
// react-router navigate to this page's own "home").
export function useBackButton(onBack: () => boolean, fallback: () => void) {
  const onBackRef = useRef(onBack);
  const fallbackRef = useRef(fallback);
  onBackRef.current = onBack;
  fallbackRef.current = fallback;

  useEffect(() => {
    const AppPlugin = (window as any).Capacitor?.Plugins?.App;
    if (!AppPlugin) return;
    const handle = AppPlugin.addListener("backButton", () => {
      if (!onBackRef.current()) {
        fallbackRef.current();
      }
    });
    return () => {
      // 07.10, Sid ("écran noir" root cause, finally caught by the
      // ErrorBoundary: "TypeError: y.then is not a function"): this
      // assumed addListener() always returns a real Promise (per
      // Capacitor's documented API), but whatever actually ran here
      // returned something without a `.then` - calling it directly threw
      // on every single unmount (every navigation, every swipe-triggered
      // back). Promise.resolve() normalizes any value (thenable or not)
      // into a real promise first, so this can never throw regardless of
      // what addListener() actually handed back.
      Promise.resolve(handle)
        .then((h: { remove?: () => void }) => h?.remove?.())
        .catch(() => {});
    };
  }, []);
}
