import { useCallback, useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router";
import BottomNav from "../components/BottomNav";
import EventCard, { type ChronologyEvent } from "../components/EventCard";
import EventModal from "../components/EventModal";
import TopBanner from "../components/TopBanner";
import { fetchJson } from "../lib/api";
import { shiftDate, todayLocalIso } from "../lib/date";
import { useBackButton } from "../lib/useBackButton";
import { usePageTitle } from "../lib/usePageTitle";

interface ChronologyResponse {
  date: string;
  events: ChronologyEvent[];
}

interface DayGroup {
  date: string;
  events: ChronologyEvent[];
}

// 07.10, Sid ("tout en défilant... ça passe au jour d'avant sans devoir
// changer de page"): how many consecutive EMPTY days loadMore() will chain
// through automatically in one go before giving up and waiting for the
// next scroll trigger - without this, a quiet stretch (no cat activity for
// several days) would either spin fetching forever with nothing to show for
// it, or leave the list looking stuck with no feedback.
const MAX_EMPTY_DAYS_PER_LOAD = 14;

function fetchDay(date: string): Promise<ChronologyResponse> {
  return fetchJson<ChronologyResponse>(`/app-api/v1/chronology?date=${date}`);
}

export default function ChronologyPage() {
  usePageTitle("Chronologie");
  const navigate = useNavigate();
  const [date, setDate] = useState(todayLocalIso);
  const [days, setDays] = useState<DayGroup[] | null>(null);
  const [loadingMore, setLoadingMore] = useState(false);
  const [exhausted, setExhausted] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [eventModal, setEventModal] = useState<{ blockId: number; mode: "video" | "photo" } | null>(
    null
  );
  const sentinelRef = useRef<HTMLDivElement | null>(null);
  // Mirrors `days` for use inside the IntersectionObserver callback without
  // re-subscribing it on every fetch (the observer is only set up once).
  const daysRef = useRef<DayGroup[] | null>(null);
  const loadingRef = useRef(false);

  useBackButton(
    () => {
      if (eventModal) {
        setEventModal(null);
        return true;
      }
      return false;
    },
    () => navigate("/presence")
  );

  // Jump to a specific date (date picker, chevrons, "Aujourd'hui") - resets
  // the feed to start fresh from that single day, same as before scrolling
  // was added.
  const jumpTo = useCallback(async (d: string) => {
    setError(null);
    setExhausted(false);
    setDays(null);
    try {
      const data = await fetchDay(d);
      setDays([{ date: data.date, events: data.events }]);
    } catch {
      // Expired session cookie: WebAuthMiddleware serves the HTML login
      // page body instead of JSON for an unauthenticated fetch too - a
      // real page reload lets it show that login form properly.
      window.location.reload();
    }
  }, []);

  useEffect(() => {
    jumpTo(date);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [date]);

  useEffect(() => {
    daysRef.current = days;
  }, [days]);

  // Chains backward through days, skipping empty ones, until it finds one
  // with events or hits MAX_EMPTY_DAYS_PER_LOAD - called again by the
  // IntersectionObserver whenever the sentinel is back in view.
  const loadMore = useCallback(async () => {
    if (loadingRef.current || exhausted) return;
    const current = daysRef.current;
    if (!current || current.length === 0) return;
    loadingRef.current = true;
    setLoadingMore(true);
    try {
      let cursor = current[current.length - 1].date;
      const appended: DayGroup[] = [];
      for (let i = 0; i < MAX_EMPTY_DAYS_PER_LOAD; i++) {
        cursor = shiftDate(cursor, -1);
        const data = await fetchDay(cursor);
        appended.push({ date: data.date, events: data.events });
        if (data.events.length > 0) break;
      }
      setDays((prev) => (prev ? [...prev, ...appended] : appended));
    } catch {
      window.location.reload();
    } finally {
      loadingRef.current = false;
      setLoadingMore(false);
    }
  }, [exhausted]);

  useEffect(() => {
    const el = sentinelRef.current;
    if (!el) return;
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries[0]?.isIntersecting) loadMore();
      },
      { rootMargin: "400px" }
    );
    observer.observe(el);
    return () => observer.disconnect();
  }, [loadMore]);

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

        {error && <p className="text-center text-red-300">{error}</p>}

        {days === null && !error && (
          <p className="text-center opacity-70">Chargement…</p>
        )}

        {days !== null && days.every((g) => g.events.length === 0) && (
          <p className="text-center opacity-70">Aucun événement.</p>
        )}

        {days !== null && (
          <div className="flex flex-col gap-1">
            {days.map((group) =>
              group.events.map((ev) => (
                <EventCard
                  key={`${group.date}-${ev.block_id}`}
                  ev={ev}
                  onOpenModal={(mode) => setEventModal({ blockId: ev.block_id, mode })}
                />
              ))
            )}
          </div>
        )}

        {/* 07.10, Sid: scroll-triggered pagination sentinel - loads the
            previous day automatically as it nears the viewport, chaining
            through empty days internally (see loadMore/MAX_EMPTY_DAYS_PER_LOAD
            above) rather than requiring a tap on the date chevrons. */}
        <div ref={sentinelRef} className="h-4" />
        {loadingMore && (
          <p className="py-2 text-center text-sm opacity-60">Chargement…</p>
        )}

        {eventModal && (
          <EventModal
            blockId={eventModal.blockId}
            mode={eventModal.mode}
            onClose={() => setEventModal(null)}
          />
        )}
      </div>
      <BottomNav active="chronology" />
    </>
  );
}
