import { useCallback, useEffect, useRef, useState } from "react";
import { Link } from "react-router";
import BottomNav from "../components/BottomNav";
import EventCard, { type ChronologyEvent } from "../components/EventCard";
import EventModal from "../components/EventModal";
import TopBanner from "../components/TopBanner";
import { fetchJson } from "../lib/api";
import { useBackButton } from "../lib/useBackButton";
import { usePageTitle } from "../lib/usePageTitle";

interface DoorState {
  inside_unlocked: boolean | null;
  outside_unlocked: boolean | null;
  inside_unlock_source: "manual" | "auto" | null;
  outside_unlock_source: "manual" | "auto" | null;
  available: boolean;
}

interface CatCard {
  rfid: string | null;
  name: string;
  photo_url: string | null;
  location: "in" | "out" | "unknown";
  since: string | null;
}

// Mirrors presence.py's _format_elapsed() - re-derived client-side from a
// raw ISO timestamp (sent once by the server) so it keeps ticking forward
// without re-fetching, instead of going stale between polls.
function formatElapsed(sinceIso: string | null): string {
  if (!sinceIso) return "Jamais vu";
  const seconds = Math.max(0, (Date.now() - new Date(sinceIso).getTime()) / 1000);
  if (seconds < 60) return "à l'instant";
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `il y a ${minutes} min`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `il y a ${hours} h`;
  const days = Math.floor(hours / 24);
  return `il y a ${days} j`;
}

function DoorCard({
  label,
  unlocked,
  unlockSource,
  remainingSeconds,
  blockedByOther,
  onToggle,
}: {
  label: string;
  unlocked: boolean | null;
  unlockSource: "manual" | "auto" | null;
  remainingSeconds: number | null;
  blockedByOther: boolean;
  onToggle: () => void;
}) {
  // 09.10, Sid ("c'est moi, car j'ai activé le bouton... il faut aussi que
  // ton correctif fasse que le bouton n'est pas cliquable ou qu'il attende
  // que l'autre soit refermé"): the backend now refuses this the same way
  // it already did for the automatic path (see loop.py's manual-override
  // "only one magnet" guards, added after two confirmed Pi crashes caused
  // by opening both magnets at once from these exact buttons) - but a
  // silent backend refusal still looks like "nothing happened" from here.
  // Disabling the OPEN action client-side (never the CLOSE action in the
  // blockedByOther case - closing must always stay possible) makes the
  // real constraint visible instead of just failing quietly.
  const disableOpen = !unlocked && blockedByOther;
  // 09.10, Sid ("verrouillé au clic si l'aimant a été activé par un
  // mouvement de chat ou un RFID"): a magnet opened by the automatic
  // entry/exit flow (a real cat mid-passage) must never be closed by a
  // stray manual click - that's a real physical risk, not just a UX
  // nuisance. A magnet SHE opened manually can still be closed manually.
  const disableClose = !!unlocked && unlockSource === "auto";
  const disabled = disableOpen || disableClose;
  return (
    <div className="flex flex-col gap-3 rounded-xl border border-white/10 bg-white/5 p-4">
      <div>
        <div className="text-lg font-semibold">{label}</div>
        <div className="flex items-center gap-1.5 text-sm text-white/70">
          {unlocked ? "🔓" : "🔒"} {unlocked ? "Déverrouillé" : "Verrouillé"}
        </div>
        {/* 07.10, Sid ("une fois cliqué dessus, ça mette un décompte de
            secondes"): live countdown to the backend's own MAX_UNLOCK_TIME
            auto-relock (60s) - re-derived from a local timestamp, not
            polled, so it ticks smoothly between the 15s status polls. */}
        {unlocked && remainingSeconds !== null && (
          <div className="text-xs text-white/65">
            Se reverrouille dans {remainingSeconds}s
          </div>
        )}
        {disableOpen && (
          <div className="text-xs text-amber-300/80">
            L'autre côté est ouvert - attends qu'il se referme.
          </div>
        )}
        {disableClose && (
          <div className="text-xs text-amber-300/80">
            Ouvert par un passage en cours - attends qu'il se termine.
          </div>
        )}
      </div>
      <button
        onClick={onToggle}
        disabled={disabled}
        className={`rounded-lg py-3 text-sm font-medium disabled:cursor-not-allowed disabled:opacity-40 ${
          unlocked
            ? "bg-red-500/15 text-red-300 hover:bg-red-500/25"
            : "bg-green-500/15 text-green-300 hover:bg-green-500/25"
        }`}
      >
        {unlocked ? "🔒 Verrouiller maintenant" : "🔓 Déverrouiller maintenant"}
      </button>
    </div>
  );
}

function CatCardView({ cat }: { cat: CatCard }) {
  const badge = cat.location === "in" ? "🏠" : cat.location === "out" ? "🌳" : "❓";
  const content = (
    <div className="rounded-xl border border-white/10 bg-white/5 p-4">
      <div className="relative mx-auto h-24 w-24">
        {cat.photo_url ? (
          <img
            src={cat.photo_url}
            alt=""
            className="h-24 w-24 rounded-full object-cover"
          />
        ) : (
          <div className="flex h-24 w-24 items-center justify-center rounded-full bg-white/10 text-3xl">
            🐱
          </div>
        )}
        <span className="absolute bottom-0 right-0 flex h-8 w-8 items-center justify-center rounded-full bg-neutral-900 text-base">
          {badge}
        </span>
      </div>
      <div className="mt-3 text-center">
        <div className="text-lg font-semibold">{cat.name}</div>
        <div className="text-sm text-white/70">
          {cat.location === "in" ? "Intérieur" : cat.location === "out" ? "Extérieur" : "Inconnu"}
        </div>
        <div className="text-xs text-white/65">{formatElapsed(cat.since)}</div>
      </div>
    </div>
  );
  if (!cat.rfid) return content;
  return (
    <Link to={`/presence/cat/${cat.rfid}`} className="block">
      {content}
    </Link>
  );
}

// Mirrors backend/constants.py's MAX_UNLOCK_TIME - the countdown is
// cosmetic (re-derived client-side), the real enforcement is server-side.
const MAX_UNLOCK_TIME_S = 60;

export default function PresencePage() {
  usePageTitle("Chats");
  const [doors, setDoors] = useState<DoorState | null>(null);
  const [cats, setCats] = useState<CatCard[] | null>(null);
  const [, forceTick] = useState(0);
  // 07.10, Sid ("une fois cliqué dessus, ça mette un décompte de
  // secondes"): when each door last became unlocked (client timestamp,
  // ms) - set on our own toggle AND on noticing a door already unlocked
  // via a poll (e.g. an automatic entry), so the countdown shows up
  // either way, not just for clicks made from this page.
  const unlockedSinceRef = useRef<{ inside: number | null; outside: number | null }>({
    inside: null,
    outside: null,
  });
  const prevDoorsRef = useRef<DoorState | null>(null);

  // Expired session cookie: WebAuthMiddleware serves the HTML login page
  // body instead of JSON even here - a real reload lets it show that login
  // form properly. Only forced on the FIRST load (loadedOnce still false);
  // a failed background poll just skips that cycle instead of yanking the
  // page out from under her over one transient blip.
  const loadedOnceRef = useRef(false);
  const loadDoors = useCallback(() => {
    fetchJson<DoorState>("/app-api/v1/presence/doors")
      .then(setDoors)
      .catch(() => {
        if (!loadedOnceRef.current) window.location.reload();
      });
  }, []);
  const loadCats = useCallback(() => {
    fetchJson<{ cats: CatCard[] }>("/app-api/v1/presence/cats")
      .then((d) => {
        loadedOnceRef.current = true;
        setCats(d.cats);
      })
      .catch(() => {
        if (!loadedOnceRef.current) window.location.reload();
      });
  }, []);

  // 09.10, Sid ("page chats, en dessous des 4 chips des chats, les 50
  // derniers événements... et on ne garde pas la page chronologie"): same
  // data/serialization as the old Chronology page, just not scoped to one
  // calendar day - polled alongside doors/cats below so a real passage
  // shows up here without a manual refresh.
  const [recentEvents, setRecentEvents] = useState<ChronologyEvent[] | null>(null);
  const [eventModal, setEventModal] = useState<{ blockId: number; mode: "video" | "photo" } | null>(
    null
  );
  const loadRecentEvents = useCallback(() => {
    fetchJson<{ events: ChronologyEvent[] }>("/app-api/v1/chronology/recent?limit=50")
      .then((d) => setRecentEvents(d.events))
      .catch(() => {
        // Best-effort - a failed poll just keeps showing the last good list.
      });
  }, []);

  // This is the app's own home page - no "back" destination above it, so
  // the only thing to intercept is closing the event modal; falling back
  // to the OS default (minimize/exit) otherwise, same as before this page
  // had any modal to manage.
  useBackButton(
    () => {
      if (eventModal) {
        setEventModal(null);
        return true;
      }
      return false;
    },
    () => {
      (window as any).Capacitor?.Plugins?.App?.exitApp?.();
    }
  );

  useEffect(() => {
    loadDoors();
    loadCats();
    loadRecentEvents();
    // Cat statuses/door state can change from a real passage at any time,
    // not just from this page's own actions - poll gently rather than
    // leaving a stale "Intérieur depuis 2h" up after the cat already left.
    const interval = setInterval(() => {
      loadDoors();
      loadCats();
      loadRecentEvents();
    }, 15000);
    // Re-render every second so the unlock countdown ticks smoothly between
    // the 15s polls above (also keeps formatElapsed()'s relative text fresh).
    const tick = setInterval(() => forceTick((n) => n + 1), 1000);
    return () => {
      clearInterval(interval);
      clearInterval(tick);
    };
  }, [loadDoors, loadCats, loadRecentEvents]);

  // Tracks locked->unlocked transitions (from our own click OR a poll
  // noticing an automatic unlock) to start the countdown, and clears it on
  // any transition back to locked.
  useEffect(() => {
    const prev = prevDoorsRef.current;
    if (doors) {
      if (doors.inside_unlocked && !prev?.inside_unlocked) {
        unlockedSinceRef.current.inside = Date.now();
      } else if (!doors.inside_unlocked) {
        unlockedSinceRef.current.inside = null;
      }
      if (doors.outside_unlocked && !prev?.outside_unlocked) {
        unlockedSinceRef.current.outside = Date.now();
      } else if (!doors.outside_unlocked) {
        unlockedSinceRef.current.outside = null;
      }
    }
    prevDoorsRef.current = doors;
  }, [doors]);

  const toggleDoor = async (key: string) => {
    try {
      const updated = await fetchJson<DoorState>(`/app-api/v1/presence/doors/${key}`, {
        method: "POST",
      });
      setDoors(updated);
    } catch {
      loadDoors();
    }
  };

  const remainingSeconds = (side: "inside" | "outside") => {
    const since = unlockedSinceRef.current[side];
    if (!since) return null;
    return Math.max(0, MAX_UNLOCK_TIME_S - Math.round((Date.now() - since) / 1000));
  };

  return (
    <>
      <TopBanner />
      <div className="container mx-auto max-w-2xl px-3 py-4">
        {doors && (
          <div className="mb-4 grid grid-cols-2 gap-3">
            <DoorCard
              label="Intérieur"
              unlocked={doors.inside_unlocked}
              unlockSource={doors.inside_unlock_source}
              remainingSeconds={remainingSeconds("inside")}
              blockedByOther={!!doors.outside_unlocked}
              onToggle={() => toggleDoor(doors.inside_unlocked ? "lock_inside" : "unlock_inside")}
            />
            <DoorCard
              label="Extérieur"
              unlocked={doors.outside_unlocked}
              unlockSource={doors.outside_unlock_source}
              remainingSeconds={remainingSeconds("outside")}
              blockedByOther={!!doors.inside_unlocked}
              onToggle={() => toggleDoor(doors.outside_unlocked ? "lock_outside" : "unlock_outside")}
            />
          </div>
        )}

        {cats === null && <p className="text-center opacity-70">Chargement…</p>}
        {cats !== null && cats.length === 0 && (
          <p className="text-center opacity-70">Aucun chat enregistré.</p>
        )}
        {cats !== null && cats.length > 0 && (
          <div className="grid grid-cols-2 gap-3">
            {cats.map((cat) => (
              <CatCardView key={cat.rfid ?? cat.name} cat={cat} />
            ))}
          </div>
        )}

        <div className="mt-5">
          <h2 className="mb-2 text-sm font-semibold uppercase tracking-wide text-white/65">
            Derniers événements
          </h2>
          {recentEvents === null && <p className="text-center opacity-70">Chargement…</p>}
          {recentEvents !== null && recentEvents.length === 0 && (
            <p className="text-center opacity-70">Aucun événement.</p>
          )}
          {recentEvents !== null && recentEvents.length > 0 && (
            <div className="flex flex-col gap-1">
              {recentEvents.map((ev) => (
                <EventCard
                  key={ev.block_id}
                  ev={ev}
                  onOpenModal={(mode) => setEventModal({ blockId: ev.block_id, mode })}
                />
              ))}
            </div>
          )}
          <Link
            to="/journey/"
            className="mt-2 block text-center text-sm text-white/60 hover:text-white/90"
          >
            Voir plus de détails dans Parcours →
          </Link>
        </div>
      </div>

      {eventModal && (
        <EventModal
          blockId={eventModal.blockId}
          mode={eventModal.mode}
          onClose={() => setEventModal(null)}
        />
      )}

      <BottomNav active="presence" />
    </>
  );
}
