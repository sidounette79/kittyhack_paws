import { useEffect, useRef, useState } from "react";
import { fetchJson } from "../lib/api";

interface DetectedObject {
  x: number;
  y: number;
  w: number;
  h: number;
  name: string;
  prob: number;
  idx: number;
  reviewed?: boolean;
  reviewedAs?: string;
}

interface Frame {
  pid: number;
  ts: string;
  thumb_url: string;
  objects: DetectedObject[];
}

// 10.10, Sid ("deux vidéos côte à côte... avec les incrustations aussi"):
// the dense Reolink "chatiere" sequence for the same event - no `pid`/
// correction fields, it's a read-only comparison view, not a reviewable
// dataset like the flap's own photos.
interface ReolinkFrame {
  ts: string;
  url: string;
  filename: string;
  objects: DetectedObject[];
}

// 08.10, Sid ("j'ai tagué une image pour indiquer une proie... je sais pas
// s'il manque un bouton pour valider"): confirmed via the server logs - no
// POST ever arrived, because this component never had the manual
// draw-a-missed-detection feature at all, only PictureDetail.tsx (the
// Photos grid) did. Same /app-api/v1/pictures/{pid}/add-detection endpoint
// works here unchanged - a frame's `pid` is the same photo id either way.
interface EventData {
  block_id: number;
  fps: number;
  fallback_mode: boolean;
  overlay_initial: boolean;
  mouse_threshold: number;
  correction_labels: string[];
  aspect: string | null;
  frames: Frame[];
  reolink_frames: ReolinkFrame[];
  reolink_fps: number;
}

// 11.10 (round 2), Sid ("les cameras sont toujours extremement en decalage
// quand je regarde les videos"): both `Frame.ts` and `ReolinkFrame.ts` are
// server-formatted "HH:MM:SS.d" (one decimal, see _format_frame_ts /
// _list_event_frames server-side - same convention for both, same day).
// Parses to seconds-since-midnight so playback can be driven by real
// elapsed time instead of a fixed per-frame interval.
function parseFrameTs(ts: string): number {
  const m = /^(\d{1,2}):(\d{1,2}):(\d{1,2}(?:\.\d+)?)$/.exec(ts);
  if (!m) return 0;
  return Number(m[1]) * 3600 + Number(m[2]) * 60 + Number(m[3]);
}

// Index of the last frame whose timestamp is <= target (clamped to 0) -
// "which frame was actually showing at this real moment".
function indexAtOrBefore(times: number[], target: number): number {
  let result = 0;
  for (let i = 0; i < times.length; i++) {
    if (times[i] <= target) result = i;
    else break;
  }
  return result;
}

// 07.10, Sid ("avant j'avais la loupe et ça me donnait la vidéo et
// maintenant j'ai juste un instantané... c'est important ça" / "la vidéo
// complète avec une petite icône de film et... la version photo par photo
// avec l'incrustation"): real restoration of event_modal.py's multi-frame
// player + detection overlays + corrections, with two entry points into the
// same component - "video" (autoplay, overlay off) and "photo" (paused,
// overlay on) - rather than two separate implementations.
export default function EventModal({
  blockId,
  mode,
  onClose,
  onDeleted,
}: {
  blockId: number;
  mode: "video" | "photo";
  onClose: () => void;
  onDeleted?: () => void;
}) {
  const [data, setData] = useState<EventData | null>(null);
  const [index, setIndex] = useState(0);
  const [playing, setPlaying] = useState(mode === "video");
  const [overlayOn, setOverlayOn] = useState(true);
  // 11.10 (round 2), Sid ("une correction globale en plus par bloc tant
  // pour reolink que pour pi"): one action applied to every cat-identity
  // detection across the whole block at once (never Prey - see the server-
  // side endpoints' own comments), instead of confirming/correcting each
  // noisy frame one at a time. `bulkTarget` picks which panel's popover is
  // open; the two panels already have separate pid/filename-keyed storage
  // server-side (detection_feedback DB rows vs JSON sidecars), so this
  // stays two small endpoints rather than one shared one.
  const [bulkTarget, setBulkTarget] = useState<"pi" | "reolink" | null>(null);
  const [bulkBusy, setBulkBusy] = useState(false);
  // 10.10, Sid ("deux vidéos côte à côte"): the Reolink comparison panel
  // scrubs independently - its own dense capture cadence (set by
  // watchdog.py's chatiere boost window) doesn't line up frame-for-frame
  // with the flap camera's own, so a forced single-slider sync would just
  // be misleading.
  // 10.10, Sid ("kittyflap démarre toute seule, reolink pas"): starts
  // playing together with the main panel instead (same `mode === "video"`
  // condition), not paused-by-default anymore.
  const [reolinkIndex, setReolinkIndex] = useState(0);
  const [reolinkPlaying, setReolinkPlaying] = useState(mode === "video");
  const [reolinkOverlayOn, setReolinkOverlayOn] = useState(true);
  const reolinkPlayTimerRef = useRef<number | null>(null);
  const [activeObject, setActiveObject] = useState<DetectedObject | null>(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [drawing, setDrawing] = useState(false);
  const [draft, setDraft] = useState<{ x: number; y: number; w: number; h: number } | null>(null);
  const playTimerRef = useRef<number | null>(null);
  const imgWrapRef = useRef<HTMLDivElement | null>(null);
  const dragStart = useRef<{ x: number; y: number } | null>(null);
  // 10.10, Sid ("il faut me donner les outils de correction/modification
  // d'incrustation pour reolink"): same confirm/correct/false-positive +
  // draw workflow as the main panel, mirrored with its own state - the
  // reolink frame has no DB `pid`, corrections key on its filename instead
  // (see correctReolink/saveReolinkDraft below).
  const [activeReolinkObject, setActiveReolinkObject] = useState<DetectedObject | null>(null);
  const [reolinkDrawing, setReolinkDrawing] = useState(false);
  const [reolinkDraft, setReolinkDraft] = useState<{ x: number; y: number; w: number; h: number } | null>(null);
  const reolinkImgWrapRef = useRef<HTMLDivElement | null>(null);
  const reolinkDragStart = useRef<{ x: number; y: number } | null>(null);

  useEffect(() => {
    fetchJson<EventData>(`/app-api/v1/events/${blockId}`)
      .then((d) => {
        setData(d);
        // 07.10, Sid ("le bouton pour voir les incrustations pourrait être
        // activé par défaut ?"): overlay_initial already reflects Sid's own
        // SHOW_IMAGES_WITH_OVERLAY setting (+ fallback-mode safety), no
        // reason to still force it off in video mode specifically.
        setOverlayOn(d.overlay_initial);
        setReolinkIndex(0);
        setReolinkPlaying(mode === "video");
      })
      .catch(() => setMessage("Échec du chargement."));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [blockId]);

  useEffect(() => {
    if (!playing || !data || data.frames.length <= 1) return;
    // 11.10 (round 2), Sid ("les cameras sont toujours extremement en
    // decalage quand je regarde les videos"): the previous version just
    // stepped the index by 1 every fixed 1000/fps ms - that targets the
    // right TOTAL duration (fps was computed as (N-1)/real_span server-
    // side), but real captures aren't evenly spaced (tight bursts, then
    // gaps), so a frame from a dense burst got the same on-screen dwell
    // time as one that was isolated by a multi-second gap. Driving
    // playback from real elapsed time against each frame's own `ts`
    // instead - this panel "holds" a frame until its actual next frame's
    // real timestamp arrives, matching what was truly captured when.
    const times = data.frames.map((f) => parseFrameTs(f.ts));
    const total = data.frames.length;
    const startWallMs = Date.now();
    const startVideoSec = times[Math.min(index, total - 1)] ?? times[0];
    const tickMs = 80;
    playTimerRef.current = window.setInterval(() => {
      const elapsed = startVideoSec + (Date.now() - startWallMs) / 1000;
      const newIndex = indexAtOrBefore(times, elapsed);
      if (newIndex >= total - 1) {
        setIndex(total - 1);
        setPlaying(false);
        return;
      }
      setIndex(newIndex);
    }, tickMs);
    return () => {
      if (playTimerRef.current) window.clearInterval(playTimerRef.current);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [playing, data]);

  useEffect(() => {
    if (!reolinkPlaying || !data || data.reolink_frames.length <= 1) return;
    // Same real-time-driven approach as the main panel above, against this
    // panel's own (far sparser) frame timestamps - keeps both panels
    // genuinely aligned to the same real-world moment while playing
    // together, without forcing a misleading frame-for-frame index sync
    // (10.10's original concern - still avoided, this syncs by real time,
    // not by index).
    const times = data.reolink_frames.map((f) => parseFrameTs(f.ts));
    const total = data.reolink_frames.length;
    const startWallMs = Date.now();
    const startVideoSec = times[Math.min(reolinkIndex, total - 1)] ?? times[0];
    const tickMs = 80;
    reolinkPlayTimerRef.current = window.setInterval(() => {
      const elapsed = startVideoSec + (Date.now() - startWallMs) / 1000;
      const newIndex = indexAtOrBefore(times, elapsed);
      if (newIndex >= total - 1) {
        setReolinkIndex(total - 1);
        setReolinkPlaying(false);
        return;
      }
      setReolinkIndex(newIndex);
    }, tickMs);
    return () => {
      if (reolinkPlayTimerRef.current) window.clearInterval(reolinkPlayTimerRef.current);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [reolinkPlaying, data]);

  const frame = data?.frames[index];
  const reolinkFrame = data?.reolink_frames[reolinkIndex];

  // 07.10, Sid ("il manque toujours la barre en dessous... ça indique sur
  // la vidéo quand il y a des incrustations"): the old event-modal.js drew
  // a two-lane marker strip under the scrubber (prey detections above,
  // other detections below) so you could see at a glance where in the
  // event something was flagged, without scrubbing frame by frame. Same
  // run-length-encoding logic as _buildMarkerSegments, same colors as
  // styles.css's --kh-scrubber-marker-* custom properties.
  // 10.10, Sid ("tu peux mettre ça aussi sous reolink?"): extracted so the
  // Reolink panel's scrubber gets the same prey/other marker strip as the
  // main one, from its own (sparser) frame list.
  type Seg = { start: number; end: number; lane: "prey" | "other"; kind: string };
  function buildMarkerSegments(
    objFrames: { objects: DetectedObject[] }[],
    mouseThreshold: number
  ): Seg[] {
    const segs: Seg[] = [];
    let preyStart = -1,
      preyEnd = -1,
      preyKind = "";
    let otherStart = -1,
      otherEnd = -1,
      otherOn = false;
    objFrames.forEach((f, i) => {
      let preyMaxProb: number | null = null;
      let hasOther = false;
      f.objects.forEach((obj) => {
        const nameL = obj.name.toLowerCase();
        if (nameL === "false-accept") return;
        if (nameL === "prey" || nameL === "beute") {
          preyMaxProb = preyMaxProb === null ? obj.prob : Math.max(preyMaxProb, obj.prob);
        } else if (nameL) {
          hasOther = true;
        }
      });
      const hasPrey = preyMaxProb !== null;
      const preyStrong = hasPrey && (preyMaxProb ?? 0) >= (mouseThreshold ?? 50);
      const thisPreyKind = hasPrey ? (preyStrong ? "prey-strong" : "prey-soft") : "";

      if (!thisPreyKind) {
        if (preyKind) segs.push({ start: preyStart, end: preyEnd, lane: "prey", kind: preyKind });
        preyKind = "";
      } else if (thisPreyKind === preyKind && i === preyEnd + 1) {
        preyEnd = i;
      } else {
        if (preyKind) segs.push({ start: preyStart, end: preyEnd, lane: "prey", kind: preyKind });
        preyKind = thisPreyKind;
        preyStart = i;
        preyEnd = i;
      }

      if (!hasOther) {
        if (otherOn) segs.push({ start: otherStart, end: otherEnd, lane: "other", kind: "other" });
        otherOn = false;
      } else if (otherOn && i === otherEnd + 1) {
        otherEnd = i;
      } else {
        if (otherOn) segs.push({ start: otherStart, end: otherEnd, lane: "other", kind: "other" });
        otherOn = true;
        otherStart = i;
        otherEnd = i;
      }
    });
    if (preyKind) segs.push({ start: preyStart, end: preyEnd, lane: "prey", kind: preyKind });
    if (otherOn) segs.push({ start: otherStart, end: otherEnd, lane: "other", kind: "other" });
    return segs;
  }

  const markerSegments =
    data && data.frames.length > 1 ? buildMarkerSegments(data.frames, data.mouse_threshold ?? 50) : [];
  const reolinkMarkerSegments =
    data && data.reolink_frames.length > 1 ? buildMarkerSegments(data.reolink_frames, data.mouse_threshold ?? 50) : [];

  const SEG_COLOR: Record<string, string> = {
    "prey-strong": "#ef4444",
    "prey-soft": "#fca5a5",
    other: "#22c55e",
  };

  const goPrev = () => {
    setPlaying(false);
    if (data) setIndex((i) => (i - 1 + data.frames.length) % data.frames.length);
  };
  const goNext = () => {
    setPlaying(false);
    if (data) setIndex((i) => (i + 1) % data.frames.length);
  };

  const reolinkGoPrev = () => {
    setReolinkPlaying(false);
    if (data) setReolinkIndex((i) => (i - 1 + data.reolink_frames.length) % data.reolink_frames.length);
  };
  const reolinkGoNext = () => {
    setReolinkPlaying(false);
    if (data) setReolinkIndex((i) => (i + 1) % data.reolink_frames.length);
  };

  const correct = async (
    obj: DetectedObject,
    action: "confirm" | "false_positive" | "correct",
    correctedName?: string
  ) => {
    if (!frame) return;
    try {
      await fetchJson(`/app-api/v1/events/${blockId}/correction`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ pid: frame.pid, idx: obj.idx, action, correctedName }),
      });
      setData((d) => {
        if (!d) return d;
        const frames = d.frames.map((f) =>
          f.pid !== frame.pid
            ? f
            : {
                ...f,
                objects: f.objects.map((o) =>
                  o.idx !== obj.idx
                    ? o
                    : {
                        ...o,
                        reviewed: true,
                        reviewedAs:
                          action === "confirm" ? o.name : action === "false_positive" ? "none" : correctedName,
                      }
                ),
              }
        );
        return { ...d, frames };
      });
      setActiveObject(null);
    } catch {
      setMessage("Échec de l'enregistrement de la correction.");
    }
  };

  // 11.10 (round 2), Sid ("une correction globale en plus par bloc"): a
  // bulk action touches every cat-identity detection across every frame of
  // the panel, not just one object - re-fetching the whole event afterward
  // is simpler and more reliable than hand-patching every affected frame's
  // local state the way `correct`/`correctReolink` do for a single object.
  const correctBulk = async (action: "confirm" | "false_positive" | "correct", correctedName?: string) => {
    if (!bulkTarget) return;
    setBulkBusy(true);
    try {
      const path = bulkTarget === "pi" ? "correction/bulk" : "reolink-correction/bulk";
      const result = await fetchJson<{ ok: boolean; applied: number }>(`/app-api/v1/events/${blockId}/${path}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action, correctedName }),
      });
      const fresh = await fetchJson<EventData>(`/app-api/v1/events/${blockId}`);
      setData(fresh);
      setMessage(`${result.applied} détection(s) corrigée(s) pour tout le bloc.`);
      setBulkTarget(null);
    } catch {
      setMessage("Échec de la correction groupée.");
    } finally {
      setBulkBusy(false);
    }
  };

  const pctFromEvent = (e: React.PointerEvent) => {
    const rect = imgWrapRef.current?.getBoundingClientRect();
    if (!rect) return { x: 0, y: 0 };
    return {
      x: Math.min(100, Math.max(0, ((e.clientX - rect.left) / rect.width) * 100)),
      y: Math.min(100, Math.max(0, ((e.clientY - rect.top) / rect.height) * 100)),
    };
  };

  const onPointerDown = (e: React.PointerEvent) => {
    if (!drawing) return;
    const p = pctFromEvent(e);
    dragStart.current = p;
    setDraft({ x: p.x, y: p.y, w: 0, h: 0 });
    (e.target as Element).setPointerCapture(e.pointerId);
  };

  const onPointerMove = (e: React.PointerEvent) => {
    if (!drawing || !dragStart.current) return;
    const p = pctFromEvent(e);
    const x = Math.min(dragStart.current.x, p.x);
    const y = Math.min(dragStart.current.y, p.y);
    setDraft({ x, y, w: Math.abs(p.x - dragStart.current.x), h: Math.abs(p.y - dragStart.current.y) });
  };

  const onPointerUp = () => {
    dragStart.current = null;
  };

  const saveDraft = async (label: string) => {
    if (!draft || !frame || draft.w < 1 || draft.h < 1) {
      setDraft(null);
      setDrawing(false);
      return;
    }
    setBusy(true);
    try {
      const result = await fetchJson<{ idx: number }>(`/app-api/v1/pictures/${frame.pid}/add-detection`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ x: draft.x, y: draft.y, width: draft.w, height: draft.h, label }),
      });
      setData((d) => {
        if (!d) return d;
        const frames = d.frames.map((f) =>
          f.pid !== frame.pid
            ? f
            : {
                ...f,
                objects: [
                  ...f.objects,
                  {
                    idx: result.idx,
                    name: label,
                    prob: 0,
                    x: draft.x,
                    y: draft.y,
                    w: draft.w,
                    h: draft.h,
                    reviewed: true,
                    reviewedAs: label,
                  },
                ],
              }
        );
        return { ...d, frames };
      });
      setOverlayOn(true);
    } catch {
      setMessage("Échec de l'ajout.");
    } finally {
      setBusy(false);
      setDraft(null);
      setDrawing(false);
    }
  };

  // 10.10, Sid ("il faut me donner les outils de correction/modification
  // d'incrustation pour reolink"): mirrors correct/pctFromEvent/onPointer*/
  // saveDraft above, keyed on the reolink frame's filename instead of a DB
  // pid (see the new /reolink-correction and /reolink-add-detection routes).
  const correctReolink = async (
    obj: DetectedObject,
    action: "confirm" | "false_positive" | "correct",
    correctedName?: string
  ) => {
    if (!reolinkFrame) return;
    try {
      await fetchJson(`/app-api/v1/events/${blockId}/reolink-correction`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ filename: reolinkFrame.filename, idx: obj.idx, action, correctedName }),
      });
      setData((d) => {
        if (!d) return d;
        const reolink_frames = d.reolink_frames.map((f) =>
          f.filename !== reolinkFrame.filename
            ? f
            : {
                ...f,
                objects: f.objects.map((o) =>
                  o.idx !== obj.idx
                    ? o
                    : {
                        ...o,
                        reviewed: true,
                        reviewedAs:
                          action === "confirm" ? o.name : action === "false_positive" ? "none" : correctedName,
                      }
                ),
              }
        );
        return { ...d, reolink_frames };
      });
      setActiveReolinkObject(null);
    } catch {
      setMessage("Échec de l'enregistrement de la correction.");
    }
  };

  const pctFromReolinkEvent = (e: React.PointerEvent) => {
    const rect = reolinkImgWrapRef.current?.getBoundingClientRect();
    if (!rect) return { x: 0, y: 0 };
    return {
      x: Math.min(100, Math.max(0, ((e.clientX - rect.left) / rect.width) * 100)),
      y: Math.min(100, Math.max(0, ((e.clientY - rect.top) / rect.height) * 100)),
    };
  };

  const onReolinkPointerDown = (e: React.PointerEvent) => {
    if (!reolinkDrawing) return;
    const p = pctFromReolinkEvent(e);
    reolinkDragStart.current = p;
    setReolinkDraft({ x: p.x, y: p.y, w: 0, h: 0 });
    (e.target as Element).setPointerCapture(e.pointerId);
  };

  const onReolinkPointerMove = (e: React.PointerEvent) => {
    if (!reolinkDrawing || !reolinkDragStart.current) return;
    const p = pctFromReolinkEvent(e);
    const x = Math.min(reolinkDragStart.current.x, p.x);
    const y = Math.min(reolinkDragStart.current.y, p.y);
    setReolinkDraft({
      x,
      y,
      w: Math.abs(p.x - reolinkDragStart.current.x),
      h: Math.abs(p.y - reolinkDragStart.current.y),
    });
  };

  const onReolinkPointerUp = () => {
    reolinkDragStart.current = null;
  };

  const saveReolinkDraft = async (label: string) => {
    if (!reolinkDraft || !reolinkFrame || reolinkDraft.w < 1 || reolinkDraft.h < 1) {
      setReolinkDraft(null);
      setReolinkDrawing(false);
      return;
    }
    setBusy(true);
    try {
      const result = await fetchJson<{ idx: number }>(`/app-api/v1/events/${blockId}/reolink-add-detection`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          filename: reolinkFrame.filename,
          x: reolinkDraft.x,
          y: reolinkDraft.y,
          width: reolinkDraft.w,
          height: reolinkDraft.h,
          label,
        }),
      });
      setData((d) => {
        if (!d) return d;
        const reolink_frames = d.reolink_frames.map((f) =>
          f.filename !== reolinkFrame.filename
            ? f
            : {
                ...f,
                objects: [
                  ...f.objects,
                  {
                    idx: result.idx,
                    name: label,
                    prob: 0,
                    x: reolinkDraft.x,
                    y: reolinkDraft.y,
                    w: reolinkDraft.w,
                    h: reolinkDraft.h,
                    reviewed: true,
                    reviewedAs: label,
                  },
                ],
              }
        );
        return { ...d, reolink_frames };
      });
      setReolinkOverlayOn(true);
    } catch {
      setMessage("Échec de l'ajout.");
    } finally {
      setBusy(false);
      setReolinkDraft(null);
      setReolinkDrawing(false);
    }
  };

  const doDelete = async () => {
    setBusy(true);
    try {
      await fetchJson(`/app-api/v1/events/${blockId}`, { method: "DELETE" });
      onDeleted?.();
      onClose();
    } catch {
      setMessage("Échec de la suppression.");
      setBusy(false);
    }
  };

  const sendToLabelStudio = async () => {
    if (!frame) return;
    setBusy(true);
    setMessage(null);
    try {
      await fetchJson(`/app-api/v1/events/${blockId}/send/${frame.pid}`, { method: "POST" });
      setMessage("Envoyé à Label Studio.");
    } catch (err) {
      setMessage(err instanceof Error ? err.message : "Échec de l'envoi.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div
      className="kh-photo-lightbox-backdrop fixed inset-0 z-50 flex items-center justify-center bg-black/90 p-2"
      style={{ touchAction: "pan-y" }}
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div
        className={`flex max-h-full w-full flex-col overflow-hidden rounded-xl bg-neutral-950 ${
          data && data.reolink_frames.length > 0 ? "max-w-lg sm:max-w-3xl" : "max-w-lg"
        }`}
      >
        {/* 10.10, Sid ("deux vidéos côte à côte"): stacked on mobile
            (her primary device - a half-width panel there would be too
            small to read), side by side from sm: up. */}
        <div className="flex flex-col overflow-y-auto sm:flex-row">
        <div className="flex w-full shrink-0 flex-col sm:w-1/2">
        {/* Picture + overlay */}
        <div
          ref={imgWrapRef}
          className="relative mx-auto w-full touch-none bg-black"
          style={{ aspectRatio: data?.aspect ?? "4 / 3" }}
          onPointerDown={onPointerDown}
          onPointerMove={onPointerMove}
          onPointerUp={onPointerUp}
        >
          {!data && (
            <div className="flex h-full items-center justify-center text-white/60">Chargement…</div>
          )}
          {frame && (
            <>
              <img src={frame.thumb_url} alt="" className="h-full w-full object-contain" />
              {overlayOn &&
                !data?.fallback_mode &&
                frame.objects.map((obj) => {
                  // 07.10, Sid ("avant j'avais... vert quand c'était un chat
                  // et rouge/orange selon le pourcentage de certitude"):
                  // exact colors from the old www/event-modal.js
                  // (_renderOverlay) - green for anything that isn't prey,
                  // light red for a weak prey guess, solid red once it
                  // crosses mouseThreshold (the same threshold the backend
                  // uses to decide whether to actually block the cat).
                  const isPrey = obj.name.toLowerCase() === "prey" || obj.name.toLowerCase() === "beute";
                  const preyStrong = isPrey && obj.prob >= (data?.mouse_threshold ?? 50);
                  const color =
                    isPrey && !preyStrong
                      ? "rgb(252, 165, 165)"
                      : isPrey
                        ? "rgb(255, 0, 0)"
                        : "rgb(0, 180, 0)";
                  let label = `${obj.name} (${Math.round(obj.prob)}%)`;
                  if (obj.reviewed) {
                    if (!obj.reviewedAs || obj.reviewedAs === "none") label = `✗ ${label}`;
                    else if (obj.reviewedAs !== obj.name) label = `✓ ${label} → ${obj.reviewedAs}`;
                    else label = `✓ ${label}`;
                  }
                  return (
                    <button
                      key={obj.idx}
                      onClick={() => {
                        if (drawing) return;
                        setPlaying(false);
                        setActiveObject(obj);
                      }}
                      className="absolute text-left"
                      style={{
                        left: `${obj.x}%`,
                        top: `${obj.y}%`,
                        width: `${obj.w}%`,
                        height: `${obj.h}%`,
                        border: `2px solid ${color}`,
                        backgroundColor: color.replace("rgb(", "rgba(").replace(")", ", 0.05)"),
                      }}
                    >
                      <span
                        className="absolute left-0 top-1 whitespace-nowrap rounded px-1 text-[0.65rem] font-medium text-white"
                        style={{ backgroundColor: color.replace("rgb(", "rgba(").replace(")", ", 0.85)") }}
                      >
                        {label}
                      </span>
                    </button>
                  );
                })}
              {frame.ts && (
                <span className="absolute bottom-1 right-1 rounded bg-black/60 px-1.5 py-0.5 text-[0.7rem] text-white/80">
                  {frame.ts}
                </span>
              )}
              {draft && (
                <div
                  className="pointer-events-none absolute border-2 border-dashed border-white"
                  style={{ left: `${draft.x}%`, top: `${draft.y}%`, width: `${draft.w}%`, height: `${draft.h}%` }}
                />
              )}
            </>
          )}
          </div>

          {/* 10.10, Sid ("deux lecteurs vidéos identiques côte à côte"):
              label + scrubber + mini-toolbar, confined to this panel's own
              half - mirrors the Reolink panel's structure exactly instead
              of sharing one full-width scrubber/toolbar below both. */}
          <div className="px-2 pb-1 pt-1 text-center text-[0.65rem] text-white/50">Caméra interne (Kittyflap)</div>
          {/* Scrubber + detection-marker strip. 07.10, Sid ("la barre est
              trop petite et complètement masquée par la barre de progression
              quand elle avance"): overlaying the markers directly on top of
              the native range input let the slider's own thumb/track paint
              over them as it moved - now its own separate row underneath,
              thicker (6px/lane), never covered by anything. */}
          {data && data.frames.length > 1 && (
            <div className="px-2 pb-2">
              <input
                type="range"
                min={0}
                max={data.frames.length - 1}
                value={index}
                onChange={(e) => {
                  setPlaying(false);
                  setIndex(Number(e.target.value));
                }}
                className="w-full accent-white"
              />
              <div className="relative mt-1.5 h-[14px]">
                {markerSegments.map((seg, i) => {
                  const n = data.frames.length;
                  const pct = (idx: number) => (idx / Math.max(1, n - 1)) * 100;
                  const left = pct(seg.start);
                  const width = Math.max(0, pct(seg.end) - left);
                  const single = seg.start === seg.end;
                  return (
                    <div
                      key={i}
                      className="absolute h-[6px] rounded-full"
                      style={{
                        left: `${left.toFixed(4)}%`,
                        width: single ? "10px" : `${width.toFixed(4)}%`,
                        transform: single ? "translateX(-50%)" : undefined,
                        top: seg.lane === "prey" ? 0 : "8px",
                        backgroundColor: SEG_COLOR[seg.kind],
                      }}
                    />
                  );
                })}
              </div>
            </div>
          )}
          <div className="flex items-center justify-center gap-1 border-t border-white/10 p-2">
            <button onClick={goPrev} disabled={!data || data.frames.length <= 1} className="rounded-md bg-white/10 px-3 py-2 disabled:opacity-30">
              ⏮
            </button>
            <button
              onClick={() => setPlaying((p) => !p)}
              disabled={!data || data.frames.length <= 1}
              className="rounded-md bg-white/10 px-3 py-2 disabled:opacity-30"
            >
              {playing ? "⏸" : "▶️"}
            </button>
            <button onClick={goNext} disabled={!data || data.frames.length <= 1} className="rounded-md bg-white/10 px-3 py-2 disabled:opacity-30">
              ⏭
            </button>
            <button
              onClick={() => setOverlayOn((o) => !o)}
              disabled={data?.fallback_mode}
              className={`rounded-md px-3 py-2 disabled:opacity-30 ${overlayOn ? "bg-white/30" : "bg-white/10"}`}
              title="Afficher/masquer les détections"
            >
              🏷️
            </button>
            <button
              onClick={() => {
                setDrawing((d) => !d);
                setDraft(null);
                setActiveObject(null);
                setPlaying(false);
              }}
              disabled={!frame}
              className={`rounded-md px-3 py-2 text-sm disabled:opacity-30 ${drawing ? "bg-white/30" : "bg-white/10"}`}
              title="Ajouter une détection manquée"
            >
              {drawing ? "✕ Dessin" : "✏️"}
            </button>
            <button
              onClick={() => {
                setBulkTarget("pi");
                setActiveObject(null);
                setDrawing(false);
              }}
              disabled={!data || data.frames.length === 0}
              className={`rounded-md px-3 py-2 text-sm disabled:opacity-30 ${bulkTarget === "pi" ? "bg-white/30" : "bg-white/10"}`}
              title="Corriger tout le bloc (caméra Pi)"
            >
              📦
            </button>
          </div>
        </div>

        {/* 10.10, Sid ("il faut me donner les outils de
            correction/modification d'incrustation pour reolink"): same
            click-a-box-to-correct + draw-a-missed-detection workflow as
            the main panel now, see correctReolink/saveReolinkDraft. */}
        {data && data.reolink_frames.length > 0 && (
          <div className="flex w-full shrink-0 flex-col border-t border-white/10 sm:w-1/2 sm:border-l sm:border-t-0">
            <div
              ref={reolinkImgWrapRef}
              className="relative mx-auto w-full touch-none bg-black"
              style={{ aspectRatio: "16 / 9" }}
              onPointerDown={onReolinkPointerDown}
              onPointerMove={onReolinkPointerMove}
              onPointerUp={onReolinkPointerUp}
            >
              {reolinkFrame ? (
                <>
                  <img src={reolinkFrame.url} alt="" className="h-full w-full object-contain" />
                  {reolinkOverlayOn &&
                    reolinkFrame.objects.map((obj) => {
                      const isPrey = obj.name.toLowerCase() === "prey" || obj.name.toLowerCase() === "beute";
                      const preyStrong = isPrey && obj.prob >= (data?.mouse_threshold ?? 50);
                      const color =
                        isPrey && !preyStrong
                          ? "rgb(252, 165, 165)"
                          : isPrey
                            ? "rgb(255, 0, 0)"
                            : "rgb(0, 180, 0)";
                      let label = `${obj.name} (${Math.round(obj.prob)}%)`;
                      if (obj.reviewed) {
                        if (!obj.reviewedAs || obj.reviewedAs === "none") label = `✗ ${label}`;
                        else if (obj.reviewedAs !== obj.name) label = `✓ ${label} → ${obj.reviewedAs}`;
                        else label = `✓ ${label}`;
                      }
                      return (
                        <button
                          key={obj.idx}
                          onClick={() => {
                            if (reolinkDrawing) return;
                            setReolinkPlaying(false);
                            setActiveReolinkObject(obj);
                          }}
                          className="absolute text-left"
                          style={{
                            left: `${obj.x}%`,
                            top: `${obj.y}%`,
                            width: `${obj.w}%`,
                            height: `${obj.h}%`,
                            border: `2px solid ${color}`,
                            backgroundColor: color.replace("rgb(", "rgba(").replace(")", ", 0.05)"),
                          }}
                        >
                          <span
                            className="absolute left-0 top-1 whitespace-nowrap rounded px-1 text-[0.65rem] font-medium text-white"
                            style={{ backgroundColor: color.replace("rgb(", "rgba(").replace(")", ", 0.85)") }}
                          >
                            {label}
                          </span>
                        </button>
                      );
                    })}
                  <span className="absolute bottom-1 right-1 rounded bg-black/60 px-1.5 py-0.5 text-[0.7rem] text-white/80">
                    {reolinkFrame.ts}
                  </span>
                  {reolinkDraft && (
                    <div
                      className="pointer-events-none absolute border-2 border-dashed border-white"
                      style={{
                        left: `${reolinkDraft.x}%`,
                        top: `${reolinkDraft.y}%`,
                        width: `${reolinkDraft.w}%`,
                        height: `${reolinkDraft.h}%`,
                      }}
                    />
                  )}
                </>
              ) : (
                <div className="flex h-full items-center justify-center text-white/40 text-sm">
                  Pas de capture Reolink pour cet événement
                </div>
              )}
            </div>
            <div className="px-2 pb-1 pt-1 text-center text-[0.65rem] text-white/50">Chatière ext (Reolink)</div>
            {data.reolink_frames.length > 1 && (
              <div className="px-2 pb-2">
                <input
                  type="range"
                  min={0}
                  max={data.reolink_frames.length - 1}
                  value={reolinkIndex}
                  onChange={(e) => {
                    setReolinkPlaying(false);
                    setReolinkIndex(Number(e.target.value));
                  }}
                  className="w-full accent-white"
                />
                <div className="relative mt-1.5 h-[14px]">
                  {reolinkMarkerSegments.map((seg, i) => {
                    const n = data.reolink_frames.length;
                    const pct = (idx: number) => (idx / Math.max(1, n - 1)) * 100;
                    const left = pct(seg.start);
                    const width = Math.max(0, pct(seg.end) - left);
                    const single = seg.start === seg.end;
                    return (
                      <div
                        key={i}
                        className="absolute h-[6px] rounded-full"
                        style={{
                          left: `${left.toFixed(4)}%`,
                          width: single ? "10px" : `${width.toFixed(4)}%`,
                          transform: single ? "translateX(-50%)" : undefined,
                          top: seg.lane === "prey" ? 0 : "8px",
                          backgroundColor: SEG_COLOR[seg.kind],
                        }}
                      />
                    );
                  })}
                </div>
              </div>
            )}
            <div className="flex items-center justify-center gap-1 border-t border-white/10 p-2">
              <button
                onClick={reolinkGoPrev}
                disabled={data.reolink_frames.length <= 1}
                className="rounded-md bg-white/10 px-3 py-2 disabled:opacity-30"
              >
                ⏮
              </button>
              <button
                onClick={() => setReolinkPlaying((p) => !p)}
                disabled={data.reolink_frames.length <= 1}
                className="rounded-md bg-white/10 px-3 py-2 disabled:opacity-30"
              >
                {reolinkPlaying ? "⏸" : "▶️"}
              </button>
              <button
                onClick={reolinkGoNext}
                disabled={data.reolink_frames.length <= 1}
                className="rounded-md bg-white/10 px-3 py-2 disabled:opacity-30"
              >
                ⏭
              </button>
              <button
                onClick={() => setReolinkOverlayOn((o) => !o)}
                className={`rounded-md px-3 py-2 ${reolinkOverlayOn ? "bg-white/30" : "bg-white/10"}`}
                title="Afficher/masquer les détections"
              >
                🏷️
              </button>
              <button
                onClick={() => {
                  setReolinkDrawing((d) => !d);
                  setReolinkDraft(null);
                  setActiveReolinkObject(null);
                  setReolinkPlaying(false);
                }}
                disabled={!reolinkFrame}
                className={`rounded-md px-3 py-2 text-sm disabled:opacity-30 ${reolinkDrawing ? "bg-white/30" : "bg-white/10"}`}
                title="Ajouter une détection manquée"
              >
                {reolinkDrawing ? "✕ Dessin" : "✏️"}
              </button>
              <button
                onClick={() => {
                  setBulkTarget("reolink");
                  setActiveReolinkObject(null);
                  setReolinkDrawing(false);
                }}
                disabled={!data || data.reolink_frames.length === 0}
                className={`rounded-md px-3 py-2 text-sm disabled:opacity-30 ${bulkTarget === "reolink" ? "bg-white/30" : "bg-white/10"}`}
                title="Corriger tout le bloc (Reolink)"
              >
                📦
              </button>
            </div>
          </div>
        )}
        </div>

        {/* Correction popover */}
        {activeObject && data && (
          <div className="border-t border-white/10 bg-neutral-900 p-3">
            <div className="mb-2 text-sm">
              {activeObject.name} ({activeObject.prob}%)
            </div>
            <div className="flex flex-wrap gap-2">
              <button
                onClick={() => correct(activeObject, "confirm")}
                className="rounded-md bg-green-500/20 px-3 py-1.5 text-sm text-green-200"
              >
                ✓ Correct
              </button>
              {data.correction_labels
                .filter((l) => l !== activeObject.name)
                .map((label) => (
                  <button
                    key={label}
                    onClick={() => correct(activeObject, "correct", label)}
                    className="rounded-md bg-white/10 px-3 py-1.5 text-sm"
                  >
                    → {label}
                  </button>
                ))}
              <button
                onClick={() => correct(activeObject, "false_positive")}
                className="rounded-md bg-red-500/15 px-3 py-1.5 text-sm text-red-300"
              >
                Fausse détection
              </button>
              <button
                onClick={() => setActiveObject(null)}
                className="ml-auto rounded-md px-3 py-1.5 text-sm text-white/60"
              >
                Fermer
              </button>
            </div>
          </div>
        )}

        {/* 11.10 (round 2), Sid ("une correction globale en plus par
            bloc tant pour reolink que pour pi"): same action set as the
            single-object popover above, applied to every cat-identity
            detection across the whole bulkTarget panel at once. */}
        {bulkTarget && data && (
          <div className="border-t border-white/10 bg-neutral-900 p-3">
            <div className="mb-2 text-sm">
              Corriger tout le bloc ({bulkTarget === "pi" ? "caméra Pi" : "Reolink"}) :
            </div>
            <div className="flex flex-wrap gap-2">
              <button
                onClick={() => correctBulk("confirm")}
                disabled={bulkBusy}
                className="rounded-md bg-green-500/20 px-3 py-1.5 text-sm text-green-200 disabled:opacity-40"
              >
                ✓ Confirmer tout
              </button>
              {data.correction_labels.map((label) => (
                <button
                  key={label}
                  onClick={() => correctBulk("correct", label)}
                  disabled={bulkBusy}
                  className="rounded-md bg-white/10 px-3 py-1.5 text-sm disabled:opacity-40"
                >
                  → {label}
                </button>
              ))}
              <button
                onClick={() => correctBulk("false_positive")}
                disabled={bulkBusy}
                className="rounded-md bg-red-500/15 px-3 py-1.5 text-sm text-red-300 disabled:opacity-40"
              >
                Fausse détection
              </button>
              <button
                onClick={() => setBulkTarget(null)}
                disabled={bulkBusy}
                className="ml-auto rounded-md px-3 py-1.5 text-sm text-white/60 disabled:opacity-40"
              >
                Fermer
              </button>
            </div>
          </div>
        )}

        {drawing && draft && draft.w >= 1 && draft.h >= 1 && data && (
          <div className="border-t border-white/10 bg-neutral-900 p-3">
            <div className="mb-2 text-sm">Nouvelle détection :</div>
            <div className="flex flex-wrap gap-2">
              {data.correction_labels.map((label) => (
                <button
                  key={label}
                  onClick={() => saveDraft(label)}
                  disabled={busy}
                  className="rounded-md bg-white/10 px-3 py-1.5 text-sm disabled:opacity-40"
                >
                  {label}
                </button>
              ))}
              <button
                onClick={() => {
                  setDraft(null);
                  setDrawing(false);
                }}
                className="ml-auto rounded-md px-3 py-1.5 text-sm text-white/60"
              >
                Annuler
              </button>
            </div>
          </div>
        )}

        {/* 10.10, Sid ("outils de correction... pour reolink"): same two
            popovers as above, scoped to the reolink panel's own state. */}
        {activeReolinkObject && data && (
          <div className="border-t border-white/10 bg-neutral-900 p-3">
            <div className="mb-2 text-sm">
              {activeReolinkObject.name} ({activeReolinkObject.prob}%)
            </div>
            <div className="flex flex-wrap gap-2">
              <button
                onClick={() => correctReolink(activeReolinkObject, "confirm")}
                className="rounded-md bg-green-500/20 px-3 py-1.5 text-sm text-green-200"
              >
                ✓ Correct
              </button>
              {data.correction_labels
                .filter((l) => l !== activeReolinkObject.name)
                .map((label) => (
                  <button
                    key={label}
                    onClick={() => correctReolink(activeReolinkObject, "correct", label)}
                    className="rounded-md bg-white/10 px-3 py-1.5 text-sm"
                  >
                    → {label}
                  </button>
                ))}
              <button
                onClick={() => correctReolink(activeReolinkObject, "false_positive")}
                className="rounded-md bg-red-500/15 px-3 py-1.5 text-sm text-red-300"
              >
                Fausse détection
              </button>
              <button
                onClick={() => setActiveReolinkObject(null)}
                className="ml-auto rounded-md px-3 py-1.5 text-sm text-white/60"
              >
                Fermer
              </button>
            </div>
          </div>
        )}

        {reolinkDrawing && reolinkDraft && reolinkDraft.w >= 1 && reolinkDraft.h >= 1 && data && (
          <div className="border-t border-white/10 bg-neutral-900 p-3">
            <div className="mb-2 text-sm">Nouvelle détection (Reolink) :</div>
            <div className="flex flex-wrap gap-2">
              {data.correction_labels.map((label) => (
                <button
                  key={label}
                  onClick={() => saveReolinkDraft(label)}
                  disabled={busy}
                  className="rounded-md bg-white/10 px-3 py-1.5 text-sm disabled:opacity-40"
                >
                  {label}
                </button>
              ))}
              <button
                onClick={() => {
                  setReolinkDraft(null);
                  setReolinkDrawing(false);
                }}
                className="ml-auto rounded-md px-3 py-1.5 text-sm text-white/60"
              >
                Annuler
              </button>
            </div>
          </div>
        )}

        {message && <p className="px-3 text-center text-xs text-white/60">{message}</p>}

        {/* Toolbar - global, per-event actions only now; per-camera
            play/scrub/overlay controls moved into each panel above (10.10,
            Sid: "deux lecteurs vidéos identiques côte à côte"). */}
        <div className="flex items-center justify-end gap-1 border-t border-white/10 p-2">
          <div className="flex gap-1">
            <a
              href={frame ? `/app-api/v1/events/${blockId}/download/${frame.pid}` : undefined}
              className="rounded-md bg-white/10 px-3 py-2"
              title="Télécharger cette photo"
            >
              💾
            </a>
            <button onClick={sendToLabelStudio} disabled={busy} className="rounded-md bg-white/10 px-3 py-2 disabled:opacity-30" title="Envoyer à Label Studio">
              ⬆️
            </button>
            <a
              href={`/app-api/v1/events/${blockId}/download-zip`}
              className="rounded-md bg-white/10 px-3 py-2"
              title="Télécharger tout (ZIP)"
            >
              🗂️
            </a>
            <button onClick={doDelete} disabled={busy} className="rounded-md bg-red-500/15 px-3 py-2 text-red-300 disabled:opacity-30" title="Supprimer l'événement">
              🗑️
            </button>
            <button onClick={onClose} className="rounded-md bg-white/10 px-3 py-2" title="Fermer">
              ✕
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
