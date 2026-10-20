import { useCallback, useEffect, useState } from "react";
import { useNavigate } from "react-router";
import BottomNav from "../components/BottomNav";
import EventModal from "../components/EventModal";
import PictureDetail, { type Picture } from "../components/PictureDetail";
import TopBanner from "../components/TopBanner";
import { fetchJson } from "../lib/api";
import { shiftDate, todayLocalIso } from "../lib/date";
import { useBackButton } from "../lib/useBackButton";
import { usePageTitle } from "../lib/usePageTitle";

interface PicturesResponse {
  date: string;
  page: number;
  total_pages: number;
  total_count: number;
  photos: Picture[];
  correction_labels: string[];
}

interface CatOption {
  rfid: string;
  name: string;
}

interface GroupedEvent {
  block_id: number;
  photo_id: number;
  time: string;
  date_label: string;
  cat_name: string | null;
  direction: "in" | "out" | "glance_in" | "glance_out" | null;
  text: string;
  thumb_url: string;
}

const DIRECTION_COLOR: Record<string, string> = {
  in: "rgb(134, 239, 172)",
  out: "rgb(252, 165, 165)",
  glance_in: "rgb(253, 224, 71)",
  glance_out: "rgb(253, 224, 71)",
};

function objectColor(obj: Picture["objects"][number], mouseThreshold: number): string {
  const isPrey = obj.name.toLowerCase() === "prey" || obj.name.toLowerCase() === "beute";
  const preyStrong = isPrey && obj.prob >= mouseThreshold;
  if (isPrey && !preyStrong) return "rgb(252, 165, 165)";
  if (isPrey) return "rgb(255, 0, 0)";
  return "rgb(0, 180, 0)";
}

// 07.10, Sid ("vas-y pour les photos"): grid of individual photos, with
// date/cat/cats-only/mice-only filters and the same reverse pagination as
// the old Shiny page. Tapping a card opens PictureDetail for correction or
// manually tagging a missed detection.
// 08.10, Sid ("tu as supprimé le mode groupé? il était assez utile"): a
// "Grouper par événements" toggle re-added on top of that - same
// date/cats-only/mice-only/cat filters, but swaps the flat grid for a
// Chronologie-style event list (/app-api/v1/pictures/events). Not simply
// redundant with Chronologie/Parcours since neither of those offer the
// cats-only/mice-only/per-cat filtering this page's own filter bar does.
export default function PhotosPage() {
  usePageTitle("Photos");
  const navigate = useNavigate();
  const [date, setDate] = useState(todayLocalIso);
  const [page, setPage] = useState(1);
  const [catsOnly, setCatsOnly] = useState(false);
  const [miceOnly, setMiceOnly] = useState(false);
  const [showOverlay, setShowOverlay] = useState(true);
  const [rfidFilter, setRfidFilter] = useState("");
  const [cats, setCats] = useState<CatOption[]>([]);
  const [data, setData] = useState<PicturesResponse | null>(null);
  // 08.10, Sid ("je peux avoir des chevrons pour passer à celle d'après ou
  // d'avant sans fermer la photo ?"): track the open photo by its index in
  // the current page's array rather than the Picture object itself, so
  // prev/next just moves the index - no need to re-find anything.
  const [openIndex, setOpenIndex] = useState<number | null>(null);
  const openPicture = openIndex !== null ? (data?.photos[openIndex] ?? null) : null;

  // 08.10, Sid ("tu as supprimé le mode groupé? il était assez utile
  // pourtant"): the Shiny original's "Group pictures to events" switch,
  // re-added - same date/cats-only/mice-only/cat filters as the flat grid
  // above, just grouped by motion block instead of one card per photo.
  const [groupedView, setGroupedView] = useState(false);
  const [events, setEvents] = useState<GroupedEvent[] | null>(null);
  const [eventModal, setEventModal] = useState<{ blockId: number; mode: "video" | "photo" } | null>(
    null
  );

  useBackButton(
    () => {
      if (eventModal) {
        setEventModal(null);
        return true;
      }
      if (openIndex !== null) {
        setOpenIndex(null);
        return true;
      }
      return false;
    },
    () => navigate("/presence")
  );

  useEffect(() => {
    fetchJson<{ cats: CatOption[] }>("/app-api/v1/presence/cats")
      .then((d) => setCats(d.cats))
      .catch(() => {});
  }, []);

  const load = useCallback(() => {
    const params = new URLSearchParams({
      date,
      page: String(page),
      cats_only: String(catsOnly),
      mice_only: String(miceOnly),
      overlay: String(showOverlay),
      rfid: rfidFilter,
    });
    fetchJson<PicturesResponse>(`/app-api/v1/pictures?${params}`)
      .then(setData)
      .catch(() => window.location.reload());
  }, [date, page, catsOnly, miceOnly, showOverlay, rfidFilter]);

  useEffect(() => {
    load();
  }, [load]);

  const loadEvents = useCallback(() => {
    const params = new URLSearchParams({
      date,
      cats_only: String(catsOnly),
      mice_only: String(miceOnly),
      rfid: rfidFilter,
    });
    fetchJson<{ date: string; events: GroupedEvent[] }>(`/app-api/v1/pictures/events?${params}`)
      .then((d) => setEvents(d.events))
      .catch(() => window.location.reload());
  }, [date, catsOnly, miceOnly, rfidFilter]);

  useEffect(() => {
    if (groupedView) loadEvents();
  }, [groupedView, loadEvents]);

  // Any filter change resets back to page 1 - otherwise a narrower filter
  // can leave `page` pointing past the new, smaller total_pages.
  const onFilterChange = (fn: () => void) => {
    fn();
    setPage(1);
  };

  const removePhoto = (id: number) => {
    setData((prev) => (prev ? { ...prev, photos: prev.photos.filter((p) => p.id !== id) } : prev));
    setOpenIndex(null);
  };

  const updatePhoto = (updated: Picture) => {
    setData((prev) =>
      prev ? { ...prev, photos: prev.photos.map((p) => (p.id === updated.id ? updated : p)) } : prev
    );
  };

  const Pager = () =>
    data && data.total_pages > 1 ? (
      <div className="flex items-center justify-center gap-3 py-2 text-sm">
        <button
          onClick={() => setPage((p) => Math.max(1, p - 1))}
          disabled={data.page <= 1}
          className="rounded-md border border-white/15 px-3 py-1.5 hover:bg-white/10 disabled:opacity-30"
        >
          ‹
        </button>
        <span>
          {data.page} / {data.total_pages} ({data.total_count} photos)
        </span>
        <button
          onClick={() => setPage((p) => Math.min(data.total_pages, p + 1))}
          disabled={data.page >= data.total_pages}
          className="rounded-md border border-white/15 px-3 py-1.5 hover:bg-white/10 disabled:opacity-30"
        >
          ›
        </button>
      </div>
    ) : null;

  return (
    <>
      <TopBanner />
      <div className="container mx-auto max-w-2xl px-3 py-4">
        <div className="mb-3 flex items-center justify-center gap-2">
          <button
            className="rounded-md border border-white/15 px-3 py-1.5 text-sm hover:bg-white/10"
            onClick={() => onFilterChange(() => setDate((d) => shiftDate(d, -1)))}
            aria-label="Jour précédent"
          >
            ‹
          </button>
          <input
            type="date"
            value={date}
            onChange={(e) => onFilterChange(() => setDate(e.target.value))}
            className="rounded-md border border-white/15 bg-transparent px-2 py-1.5 text-sm"
          />
          <button
            className="rounded-md border border-white/15 px-3 py-1.5 text-sm hover:bg-white/10"
            onClick={() => onFilterChange(() => setDate((d) => shiftDate(d, 1)))}
            aria-label="Jour suivant"
          >
            ›
          </button>
          <button
            className="ml-2 rounded-md border border-white/15 px-3 py-1.5 text-sm hover:bg-white/10"
            onClick={() => onFilterChange(() => setDate(todayLocalIso()))}
          >
            Aujourd'hui
          </button>
        </div>

        <div className="mb-3 flex flex-wrap items-center justify-center gap-x-4 gap-y-1 text-sm">
          <label className="flex items-center gap-1.5">
            <input
              type="checkbox"
              checked={catsOnly}
              onChange={(e) => onFilterChange(() => setCatsOnly(e.target.checked))}
            />
            Chats détectés uniquement
          </label>
          <label className="flex items-center gap-1.5">
            <input
              type="checkbox"
              checked={miceOnly}
              onChange={(e) => onFilterChange(() => setMiceOnly(e.target.checked))}
            />
            Souris détectées uniquement
          </label>
          <label className="flex items-center gap-1.5">
            <input
              type="checkbox"
              checked={showOverlay}
              onChange={(e) => setShowOverlay(e.target.checked)}
            />
            Afficher les détections
          </label>
          <label className="flex items-center gap-1.5">
            <input
              type="checkbox"
              checked={groupedView}
              onChange={(e) => setGroupedView(e.target.checked)}
            />
            Grouper par événements
          </label>
        </div>

        <div className="mb-4 flex justify-center">
          <select
            value={rfidFilter}
            onChange={(e) => onFilterChange(() => setRfidFilter(e.target.value))}
            className="rounded-md border border-white/15 bg-neutral-900 px-2 py-1.5 text-sm"
          >
            <option value="">Tous les chats</option>
            {cats.map((c) => (
              <option key={c.rfid} value={c.rfid}>
                {c.name}
              </option>
            ))}
          </select>
        </div>

        {!groupedView && (
          <>
            <Pager />

            {data === null && <p className="text-center opacity-70">Chargement…</p>}
            {data !== null && data.photos.length === 0 && (
              <p className="text-center opacity-70">Aucune photo pour ces filtres.</p>
            )}

            {data !== null && data.photos.length > 0 && (
              <div className="grid grid-cols-2 gap-2 sm:grid-cols-3">
                {data.photos.map((p, i) => (
                  <button
                    key={p.id}
                    onClick={() => setOpenIndex(i)}
                    className="relative overflow-hidden rounded-lg bg-black text-left"
                  >
                    <img src={p.thumb_url} alt="" loading="lazy" className="aspect-square w-full object-cover" />
                    {showOverlay &&
                      p.objects.map((obj) => (
                        <div
                          key={obj.idx}
                          className="absolute"
                          style={{
                            left: `${obj.x}%`,
                            top: `${obj.y}%`,
                            width: `${obj.w}%`,
                            height: `${obj.h}%`,
                            border: `2px solid ${objectColor(obj, p.mouse_threshold)}`,
                          }}
                        />
                      ))}
                    <div className="absolute inset-x-0 bottom-0 bg-black/60 px-1.5 py-1 text-[0.7rem]">
                      <div className="truncate">{p.cat_name ?? "Inconnu"}</div>
                      <div className="text-white/60">
                        {p.time} · 🔍{p.mouse_probability.toFixed(0)}%
                      </div>
                    </div>
                  </button>
                ))}
              </div>
            )}

            <Pager />
          </>
        )}

        {groupedView && (
          <div className="flex flex-col gap-1">
            {events === null && <p className="text-center opacity-70">Chargement…</p>}
            {events !== null && events.length === 0 && (
              <p className="text-center opacity-70">Aucun événement pour ces filtres.</p>
            )}
            {events !== null &&
              events.map((ev) => (
                <div key={ev.block_id} className="rounded-lg bg-white/5 px-3 py-2">
                  <div className="flex items-center gap-2">
                    <div className="flex flex-wrap items-baseline gap-2 text-[0.95rem]">
                      <span className="whitespace-nowrap text-[0.85rem] font-semibold opacity-75">
                        {ev.time}
                      </span>
                      <span
                        className="font-medium"
                        style={ev.direction ? { color: DIRECTION_COLOR[ev.direction] } : undefined}
                      >
                        {ev.text}
                      </span>
                    </div>
                    <div className="ml-auto flex flex-shrink-0 gap-2">
                      <button
                        className="opacity-80 hover:opacity-100"
                        onClick={() => setEventModal({ blockId: ev.block_id, mode: "video" })}
                        aria-label="Voir la vidéo"
                        title="Voir la vidéo"
                      >
                        🎬
                      </button>
                      <button
                        className="opacity-80 hover:opacity-100"
                        onClick={() => setEventModal({ blockId: ev.block_id, mode: "photo" })}
                        aria-label="Voir photo par photo"
                        title="Voir photo par photo"
                      >
                        📷
                      </button>
                    </div>
                  </div>
                </div>
              ))}
          </div>
        )}

        {eventModal && (
          <EventModal
            blockId={eventModal.blockId}
            mode={eventModal.mode}
            onClose={() => setEventModal(null)}
          />
        )}

        {openPicture && openIndex !== null && (
          <PictureDetail
            picture={openPicture}
            correctionLabels={data?.correction_labels ?? []}
            onClose={() => setOpenIndex(null)}
            onChanged={updatePhoto}
            onDeleted={() => removePhoto(openPicture.id)}
            onPrev={openIndex > 0 ? () => setOpenIndex(openIndex - 1) : undefined}
            onNext={
              data && openIndex < data.photos.length - 1 ? () => setOpenIndex(openIndex + 1) : undefined
            }
          />
        )}
      </div>
      <BottomNav active="pictures" />
    </>
  );
}
