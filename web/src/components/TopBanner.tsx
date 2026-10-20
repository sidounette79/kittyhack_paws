import { useEffect, useRef, useState } from "react";
import { fetchJson } from "../lib/api";

type RemoteStatus = "online" | "connecting" | "offline";

const STATUS_COLOR: Record<RemoteStatus, string> = {
  online: "bg-green-400",
  connecting: "bg-amber-400",
  offline: "bg-red-400",
};

const STATUS_TITLE: Record<RemoteStatus, string> = {
  online: "Distant : connecté",
  connecting: "Distant : connexion en cours...",
  offline: "Distant : déconnecté",
};

const ACTION_LABEL: Record<RemoteStatus, string> = {
  online: "Déconnecter",
  connecting: "Reconnecter",
  offline: "Reconnecter",
};

// 07.10, Sid ("on peut remettre mon logo rectangulaire en bannière en haut
// des pages ? centré ? ... s'il prend toute la largeur, probablement que ça
// me va" / "logo, tu peux le faire 2x moins haut" / "pas oublier de
// reprendre quelque part le statut connecté déconnecté qui est dans la
// barre de navigation... des feux dans la bannière, à droite"): logo
// height-bound and centered, the old Shiny navbar's remote-connection
// badge (session_setup.py's ui_remote_connection_badge) now lives here as
// a traffic-light dot on the right instead, with a tap-to-disconnect/
// reconnect action - same spot every migrated page already shares.
export default function TopBanner() {
  const [status, setStatus] = useState<RemoteStatus | null>(null);
  const [busy, setBusy] = useState(false);
  // 11.10, Sid: a real crash night, real confusion - "je pensais voir
  // apparaître un menu déroulant... est-ce que ça fait une action au clic ?
  // Si oui, il faut changer". It did: a single tap called
  // disconnect/reconnect immediately, with a native confirm() guarding only
  // the online->disconnect direction (and not reliably even there, across
  // PC/tablet/the native Android webview). offline->reconnect had zero
  // confirmation at all - exactly the state the dot was in that night.
  // Replaced with the dropdown she actually expected: tap opens a small
  // menu showing the status + an explicit action button, tap elsewhere
  // closes it without doing anything. No native confirm() anywhere now.
  const [menuOpen, setMenuOpen] = useState(false);
  const menuRef = useRef<HTMLDivElement>(null);

  const load = () => {
    fetchJson<{ remote_status: RemoteStatus }>("/app-api/v1/remote/status")
      .then((d) => setStatus(d.remote_status))
      .catch(() => {});
  };

  useEffect(() => {
    load();
    const interval = setInterval(load, 10000);
    return () => clearInterval(interval);
  }, []);

  useEffect(() => {
    if (!menuOpen) return;
    const onClickOutside = (e: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) {
        setMenuOpen(false);
      }
    };
    document.addEventListener("mousedown", onClickOutside);
    return () => document.removeEventListener("mousedown", onClickOutside);
  }, [menuOpen]);

  const runAction = async () => {
    if (!status || busy) return;
    setBusy(true);
    setMenuOpen(false);
    try {
      await fetchJson(status === "online" ? "/app-api/v1/remote/disconnect" : "/app-api/v1/remote/reconnect", {
        method: "POST",
      });
    } catch {
      // best-effort - the next poll reflects whatever actually happened
    } finally {
      setBusy(false);
      load();
    }
  };

  return (
    <div className="relative flex w-full items-center justify-center bg-black/30 py-2">
      <img src="/web-assets/logo-navbar.png" alt="Kittyhack" className="h-10 w-auto" />
      {status && (
        <div ref={menuRef} className="absolute right-3 top-1/2 -translate-y-1/2">
          <button
            onClick={() => setMenuOpen((v) => !v)}
            disabled={busy}
            title={STATUS_TITLE[status]}
            className="flex h-5 w-5 items-center justify-center disabled:opacity-50"
          >
            <span className={`h-2.5 w-2.5 rounded-full ${STATUS_COLOR[status]}`} />
          </button>
          {menuOpen && (
            <div className="absolute right-0 top-full z-50 mt-2 w-48 rounded-lg bg-neutral-800 p-2 text-sm text-white shadow-lg">
              <p className="px-2 pb-2 text-neutral-300">{STATUS_TITLE[status]}</p>
              <button
                onClick={runAction}
                disabled={busy || status === "connecting"}
                className="w-full rounded-md bg-neutral-700 px-2 py-1.5 text-left hover:bg-neutral-600 disabled:opacity-50"
              >
                {ACTION_LABEL[status]}
              </button>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
