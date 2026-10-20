import { useCallback, useEffect, useState } from "react";
import { useNavigate } from "react-router";
import BottomNav from "../components/BottomNav";
import EventModal from "../components/EventModal";
import EventTimelinePanel, { type TimelineEntry } from "../components/EventTimelinePanel";
import InfoDot from "../components/InfoDot";
import Lightbox from "../components/Lightbox";
import MouseIcon from "../components/MouseIcon";
import TopBanner from "../components/TopBanner";
import { fetchJson } from "../lib/api";
import { shiftDate, todayLocalIso } from "../lib/date";
import { useBackButton } from "../lib/useBackButton";
import { usePageTitle } from "../lib/usePageTitle";

interface Capture {
  filename: string;
  cam: string;
  time: string;
  url: string;
  thumb_url: string;
}

interface EventItem {
  kind: "event";
  block_id: number;
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
  cluster: Capture[];
}

interface SoloGroupItem {
  kind: "solo_group";
  captures: Capture[];
}

type TimelineItem = EventItem | SoloGroupItem;

const DIRECTION_COLOR: Record<string, string> = {
  in: "rgb(134, 239, 172)",
  out: "rgb(252, 165, 165)",
  glance_in: "rgb(253, 224, 71)",
  glance_out: "rgb(253, 224, 71)",
};

function CaptureThumb({
  capture,
  onView,
  onSend,
  onDelete,
  busy,
}: {
  capture: Capture;
  onView: () => void;
  onSend: () => void;
  onDelete: () => void;
  busy: boolean;
}) {
  return (
    <div className="w-[108px] shrink-0">
      <button onClick={onView} className="block w-full">
        <img
          src={capture.thumb_url}
          loading="lazy"
          decoding="async"
          alt=""
          className="h-[108px] w-[108px] rounded-lg object-cover"
        />
      </button>
      <div className="mt-1 truncate text-[0.7rem] text-white/60">
        {capture.cam} · {capture.time}
      </div>
      <div className="mt-1 flex gap-1">
        <button
          onClick={onSend}
          disabled={busy}
          title="Envoyer à Label Studio"
          className="flex-1 rounded bg-white/10 py-1 text-xs hover:bg-white/20 disabled:opacity-40"
        >
          ⬆️
        </button>
        <button
          onClick={onDelete}
          disabled={busy}
          title="Supprimer"
          className="flex-1 rounded bg-red-500/15 py-1 text-xs text-red-300 hover:bg-red-500/25 disabled:opacity-40"
        >
          🗑️
        </button>
      </div>
    </div>
  );
}

export default function JourneyPage() {
  usePageTitle("Parcours");
  const navigate = useNavigate();
  const [date, setDate] = useState(todayLocalIso);
  const [items, setItems] = useState<TimelineItem[] | null>(null);
  const [lightboxUrl, setLightboxUrl] = useState<string | null>(null);
  const [eventModal, setEventModal] = useState<{ blockId: number; mode: "video" | "photo" } | null>(
    null
  );
  const [busyFile, setBusyFile] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [timelines, setTimelines] = useState<Record<number, TimelineEntry[] | null>>({});

  useBackButton(
    () => {
      if (lightboxUrl) {
        setLightboxUrl(null);
        return true;
      }
      if (eventModal) {
        setEventModal(null);
        return true;
      }
      return false;
    },
    () => navigate("/presence")
  );

  const load = useCallback((d: string) => {
    setItems(null);
    fetchJson<{ items: TimelineItem[] }>(`/app-api/v1/journey?date=${d}`)
      .then((data) => setItems(data.items))
      .catch(() => window.location.reload());
  }, []);

  useEffect(() => {
    load(date);
  }, [date, load]);

  const captureAction = async (filename: string, action: "send" | "delete") => {
    setBusyFile(filename);
    setMessage(null);
    try {
      await fetchJson(`/app-api/v1/journey/capture`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ filename, action }),
      });
      if (action === "delete") {
        setItems((prev) =>
          prev
            ? prev
                .map((it) =>
                  it.kind === "event"
                    ? { ...it, cluster: it.cluster.filter((c) => c.filename !== filename) }
                    : { ...it, captures: it.captures.filter((c) => c.filename !== filename) }
                )
                .filter((it) => it.kind === "event" || it.captures.length > 0)
            : prev
        );
        setMessage("Capture supprimée.");
      } else {
        setMessage("Envoyé à Label Studio.");
      }
    } catch (err) {
      setMessage(err instanceof Error ? err.message : "Échec de l'action.");
    } finally {
      setBusyFile(null);
    }
  };

  const toggleTimeline = (blockId: number) => {
    if (blockId in timelines) {
      setTimelines((prev) => {
        const next = { ...prev };
        delete next[blockId];
        return next;
      });
      return;
    }
    setTimelines((prev) => ({ ...prev, [blockId]: null }));
    fetchJson<{ entries: TimelineEntry[] }>(`/app-api/v1/events/${blockId}/timeline`)
      .then((d) => setTimelines((prev) => (blockId in prev ? { ...prev, [blockId]: d.entries } : prev)))
      .catch(() => setTimelines((prev) => (blockId in prev ? { ...prev, [blockId]: [] } : prev)));
  };

  return (
    <>
      <TopBanner />
      <div className="container mx-auto max-w-2xl px-3 py-4">
        <div className="mb-4 flex items-center justify-center gap-2">
          <button
            className="rounded-md border border-white/15 px-3 py-1.5 text-sm hover:bg-white/10"
            onClick={() => setDate((d) => shiftDate(d, -1))}
            aria-label="Jour précédent"
          >
            ‹
          </button>
          <input
            type="date"
            value={date}
            onChange={(e) => setDate(e.target.value)}
            className="rounded-md border border-white/15 bg-transparent px-2 py-1.5 text-sm"
          />
          <button
            className="rounded-md border border-white/15 px-3 py-1.5 text-sm hover:bg-white/10"
            onClick={() => setDate((d) => shiftDate(d, 1))}
            aria-label="Jour suivant"
          >
            ›
          </button>
          <button
            className="ml-2 rounded-md border border-white/15 px-3 py-1.5 text-sm hover:bg-white/10"
            onClick={() => setDate(todayLocalIso())}
          >
            Aujourd'hui
          </button>
        </div>

        {message && <p className="mb-3 text-center text-sm text-white/70">{message}</p>}

        {items === null && <p className="text-center opacity-70">Chargement…</p>}
        {items !== null && items.length === 0 && (
          <p className="text-center opacity-70">Aucune photo trouvée.</p>
        )}

        {items !== null && items.length > 0 && (
          <div className="flex flex-col gap-2">
            {items.map((item, i) =>
              item.kind === "event" ? (
                <div key={`ev-${item.block_id}`} className="rounded-lg bg-white/5 px-3 py-2">
                  <div className="flex items-center gap-2">
                    {/* 08.10, Sid ("l'image générée par Kittyflap... elle
                        n'apparaît plus"): thumb_url was already in the data
                        model (backend sends it) but never actually
                        rendered - dropped somewhere during the React port.
                        Same modal the 📷 button opens, matching the
                        original's own "un lien direct avec les photos...
                        quand je clique, ça envoie vers la photo" intent
                        (04.10 comment in journeys.py). */}
                    <button
                      onClick={() => setEventModal({ blockId: item.block_id, mode: "photo" })}
                      aria-label="Voir la photo"
                      className="shrink-0"
                    >
                      <img
                        src={item.thumb_url}
                        loading="lazy"
                        decoding="async"
                        alt=""
                        className="h-[72px] w-[72px] rounded-lg object-cover"
                      />
                    </button>
                    <div className="min-w-0 flex flex-wrap items-baseline gap-2 text-[0.95rem]">
                      <span className="whitespace-nowrap text-[0.85rem] font-semibold opacity-75">
                        {item.date_label} · {item.time}
                      </span>
                      <span
                        className="font-medium"
                        style={item.direction ? { color: DIRECTION_COLOR[item.direction] } : undefined}
                      >
                        {item.text}
                      </span>
                      {item.cat_name && item.rfid_confirmed === false && (
                        <InfoDot color="bg-amber-400" label="Non confirmé - identité devinée par la caméra, pas une vraie lecture RFID" />
                      )}
                      {item.mouse_probability > 0 && (
                        <span
                          className="inline-flex items-center gap-1 whitespace-nowrap text-[0.85rem]"
                          style={{
                            color:
                              item.mouse_probability >= item.mouse_threshold
                                ? "rgb(255, 90, 90)"
                                : "rgb(252, 165, 165)",
                          }}
                        >
                          <MouseIcon className="h-3.5 w-3.5" />
                          {item.mouse_probability.toFixed(0)}%
                        </span>
                      )}
                      {item.had_skipped_entry && (
                        // 10.10, Sid: same compact-dot treatment as
                        // EventCard.tsx - see its own comment.
                        <InfoDot
                          color="bg-amber-400"
                          label="⚠️ Entrée ignorée (sortie en cours)"
                        />
                      )}
                    </div>
                    <div className="ml-auto flex flex-shrink-0 gap-2">
                      <button
                        className="opacity-80 hover:opacity-100"
                        onClick={() => toggleTimeline(item.block_id)}
                        aria-label="Détails du mouvement"
                        title="Détails du mouvement"
                      >
                        {item.block_id in timelines ? "▾" : "ⓘ"}
                      </button>
                      <button
                        className="opacity-80 hover:opacity-100"
                        onClick={() => setEventModal({ blockId: item.block_id, mode: "video" })}
                        aria-label="Voir la vidéo"
                        title="Voir la vidéo"
                      >
                        🎬
                      </button>
                      <button
                        className="opacity-80 hover:opacity-100"
                        onClick={() => setEventModal({ blockId: item.block_id, mode: "photo" })}
                        aria-label="Voir photo par photo"
                        title="Voir photo par photo"
                      >
                        📷
                      </button>
                    </div>
                  </div>
                  {item.block_id in timelines && (
                    <EventTimelinePanel entries={timelines[item.block_id]} />
                  )}
                  {item.cluster.length > 0 && (
                    <div className="mt-2 flex gap-2 overflow-x-auto overscroll-x-contain pb-1">
                      {item.cluster.map((c) => (
                        <CaptureThumb
                          key={c.filename}
                          capture={c}
                          busy={busyFile === c.filename}
                          onView={() => setLightboxUrl(c.url)}
                          onSend={() => captureAction(c.filename, "send")}
                          onDelete={() => captureAction(c.filename, "delete")}
                        />
                      ))}
                    </div>
                  )}
                </div>
              ) : (
                <div key={`solo-${i}`} className="rounded-lg bg-white/5 px-3 py-2">
                  <div className="mb-2 text-[0.85rem] font-semibold opacity-75">
                    Vu dehors, sans passage par la chatière
                  </div>
                  <div className="flex gap-2 overflow-x-auto overscroll-x-contain pb-1">
                    {item.captures.map((c) => (
                      <CaptureThumb
                        key={c.filename}
                        capture={c}
                        busy={busyFile === c.filename}
                        onView={() => setLightboxUrl(c.url)}
                        onSend={() => captureAction(c.filename, "send")}
                        onDelete={() => captureAction(c.filename, "delete")}
                      />
                    ))}
                  </div>
                </div>
              )
            )}
          </div>
        )}

        <Lightbox url={lightboxUrl} onClose={() => setLightboxUrl(null)} />
        {eventModal && (
          <EventModal
            blockId={eventModal.blockId}
            mode={eventModal.mode}
            onClose={() => setEventModal(null)}
            onDeleted={() => load(date)}
          />
        )}
      </div>
      <BottomNav active="journey" />
    </>
  );
}
