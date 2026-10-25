import { useEffect, useRef, useState } from "react";
import { fetchJson } from "../lib/api";

export interface DetectedObject {
  x: number;
  y: number;
  w: number;
  h: number;
  name: string;
  prob: number;
  idx: number;
  reviewed?: boolean;
  reviewedAs?: string;
  manual?: boolean;
}

export interface Picture {
  id: number;
  block_id: number;
  time: string;
  thumb_url: string;
  cat_name: string | null;
  rfid: string | null;
  mouse_probability: number;
  mouse_threshold: number;
  objects: DetectedObject[];
}

function objectColor(obj: DetectedObject, mouseThreshold: number): string {
  const isPrey = obj.name.toLowerCase() === "prey" || obj.name.toLowerCase() === "beute";
  const preyStrong = isPrey && obj.prob >= mouseThreshold;
  if (isPrey && !preyStrong) return "rgb(252, 165, 165)";
  if (isPrey) return "rgb(255, 0, 0)";
  return "rgb(0, 180, 0)";
}

function objectLabel(obj: DetectedObject): string {
  let label = `${obj.name} (${Math.round(obj.prob)}%)`;
  if (obj.reviewed) {
    if (!obj.reviewedAs || obj.reviewedAs === "none") label = `✗ ${label}`;
    else if (obj.reviewedAs !== obj.name) label = `✓ ${label} → ${obj.reviewedAs}`;
    else label = `✓ ${label}`;
  }
  return label;
}

// 07.10, Sid ("vas-y pour les photos... je puisse tagger directement depuis
// ces photos"): single-photo detail - tap a detection box to confirm/
// correct/mark false, or draw a new one by hand when the model missed it
// entirely. Separate from EventModal (multi-frame event player) - a Photos
// grid card is always exactly one photo, not a block of frames.
export default function PictureDetail({
  picture,
  correctionLabels,
  onClose,
  onChanged,
  onDeleted,
  onPrev,
  onNext,
}: {
  picture: Picture;
  correctionLabels: string[];
  onClose: () => void;
  onChanged: (updated: Picture) => void;
  onDeleted: () => void;
  onPrev?: () => void;
  onNext?: () => void;
}) {
  const [activeObject, setActiveObject] = useState<DetectedObject | null>(null);
  const [drawing, setDrawing] = useState(false);
  const [draft, setDraft] = useState<{ x: number; y: number; w: number; h: number } | null>(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const imgWrapRef = useRef<HTMLDivElement | null>(null);
  const dragStart = useRef<{ x: number; y: number } | null>(null);

  // 08.10: PictureDetail stays mounted across prev/next (only the `picture`
  // prop changes) - without this, an open correction popover or in-progress
  // draw from the last photo would stay visible over the new one.
  useEffect(() => {
    setActiveObject(null);
    setDrawing(false);
    setDraft(null);
    setMessage(null);
  }, [picture.id]);

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

  const correct = async (
    obj: DetectedObject,
    action: "confirm" | "false_positive" | "correct",
    correctedName?: string
  ) => {
    try {
      await fetchJson(`/app-api/v1/pictures/${picture.id}/correction`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ idx: obj.idx, action, correctedName }),
      });
      const objects = picture.objects.map((o) =>
        o.idx !== obj.idx
          ? o
          : {
              ...o,
              reviewed: true,
              reviewedAs:
                action === "confirm" ? o.name : action === "false_positive" ? "none" : correctedName,
            }
      );
      onChanged({ ...picture, objects });
      setActiveObject(null);
    } catch {
      setMessage("Échec de l'enregistrement de la correction.");
    }
  };

  const saveDraft = async (label: string) => {
    if (!draft || draft.w < 1 || draft.h < 1) {
      setDraft(null);
      setDrawing(false);
      return;
    }
    setBusy(true);
    try {
      const result = await fetchJson<{ idx: number }>(
        `/app-api/v1/pictures/${picture.id}/add-detection`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ x: draft.x, y: draft.y, width: draft.w, height: draft.h, label }),
        }
      );
      onChanged({
        ...picture,
        objects: [
          ...picture.objects,
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
            manual: true,
          },
        ],
      });
    } catch {
      setMessage("Échec de l'ajout.");
    } finally {
      setBusy(false);
      setDraft(null);
      setDrawing(false);
    }
  };

  const doDelete = async () => {
    setBusy(true);
    try {
      await fetchJson(`/app-api/v1/pictures/${picture.id}`, { method: "DELETE" });
      onDeleted();
      onClose();
    } catch {
      setMessage("Échec de la suppression.");
      setBusy(false);
    }
  };

  // 25.10, Sid ("j'ai que deux photos qui sont venues de pookie en HK...
  // faut que je corrige la forme de la boîte. Donc une fonction de
  // transfert vers paws review") - envoie l'état CORRIGÉ actuel (après
  // confirm/correct/false_positive/ajout manuel ci-dessus, déjà enregistré
  // côté serveur) vers Paws Review, qui upsert sur cette capture (même
  // photo renvoyée après une nouvelle correction = mise à jour, pas de
  // doublon).
  const sendToPawsReview = async () => {
    setBusy(true);
    try {
      const result = await fetchJson<{ action: string }>(
        `/app-api/v1/pictures/${picture.id}/send-to-paws-review`,
        { method: "POST" }
      );
      setMessage(
        result.action === "updated"
          ? "Mise à jour envoyée vers Paws Review."
          : "Envoyée vers Paws Review."
      );
    } catch {
      setMessage("Échec de l'envoi vers Paws Review.");
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
      <div className="flex max-h-full w-full max-w-lg flex-col overflow-hidden rounded-xl bg-neutral-950">
        <div
          ref={imgWrapRef}
          className="relative mx-auto w-full touch-none bg-black"
          style={{ aspectRatio: "4 / 3" }}
          onPointerDown={onPointerDown}
          onPointerMove={onPointerMove}
          onPointerUp={onPointerUp}
        >
          <img src={picture.thumb_url} alt="" className="h-full w-full object-contain" />

          {picture.objects.map((obj) => {
            const color = objectColor(obj, picture.mouse_threshold);
            return (
              <button
                key={obj.idx}
                onClick={() => {
                  if (drawing) return;
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
                  {objectLabel(obj)}
                </span>
              </button>
            );
          })}

          {draft && (
            <div
              className="pointer-events-none absolute border-2 border-dashed border-white"
              style={{ left: `${draft.x}%`, top: `${draft.y}%`, width: `${draft.w}%`, height: `${draft.h}%` }}
            />
          )}

          {/* 08.10, Sid ("des chevrons pour passer à celle d'après ou
              d'avant sans fermer la photo ?"): prev/next within the
              currently loaded grid page - hidden while drawing a manual
              box so they don't fight the drag gesture. */}
          {!drawing && onPrev && (
            <button
              onClick={(e) => {
                e.stopPropagation();
                onPrev();
              }}
              onPointerDown={(e) => e.stopPropagation()}
              aria-label="Photo précédente"
              className="absolute left-2 top-1/2 flex h-9 w-9 -translate-y-1/2 items-center justify-center rounded-full bg-black/50 text-xl text-white"
            >
              ‹
            </button>
          )}
          {!drawing && onNext && (
            <button
              onClick={(e) => {
                e.stopPropagation();
                onNext();
              }}
              onPointerDown={(e) => e.stopPropagation()}
              aria-label="Photo suivante"
              className="absolute right-2 top-1/2 flex h-9 w-9 -translate-y-1/2 items-center justify-center rounded-full bg-black/50 text-xl text-white"
            >
              ›
            </button>
          )}
        </div>

        {activeObject && (
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
              {correctionLabels
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

        {drawing && draft && draft.w >= 1 && draft.h >= 1 && (
          <div className="border-t border-white/10 bg-neutral-900 p-3">
            <div className="mb-2 text-sm">Nouvelle détection :</div>
            <div className="flex flex-wrap gap-2">
              {correctionLabels.map((label) => (
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

        {message && <p className="px-3 pt-2 text-center text-xs text-white/60">{message}</p>}

        <div className="flex items-center justify-between gap-1 border-t border-white/10 p-2">
          <button
            onClick={() => {
              setDrawing((d) => !d);
              setDraft(null);
              setActiveObject(null);
            }}
            className={`rounded-md px-3 py-2 text-sm ${drawing ? "bg-white/30" : "bg-white/10"}`}
            title="Ajouter une détection manquée"
          >
            {drawing ? "✕ Dessin" : "✏️ Ajouter"}
          </button>
          <div className="flex gap-1">
            <button
              onClick={sendToPawsReview}
              disabled={busy}
              className="rounded-md bg-white/10 px-3 py-2 disabled:opacity-30"
              title="Envoyer (ou mettre à jour) cette photo vers Paws Review"
            >
              📤
            </button>
            <a
              href={`/app-api/v1/events/${picture.block_id}/download/${picture.id}`}
              className="rounded-md bg-white/10 px-3 py-2"
              title="Télécharger cette photo"
            >
              💾
            </a>
            <button onClick={doDelete} disabled={busy} className="rounded-md bg-red-500/15 px-3 py-2 text-red-300 disabled:opacity-30" title="Supprimer la photo">
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
