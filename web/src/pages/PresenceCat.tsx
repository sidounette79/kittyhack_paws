import { useCallback, useEffect, useRef, useState } from "react";
import { useNavigate, useParams } from "react-router";
import BottomNav from "../components/BottomNav";
import EventModal from "../components/EventModal";
import { SessionExpiredError, fetchJson } from "../lib/api";
import { monthYearLabel, shiftDate, todayLocalIso, weekdayAbbr } from "../lib/date";
import { useBackButton } from "../lib/useBackButton";
import { usePageTitle } from "../lib/usePageTitle";

interface CatDetail {
  rfid: string;
  name: string;
  photo_url: string | null;
  location: "in" | "out" | "unknown";
  since: string | null;
  last_entry: string | null;
  last_exit: string | null;
  date: string;
  correction_target: string | null;
  stats: {
    entries: number;
    exits: number;
    glances: number;
    prey_blocked: number;
    seconds_outside: number;
    seconds_outside_label: string;
  };
}

interface FilteredEvent {
  block_id: number;
  time: string;
  date_label: string;
  direction: "in" | "out" | "glance_in" | "glance_out" | null;
  text: string;
  thumb_url: string;
}

interface WeeklyTick {
  frac: number;
  type: "in" | "out" | "prey";
  block_id: number | null;
  thumb_url: string | null;
}

interface WeeklyDay {
  date: string;
  day_abbr: string;
  entries: number;
  seconds_outside: number;
  seconds_outside_label: string;
  ticks: WeeklyTick[];
}

interface WeeklyData {
  window_start: string;
  window_end: string;
  offset: number;
  days: WeeklyDay[];
  totals: {
    avg_entries: number;
    avg_seconds_outside_label: string;
    prey_blocked: number;
  };
}

type SubView = null | { kind: "events"; filter: string; label: string } | { kind: "weekly" };

interface MonthlyPrey {
  months: { month: string; label: string; count: number }[];
  total: number;
}

const DIRECTION_COLOR: Record<string, string> = {
  in: "rgb(134, 239, 172)",
  out: "rgb(252, 165, 165)",
  glance_in: "rgb(253, 224, 71)",
  glance_out: "rgb(253, 224, 71)",
};

const FILTER_LABELS: Record<string, string> = {
  in_out: "Entrées et sorties",
  glances: "Coups d'œil",
  prey_blocked: "Cadeaux bloqués",
  all: "Activité totale",
};

function relativeTime(iso: string | null): string {
  if (!iso) return "";
  const seconds = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (seconds < 60) return "à l'instant";
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `il y a ${minutes} min`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `il y a ${hours} h`;
  const days = Math.floor(hours / 24);
  return `il y a ${days} j`;
}

// 07.10, Sid ("une autre icône de maison ? elle est pas très jolie
// celle-ci"): replaced the 🏠/🌳 emoji with plain line icons, same style
// family as BottomNav's SVGs - no risk of looking different across devices
// the way emoji rendering does.
function HouseIcon() {
  return (
    <svg
      viewBox="0 0 24 24"
      className="h-6 w-6"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M4 11.5 12 4l8 7.5" />
      <path d="M6 10v9a1 1 0 0 0 1 1h3v-5h4v5h3a1 1 0 0 0 1-1v-9" />
    </svg>
  );
}

function TreeIcon() {
  return (
    <svg
      viewBox="0 0 24 24"
      className="h-6 w-6"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M12 3 7 10h2.5L6 15.5h3L6 20.5h12L15 15.5h3L14.5 10H17L12 3Z" />
      <path d="M12 20.5V22" />
    </svg>
  );
}

function BackHeader({ title, onBack }: { title: string; onBack: () => void }) {
  return (
    <div className="mb-3 flex items-center gap-3">
      <button
        onClick={onBack}
        aria-label="Retour"
        className="rounded-md border border-white/15 px-3 py-1.5 text-sm hover:bg-white/10"
      >
        ‹
      </button>
      <div className="text-lg font-semibold">{title}</div>
    </div>
  );
}

export default function PresenceCatPage() {
  const navigate = useNavigate();
  const { rfid } = useParams<{ rfid: string }>();
  const [date, setDate] = useState(todayLocalIso);
  const [detail, setDetail] = useState<CatDetail | null>(null);
  usePageTitle(detail?.name ?? "Chat");
  // 09.10, Sid ("j'ai pas eu de message, j'ai pas vu que la couleur a
  // changé"): the correction worked but gave zero visible feedback - the
  // silent catch{} meant even a real failure looked identical to success.
  const [correctMsg, setCorrectMsg] = useState<string | null>(null);
  const [correcting, setCorrecting] = useState(false);
  const [blurPx, setBlurPx] = useState(10);
  const [subView, setSubView] = useState<SubView>(null);
  const [events, setEvents] = useState<FilteredEvent[] | null>(null);
  const [weekly, setWeekly] = useState<WeeklyData | null>(null);
  const [weeklyOffset, setWeeklyOffset] = useState(0);
  const [monthlyPrey, setMonthlyPrey] = useState<MonthlyPrey | null>(null);
  const [eventModal, setEventModal] = useState<{ blockId: number; mode: "video" | "photo" } | null>(
    null
  );
  const pillStripRef = useRef<HTMLDivElement>(null);
  const todayPillRef = useRef<HTMLButtonElement>(null);

  // 21.10, Sid ("il faudrait que le 'Aujourd'hui' soit en gras et au
  // centre... ça ouvre je sais pas où") - scrollTo(scrollWidth) poussait
  // aujourd'hui tout au bord droit, pas franchement repérable. scrollIntoView
  // avec inline:"center" sur le pill d'aujourd'hui spécifiquement (pas juste
  // le bord de la liste) le centre vraiment, avec du contexte visible des
  // deux côtés. "auto" plutôt que "smooth" : positionnement direct au
  // montage, pas une animation à chaque ouverture de page.
  useEffect(() => {
    todayPillRef.current?.scrollIntoView({ inline: "center", block: "nearest", behavior: "auto" });
  }, []);

  useBackButton(
    () => {
      if (eventModal) {
        setEventModal(null);
        return true;
      }
      if (subView) {
        setSubView(null);
        return true;
      }
      return false;
    },
    () => navigate("/presence")
  );

  const loadDetail = useCallback(() => {
    if (!rfid) return;
    fetchJson<CatDetail>(`/app-api/v1/presence/cats/${rfid}?date=${date}`)
      .then(setDetail)
      .catch((err) => {
        // Expired session: reload so WebAuthMiddleware's login page actually
        // shows. A real error (unknown rfid, deleted cat) instead just goes
        // back to the cat grid - navigate() there would hit the exact same
        // expired-session wall again if that were the cause.
        if (err instanceof SessionExpiredError) {
          window.location.reload();
        } else {
          navigate("/presence");
        }
      });
  }, [rfid, date, navigate]);

  useEffect(() => {
    loadDetail();
  }, [loadDetail]);

  useEffect(() => {
    fetchJson<{ blur_px: number }>("/app-api/v1/appearance/photo-blur")
      .then((d) => setBlurPx(d.blur_px))
      .catch(() => {});
  }, []);

  useEffect(() => {
    if (!rfid) return;
    fetchJson<MonthlyPrey>(`/app-api/v1/presence/cats/${rfid}/monthly-prey`)
      .then(setMonthlyPrey)
      .catch(() => {});
  }, [rfid]);

  const openEvents = (filter: string) => {
    if (!rfid) return;
    setSubView({ kind: "events", filter, label: FILTER_LABELS[filter] ?? filter });
    setEvents(null);
    fetchJson<{ events: FilteredEvent[] }>(
      `/app-api/v1/presence/cats/${rfid}/events?filter=${filter}`
    )
      .then((d) => setEvents(d.events))
      .catch(() => setEvents([]));
  };

  const openWeekly = () => {
    setSubView({ kind: "weekly" });
    setWeeklyOffset(0);
    setWeekly(null);
  };

  useEffect(() => {
    if (subView?.kind !== "weekly" || !rfid) return;
    fetchJson<WeeklyData>(`/app-api/v1/presence/cats/${rfid}/weekly?offset=${weeklyOffset}`)
      .then(setWeekly)
      .catch(() => {});
  }, [subView, weeklyOffset, rfid]);

  const correct = async () => {
    if (!rfid || !detail?.correction_target) return;
    setCorrecting(true);
    setCorrectMsg(null);
    try {
      await fetchJson(`/app-api/v1/presence/cats/${rfid}/correct`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ event_type: detail.correction_target }),
      });
      setCorrectMsg("Statut corrigé.");
      loadDetail();
    } catch (err) {
      setCorrectMsg(err instanceof Error ? err.message : "Échec de la correction.");
    } finally {
      setCorrecting(false);
    }
  };

  if (!rfid) return null;

  // 21.10, Sid ("rendre la ligne de calendrier scrollable") - les stats
  // sont maintenant gardées pour toujours (voir media_purged côté
  // database.py), donc figer la bande à 8 jours fixes empêchait d'accéder
  // à tout cet historique pérenne. 365 jours en arrière, vraiment
  // scrollable (overflow-x + un seul ref pour la positionner sur
  // aujourd'hui au chargement) - swipe/scroll vers la gauche pour
  // remonter, le rendu par défaut reste identique à avant.
  const pillDates = Array.from({ length: 366 }, (_, i) => shiftDate(todayLocalIso(), -(365 - i)));
  const todayIso = todayLocalIso();

  return (
    <>
      <div className="container mx-auto max-w-2xl px-3 py-4">
        {subView?.kind === "events" && (
          <>
            <BackHeader
              title={`${subView.label} — ${detail?.name ?? ""}`}
              onBack={() => setSubView(null)}
            />
            {events === null && <p className="text-center opacity-70">Chargement…</p>}
            {events !== null && events.length === 0 && (
              <p className="text-center opacity-70">Aucun événement.</p>
            )}
            {events !== null && events.length > 0 && (
              <div className="flex flex-col gap-1">
                {events.map((ev) => (
                  <div
                    key={ev.block_id}
                    className="flex items-center gap-2 rounded-lg bg-white/5 px-3 py-2"
                  >
                    <div className="flex flex-wrap items-baseline gap-2 text-[0.95rem]">
                      <span className="whitespace-nowrap text-[0.85rem] font-semibold opacity-75">
                        {ev.date_label} · {ev.time}
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
                ))}
              </div>
            )}
          </>
        )}

        {subView?.kind === "weekly" && (
          <>
            <BackHeader title="Accès" onBack={() => setSubView(null)} />
            <div className="mb-3 flex items-center justify-center gap-3">
              <button
                className="rounded-md border border-white/15 px-3 py-1.5 text-sm hover:bg-white/10"
                onClick={() => setWeeklyOffset((o) => o + 1)}
              >
                ‹
              </button>
              <span className="text-sm">
                {weekly
                  ? `${weekly.window_start.slice(8, 10)}.${weekly.window_start.slice(5, 7)} – ${weekly.window_end.slice(8, 10)}.${weekly.window_end.slice(5, 7)}`
                  : "…"}
              </span>
              <button
                className={`rounded-md border border-white/15 px-3 py-1.5 text-sm ${
                  weeklyOffset === 0 ? "opacity-30" : "hover:bg-white/10"
                }`}
                disabled={weeklyOffset === 0}
                onClick={() => setWeeklyOffset((o) => Math.max(0, o - 1))}
              >
                ›
              </button>
            </div>

            {weekly && (
              <>
                <div className="mb-3 grid grid-cols-3 gap-2">
                  <div className="rounded-lg bg-white/5 p-3 text-center">
                    <div className="text-xl font-semibold">{weekly.totals.avg_entries}</div>
                    <div className="text-xs text-white/60">Entrées/jour</div>
                  </div>
                  <div className="rounded-lg bg-white/5 p-3 text-center">
                    <div className="text-xl font-semibold">
                      {weekly.totals.avg_seconds_outside_label}
                    </div>
                    <div className="text-xs text-white/60">Dehors/jour</div>
                  </div>
                  <div className="rounded-lg bg-white/5 p-3 text-center">
                    <div className="text-xl font-semibold">{weekly.totals.prey_blocked}</div>
                    <div className="text-xs text-white/60">Cadeaux bloqués</div>
                  </div>
                </div>
                <div className="mb-2 flex items-center justify-center gap-4 text-xs text-white/60">
                  <span>🟢 Entrée</span>
                  <span>🔴 Sortie</span>
                  <span>🟡 Proie bloquée</span>
                </div>
                <div className="flex flex-col gap-2">
                  {[...weekly.days].reverse().map((day) => (
                    <div key={day.date}>
                      <div className="mb-1 flex items-center justify-between text-xs text-white/70">
                        <span>
                          {weekdayAbbr(day.date)} {day.date.slice(8, 10)}.{day.date.slice(5, 7)}
                        </span>
                        <span>
                          {day.entries} · {day.seconds_outside_label}
                        </span>
                      </div>
                      <div className="relative h-3 rounded bg-white/5">
                        {day.ticks.map((tick, i) => (
                          <button
                            key={i}
                            className="absolute top-0 h-3 w-1 -translate-x-1/2 rounded"
                            style={{
                              left: `${tick.frac * 100}%`,
                              backgroundColor:
                                tick.type === "in"
                                  ? "rgb(134, 239, 172)"
                                  : tick.type === "out"
                                    ? "rgb(252, 165, 165)"
                                    : "rgb(253, 224, 71)",
                            }}
                            onClick={() =>
                              tick.block_id !== null &&
                              setEventModal({ blockId: tick.block_id, mode: "photo" })
                            }
                            aria-label={tick.type}
                          />
                        ))}
                      </div>
                    </div>
                  ))}
                </div>
              </>
            )}
          </>
        )}

        {!subView && detail && (
          <>
            {/* 07.10, Sid ("on peut l'effet flou de la photo derrière le
                calendrier ?"): the cat's own photo, blurred + darkened, as a
                hero behind the avatar/name/date-pills - -mx-3 bleeds it to
                the edges of the page's own px-3 container padding. */}
            <div className="relative -mx-3 mb-4 overflow-hidden">
              {detail.photo_url && (
                <div
                  className="absolute inset-0 bg-cover bg-center"
                  style={{
                    backgroundImage: `url(${detail.photo_url})`,
                    filter: `blur(${blurPx}px) brightness(0.5)`,
                    transform: "scale(1.15)",
                  }}
                />
              )}
              <div className="relative px-3 pb-4 pt-4">
                <button
                  onClick={() => navigate("/presence")}
                  aria-label="Retour"
                  className="mb-3 rounded-md border border-white/20 bg-black/30 px-3 py-1.5 text-sm hover:bg-black/50"
                >
                  ‹
                </button>

                <div className="flex flex-col items-center">
                  {/* 07.10, Sid: clicking the photo opens the cat's settings
                      (name/RFID/photo/door-access switches) as a real page
                      now, not merged inline below the stat tiles like the
                      old Shiny version. */}
                  <button
                    onClick={() => navigate(`/presence/cat/${rfid}/edit`)}
                    aria-label="Paramètres du chat"
                  >
                    {detail.photo_url ? (
                      <img
                        src={detail.photo_url}
                        alt=""
                        className="h-16 w-16 rounded-full object-cover ring-2 ring-white/80"
                      />
                    ) : (
                      <div className="flex h-16 w-16 items-center justify-center rounded-full bg-white/10 text-2xl ring-2 ring-white/80">
                        🐱
                      </div>
                    )}
                  </button>
                  <div className="mt-2 text-xl font-semibold text-white" style={{ textShadow: "0 1px 4px rgba(0,0,0,0.6)" }}>
                    {detail.name}
                  </div>
                </div>

                {/* 21.10, Sid ("je sais pas quel mois on est") - suit la
                    date SÉLECTIONNÉE (pas juste aujourd'hui), donc reste
                    juste même après avoir tapé un pill ailleurs dans le
                    mois précédent/suivant. */}
                <div className="mt-3 text-center text-xs font-medium capitalize text-white/70">
                  {monthYearLabel(date)}
                </div>
                <div ref={pillStripRef} className="mt-1 flex gap-1 overflow-x-auto scroll-smooth">
                  {pillDates.map((d) => {
                    const isToday = d === todayIso;
                    const isSelected = d === date;
                    return (
                      <button
                        key={d}
                        ref={isToday ? todayPillRef : undefined}
                        onClick={() => setDate(d)}
                        className={`flex min-w-[2.6rem] flex-shrink-0 flex-col items-center rounded-lg px-1.5 py-2 text-xs ${
                          isSelected ? "bg-white text-black" : "bg-black/30 text-white/80"
                        } ${isToday ? "ring-2 ring-white" : ""}`}
                      >
                        <span className={`text-sm ${isToday ? "font-extrabold" : "font-semibold"}`}>
                          {Number(d.slice(8, 10))}
                        </span>
                        <span>{weekdayAbbr(d)}</span>
                      </button>
                    );
                  })}
                </div>
              </div>
            </div>

            {/* 07.10, Sid ("que la puce ait une couleur différente dedans/
                dehors, le fond de la carte" + "que ce soit que l'icône
                maison/arbre qui change le statut et pas toute la ligne"):
                the card itself is no longer a button - only the icon badge
                is, so a tap anywhere else on the row (e.g. misjudging the
                row's edge) can't accidentally flip the door override. */}
            <div
              className={`mb-2 flex w-full items-center justify-between rounded-xl border p-4 ${
                detail.location === "out"
                  ? "border-green-400/20 bg-green-400/10"
                  : detail.location === "in"
                    ? "border-amber-400/20 bg-amber-400/10"
                    : "border-white/10 bg-white/5"
              }`}
            >
              <div>
                <div className="text-base font-medium">
                  {detail.location === "in" ? "Intérieur" : detail.location === "out" ? "Extérieur" : "Inconnu"}
                </div>
                <div className="text-sm text-white/60">{relativeTime(detail.since)}</div>
              </div>
              <button
                onClick={correct}
                disabled={!detail.correction_target || correcting}
                aria-label="Changer le statut intérieur/extérieur"
                className="flex flex-col items-center gap-1 disabled:opacity-40"
              >
                <span className="flex h-11 w-11 items-center justify-center rounded-full bg-white/15">
                  {detail.location === "out" ? <TreeIcon /> : <HouseIcon />}
                </span>
                <span className="text-[0.65rem] text-white/65">
                  {correcting ? "Correction…" : "(cliquer pour changer)"}
                </span>
              </button>
            </div>
            {correctMsg && <p className="mb-2 text-center text-sm text-white/70">{correctMsg}</p>}

            {/* 07.10, Sid ("on peut mettre dernière entrée et dernière
                sortie dans une puce aussi"): same rounded-xl/bg-white/5
                pill styling as the 6 stat tiles below, instead of plain
                text lines. */}
            <div className="mb-4 grid grid-cols-2 gap-2">
              <div className="rounded-xl bg-white/5 p-4 text-center">
                <div className="text-base font-semibold">
                  {detail.last_entry ? relativeTime(detail.last_entry) : "—"}
                </div>
                <div className="text-sm text-white/60">Dernière entrée</div>
              </div>
              <div className="rounded-xl bg-white/5 p-4 text-center">
                <div className="text-base font-semibold">
                  {detail.last_exit ? relativeTime(detail.last_exit) : "—"}
                </div>
                <div className="text-sm text-white/60">Dernière sortie</div>
              </div>
            </div>

            <div className="grid grid-cols-2 gap-2">
              <button
                onClick={() => openEvents("in_out")}
                className="rounded-xl bg-white/5 p-4 text-center hover:bg-white/10"
              >
                <div className="text-2xl font-semibold">{detail.stats.entries}</div>
                <div className="text-sm text-white/60">Entrées</div>
              </button>
              <button
                onClick={() => openEvents("in_out")}
                className="rounded-xl bg-white/5 p-4 text-center hover:bg-white/10"
              >
                <div className="text-2xl font-semibold">{detail.stats.exits}</div>
                <div className="text-sm text-white/60">Sorties</div>
              </button>
              <button
                onClick={() => openEvents("prey_blocked")}
                className="rounded-xl bg-white/5 p-4 text-center hover:bg-white/10"
              >
                <div className="text-2xl font-semibold">{detail.stats.prey_blocked}</div>
                <div className="text-sm text-white/60">Cadeaux bloqués</div>
              </button>
              <button
                onClick={() => openEvents("glances")}
                className="rounded-xl bg-white/5 p-4 text-center hover:bg-white/10"
              >
                <div className="text-2xl font-semibold">{detail.stats.glances}</div>
                <div className="text-sm text-white/60">Coups d'œil</div>
              </button>
              <button
                onClick={openWeekly}
                className="rounded-xl bg-white/5 p-4 text-center hover:bg-white/10"
              >
                <div className="text-2xl font-semibold">{detail.stats.seconds_outside_label}</div>
                <div className="text-sm text-white/60">Temps à l'extérieur</div>
              </button>
              <button
                onClick={() => openEvents("all")}
                className="rounded-xl bg-white/5 p-4 text-center hover:bg-white/10"
              >
                <div className="text-2xl">📋</div>
                <div className="text-sm text-white/60">Activité totale</div>
              </button>
            </div>

            {/* 07.10, Sid ("un petit graphique comme ça et des stats aussi
                en dessous des boutons - le nombre total"): monthly prey-
                blocked bar chart, same idea as the reference screenshot -
                6 months, oldest to newest, plus a running total. */}
            {monthlyPrey && (
              <div className="mt-4 rounded-xl bg-white/5 p-4">
                <div className="mb-3 text-sm font-semibold text-white/80">
                  Proies bloquées par mois
                </div>
                <div className="flex items-end gap-2" style={{ height: "6rem" }}>
                  {monthlyPrey.months.map((m) => {
                    const max = Math.max(1, ...monthlyPrey.months.map((x) => x.count));
                    return (
                      <div key={m.month} className="flex flex-1 flex-col items-center justify-end gap-1" style={{ height: "100%" }}>
                        <div
                          className="w-full rounded-t bg-red-300/70"
                          style={{ height: `${Math.max(4, (m.count / max) * 80)}px` }}
                          title={`${m.count}`}
                        />
                        <span className="text-[0.65rem] text-white/65">{m.label}</span>
                      </div>
                    );
                  })}
                </div>
                <div className="mt-3 text-sm text-white/60">
                  Total : {monthlyPrey.total} proies bloquées
                </div>
              </div>
            )}
          </>
        )}

        {!subView && !detail && <p className="text-center opacity-70">Chargement…</p>}

        {eventModal && (
          <EventModal
            blockId={eventModal.blockId}
            mode={eventModal.mode}
            onClose={() => setEventModal(null)}
            onDeleted={loadDetail}
          />
        )}
      </div>
      <BottomNav active="presence" />
    </>
  );
}
