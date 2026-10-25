import { useCallback, useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router";
import BottomNav from "../components/BottomNav";
import TopBanner from "../components/TopBanner";
import { fetchJson } from "../lib/api";
import { useBackButton } from "../lib/useBackButton";
import { usePageTitle } from "../lib/usePageTitle";

interface TrainingState {
  in_progress: boolean;
  job_id?: string;
  status_label?: string;
  server_status?: { maintenance: boolean; message: string } | null;
}

interface ModelOptions {
  image_sizes: number[];
  user_name: string;
  email: string;
}

interface YoloModelInfo {
  unique_id: string;
  display_name: string;
  creation_date: string;
  is_active: boolean;
  fps: number | null;
  model_image_size: number;
  variant_letter: string;
}

interface ModelReviewResult {
  error?: string;
  all_scanned?: boolean;
  scanned?: number;
  events?: number;
  flagged?: number;
  elapsed_s?: number;
  cancelled?: boolean;
}

interface ModelReviewRow {
  id: number;
  photo_id: number;
  event_block_id: number | null;
  old_name: string | null;
  old_probability: number;
  new_name: string | null;
  new_probability: number;
  rfid_label: string | null;
  // 08.10, Sid ("le RFID pris en compte dans la révision, il se base sur
  // quoi?"): true = vraie lecture de puce, false = simple supposition du
  // modèle vidéo, null = provenance inconnue (ligne antérieure à ce suivi).
  rfid_confirmed: boolean | null;
}

const BASE_MODELS: { value: string; label: string }[] = [
  { value: "n", label: "YOLOv8n" },
  { value: "s", label: "YOLOv8s" },
  { value: "m", label: "YOLOv8m" },
  { value: "l", label: "YOLOv8l" },
  { value: "x", label: "YOLOv8x" },
  { value: "26n", label: "YOLO26n (expérimental)" },
  { value: "26s", label: "YOLO26s (expérimental)" },
  { value: "26m", label: "YOLO26m (expérimental)" },
  { value: "26l", label: "YOLO26l (expérimental)" },
  { value: "26x", label: "YOLO26x (expérimental)" },
];

function Section({
  title,
  description,
  defaultOpen,
  children,
}: {
  title: string;
  description?: string;
  defaultOpen?: boolean;
  children: React.ReactNode;
}) {
  return (
    <details
      open={defaultOpen}
      className="mb-3 rounded-xl border border-white/10 bg-white/5 [&_summary::-webkit-details-marker]:hidden"
    >
      <summary className="cursor-pointer list-none p-4">
        <span className="text-base font-semibold">{title}</span>
        {description && <p className="mt-1 text-sm text-white/65">{description}</p>}
      </summary>
      <div className="border-t border-white/10 p-4">{children}</div>
    </details>
  );
}

const inputClass = "w-full rounded-md border border-white/15 bg-transparent px-3 py-2";

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="mb-3 block">
      <span className="mb-1 block text-sm text-white/70">{label}</span>
      {children}
    </label>
  );
}

export default function AiTrainingPage() {
  usePageTitle("Entraînement de l'IA");
  const navigate = useNavigate();

  useBackButton(
    () => false,
    () => navigate("/presence")
  );

  // ---- Model Training ----
  const [training, setTraining] = useState<TrainingState | null>(null);
  const [modelOptions, setModelOptions] = useState<ModelOptions | null>(null);
  const [modelName, setModelName] = useState("");
  const [userName, setUserName] = useState("");
  const [email, setEmail] = useState("");
  const [baseModel, setBaseModel] = useState("n");
  const [imageSize, setImageSize] = useState(320);
  const [trainingFile, setTrainingFile] = useState<File | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [cancelling, setCancelling] = useState(false);
  const [trainingMsg, setTrainingMsg] = useState<string | null>(null);
  const trainingFileRef = useRef<HTMLInputElement | null>(null);

  const loadTraining = () => {
    fetchJson<TrainingState>("/app-api/v1/ai-training/model-training/state")
      .then(setTraining)
      .catch(() => {});
  };

  useEffect(() => {
    loadTraining();
    fetchJson<ModelOptions>("/app-api/v1/ai-training/model-options")
      .then((d) => {
        setModelOptions(d);
        setUserName(d.user_name);
        setEmail(d.email);
        if (d.image_sizes.length && !d.image_sizes.includes(320)) setImageSize(d.image_sizes[0]);
      })
      .catch(() => {});
  }, []);

  // Poll status every 15s while a training job is in progress.
  useEffect(() => {
    if (!training?.in_progress) return;
    const interval = setInterval(loadTraining, 15000);
    return () => clearInterval(interval);
  }, [training?.in_progress]);

  const submitTraining = async () => {
    setSubmitting(true);
    setTrainingMsg("Envoi en cours…");
    try {
      const form = new FormData();
      form.append("model_name", modelName);
      form.append("user_name", userName);
      form.append("email", email);
      form.append("base_model", baseModel);
      form.append("image_size", String(imageSize));
      if (!trainingFile) {
        setTrainingMsg("Un fichier ZIP est requis.");
        setSubmitting(false);
        return;
      }
      form.append("file", trainingFile);
      await fetchJson<{ ok: boolean; job_id?: string }>(
        "/app-api/v1/ai-training/model-training/submit",
        { method: "POST", body: form }
      );
      setTrainingMsg("Entraînement soumis.");
      setTrainingFile(null);
      if (trainingFileRef.current) trainingFileRef.current.value = "";
      loadTraining();
      setSubmitting(false);
    } catch (err) {
      setTrainingMsg(err instanceof Error ? err.message : "Échec de la soumission.");
      setSubmitting(false);
    }
  };

  const cancelTraining = async () => {
    if (!confirm("Annuler cet entraînement en cours ?")) return;
    setCancelling(true);
    try {
      await fetchJson("/app-api/v1/ai-training/model-training/cancel", { method: "POST" });
      loadTraining();
    } finally {
      setCancelling(false);
    }
  };

  // ---- Model Management ----
  const [models, setModels] = useState<YoloModelInfo[] | null>(null);
  const [editingModel, setEditingModel] = useState<YoloModelInfo | null>(null);
  const [editName, setEditName] = useState("");
  const [modelActionMsg, setModelActionMsg] = useState<string | null>(null);
  const [modelActionBusy, setModelActionBusy] = useState(false);

  const loadModels = () => {
    fetchJson<{ models: YoloModelInfo[] }>("/app-api/v1/ai-training/models")
      .then((d) => setModels(d.models))
      .catch(() => {});
  };

  useEffect(loadModels, []);

  const activateModel = async (unique_id: string) => {
    setModelActionBusy(true);
    setModelActionMsg(null);
    try {
      const result = await fetchJson<{ ok: boolean; already_active?: boolean; live_reload?: boolean }>(
        `/app-api/v1/ai-training/models/${unique_id}/activate`,
        { method: "POST" }
      );
      if (result.already_active) setModelActionMsg("Ce modèle est déjà actif.");
      else if (result.live_reload) setModelActionMsg("Modèle changé avec succès.");
      else setModelActionMsg("Modèle sélectionné, mais le rechargement à chaud a échoué — redémarre pour l'appliquer.");
      loadModels();
    } catch (err) {
      setModelActionMsg(err instanceof Error ? err.message : "Échec de l'activation.");
    } finally {
      setModelActionBusy(false);
    }
  };

  const openEditModel = (m: YoloModelInfo) => {
    setEditingModel(m);
    setEditName(m.display_name);
    setModelActionMsg(null);
  };

  const saveModelName = async () => {
    if (!editingModel) return;
    setModelActionBusy(true);
    try {
      await fetchJson(`/app-api/v1/ai-training/models/${editingModel.unique_id}/rename`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: editName }),
      });
      setEditingModel(null);
      loadModels();
    } catch (err) {
      setModelActionMsg(err instanceof Error ? err.message : "Échec du renommage.");
    } finally {
      setModelActionBusy(false);
    }
  };

  const deleteModel = async () => {
    if (!editingModel) return;
    if (!confirm("Supprimer définitivement ce modèle ?")) return;
    setModelActionBusy(true);
    try {
      await fetchJson(`/app-api/v1/ai-training/models/${editingModel.unique_id}`, { method: "DELETE" });
      setEditingModel(null);
      loadModels();
    } catch (err) {
      setModelActionMsg(err instanceof Error ? err.message : "Échec de la suppression.");
    } finally {
      setModelActionBusy(false);
    }
  };

  // ---- Retroactive Model Review ----
  const [reviewState, setReviewState] = useState<{ running: boolean; pending: number; last_result: ModelReviewResult | null } | null>(null);
  const [reviewQueue, setReviewQueue] = useState<{ pending: number; rows: ModelReviewRow[]; cat_names: string[] } | null>(null);
  const [scanning, setScanning] = useState(false);
  const [rowBusy, setRowBusy] = useState<number | null>(null);
  const [otherPick, setOtherPick] = useState<Record<number, string>>({});

  const loadReviewState = () => {
    fetchJson<{ running: boolean; pending: number; last_result: ModelReviewResult | null }>(
      "/app-api/v1/ai-training/model-review/state"
    )
      .then(setReviewState)
      .catch(() => {});
  };

  const loadReviewQueue = () => {
    fetchJson<{ pending: number; rows: ModelReviewRow[]; cat_names: string[] }>(
      "/app-api/v1/ai-training/model-review/queue"
    )
      .then(setReviewQueue)
      .catch(() => {});
  };

  useEffect(() => {
    loadReviewState();
    loadReviewQueue();
  }, []);

  const [cancellingReview, setCancellingReview] = useState(false);

  const runReview = async () => {
    setScanning(true);
    try {
      await fetchJson("/app-api/v1/ai-training/model-review/run", { method: "POST" });
      loadReviewState();
      loadReviewQueue();
    } finally {
      setScanning(false);
      setCancellingReview(false);
    }
  };

  // 08.10, Sid ("faudra faire un bouton du coup"): the scan loads its own
  // standalone model and ran unthrottled before - the only way to stop it
  // was restarting the whole container, which locked the cats out for a
  // few minutes. This just flags the running scan to stop within a photo
  // or two; runReview()'s own await above resolves once it actually does.
  const cancelReview = async () => {
    setCancellingReview(true);
    try {
      await fetchJson("/app-api/v1/ai-training/model-review/cancel", { method: "POST" });
    } catch {
      setCancellingReview(false);
    }
  };

  // 08.10, Sid ("on peut mettre en place une validation automatique en cas
  // de désaccord... si RFID détecté"): preview-then-confirm, never a single
  // blind "run it" button - this writes real training data in bulk (up to
  // several thousand rows), and her own concern mid-discussion ("une proie
  // non détectée qui passerait entre les gouttes") is exactly why the
  // backend plan excludes any row touching "prey"/rien at all (see
  // _rfid_auto_resolve_plan's comment) - the preview lets her see the real
  // counts before anything is written.
  const [rfidPreview, setRfidPreview] = useState<{
    total_pending: number;
    counts: Record<string, number>;
  } | null>(null);
  const [loadingRfidPreview, setLoadingRfidPreview] = useState(false);
  const [runningRfidResolve, setRunningRfidResolve] = useState(false);
  const [rfidResolveMsg, setRfidResolveMsg] = useState<string | null>(null);

  const loadRfidPreview = async () => {
    setLoadingRfidPreview(true);
    setRfidResolveMsg(null);
    try {
      const d = await fetchJson<{ total_pending: number; counts: Record<string, number> }>(
        "/app-api/v1/ai-training/model-review/rfid-auto-resolve-preview"
      );
      setRfidPreview(d);
    } finally {
      setLoadingRfidPreview(false);
    }
  };

  const runRfidAutoResolve = async () => {
    if (
      !confirm(
        `Confirmer : ${rfidPreview?.counts["keep-old"] ?? 0} "garder l'ancien", ${
          rfidPreview?.counts["confirm-new"] ?? 0
        } "confirmer le nouveau", ${
          rfidPreview?.counts["other"] ?? 0
        } "autre = chat du RFID" seront écrits automatiquement. Continuer ?`
      )
    ) {
      return;
    }
    setRunningRfidResolve(true);
    try {
      const d = await fetchJson<{ counts: Record<string, number> }>(
        "/app-api/v1/ai-training/model-review/rfid-auto-resolve",
        { method: "POST" }
      );
      setRfidResolveMsg(
        `Fait : ${d.counts["keep-old"]} gardés, ${d.counts["confirm-new"]} confirmés, ${d.counts["other"]} corrigés via RFID, ${d.counts.skipped} laissés pour révision manuelle${d.counts.failed ? `, ${d.counts.failed} échoués` : ""}.`
      );
      setRfidPreview(null);
      loadReviewState();
      loadReviewQueue();
    } finally {
      setRunningRfidResolve(false);
    }
  };

  // 09.10, Sid ("la revérification doit gérer automatiquement de réattribuer
  // cet événement-là au bon chat tout du long, pour relancer un
  // entrainement et permettre aux détections suivantes d'apprendre
  // mieux"): a real RFID chip read is ground truth on its own - no YOLO
  // scan needed at all, unlike the section above which only auto-resolves
  // disagreements the scan already flagged. Same preview-then-confirm
  // pattern since this also writes real training data in bulk.
  const [rgtState, setRgtState] = useState<{
    running: boolean;
    remaining_blocks: number;
    last_result: Record<string, unknown> | null;
  } | null>(null);
  const [rgtPreview, setRgtPreview] = useState<{
    blocks: number;
    photos_checked: number;
    corrected: number;
    already_correct: number;
    skipped_prey: number;
    skipped_no_detection: number;
  } | null>(null);
  const [loadingRgtPreview, setLoadingRgtPreview] = useState(false);
  const [runningRgt, setRunningRgt] = useState(false);
  const [cancellingRgt, setCancellingRgt] = useState(false);
  const [rgtMsg, setRgtMsg] = useState<string | null>(null);

  const loadRgtState = useCallback(async () => {
    try {
      const d = await fetchJson<typeof rgtState>("/app-api/v1/ai-training/rfid-ground-truth/state");
      setRgtState(d);
    } catch {
      // Ignore - just a status poll.
    }
  }, []);

  useEffect(() => {
    loadRgtState();
  }, [loadRgtState]);

  const loadRgtPreview = async () => {
    setLoadingRgtPreview(true);
    setRgtMsg(null);
    try {
      const d = await fetchJson<{ ok: boolean; result: typeof rgtPreview }>(
        "/app-api/v1/ai-training/rfid-ground-truth/preview",
        { method: "POST" }
      );
      setRgtPreview(d.result);
    } finally {
      setLoadingRgtPreview(false);
    }
  };

  const runRgt = async () => {
    if (
      !confirm(
        `Confirmer : ${rgtPreview?.corrected ?? 0} photo(s) seront réattribuées directement au chat identifié par RFID, sur ${rgtPreview?.blocks ?? 0} événement(s). Continuer ?`
      )
    ) {
      return;
    }
    setRunningRgt(true);
    setRgtMsg(null);
    try {
      const d = await fetchJson<{ ok: boolean; result: { corrected: number; blocks: number; cancelled: boolean } }>(
        "/app-api/v1/ai-training/rfid-ground-truth/run",
        { method: "POST" }
      );
      setRgtMsg(
        `${d.result.cancelled ? "Annulé après" : "Fait :"} ${d.result.corrected} photo(s) réattribuée(s) sur ${d.result.blocks} événement(s).`
      );
      setRgtPreview(null);
      loadRgtState();
    } finally {
      setRunningRgt(false);
    }
  };

  const cancelRgt = async () => {
    setCancellingRgt(true);
    try {
      await fetchJson("/app-api/v1/ai-training/rfid-ground-truth/cancel", { method: "POST" });
    } finally {
      setCancellingRgt(false);
    }
  };

  const decideRow = async (row: ModelReviewRow, action: "keep-old" | "confirm-new" | "other", otherName?: string) => {
    setRowBusy(row.id);
    try {
      await fetchJson(`/app-api/v1/ai-training/model-review/${row.id}/${action}`, {
        method: "POST",
        ...(action === "other"
          ? { headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name: otherName }) }
          : {}),
      });
      loadReviewState();
      loadReviewQueue();
    } finally {
      setRowBusy(null);
    }
  };

  const displayLabel = (n: string | null) => {
    if (!n) return "rien";
    return n === "prey" ? "proie" : n;
  };

  return (
    <>
      <TopBanner />
      <div className="container mx-auto max-w-2xl px-3 py-4">
        <div className="mb-4 flex items-center gap-3">
          <button
            onClick={() => navigate("/presence")}
            aria-label="Retour"
            className="rounded-md border border-white/15 px-3 py-1.5 text-sm hover:bg-white/10"
          >
            ‹
          </button>
          <div className="text-lg font-semibold">Entraînement de l'IA</div>
        </div>

        <a
          href="http://192.168.178.127:8082"
          target="_blank"
          rel="noreferrer"
          className="mb-4 flex items-center justify-between rounded-lg bg-white/5 p-3 text-sm hover:bg-white/10"
        >
          <span>Trier et équilibrer les photos (Paws Review)</span>
          <span className="rounded-md border border-white/20 px-3 py-1.5">Ouvrir ↗</span>
        </a>

        <Section
          title="Gestion des modèles"
          description="Modèles YOLO installés : activer, renommer ou supprimer."
        >
          {models && models.length === 0 && (
            <p className="text-sm text-white/65">Rien ici pour l'instant. Entraîne d'abord un modèle.</p>
          )}
          {models && models.length > 0 && (
            <div className="flex flex-col gap-2">
              {models.map((m) => (
                <div
                  key={m.unique_id}
                  className="flex items-center justify-between gap-2 rounded-lg bg-white/5 p-3"
                >
                  <div className="min-w-0">
                    <div className="flex items-center gap-2">
                      {m.is_active && (
                        <span className="rounded bg-green-500/20 px-1.5 py-0.5 text-xs text-green-300">
                          Actif
                        </span>
                      )}
                      <span className="truncate font-medium">{m.display_name}</span>
                    </div>
                    <div className="mt-1 text-xs text-white/65">{m.creation_date}</div>
                    <div className="mt-1 flex gap-1 text-xs text-white/60">
                      <span className="rounded bg-white/10 px-1.5 py-0.5">{m.variant_letter}</span>
                      <span className="rounded bg-white/10 px-1.5 py-0.5">{m.model_image_size}px</span>
                      <span className="rounded bg-white/10 px-1.5 py-0.5">
                        {m.fps != null ? `${m.fps.toFixed(1)} FPS` : "—"}
                      </span>
                    </div>
                  </div>
                  <div className="flex shrink-0 gap-1">
                    <button
                      onClick={() => activateModel(m.unique_id)}
                      disabled={modelActionBusy || m.is_active}
                      title="Utiliser ce modèle maintenant"
                      className="rounded-md border border-white/20 px-2 py-1.5 text-sm hover:bg-white/10 disabled:opacity-40"
                    >
                      {modelActionBusy ? "…" : "✓"}
                    </button>
                    <button
                      onClick={() => openEditModel(m)}
                      title="Modifier ou supprimer ce modèle"
                      className="rounded-md border border-white/20 px-2 py-1.5 text-sm hover:bg-white/10"
                    >
                      ✏️
                    </button>
                  </div>
                </div>
              ))}
            </div>
          )}
          {modelActionMsg && !editingModel && <p className="mt-2 text-sm text-white/70">{modelActionMsg}</p>}

          {editingModel && (
            <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4">
              <div className="w-full max-w-sm rounded-xl bg-neutral-900 p-4">
                <h3 className="mb-3 text-base font-semibold">Modifier la configuration du modèle</h3>
                <Field label="Nom">
                  <input
                    value={editName}
                    onChange={(e) => setEditName(e.target.value)}
                    className={inputClass}
                  />
                </Field>
                <p className="mb-3 text-xs text-white/65">
                  Créé le : {editingModel.creation_date}
                </p>
                {editingModel.is_active && (
                  <p className="mb-3 rounded bg-blue-500/10 p-2 text-xs text-blue-300">
                    Ce modèle est actuellement utilisé. Tu ne peux pas le supprimer.
                  </p>
                )}
                {modelActionMsg && <p className="mb-3 text-sm text-white/70">{modelActionMsg}</p>}
                <div className="flex flex-wrap gap-2">
                  <button
                    onClick={saveModelName}
                    disabled={modelActionBusy}
                    className="rounded-md border border-white/20 px-3 py-1.5 text-sm hover:bg-white/10 disabled:opacity-40"
                  >
                    {modelActionBusy ? "Enregistrement…" : "Enregistrer"}
                  </button>
                  <button
                    onClick={() => setEditingModel(null)}
                    className="rounded-md border border-white/20 px-3 py-1.5 text-sm hover:bg-white/10"
                  >
                    Annuler
                  </button>
                  <button
                    onClick={deleteModel}
                    disabled={modelActionBusy || editingModel.is_active}
                    className="rounded-md border border-red-400/40 px-3 py-1.5 text-sm text-red-300 hover:bg-red-400/10 disabled:opacity-40"
                  >
                    {modelActionBusy ? "Suppression…" : "Supprimer"}
                  </button>
                </div>
              </div>
            </div>
          )}
        </Section>

        <Section
          title="Révision rétroactive du modèle"
          description="Relance le modèle actif sur des événements passés et ne signale que les désaccords."
        >
          <p className="mb-3 text-sm text-white/65">
            Relance le modèle actuellement actif sur des photos d'événements passés, et ne signale que
            les cas où il n'est plus d'accord avec ce qui est enregistré. Chaque passage avance vers le
            lot suivant, plus ancien (jamais les mêmes deux fois) - rien n'est écrasé automatiquement, tu
            confirmes ou rejettes chaque cas ci-dessous.
          </p>
          <div className="flex flex-wrap gap-2">
            <button
              onClick={runReview}
              disabled={scanning || !!reviewState?.running}
              className="rounded-md border border-white/20 px-4 py-2 text-sm hover:bg-white/10 disabled:opacity-40"
            >
              {scanning || reviewState?.running ? "Scan en cours…" : "🔄 Scanner avec le modèle actif"}
            </button>
            {(scanning || reviewState?.running) && (
              <button
                onClick={cancelReview}
                disabled={cancellingReview}
                className="rounded-md border border-red-400/40 px-4 py-2 text-sm text-red-300 hover:bg-red-400/10 disabled:opacity-40"
              >
                {cancellingReview ? "Arrêt en cours…" : "⏹ Annuler"}
              </button>
            )}
          </div>

          {reviewState?.last_result && !reviewState.last_result.error && !reviewState.last_result.all_scanned && (
            <p className="mt-2 text-sm text-white/65">
              {reviewState.last_result.cancelled ? "Scan annulé après " : "Dernier scan : "}
              {reviewState.last_result.scanned} photo(s) de {reviewState.last_result.events} événement(s) en{" "}
              {reviewState.last_result.elapsed_s?.toFixed(0)}s, {reviewState.last_result.flagged} signalée(s).
            </p>
          )}
          {reviewState?.last_result?.all_scanned && (
            <p className="mt-2 text-sm text-white/65">
              Tous les événements ont déjà été scannés avec ce modèle. Entraîne et active un nouveau modèle
              pour obtenir un nouveau lot.
            </p>
          )}

          <div className="mt-4 rounded-lg border border-white/10 p-3">
            <h3 className="mb-1 text-sm font-medium">Validation automatique via RFID</h3>
            <p className="mb-2 text-xs text-white/65">
              Si le chat détecté avant OU après correspond au chat identifié par RFID, valide
              automatiquement. Si aucun des deux ne correspond (mais que les deux sont des noms de chats,
              jamais "proie" ou "rien"), corrige directement vers le chat du RFID. Les cas impliquant une
              proie ou aucune détection restent toujours pour révision manuelle.
            </p>
            <button
              onClick={loadRfidPreview}
              disabled={loadingRfidPreview}
              className="rounded-md border border-white/20 px-3 py-1.5 text-xs hover:bg-white/10 disabled:opacity-40"
            >
              {loadingRfidPreview ? "Calcul…" : "📊 Aperçu"}
            </button>
            {rfidPreview && (
              <>
                <div className="mt-2 text-xs text-white/70">
                  Sur {rfidPreview.total_pending} en attente : {rfidPreview.counts["keep-old"]} garderaient
                  l'ancien, {rfidPreview.counts["confirm-new"]} confirmeraient le nouveau,{" "}
                  {rfidPreview.counts["other"]} seraient corrigés vers le chat du RFID,{" "}
                  {rfidPreview.counts["skipped"]} resteraient pour révision manuelle (pas de RFID, ou proie/rien
                  impliqué).
                </div>
                <button
                  onClick={runRfidAutoResolve}
                  disabled={runningRfidResolve}
                  className="mt-2 rounded-md border border-amber-400/40 px-3 py-1.5 text-xs text-amber-300 hover:bg-amber-400/10 disabled:opacity-40"
                >
                  {runningRfidResolve ? "En cours…" : "✅ Appliquer"}
                </button>
              </>
            )}
            {rfidResolveMsg && <p className="mt-2 text-xs text-white/70">{rfidResolveMsg}</p>}
          </div>

          <div className="mt-4 rounded-lg border border-white/10 p-3">
            <h3 className="mb-1 text-sm font-medium">Réattribution directe via RFID confirmé (sans IA)</h3>
            <p className="mb-2 text-xs text-white/65">
              Pour chaque événement avec une vraie lecture de puce RFID (pas une estimation vidéo), compare le
              chat actuellement enregistré au chat du RFID et corrige directement si besoin - sans passer par un
              scan du modèle. Les photos où une proie est détectée, ou sans aucune détection, ne sont jamais
              touchées et restent pour révision manuelle.
            </p>
            <div className="flex flex-wrap items-center gap-2">
              <button
                onClick={loadRgtPreview}
                disabled={loadingRgtPreview || runningRgt}
                className="rounded-md border border-white/20 px-3 py-1.5 text-xs hover:bg-white/10 disabled:opacity-40"
              >
                {loadingRgtPreview ? "Calcul…" : "📊 Aperçu"}
              </button>
              {(runningRgt || rgtState?.running) && (
                <button
                  onClick={cancelRgt}
                  disabled={cancellingRgt}
                  className="rounded-md border border-red-400/40 px-3 py-1.5 text-xs text-red-300 hover:bg-red-400/10 disabled:opacity-40"
                >
                  {cancellingRgt ? "Arrêt en cours…" : "⏹ Annuler"}
                </button>
              )}
              {rgtState && rgtState.remaining_blocks > 0 && (
                <span className="text-xs text-white/60">
                  {rgtState.remaining_blocks} événement(s) avec RFID confirmé pas encore traité(s)
                </span>
              )}
            </div>
            {rgtPreview && (
              <>
                <div className="mt-2 text-xs text-white/70">
                  Sur {rgtPreview.blocks} événement(s) avec RFID confirmé : {rgtPreview.corrected} photo(s)
                  seraient réattribuées, {rgtPreview.already_correct} déjà correctes,{" "}
                  {rgtPreview.skipped_prey} laissées (proie), {rgtPreview.skipped_no_detection} laissées (pas de
                  détection).
                </div>
                <button
                  onClick={runRgt}
                  disabled={runningRgt}
                  className="mt-2 rounded-md border border-amber-400/40 px-3 py-1.5 text-xs text-amber-300 hover:bg-amber-400/10 disabled:opacity-40"
                >
                  {runningRgt ? "En cours…" : "✅ Appliquer"}
                </button>
              </>
            )}
            {rgtMsg && <p className="mt-2 text-xs text-white/70">{rgtMsg}</p>}
          </div>

          {reviewQueue && reviewQueue.pending === 0 && (
            <p className="mt-3 text-sm text-white/65">Aucun désaccord en attente de révision pour l'instant.</p>
          )}

          {reviewQueue && reviewQueue.pending > 0 && (
            <div className="mt-3">
              <p className="mb-2 text-sm font-semibold">
                {reviewQueue.rows.length < reviewQueue.pending
                  ? `Affichage des ${reviewQueue.rows.length} premiers désaccords sur ${reviewQueue.pending} en attente :`
                  : `${reviewQueue.pending} désaccord(s) en attente de révision :`}
              </p>
              <div className="flex flex-col gap-2">
                {(() => {
                  // 06.10 → 08.10, Sid ("du coup je n'ai plus le fond coloré
                  // des cartes par événements"): ported from the Shiny
                  // original's alt_row logic - toggles once per distinct
                  // event_block_id (not per row, several rows can share one
                  // event), same low-opacity blue as styles.css's
                  // .kh-model-review-row-alt so consecutive rows from the
                  // SAME passage share a color and a new passage stands out.
                  let prevBlockId: number | null = null;
                  let altRow = false;
                  return reviewQueue.rows.map((row) => {
                    if (row.event_block_id !== prevBlockId) {
                      altRow = !altRow;
                      prevBlockId = row.event_block_id;
                    }
                    return { row, altRow };
                  });
                })().map(({ row, altRow }) => (
                  <div
                    key={row.id}
                    className="flex gap-3 rounded-lg p-3"
                    style={{ backgroundColor: altRow ? "rgba(13, 110, 253, 0.14)" : "rgba(255, 255, 255, 0.05)" }}
                  >
                    <img
                      src={`/thumb/${row.photo_id}.jpg`}
                      loading="lazy"
                      className="h-16 w-16 shrink-0 rounded object-cover"
                      onClick={() => window.open(`/orig/${row.photo_id}.jpg`, "_blank")}
                    />
                    <div className="min-w-0 flex-1">
                      <div className="text-sm">
                        <span className="text-white/65">Avant : </span>
                        <strong>
                          {displayLabel(row.old_name)} ({row.old_probability.toFixed(0)}%)
                        </strong>
                      </div>
                      <div className="text-sm">
                        <span className="text-white/65">Après : </span>
                        <strong>
                          {displayLabel(row.new_name)} ({row.new_probability.toFixed(0)}%)
                        </strong>
                      </div>
                      <div className="text-xs text-white/65">
                        RFID : {row.rfid_label ?? "Aucun RFID trouvé"}
                        {row.rfid_label && row.rfid_confirmed === false && (
                          <span className="text-amber-400"> (non confirmé — supposition vidéo, pas une vraie lecture de puce)</span>
                        )}
                      </div>
                      <div className="mt-1 flex flex-wrap gap-2">
                        <button
                          onClick={() => decideRow(row, "keep-old")}
                          disabled={rowBusy === row.id}
                          className="rounded-md border border-white/20 px-2 py-1 text-xs hover:bg-white/10 disabled:opacity-40"
                        >
                          ⟲ Garder l'ancien
                        </button>
                        <button
                          onClick={() => decideRow(row, "confirm-new")}
                          disabled={rowBusy === row.id}
                          className="rounded-md border border-white/20 bg-white/10 px-2 py-1 text-xs hover:bg-white/20 disabled:opacity-40"
                        >
                          ✓ Confirmer le nouveau
                        </button>
                      </div>
                      <div className="mt-1 flex items-center gap-2">
                        {(() => {
                          const rfidDefault =
                            row.rfid_label && reviewQueue.cat_names.includes(row.rfid_label) ? row.rfid_label : "";
                          const effectiveOther = otherPick[row.id] ?? rfidDefault;
                          return (
                            <>
                              <select
                                value={effectiveOther}
                                onChange={(e) => setOtherPick((prev) => ({ ...prev, [row.id]: e.target.value }))}
                                className="rounded-md border border-white/15 bg-neutral-900 px-2 py-1 text-xs"
                              >
                                <option value="">— Autre —</option>
                                {reviewQueue.cat_names.map((n) => (
                                  <option key={n} value={n}>
                                    {n}
                                  </option>
                                ))}
                                <option value="Prey">Proie (pas un chat)</option>
                                <option value="none">Rien (fausse détection)</option>
                              </select>
                              <button
                                onClick={() => effectiveOther && decideRow(row, "other", effectiveOther)}
                                disabled={rowBusy === row.id || !effectiveOther}
                                className="rounded-md border border-white/20 px-2 py-1 text-xs hover:bg-white/10 disabled:opacity-40"
                              >
                                ✏️ Autre
                              </button>
                            </>
                          );
                        })()}
                      </div>
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}
        </Section>

        <Section
          title="Entraînement du modèle"
          description={training?.in_progress ? "Un entraînement est en cours." : "Envoyer des données et lancer un entraînement."}
          defaultOpen={!!training?.in_progress}
        >
          {training?.in_progress && (
            <>
              <p className="mb-2 text-sm">Statut actuel : {training.status_label}</p>
              <p className="mb-3 text-xs text-white/65">
                ID d'entraînement : <span className="font-mono">{training.job_id}</span> (utile pour le
                support)
              </p>
              <div className="flex gap-2">
                <button
                  onClick={loadTraining}
                  className="rounded-md border border-white/20 px-4 py-2 text-sm hover:bg-white/10"
                >
                  🔄 Actualiser le statut
                </button>
                <button
                  onClick={cancelTraining}
                  disabled={cancelling}
                  className="rounded-md border border-red-400/40 px-4 py-2 text-sm text-red-300 hover:bg-red-400/10 disabled:opacity-40"
                >
                  {cancelling ? "Annulation…" : "Annuler l'entraînement"}
                </button>
              </div>
            </>
          )}

          {training && !training.in_progress && training.server_status === null && (
            <p className="text-sm text-amber-300">
              Impossible de joindre le serveur d'entraînement pour l'instant. Réessaie plus tard.
            </p>
          )}

          {training && !training.in_progress && training.server_status?.maintenance && (
            <p className="text-sm text-amber-300">
              Le serveur d'entraînement est en maintenance.{" "}
              {training.server_status.message}
            </p>
          )}

          {training && !training.in_progress && training.server_status && !training.server_status.maintenance && (
            <>
              <h3 className="mb-2 text-sm font-medium">Données d'entraînement</h3>
              <input
                ref={trainingFileRef}
                type="file"
                accept=".zip"
                onChange={(e) => setTrainingFile(e.target.files?.[0] ?? null)}
                className="mb-3 w-full text-sm"
              />

              <Field label="Nom du modèle (optionnel)">
                <input value={modelName} onChange={(e) => setModelName(e.target.value)} className={inputClass} />
              </Field>
              <Field label="Nom d'utilisateur (optionnel)">
                <input value={userName} onChange={(e) => setUserName(e.target.value)} className={inputClass} />
              </Field>
              <Field label="Email pour notification (optionnel)">
                <input value={email} onChange={(e) => setEmail(e.target.value)} className={inputClass} />
              </Field>
              <p className="mb-3 text-xs text-white/60">
                Si tu indiques un email, tu seras notifié·e une fois l'entraînement terminé.
              </p>

              <details className="mb-3">
                <summary className="cursor-pointer text-sm text-white/70">Options avancées</summary>
                <div className="mt-2">
                  <Field label="Modèle de base">
                    <select
                      value={baseModel}
                      onChange={(e) => setBaseModel(e.target.value)}
                      className="w-full rounded-md border border-white/15 bg-neutral-900 px-3 py-2"
                    >
                      {BASE_MODELS.map((m) => (
                        <option key={m.value} value={m.value}>
                          {m.label}
                        </option>
                      ))}
                    </select>
                  </Field>
                  <Field label="Taille d'image">
                    <select
                      value={imageSize}
                      onChange={(e) => setImageSize(Number(e.target.value))}
                      className="w-full rounded-md border border-white/15 bg-neutral-900 px-3 py-2"
                    >
                      {(modelOptions?.image_sizes ?? [320]).map((s) => (
                        <option key={s} value={s}>
                          {s}
                        </option>
                      ))}
                    </select>
                  </Field>
                  <p className="text-xs text-white/60">
                    Les variantes plus grandes sont plus précises mais plus lentes. Défauts : YOLOv8n, 320.
                  </p>
                </div>
              </details>

              <button
                onClick={submitTraining}
                disabled={submitting || !trainingFile}
                className="w-full rounded-lg bg-white py-3 text-center font-medium text-black disabled:opacity-50"
              >
                Soumettre pour entraînement
              </button>
              {trainingMsg && <p className="mt-2 text-center text-sm text-white/70">{trainingMsg}</p>}
            </>
          )}
        </Section>
      </div>
      <BottomNav active="ai-training" />
    </>
  );
}
