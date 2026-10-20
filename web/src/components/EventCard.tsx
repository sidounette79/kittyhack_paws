import { useState } from "react";
import EventTimelinePanel, { type TimelineEntry } from "./EventTimelinePanel";
import InfoDot from "./InfoDot";
import MouseIcon from "./MouseIcon";
import { fetchJson } from "../lib/api";

// 09.10, Sid ("page chats, en dessous des 4 chips des chats, les 50
// derniers événements (repris comme ils sont dans la page chronologie)...
// et on ne garde pas la page chronologie"): extracted from Chronology.tsx
// so both it (kept on disk, just unlinked from nav - reversible) and the
// new recent-events block on Presence.tsx render identically without
// duplicating the JSX.
//
// The video/photo modal is NOT owned here, on purpose: useBackButton
// (every migrated page's native-back handling) registers one Capacitor
// listener per call - with up to 50 of these on one page, each owning its
// own useBackButton would fire all 50 handlers on a single back press
// (49 "nothing open here" fallbacks navigating away at once). The parent
// owns the single eventModal state + the one useBackButton + the one
// <EventModal>, same pattern Chronology.tsx already had; this component
// just reports which card wants it opened.
export interface ChronologyEvent {
  block_id: number;
  photo_id: number;
  time: string;
  date_label: string;
  cat_name: string | null;
  direction: "in" | "out" | "glance_in" | "glance_out" | null;
  text: string;
  thumb_url: string;
  mouse_probability: number;
  mouse_threshold: number;
  had_skipped_entry: boolean;
  rfid_confirmed: boolean | null;
}

// Same green/red/amber convention as www/styles.css's .kh-dir-in/.kh-dir-out/
// .kh-dir-glance (dark-mode variants - these pages are always dark).
const DIRECTION_COLOR: Record<string, string> = {
  in: "rgb(134, 239, 172)",
  out: "rgb(252, 165, 165)",
  glance_in: "rgb(253, 224, 71)",
  glance_out: "rgb(253, 224, 71)",
};

export default function EventCard({
  ev,
  onOpenModal,
}: {
  ev: ChronologyEvent;
  onOpenModal: (mode: "video" | "photo") => void;
}) {
  const [timelineEntries, setTimelineEntries] = useState<TimelineEntry[] | null | undefined>(undefined);

  const toggleTimeline = () => {
    if (timelineEntries !== undefined) {
      setTimelineEntries(undefined);
      return;
    }
    setTimelineEntries(null);
    fetchJson<{ entries: TimelineEntry[] }>(`/app-api/v1/events/${ev.block_id}/timeline`)
      .then((d) => setTimelineEntries(d.entries))
      .catch(() => setTimelineEntries([]));
  };

  return (
    <div className="rounded-lg bg-white/5 px-3 py-2">
      <div className="flex items-center gap-2">
        <div className="min-w-0 flex flex-wrap items-baseline gap-2 text-[0.95rem]">
          <span className="whitespace-nowrap text-[0.85rem] font-semibold opacity-75">
            {ev.date_label} · {ev.time}
          </span>
          <span
            className="font-medium"
            style={ev.direction ? { color: DIRECTION_COLOR[ev.direction] } : undefined}
          >
            {ev.text}
          </span>
          {ev.cat_name && ev.rfid_confirmed === false && (
            <InfoDot color="bg-amber-400" label="Non confirmé - identité devinée par la caméra, pas une vraie lecture RFID" />
          )}
          {ev.mouse_probability > 0 && (
            <span
              className="inline-flex items-center gap-1 whitespace-nowrap text-[0.85rem]"
              style={{
                color:
                  ev.mouse_probability >= ev.mouse_threshold
                    ? "rgb(255, 90, 90)"
                    : "rgb(252, 165, 165)",
              }}
            >
              <MouseIcon className="h-3.5 w-3.5" />
              {ev.mouse_probability.toFixed(0)}%
            </span>
          )}
          {ev.had_skipped_entry && (
            // 10.10, Sid ("au lieu du texte complet... un petit rond de
            // couleur... avec le texte qui s'affiche si on clique"): was a
            // long nowrap-prone badge (see 09.10 fix below on why that
            // caused real layout bugs) - now a compact dot, same pattern
            // as TopBanner's connection indicator.
            <InfoDot
              color="bg-amber-400"
              label="⚠️ Entrée ignorée (sortie en cours)"
            />
          )}
        </div>
        <div className="ml-auto flex flex-shrink-0 gap-2">
          <button
            className="opacity-80 hover:opacity-100"
            onClick={toggleTimeline}
            aria-label="Détails du mouvement"
            title="Détails du mouvement"
          >
            {timelineEntries !== undefined ? "▾" : "ⓘ"}
          </button>
          <button
            className="opacity-80 hover:opacity-100"
            onClick={() => onOpenModal("video")}
            aria-label="Voir la vidéo"
            title="Voir la vidéo"
          >
            🎬
          </button>
          <button
            className="opacity-80 hover:opacity-100"
            onClick={() => onOpenModal("photo")}
            aria-label="Voir photo par photo"
            title="Voir photo par photo"
          >
            📷
          </button>
        </div>
      </div>
      {timelineEntries !== undefined && <EventTimelinePanel entries={timelineEntries} />}
    </div>
  );
}
