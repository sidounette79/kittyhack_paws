import { useCallback, useEffect, useRef, useState } from "react";
import BottomNav from "../components/BottomNav";
import TopBanner from "../components/TopBanner";
import { fetchJson } from "../lib/api";
import { usePageTitle } from "../lib/usePageTitle";

interface ActivityEntry {
  at: string;
  time: string;
  message: string;
}

interface ActivityResponse {
  entries: ActivityEntry[];
}

const POLL_MS = 5000;

// 30.10, Sid ("je veux qu'on cree une page ou je vois tout ce qui est
// détecté tout le temps. Pas les videos ni les photos mais je veux voir
// les Mouvement detecte / Ouverture / Etc") - the raw backend-decision
// log as a live feed (same lines already shown per-event in Journey's
// expandable detail, see EventModal), newest first, auto-refreshing.
// No per-capture grouping, no photo/video - just the text trail.
function dateLabelFor(at: string): string {
  try {
    const d = new Date(at);
    if (Number.isNaN(d.getTime())) return "";
    return d.toLocaleDateString("fr-CH", { day: "2-digit", month: "2-digit" });
  } catch {
    return "";
  }
}

export default function ActivityPage() {
  usePageTitle("Activité");
  const [entries, setEntries] = useState<ActivityEntry[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [paused, setPaused] = useState(false);
  const pausedRef = useRef(paused);
  pausedRef.current = paused;

  const load = useCallback(async () => {
    if (pausedRef.current) return;
    try {
      const data = await fetchJson<ActivityResponse>("/app-api/v1/activity/recent?limit=300");
      setEntries(data.entries);
      setError(null);
    } catch {
      setError("Impossible de charger le flux d'activité.");
    }
  }, []);

  useEffect(() => {
    load();
    const id = window.setInterval(load, POLL_MS);
    return () => window.clearInterval(id);
  }, [load]);

  // Groups consecutive entries under the same date header so a feed
  // spanning several days (quiet night, nothing new since yesterday)
  // still reads clearly instead of just a flat list of bare HH:MM:SS.
  let lastDateLabel = "";

  return (
    <>
      <TopBanner />
      <div className="container mx-auto max-w-2xl px-3 py-4">
        <div className="mb-3 flex items-center justify-between">
          <h1 className="text-lg font-semibold text-white/90">Activité</h1>
          <button
            type="button"
            onClick={() => setPaused((p) => !p)}
            className={`rounded-full px-3 py-1 text-xs font-medium ${
              paused ? "bg-amber-500/90 text-black" : "bg-white/10 text-white/70"
            }`}
          >
            {paused ? "⏸ En pause" : "● En direct"}
          </button>
        </div>
        <p className="mb-4 text-xs text-white/50">
          Tout ce que le backend détecte et décide, en continu — mouvements, lecture RFID,
          ouverture/fermeture des portes, décisions d'entrée. Pas les photos ni les vidéos,
          actualisé toutes les {Math.round(POLL_MS / 1000)}s.
        </p>

        {error && (
          <p className="mb-3 rounded-lg bg-red-500/20 px-3 py-2 text-sm text-red-200">{error}</p>
        )}

        {entries === null && !error && (
          <p className="text-center text-sm text-white/50">Chargement...</p>
        )}

        {entries !== null && entries.length === 0 && (
          <p className="text-center text-sm text-white/50">Rien à afficher pour l'instant.</p>
        )}

        {entries !== null && entries.length > 0 && (
          <div className="overflow-hidden rounded-xl bg-white/5">
            {entries.map((e, i) => {
              const label = dateLabelFor(e.at);
              const showDateHeader = label !== "" && label !== lastDateLabel;
              lastDateLabel = label;
              return (
                <div key={`${e.at}-${i}`}>
                  {showDateHeader && (
                    <div className="bg-black/30 px-3 py-1 text-xs font-medium uppercase tracking-wide text-white/50">
                      {label}
                    </div>
                  )}
                  <div className="flex gap-3 border-t border-white/5 px-3 py-2 text-sm first:border-t-0">
                    <span className="shrink-0 font-mono text-white/40">{e.time}</span>
                    <span className="text-white/80">{e.message}</span>
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </div>
      <BottomNav active="activity" />
    </>
  );
}
