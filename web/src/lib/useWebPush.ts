import { useCallback, useEffect, useState } from "react";

// 07.10: React port of www/webpush-client.js's subscribe/unsubscribe flow,
// same backend routes (/webpush/vapid-public-key, /webpush/subscribe,
// /webpush/unsubscribe - these are served by WebPushMiddleware, outside the
// Shiny/React dispatch entirely, so already reachable here with no backend
// changes needed). Relies on the service worker app.js registers at scope
// '/' - already active on this origin once any page (Shiny or React) has
// loaded it once, since a service worker persists per-origin regardless of
// which page registered it.
function urlBase64ToUint8Array(base64String: string): Uint8Array {
  const padding = "=".repeat((4 - (base64String.length % 4)) % 4);
  const base64 = (base64String + padding).replace(/-/g, "+").replace(/_/g, "/");
  const rawData = window.atob(base64);
  const outputArray = new Uint8Array(rawData.length);
  for (let i = 0; i < rawData.length; ++i) {
    outputArray[i] = rawData.charCodeAt(i);
  }
  return outputArray;
}

type PushState = "unsupported" | "subscribed" | "unsubscribed" | "checking";

async function currentState(): Promise<PushState> {
  if (!("serviceWorker" in navigator) || !("PushManager" in window)) return "unsupported";
  const registration = await navigator.serviceWorker.ready;
  const existing = await registration.pushManager.getSubscription();
  return existing ? "subscribed" : "unsubscribed";
}

export function useWebPush() {
  const [state, setState] = useState<PushState>("checking");
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(() => {
    currentState()
      .then(setState)
      .catch(() => setState("unsupported"));
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const enable = useCallback(async () => {
    if (!("serviceWorker" in navigator) || !("PushManager" in window)) {
      alert("Les notifications ne sont pas supportées par ce navigateur.");
      return;
    }
    const permission = await Notification.requestPermission();
    if (permission !== "granted") {
      alert("Permission refusée. Active les notifications pour ce site dans les paramètres du navigateur.");
      return;
    }
    const registration = await navigator.serviceWorker.ready;
    let subscription = await registration.pushManager.getSubscription();
    if (!subscription) {
      const keyResp = await fetch("/webpush/vapid-public-key");
      const { publicKey } = await keyResp.json();
      subscription = await registration.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: urlBase64ToUint8Array(publicKey) as BufferSource,
      });
    }
    await fetch("/webpush/subscribe", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(subscription),
    });
  }, []);

  const disable = useCallback(async () => {
    const registration = await navigator.serviceWorker.ready;
    const subscription = await registration.pushManager.getSubscription();
    if (subscription) {
      await fetch("/webpush/unsubscribe", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ endpoint: subscription.endpoint }),
      });
      await subscription.unsubscribe();
    }
  }, []);

  const toggle = useCallback(async () => {
    setBusy(true);
    try {
      if (state === "subscribed") await disable();
      else await enable();
    } catch (e) {
      console.error("[Webpush] Toggle failed:", e);
    } finally {
      setBusy(false);
      refresh();
    }
  }, [state, enable, disable, refresh]);

  const statusLabel =
    state === "unsupported"
      ? "Non supporté par ce navigateur."
      : state === "subscribed"
        ? "Notifications activées sur cet appareil."
        : state === "unsubscribed"
          ? "Notifications désactivées sur cet appareil."
          : "Vérification du statut...";

  return {
    supported: state !== "unsupported",
    subscribed: state === "subscribed",
    busy,
    statusLabel,
    toggle,
  };
}
