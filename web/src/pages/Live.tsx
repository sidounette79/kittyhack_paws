import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router";
import BottomNav from "../components/BottomNav";
import TopBanner from "../components/TopBanner";
import { fetchJson } from "../lib/api";
import { usePageTitle } from "../lib/usePageTitle";

interface LastEvent {
  block_id: number;
  time: string;
  date_label: string;
  cat_name: string | null;
  text: string;
  thumb_url: string;
}

interface LiveStatus {
  ok: boolean;
  inside_lock?: boolean;
  outside_lock?: boolean;
  inside_motion?: boolean;
  outside_motion?: boolean;
  forced_lock_due_prey?: boolean;
  time_until_release?: number;
  remote_status?: "online" | "connecting" | "offline";
}

interface ModeState {
  entry: string;
  exit: string;
}

const ENTRY_OPTIONS: { value: string; label: string }[] = [
  { value: "all", label: "Tous les chats (à chaque mouvement détecté)" },
  { value: "all_rfids", label: "Tous les chats avec puce RFID" },
  { value: "known", label: "Chats enregistrés uniquement" },
  { value: "none", label: "Aucun chat" },
  { value: "configure_per_cat", label: "Configuration individuelle par chat" },
];

const EXIT_OPTIONS: { value: string; label: string }[] = [
  { value: "allow", label: "Autoriser la sortie" },
  { value: "deny", label: "Ne pas autoriser la sortie" },
  { value: "configure_per_cat", label: "Configuration individuelle par chat" },
];

function StatusPill({
  icon,
  active,
  title,
}: {
  icon: string;
  active: boolean;
  title: string;
}) {
  return (
    <span
      title={title}
      aria-label={title}
      className={`inline-flex h-7 w-7 items-center justify-center rounded-full text-sm ${
        active ? "bg-white/25" : "bg-white/10 text-white/50"
      }`}
    >
      {icon}
    </span>
  );
}

// 10.10, Sid ("les deux lives côte à côte... on supprime la petite image
// qui est en dessous?"): "chatiere" moved up into its own big panel next
// to the flap's own camera (below) - removed here so it isn't duplicated.
const OUTDOOR_CAMS: { name: string; label: string }[] = [
  // 09.10, Sid ("il faut rajouter l'ancienne chatière... devenue chatière
  // Int"): original flap camera, now facing the flap from indoors.
  { name: "chatiere_int", label: "Chatière int" },
  { name: "terrasse", label: "Terrasse" },
  { name: "jardin_japonais", label: "Jardin japonais" },
  { name: "entree", label: "Entrée" },
];

// 10.10, Sid ("il passe à la bonne date, puis il revient au 09.10"): a
// plain `<img src={url}>` poll has no ordering guarantee - under NAS load
// a slower *earlier* request can resolve *after* a faster later one,
// silently overwriting the fresh frame with a stale one on load. Fetches
// the bytes ourselves and only commits them if no newer request has been
// issued since - a late straggler response is simply dropped instead of
// being allowed to render.
function useRaceSafeImage(url: string): string | null {
  const [objUrl, setObjUrl] = useState<string | null>(null);
  const latestUrlRef = useRef<string>("");
  const prevObjUrlRef = useRef<string | null>(null);

  useEffect(() => {
    latestUrlRef.current = url;
    let cancelled = false;
    fetch(url)
      .then((r) => {
        if (!r.ok) throw new Error(String(r.status));
        return r.blob();
      })
      .then((blob) => {
        if (cancelled || latestUrlRef.current !== url) return;
        const next = URL.createObjectURL(blob);
        setObjUrl(next);
        if (prevObjUrlRef.current) URL.revokeObjectURL(prevObjUrlRef.current);
        prevObjUrlRef.current = next;
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [url]);

  return objUrl;
}

export default function LivePage() {
  usePageTitle("Live");
  const navigate = useNavigate();
  const [status, setStatus] = useState<LiveStatus | null>(null);
  const [mode, setMode] = useState<ModeState | null>(null);
  // 10.10, Sid ("ça reste au 09.10"): Date.now() even for the initial
  // value - starting both at a fixed 0 meant the very FIRST image request
  // on every single page load was always the same predictable `?t=0` URL,
  // same risk of matching an old cached entry as the interval tick itself.
  const [frameTick, setFrameTick] = useState(() => Date.now());
  const [wdTick, setWdTick] = useState(() => Date.now());
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [lastEvent, setLastEvent] = useState<LastEvent | null>(null);
  const imgRef = useRef<HTMLImageElement | null>(null);
  const chatiereUrl = useRaceSafeImage(`/watchdog-frame/chatiere.jpg?t=${wdTick}`);

  useEffect(() => {
    fetchJson<ModeState>("/app-api/v1/live/mode").then(setMode).catch(() => {});
  }, []);

  // 07.10, Sid ("j'aimais bien la petite ligne du dernier événement ou je
  // pouvais direct aller dessus ou qui me renvoyait à la page parcours"):
  // same teaser live_view.py's ui_last_events_teaser() showed (last motion
  // block, block_count=1), clicking it used to jump into the grouped
  // events view - here that's the Journey page instead.
  useEffect(() => {
    const loadLastEvent = () =>
      fetchJson<{ event: LastEvent | null }>("/app-api/v1/live/last-event")
        .then((d) => setLastEvent(d.event))
        .catch(() => {});
    loadLastEvent();
    const interval = setInterval(loadLastEvent, 15000);
    return () => clearInterval(interval);
  }, []);

  useEffect(() => {
    const loadStatus = () =>
      fetchJson<LiveStatus>("/app-api/v1/live/status").then(setStatus).catch(() => {});
    loadStatus();
    const statusInterval = setInterval(loadStatus, 1000);
    // 10.10, Sid ("ça reste au 09.10" - real root cause, confirmed live via
    // the Network panel: `?t=0,1,2,3...` restarts from 0 on every page
    // load, so `?t=1` is the literal same URL requested dozens of times
    // across tonight's testing - Chrome served a disk-cached response from
    // BEFORE the `Cache-Control: no-store` fix landed, for that exact URL,
    // skipping the network entirely (no-store on the *new* response can't
    // retroactively un-cache an *old* one already stored under the same
    // URL). Date.now() guarantees every request this tab ever makes is a
    // brand new URL, so no past cached entry can ever match again.
    const frameInterval = setInterval(() => setFrameTick(Date.now()), 2000);
    const wdInterval = setInterval(() => setWdTick(Date.now()), 5000);
    return () => {
      clearInterval(statusInterval);
      clearInterval(frameInterval);
      clearInterval(wdInterval);
    };
  }, []);

  const toggleInside = async () => {
    setBusy(true);
    try {
      await fetchJson("/app-api/v1/live/toggle-inside", { method: "POST" });
    } catch {
      // best-effort - next status poll will reflect whatever actually happened
    } finally {
      setBusy(false);
    }
  };

  const resetPreyCooldown = async () => {
    setBusy(true);
    try {
      await fetchJson("/app-api/v1/live/reset-prey-cooldown", { method: "POST" });
      setMessage("Délai de proie réinitialisé.");
    } catch {
      setMessage("Échec de la réinitialisation.");
    } finally {
      setBusy(false);
    }
  };

  const updateMode = async (patch: Partial<ModeState>) => {
    const next = { ...(mode ?? { entry: "known", exit: "allow" }), ...patch };
    setMode(next);
    try {
      const result = await fetchJson<ModeState>("/app-api/v1/live/mode", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(patch),
      });
      setMode(result);
    } catch {
      setMessage("Échec de la mise à jour du mode.");
    }
  };

  return (
    <>
      <TopBanner />
      <div className="container mx-auto max-w-2xl px-3 py-4">
        {/* 10.10, Sid ("les deux lives côte à côte? en flux réel?"): both
            panels poll an already-continuously-captured frame (the backend's
            own detection pipeline for the internal camera, watchdog.py's
            own capture cadence for chatiere) - showing them bigger/more
            often here doesn't trigger any extra capture or inference,
            just serves bytes that already exist. Stacked on mobile (her
            primary device), side by side from sm: up. */}
        <div className="mb-4 flex flex-col gap-2 sm:flex-row">
        <div className="relative h-64 flex-1 overflow-hidden rounded-xl bg-black">
          <img
            ref={imgRef}
            src={`/app-api/v1/live/frame.jpg?t=${frameTick}`}
            alt=""
            className="h-full w-full object-cover"
            onError={(e) => {
              (e.target as HTMLImageElement).style.opacity = "0.3";
            }}
            onLoad={(e) => {
              (e.target as HTMLImageElement).style.opacity = "1";
            }}
          />

          {/* 07.10, Sid ("y a plus le petit point de couleur qui dit si la
              chatière est en ligne ou pas" / "rouge = déconnecté et orange =
              en cours de connexion ?"): same connection source as the old
              Shiny navbar's remote badge (session_setup.py's
              ui_remote_connection_badge), just a plain dot here instead of
              a text pill - three states instead of two, since the client
              genuinely distinguishes a deliberate disconnect from still
              retrying in the background. */}
          {status?.remote_status && (
            <div
              className="absolute left-2 top-2 flex items-center gap-1.5 rounded-full bg-black/60 px-2 py-1 text-xs text-white/80"
              title={
                status.remote_status === "online"
                  ? "Chatière en ligne"
                  : status.remote_status === "connecting"
                    ? "Connexion à la chatière en cours..."
                    : "Chatière hors ligne"
              }
            >
              <span
                className={`h-2 w-2 rounded-full ${
                  status.remote_status === "online"
                    ? "bg-green-400"
                    : status.remote_status === "connecting"
                      ? "bg-amber-400"
                      : "bg-red-400"
                }`}
              />
              {status.remote_status === "online"
                ? "En ligne"
                : status.remote_status === "connecting"
                  ? "Connexion..."
                  : "Hors ligne"}
            </div>
          )}

          {status?.forced_lock_due_prey && (
            <div className="absolute inset-x-2 top-2 rounded-md bg-amber-500/90 px-2 py-1 text-center text-xs font-medium text-black">
              Proie détectée — intérieur bloqué encore {Math.ceil(status.time_until_release ?? 0)}s
            </div>
          )}

          {status?.ok && (
            <div className="absolute inset-x-2 bottom-2 flex justify-between gap-2">
              <div className="flex items-center gap-1 rounded-full bg-black/60 px-2 py-1">
                <span className="text-xs text-white/70">Intérieur</span>
                <StatusPill
                  icon={status.inside_lock ? "🔓" : "🔒"}
                  active={!!status.inside_lock}
                  title={status.inside_lock ? "Intérieur : déverrouillé" : "Intérieur : verrouillé"}
                />
                <StatusPill
                  icon={status.inside_motion ? "👁️" : "🚫"}
                  active={!!status.inside_motion}
                  title={status.inside_motion ? "Mouvement intérieur" : "Pas de mouvement intérieur"}
                />
              </div>
              <div className="flex items-center gap-1 rounded-full bg-black/60 px-2 py-1">
                <span className="text-xs text-white/70">Extérieur</span>
                <StatusPill
                  icon={status.outside_lock ? "🔓" : "🔒"}
                  active={!!status.outside_lock}
                  title={status.outside_lock ? "Extérieur : déverrouillé" : "Extérieur : verrouillé"}
                />
                <StatusPill
                  icon={status.outside_motion ? "👁️" : "🚫"}
                  active={!!status.outside_motion}
                  title={status.outside_motion ? "Mouvement extérieur" : "Pas de mouvement extérieur"}
                />
              </div>
            </div>
          )}
        </div>

        <div className="relative h-64 flex-1 overflow-hidden rounded-xl bg-black">
          {chatiereUrl && (
            <img src={chatiereUrl} alt="Chatière ext" className="h-full w-full object-cover" />
          )}
          {/* 10.10, Sid ("de la même taille... en puce semi-transparente"):
              same pill style/position as the "En ligne" badge on the main
              panel above, instead of the old full-width bottom bar. */}
          <div className="absolute left-2 top-2 rounded-full bg-black/60 px-2 py-1 text-xs text-white/80">
            Chatière ext (Reolink)
          </div>
        </div>
        </div>

        {lastEvent && (
          <button
            onClick={() => navigate("/journey")}
            className="mb-4 flex w-full items-center gap-3 rounded-xl border border-white/10 bg-white/5 p-2 text-left hover:bg-white/10"
          >
            <img
              src={lastEvent.thumb_url}
              alt=""
              className="h-12 w-12 shrink-0 rounded-lg object-cover"
            />
            <div className="min-w-0 flex-1">
              <div className="truncate text-sm">{lastEvent.text}</div>
              <div className="text-xs text-white/65">
                {lastEvent.date_label} à {lastEvent.time}
              </div>
            </div>
            <span className="text-white/40">›</span>
          </button>
        )}

        <div className="mb-4 grid grid-cols-2 gap-2">
          <button
            onClick={toggleInside}
            disabled={busy || !status?.ok}
            className="rounded-lg bg-white/10 py-3 text-sm font-medium hover:bg-white/20 disabled:opacity-40"
          >
            {status?.inside_lock ? "🔒 Fermer l'intérieur" : "🔓 Laisser entrer maintenant"}
          </button>
          <button
            onClick={resetPreyCooldown}
            disabled={busy}
            className="rounded-lg bg-white/10 py-3 text-sm font-medium hover:bg-white/20 disabled:opacity-40"
          >
            ⏱️ Réinitialiser le délai proie
          </button>
        </div>

        {message && <p className="mb-3 text-center text-sm text-white/70">{message}</p>}

        <div className="mb-4 rounded-xl border border-white/10 bg-white/5 p-4">
          <div className="mb-2 text-center text-sm font-semibold text-white/80">
            Réglages rapides
          </div>
          <label className="mb-2 block text-sm">
            <span className="mb-1 block text-white/60">Direction entrée</span>
            <select
              value={mode?.entry ?? "known"}
              onChange={(e) => updateMode({ entry: e.target.value })}
              className="w-full rounded-md border border-white/15 bg-neutral-900 px-2 py-2"
            >
              {ENTRY_OPTIONS.map((o) => (
                <option key={o.value} value={o.value}>
                  {o.label}
                </option>
              ))}
            </select>
          </label>
          <label className="block text-sm">
            <span className="mb-1 block text-white/60">Direction sortie</span>
            <select
              value={mode?.exit ?? "allow"}
              onChange={(e) => updateMode({ exit: e.target.value })}
              className="w-full rounded-md border border-white/15 bg-neutral-900 px-2 py-2"
            >
              {EXIT_OPTIONS.map((o) => (
                <option key={o.value} value={o.value}>
                  {o.label}
                </option>
              ))}
            </select>
          </label>
        </div>

        <div className="mb-2 text-center text-sm font-semibold text-white/80">
          Caméras extérieures
        </div>
        <div className="mb-4 grid grid-cols-2 gap-2">
          {OUTDOOR_CAMS.map((cam) => (
            <div key={cam.name} className="overflow-hidden rounded-lg bg-black">
              <img
                src={`/watchdog-frame/${cam.name}.jpg?t=${wdTick}`}
                alt={cam.label}
                className="aspect-video w-full object-cover"
              />
              <div className="bg-white/5 px-2 py-1 text-center text-xs text-white/60">
                {cam.label}
              </div>
            </div>
          ))}
        </div>
        <p className="mb-4 text-center text-xs text-white/60">
          Aperçu uniquement, rafraîchi toutes les quelques secondes — pas un flux vidéo continu.
        </p>
      </div>
      <BottomNav active="live-view" />
    </>
  );
}
