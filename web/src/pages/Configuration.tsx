import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router";
import BottomNav from "../components/BottomNav";
import TopBanner from "../components/TopBanner";
import { fetchJson } from "../lib/api";
import { useBackButton } from "../lib/useBackButton";
import { usePageTitle } from "../lib/usePageTitle";
import { useWebPush } from "../lib/useWebPush";

interface GeneralConfig {
  language: string;
  timezone: string;
  date_format: string;
  periodic_version_check: boolean;
  update_repository_mode: "standard" | "beta" | "custom";
  update_repository: string;
  notify: Record<string, boolean>;
}

interface CameraConfig {
  camera_source: "internal" | "ip_camera";
  ip_camera_url: string;
  enable_decode_scale_pipeline: boolean;
  ip_camera_target_resolution: string;
  ip_camera_fps_limit: number;
  ip_camera_hw_decode: string;
  is_remote_mode: boolean;
}

interface ExitRange {
  enabled: boolean;
  from: string;
  to: string;
}

interface DoorControlConfig {
  min_threshold: number;
  mouse_threshold: number;
  cat_threshold: number;
  min_seconds_to_analyze: number;
  lock_duration_after_prey_detection: number;
  mouse_check_enabled: boolean;
  block_exit_after_prey_entry_enabled: boolean;
  block_exit_after_prey_entry_duration: number;
  selected_model: string;
  use_camera_for_cat_detection: boolean;
  use_camera_for_motion_detection: boolean;
  combine_pir_and_camera_outside_motion: boolean;
  pause_camera_when_idle: boolean;
  allowed_to_enter: string;
  allowed_to_exit: string;
  exit_ranges: ExitRange[];
  pir_inside_threshold: number;
  immediate_lock_after_passage: boolean;
}

interface ModelOption {
  id: string;
  label: string;
}

interface LiveViewConfig {
  refresh_interval: number;
}

interface PicturesViewConfig {
  max_photos_count: number;
  max_pictures_per_event_with_rfid: number;
  max_pictures_per_event_without_rfid: number;
  elements_per_page: number;
}

interface SystemInfoData {
  this_device_version: string;
  target_device_version: string;
  latest_version: string;
  free_disk_mb: number;
  total_disk_mb: number;
  is_remote_mode: boolean;
}

interface UpstreamCheck {
  current_version: string;
  latest_version: string;
  has_update: boolean;
  release_notes: string;
}

interface ConnectionLogRow {
  connected: boolean;
  at: string;
  duration_label: string;
  ongoing: boolean;
  reason: string | null;
}

interface ConnectionLog {
  empty: boolean;
  uptime_pct?: number;
  disconnect_count?: number;
  longest_streak_label?: string;
  since_label?: string;
  rows?: ConnectionLogRow[];
}

interface ApiToken {
  id: string;
  label: string;
  created_at: string;
  last_used_at: string | null;
}

interface AddCatState {
  entry_mode_per_cat: boolean;
  exit_mode_per_cat: boolean;
  prey_check_enabled: boolean;
}

interface AdvancedConfig {
  loglevel: string;
  remote_inference_max_fps: number;
  restart_ip_camera_stream_on_failure: boolean;
  inference_device: string;
  disable_rfid_reader: boolean;
}

interface MqttConfig {
  mqtt_enabled: boolean;
  mqtt_broker_address: string;
  mqtt_broker_port: number;
  mqtt_username: string;
  mqtt_password: string;
  mqtt_image_publish_interval: number;
  mqtt_device_id: string;
}

const NOTIFY_LABELS: { key: string; label: string }[] = [
  { key: "NOTIFY_CAT_ENTERED", label: "Chat entré" },
  { key: "NOTIFY_CAT_EXITED", label: "Chat sorti" },
  { key: "NOTIFY_GLANCE_OUTSIDE", label: "A regardé dehors, pas sorti" },
  { key: "NOTIFY_PREY_DETECTED", label: "Proie détectée" },
  { key: "NOTIFY_MOTION_OUTSIDE", label: "Mouvement détecté dehors (brut)" },
  { key: "NOTIFY_MOTION_INSIDE", label: "Mouvement détecté dedans (brut)" },
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

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="mb-3 block">
      <span className="mb-1 block text-sm text-white/70">{label}</span>
      {children}
    </label>
  );
}

const inputClass = "w-full rounded-md border border-white/15 bg-transparent px-3 py-2";

function Slider({
  label,
  value,
  min,
  max,
  step = 1,
  unit = "",
  onChange,
}: {
  label: string;
  value: number;
  min: number;
  max: number;
  step?: number;
  unit?: string;
  onChange: (v: number) => void;
}) {
  return (
    <div className="mb-3">
      <div className="mb-1 flex items-center justify-between text-sm text-white/70">
        <span>{label}</span>
        <span className="text-white/65">
          {value}
          {unit}
        </span>
      </div>
      <input
        type="range"
        min={min}
        max={max}
        step={step}
        value={value}
        onChange={(e) => onChange(Number(e.target.value))}
        className="w-full accent-white"
      />
    </div>
  );
}

// 07.10: Configuration.tsx is the start of the React replacement for
// configuration.py (3469 lines, 13 sections) - stays UNREACHABLE (no route
// in app.py's FASTAPI_TAB_PATHS) until every section is built, per Sid's
// call ("je construis tout avant de basculer"): the Shiny page covers all
// 13 sections in one tab, so unlike every other migrated page this can't be
// cut over section-by-section - flipping early would strand her mid-page
// the moment she needs a section not built yet.
export default function ConfigurationPage() {
  usePageTitle("Configuration");
  const navigate = useNavigate();
  const [general, setGeneral] = useState<GeneralConfig | null>(null);
  const [savingGeneral, setSavingGeneral] = useState(false);
  const [generalMsg, setGeneralMsg] = useState<string | null>(null);

  const [currentPw, setCurrentPw] = useState("");
  const [newPw, setNewPw] = useState("");
  const [newPw2, setNewPw2] = useState("");
  const [pwMsg, setPwMsg] = useState<string | null>(null);
  const [savingPw, setSavingPw] = useState(false);

  const [newUsername, setNewUsername] = useState("");
  const [newUserPw, setNewUserPw] = useState("");
  const [newUserPw2, setNewUserPw2] = useState("");
  const [userMsg, setUserMsg] = useState<string | null>(null);
  const [savingUser, setSavingUser] = useState(false);

  const webpush = useWebPush();

  const [camera, setCamera] = useState<CameraConfig | null>(null);
  const [savingCamera, setSavingCamera] = useState(false);
  const [cameraMsg, setCameraMsg] = useState<string | null>(null);

  useEffect(() => {
    fetchJson<CameraConfig>("/app-api/v1/config/camera")
      .then(setCamera)
      .catch(() => {});
  }, []);

  const saveCamera = async () => {
    if (!camera) return;
    setSavingCamera(true);
    setCameraMsg(null);
    try {
      await fetchJson("/app-api/v1/config/camera", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(camera),
      });
      setCameraMsg("Enregistré.");
    } catch (err) {
      setCameraMsg(err instanceof Error ? err.message : "Échec de l'enregistrement.");
    } finally {
      setSavingCamera(false);
    }
  };

  const [doorControl, setDoorControl] = useState<DoorControlConfig | null>(null);
  const [models, setModels] = useState<ModelOption[]>([]);
  const [savingDoorControl, setSavingDoorControl] = useState(false);
  const [doorControlMsg, setDoorControlMsg] = useState<string | null>(null);

  useEffect(() => {
    fetchJson<DoorControlConfig>("/app-api/v1/config/door-control")
      .then(setDoorControl)
      .catch(() => {});
    fetchJson<{ models: ModelOption[] }>("/app-api/v1/config/models")
      .then((d) => setModels(d.models))
      .catch(() => {});
  }, []);

  const saveDoorControl = async () => {
    if (!doorControl) return;
    setSavingDoorControl(true);
    setDoorControlMsg(null);
    try {
      await fetchJson("/app-api/v1/config/door-control", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(doorControl),
      });
      setDoorControlMsg("Enregistré.");
    } catch (err) {
      setDoorControlMsg(err instanceof Error ? err.message : "Échec de l'enregistrement.");
    } finally {
      setSavingDoorControl(false);
    }
  };

  const setExitRange = (idx: number, patch: Partial<ExitRange>) => {
    if (!doorControl) return;
    const ranges = doorControl.exit_ranges.map((r, i) => (i === idx ? { ...r, ...patch } : r));
    setDoorControl({ ...doorControl, exit_ranges: ranges });
  };

  const [liveView, setLiveView] = useState<LiveViewConfig | null>(null);
  const [savingLiveView, setSavingLiveView] = useState(false);
  const [liveViewMsg, setLiveViewMsg] = useState<string | null>(null);

  const [picturesView, setPicturesView] = useState<PicturesViewConfig | null>(null);
  const [savingPicturesView, setSavingPicturesView] = useState(false);
  const [picturesViewMsg, setPicturesViewMsg] = useState<string | null>(null);

  useEffect(() => {
    fetchJson<LiveViewConfig>("/app-api/v1/config/live-view").then(setLiveView).catch(() => {});
    fetchJson<PicturesViewConfig>("/app-api/v1/config/pictures-view").then(setPicturesView).catch(() => {});
  }, []);

  const saveLiveView = async () => {
    if (!liveView) return;
    setSavingLiveView(true);
    setLiveViewMsg(null);
    try {
      await fetchJson("/app-api/v1/config/live-view", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(liveView),
      });
      setLiveViewMsg("Enregistré.");
    } catch (err) {
      setLiveViewMsg(err instanceof Error ? err.message : "Échec.");
    } finally {
      setSavingLiveView(false);
    }
  };

  const savePicturesView = async () => {
    if (!picturesView) return;
    setSavingPicturesView(true);
    setPicturesViewMsg(null);
    try {
      await fetchJson("/app-api/v1/config/pictures-view", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(picturesView),
      });
      setPicturesViewMsg("Enregistré.");
    } catch (err) {
      setPicturesViewMsg(err instanceof Error ? err.message : "Échec.");
    } finally {
      setSavingPicturesView(false);
    }
  };

  const [mqtt, setMqtt] = useState<MqttConfig | null>(null);
  const [savingMqtt, setSavingMqtt] = useState(false);
  const [mqttMsg, setMqttMsg] = useState<string | null>(null);

  useEffect(() => {
    fetchJson<MqttConfig>("/app-api/v1/config/mqtt").then(setMqtt).catch(() => {});
  }, []);

  const saveMqtt = async () => {
    if (!mqtt) return;
    setSavingMqtt(true);
    setMqttMsg(null);
    try {
      await fetchJson("/app-api/v1/config/mqtt", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(mqtt),
      });
      setMqttMsg("Enregistré.");
    } catch (err) {
      setMqttMsg(err instanceof Error ? err.message : "Échec.");
    } finally {
      setSavingMqtt(false);
    }
  };

  const [advanced, setAdvanced] = useState<AdvancedConfig | null>(null);
  const [savingAdvanced, setSavingAdvanced] = useState(false);
  const [advancedMsg, setAdvancedMsg] = useState<string | null>(null);

  useEffect(() => {
    fetchJson<AdvancedConfig>("/app-api/v1/config/advanced").then(setAdvanced).catch(() => {});
  }, []);

  const saveAdvanced = async () => {
    if (!advanced) return;
    setSavingAdvanced(true);
    setAdvancedMsg(null);
    try {
      await fetchJson("/app-api/v1/config/advanced", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(advanced),
      });
      setAdvancedMsg("Enregistré.");
    } catch (err) {
      setAdvancedMsg(err instanceof Error ? err.message : "Échec.");
    } finally {
      setSavingAdvanced(false);
    }
  };

  const [addCatState, setAddCatState] = useState<AddCatState | null>(null);
  const [newCatName, setNewCatName] = useState("");
  const [newCatRfid, setNewCatRfid] = useState("");
  const [newCatPhoto, setNewCatPhoto] = useState<File | null>(null);
  const [newCatPrey, setNewCatPrey] = useState(true);
  const [newCatAllowEntry, setNewCatAllowEntry] = useState(true);
  const [newCatAllowExit, setNewCatAllowExit] = useState(true);
  const [savingNewCat, setSavingNewCat] = useState(false);
  const [newCatMsg, setNewCatMsg] = useState<string | null>(null);
  const newCatFileRef = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    fetchJson<AddCatState>("/app-api/v1/config/add-cat-state").then(setAddCatState).catch(() => {});
  }, []);

  const newCatRfidValid = newCatRfid.trim() === "" || /^[0-9A-Fa-f]{16}$/.test(newCatRfid.trim());

  const saveNewCat = async () => {
    if (!newCatName.trim() || !newCatRfidValid) return;
    setSavingNewCat(true);
    setNewCatMsg(null);
    try {
      const form = new FormData();
      form.append("name", newCatName.trim());
      form.append("rfid", newCatRfid.trim());
      form.append("prey", String(newCatPrey));
      form.append("allow_entry", String(newCatAllowEntry));
      form.append("allow_exit", String(newCatAllowExit));
      if (newCatPhoto) form.append("photo", newCatPhoto);
      await fetchJson("/app-api/v1/config/add-cat", { method: "POST", body: form });
      setNewCatMsg(`Chat '${newCatName.trim()}' ajouté.`);
      setNewCatName("");
      setNewCatRfid("");
      setNewCatPhoto(null);
      setNewCatPrey(true);
      setNewCatAllowEntry(true);
      setNewCatAllowExit(true);
      if (newCatFileRef.current) newCatFileRef.current.value = "";
    } catch (err) {
      setNewCatMsg(err instanceof Error ? err.message : "Échec de l'ajout.");
    } finally {
      setSavingNewCat(false);
    }
  };

  const [tokens, setTokens] = useState<ApiToken[] | null>(null);
  const [newTokenLabel, setNewTokenLabel] = useState("");
  const [createdToken, setCreatedToken] = useState<string | null>(null);
  const [revokeId, setRevokeId] = useState("");
  const [tokenMsg, setTokenMsg] = useState<string | null>(null);
  const [tokenBusy, setTokenBusy] = useState(false);
  const [systemBusy, setSystemBusy] = useState(false);
  const [systemMsg, setSystemMsg] = useState<string | null>(null);

  const [sysInfo, setSysInfo] = useState<SystemInfoData | null>(null);

  useEffect(() => {
    fetchJson<SystemInfoData>("/app-api/v1/system/info").then(setSysInfo).catch(() => {});
  }, []);

  const [upstream, setUpstream] = useState<UpstreamCheck | null>(null);
  const [checkingUpstream, setCheckingUpstream] = useState(false);

  const checkUpstream = async () => {
    setCheckingUpstream(true);
    try {
      const result = await fetchJson<UpstreamCheck>("/app-api/v1/system/upstream-check");
      setUpstream(result);
    } catch {
      // best-effort - GitHub can be flaky/rate-limited, the button stays available to retry
    } finally {
      setCheckingUpstream(false);
    }
  };

  const [connectionLog, setConnectionLog] = useState<ConnectionLog | null>(null);

  useEffect(() => {
    fetchJson<ConnectionLog>("/app-api/v1/system/remote-connection-log")
      .then(setConnectionLog)
      .catch(() => {});
  }, []);

  const loadTokens = () => {
    fetchJson<{ tokens: ApiToken[] }>("/app-api/v1/system/api-tokens")
      .then((d) => setTokens(d.tokens))
      .catch(() => {});
  };

  useEffect(loadTokens, []);

  const createToken = async () => {
    if (!newTokenLabel.trim()) return;
    setTokenBusy(true);
    setTokenMsg(null);
    try {
      const result = await fetchJson<{ token: string }>("/app-api/v1/system/api-tokens", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ label: newTokenLabel.trim() }),
      });
      setCreatedToken(result.token);
      setNewTokenLabel("");
      loadTokens();
    } catch (err) {
      setTokenMsg(err instanceof Error ? err.message : "Échec de la création.");
    } finally {
      setTokenBusy(false);
    }
  };

  const revokeToken = async () => {
    if (!revokeId) return;
    if (!confirm(`Révoquer le token '${revokeId}' ? Irréversible, tout client qui l'utilise perd l'accès immédiatement.`))
      return;
    setTokenBusy(true);
    setTokenMsg(null);
    try {
      await fetchJson(`/app-api/v1/system/api-tokens/${revokeId}`, { method: "DELETE" });
      setTokenMsg("Token révoqué.");
      setRevokeId("");
      loadTokens();
    } catch (err) {
      setTokenMsg(err instanceof Error ? err.message : "Échec de la révocation.");
    } finally {
      setTokenBusy(false);
    }
  };

  // 07.10: "Redémarrer/Éteindre cet appareil" reboots/shuts down the whole
  // host this container runs on (not just Kittyhack) - two confirms, the
  // second one requiring the literal device name typed back, same spirit
  // as a destructive git/infra action elsewhere in this app.
  const systemAction = async (endpoint: string, dangerous: boolean, confirmMsg: string) => {
    if (!confirm(confirmMsg)) return;
    if (dangerous) {
      const typed = prompt(
        'Action à fort impact : tape "CONFIRMER" pour continuer (redémarre TOUT cet appareil, pas que Kittyhack).'
      );
      if (typed !== "CONFIRMER") return;
    }
    setSystemBusy(true);
    setSystemMsg(null);
    try {
      await fetchJson(`/app-api/v1/system/${endpoint}`, { method: "POST" });
      setSystemMsg("Déclenché.");
    } catch (err) {
      setSystemMsg(err instanceof Error ? err.message : "Échec.");
    } finally {
      setSystemBusy(false);
    }
  };

  useBackButton(
    () => false,
    () => navigate("/presence")
  );

  useEffect(() => {
    fetchJson<GeneralConfig>("/app-api/v1/config/general")
      .then(setGeneral)
      .catch(() => {});
  }, []);

  const saveGeneral = async () => {
    if (!general) return;
    setSavingGeneral(true);
    setGeneralMsg(null);
    try {
      await fetchJson("/app-api/v1/config/general", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(general),
      });
      setGeneralMsg("Enregistré.");
    } catch (err) {
      setGeneralMsg(err instanceof Error ? err.message : "Échec de l'enregistrement.");
    } finally {
      setSavingGeneral(false);
    }
  };

  const changePassword = async () => {
    setSavingPw(true);
    setPwMsg(null);
    try {
      await fetchJson("/app-api/v1/config/password", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          current_password: currentPw,
          new_password: newPw,
          new_password2: newPw2,
        }),
      });
      setPwMsg("Mot de passe mis à jour.");
      setCurrentPw("");
      setNewPw("");
      setNewPw2("");
    } catch (err) {
      setPwMsg(err instanceof Error ? err.message : "Échec.");
    } finally {
      setSavingPw(false);
    }
  };

  const addUser = async () => {
    setSavingUser(true);
    setUserMsg(null);
    try {
      await fetchJson("/app-api/v1/config/users", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username: newUsername, password: newUserPw, password2: newUserPw2 }),
      });
      setUserMsg(`Utilisateur '${newUsername}' ajouté.`);
      setNewUsername("");
      setNewUserPw("");
      setNewUserPw2("");
    } catch (err) {
      setUserMsg(err instanceof Error ? err.message : "Échec.");
    } finally {
      setSavingUser(false);
    }
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
          <div className="text-lg font-semibold">Configuration</div>
        </div>

        <Section
          title="Général"
          description="Langue, fuseau horaire, et options d'affichage de base."
        >
          {general && (
            <>
              <div className="grid grid-cols-2 gap-3">
                <Field label="Langue">
                  <select
                    value={general.language}
                    onChange={(e) => setGeneral({ ...general, language: e.target.value })}
                    className="w-full rounded-md border border-white/15 bg-neutral-900 px-3 py-2"
                  >
                    <option value="fr">Français</option>
                    <option value="en">English</option>
                    <option value="de">Deutsch</option>
                  </select>
                </Field>
                <Field label="Fuseau horaire">
                  <input
                    value={general.timezone}
                    onChange={(e) => setGeneral({ ...general, timezone: e.target.value })}
                    className={inputClass}
                  />
                </Field>
              </div>

              <hr className="my-4 border-white/10" />
              <h3 className="mb-2 font-medium">Changer mon mot de passe</h3>
              <Field label="Mot de passe actuel">
                <input
                  type="password"
                  value={currentPw}
                  onChange={(e) => setCurrentPw(e.target.value)}
                  className={inputClass}
                />
              </Field>
              <div className="grid grid-cols-2 gap-3">
                <Field label="Nouveau mot de passe">
                  <input
                    type="password"
                    value={newPw}
                    onChange={(e) => setNewPw(e.target.value)}
                    className={inputClass}
                  />
                </Field>
                <Field label="Confirmer">
                  <input
                    type="password"
                    value={newPw2}
                    onChange={(e) => setNewPw2(e.target.value)}
                    className={inputClass}
                  />
                </Field>
              </div>
              <button
                onClick={changePassword}
                disabled={savingPw || !currentPw || !newPw}
                className="mb-1 rounded-md border border-white/20 px-4 py-2 text-sm hover:bg-white/10 disabled:opacity-40"
              >
                🔑 Mettre à jour le mot de passe
              </button>
              {pwMsg && <p className="mt-2 text-sm text-white/70">{pwMsg}</p>}

              <hr className="my-4 border-white/10" />
              <h3 className="mb-2 font-medium">Ajouter un utilisateur</h3>
              <Field label="Nom d'utilisateur">
                <input
                  value={newUsername}
                  onChange={(e) => setNewUsername(e.target.value)}
                  className={inputClass}
                />
              </Field>
              <div className="grid grid-cols-2 gap-3">
                <Field label="Mot de passe">
                  <input
                    type="password"
                    value={newUserPw}
                    onChange={(e) => setNewUserPw(e.target.value)}
                    className={inputClass}
                  />
                </Field>
                <Field label="Confirmer">
                  <input
                    type="password"
                    value={newUserPw2}
                    onChange={(e) => setNewUserPw2(e.target.value)}
                    className={inputClass}
                  />
                </Field>
              </div>
              <button
                onClick={addUser}
                disabled={savingUser || !newUsername || !newUserPw}
                className="mb-1 rounded-md border border-white/20 px-4 py-2 text-sm hover:bg-white/10 disabled:opacity-40"
              >
                👤 Ajouter l'utilisateur
              </button>
              {userMsg && <p className="mt-2 text-sm text-white/70">{userMsg}</p>}

              <hr className="my-4 border-white/10" />
              <h3 className="mb-2 font-medium">Notifications push</h3>
              <p className="mb-2 text-sm text-white/60">{webpush.statusLabel}</p>
              {webpush.supported && (
                <button
                  onClick={webpush.toggle}
                  disabled={webpush.busy}
                  className="mb-3 rounded-md border border-white/20 px-4 py-2 text-sm hover:bg-white/10 disabled:opacity-40"
                >
                  {webpush.subscribed ? "Désactiver" : "Activer"}
                </button>
              )}
              <div className="grid grid-cols-2 gap-x-4 gap-y-1">
                {NOTIFY_LABELS.map((n) => (
                  <label key={n.key} className="flex items-center gap-2 py-1 text-sm">
                    <input
                      type="checkbox"
                      checked={general.notify[n.key] ?? false}
                      onChange={(e) =>
                        setGeneral({
                          ...general,
                          notify: { ...general.notify, [n.key]: e.target.checked },
                        })
                      }
                    />
                    {n.label}
                  </label>
                ))}
              </div>

              <hr className="my-4 border-white/10" />
              <Field label="Format de date">
                <input
                  value={general.date_format}
                  onChange={(e) => setGeneral({ ...general, date_format: e.target.value })}
                  className={inputClass}
                />
              </Field>
              <p className="mb-4 text-xs text-white/60">
                Caractères valides : `yyyy`, `mm`, `dd` (ex : `yyyy-mm-dd` ou `dd.mm.yyyy`)
              </p>

              <label className="mb-4 flex items-center gap-2 text-sm">
                <input
                  type="checkbox"
                  checked={general.periodic_version_check}
                  onChange={(e) => setGeneral({ ...general, periodic_version_check: e.target.checked })}
                />
                Vérification périodique des mises à jour
              </label>

              <Field label="Dépôt de mise à jour">
                <select
                  value={general.update_repository_mode}
                  onChange={(e) =>
                    setGeneral({ ...general, update_repository_mode: e.target.value as GeneralConfig["update_repository_mode"] })
                  }
                  className="w-full rounded-md border border-white/15 bg-neutral-900 px-3 py-2"
                >
                  <option value="standard">Standard</option>
                  <option value="beta">Beta</option>
                  <option value="custom">Personnalisé</option>
                </select>
              </Field>
              {general.update_repository_mode === "custom" && (
                <Field label="Dépôt personnalisé">
                  <input
                    value={general.update_repository}
                    onChange={(e) => setGeneral({ ...general, update_repository: e.target.value })}
                    placeholder="owner/repo@branch"
                    className={inputClass}
                  />
                </Field>
              )}

              <button
                onClick={saveGeneral}
                disabled={savingGeneral}
                className="w-full rounded-lg bg-white py-3 text-center font-medium text-black disabled:opacity-50"
              >
                💾 Enregistrer
              </button>
              {generalMsg && <p className="mt-2 text-center text-sm text-white/70">{generalMsg}</p>}
            </>
          )}
        </Section>

        <Section title="Caméra" description="Réglages de la caméra interne et des caméras IP externes.">
          {camera && (
            <>
              <Field label="Source de la caméra">
                <select
                  value={camera.camera_source}
                  onChange={(e) => setCamera({ ...camera, camera_source: e.target.value as CameraConfig["camera_source"] })}
                  className="w-full rounded-md border border-white/15 bg-neutral-900 px-3 py-2"
                >
                  <option value="internal">Caméra interne</option>
                  <option value="ip_camera">Caméra IP externe</option>
                </select>
              </Field>

              {camera.camera_source === "ip_camera" && (
                <>
                  <Field label="URL de la caméra IP externe">
                    <input
                      value={camera.ip_camera_url}
                      onChange={(e) => setCamera({ ...camera, ip_camera_url: e.target.value })}
                      placeholder="ex: rtsp://user:pass@192.168.1.100:554/stream"
                      className={inputClass}
                    />
                  </Field>
                  <p className="mb-3 text-xs text-white/60">
                    Formats supportés : rtsp://, http:// (MJPEG), rtmp://, udp://, tcp://, file://
                  </p>

                  <hr className="my-4 border-white/10" />
                  <h3 className="mb-2 font-medium">Réglages avancés caméra IP</h3>

                  <label className="mb-3 flex items-center gap-2 text-sm">
                    <input
                      type="checkbox"
                      checked={camera.enable_decode_scale_pipeline}
                      onChange={(e) => setCamera({ ...camera, enable_decode_scale_pipeline: e.target.checked })}
                    />
                    Downscaler le flux caméra IP
                  </label>
                  <p className="mb-3 text-xs text-white/60">
                    Réduit la charge CPU et améliore le FPS d'inférence pour les flux haute résolution.
                  </p>

                  <Field label="Résolution cible">
                    <select
                      value={camera.ip_camera_target_resolution}
                      onChange={(e) => setCamera({ ...camera, ip_camera_target_resolution: e.target.value })}
                      className="w-full rounded-md border border-white/15 bg-neutral-900 px-3 py-2"
                    >
                      <option value="480x320">320p (480x320)</option>
                      <option value="640x360">360p (640x360)</option>
                      <option value="640x480">480p 4:3 (640x480)</option>
                      <option value="854x480">480p 16:9 (854x480)</option>
                      <option value="960x540">540p (960x540)</option>
                      <option value="1024x576">576p (1024x576)</option>
                      <option value="1280x720">720p (1280x720)</option>
                    </select>
                  </Field>
                  <p className="mb-3 text-xs text-white/60">
                    Recommandé : 640x360 ou 1280x720 pour le meilleur compromis performance/qualité.
                  </p>

                  <Field label="Limite FPS caméra IP">
                    <select
                      value={String(camera.ip_camera_fps_limit)}
                      onChange={(e) => setCamera({ ...camera, ip_camera_fps_limit: Number(e.target.value) })}
                      className="w-full rounded-md border border-white/15 bg-neutral-900 px-3 py-2"
                    >
                      <option value="5">5 FPS</option>
                      <option value="10">10 FPS</option>
                      <option value="15">15 FPS</option>
                      <option value="20">20 FPS</option>
                      <option value="25">25 FPS</option>
                      <option value="0">Illimité</option>
                    </select>
                  </Field>

                  {camera.is_remote_mode && (
                    <Field label="Décodage matériel caméra IP">
                      <select
                        value={camera.ip_camera_hw_decode}
                        onChange={(e) => setCamera({ ...camera, ip_camera_hw_decode: e.target.value })}
                        className="w-full rounded-md border border-white/15 bg-neutral-900 px-3 py-2"
                      >
                        <option value="auto">Auto (détection GPU)</option>
                        <option value="none">Logiciel uniquement</option>
                        <option value="cuda">NVIDIA CUDA</option>
                        <option value="vaapi">VAAPI (Intel/AMD)</option>
                        <option value="qsv">Intel Quick Sync</option>
                      </select>
                    </Field>
                  )}
                </>
              )}

              <button
                onClick={saveCamera}
                disabled={savingCamera}
                className="mt-2 w-full rounded-lg bg-white py-3 text-center font-medium text-black disabled:opacity-50"
              >
                💾 Enregistrer
              </button>
              {cameraMsg && <p className="mt-2 text-center text-sm text-white/70">{cameraMsg}</p>}
            </>
          )}
        </Section>
        <Section title="Contrôle de la porte" description="Seuils de détection, modèle, et modes d'entrée/sortie.">
          {doorControl && (
            <>
              <Slider
                label="Seuil de détection minimum"
                value={doorControl.min_threshold}
                min={0}
                max={80}
                unit="%"
                onChange={(v) => setDoorControl({ ...doorControl, min_threshold: v })}
              />
              <p className="mb-3 -mt-2 text-xs text-white/60">
                Au moins une photo doit dépasser cette probabilité pour qu'un événement soit enregistré.
              </p>

              <Slider
                label="Seuil de détection souris"
                value={doorControl.mouse_threshold}
                min={0}
                max={100}
                unit="%"
                onChange={(v) => setDoorControl({ ...doorControl, mouse_threshold: v })}
              />
              <p className="mb-3 -mt-2 text-xs text-white/60">
                Si dépassé, la chatière reste fermée.
              </p>

              <Slider
                label="Seuil de détection chat"
                value={doorControl.cat_threshold}
                min={0}
                max={100}
                unit="%"
                onChange={(v) => setDoorControl({ ...doorControl, cat_threshold: v })}
              />
              <p className="mb-3 -mt-2 text-xs text-white/60">
                Utilisé si "Utiliser la caméra pour la détection du chat" est activé ci-dessous.
              </p>

              <Field label="Secondes avant décision d'ouverture">
                <input
                  type="number"
                  min={0.1}
                  step={0.1}
                  value={doorControl.min_seconds_to_analyze}
                  onChange={(e) => setDoorControl({ ...doorControl, min_seconds_to_analyze: Number(e.target.value) })}
                  className={inputClass}
                />
              </Field>

              <Slider
                label="Durée de verrouillage après détection de proie"
                value={doorControl.lock_duration_after_prey_detection}
                min={30}
                max={1800}
                step={5}
                unit="s"
                onChange={(v) => setDoorControl({ ...doorControl, lock_duration_after_prey_detection: v })}
              />

              <label className="mb-3 flex items-center gap-2 text-sm">
                <input
                  type="checkbox"
                  checked={doorControl.mouse_check_enabled}
                  onChange={(e) => setDoorControl({ ...doorControl, mouse_check_enabled: e.target.checked })}
                />
                Détecter les proies
              </label>

              <label className="mb-3 flex items-center gap-2 text-sm">
                <input
                  type="checkbox"
                  checked={doorControl.block_exit_after_prey_entry_enabled}
                  onChange={(e) =>
                    setDoorControl({ ...doorControl, block_exit_after_prey_entry_enabled: e.target.checked })
                  }
                />
                Bloquer la sortie après une entrée avec proie détectée
              </label>
              {doorControl.block_exit_after_prey_entry_enabled && (
                <Slider
                  label="Durée du blocage de sortie"
                  value={doorControl.block_exit_after_prey_entry_duration}
                  min={30}
                  max={1800}
                  step={5}
                  unit="s"
                  onChange={(v) => setDoorControl({ ...doorControl, block_exit_after_prey_entry_duration: v })}
                />
              )}

              <hr className="my-4 border-white/10" />
              <Field label="Modèle de détection d'objets">
                <select
                  value={doorControl.selected_model}
                  onChange={(e) => setDoorControl({ ...doorControl, selected_model: e.target.value })}
                  className="w-full rounded-md border border-white/15 bg-neutral-900 px-3 py-2"
                >
                  {models.map((m) => (
                    <option key={m.id} value={m.id}>
                      {m.label}
                    </option>
                  ))}
                </select>
              </Field>
              <p className="mb-3 text-xs text-white/60">
                Changer ce réglage nécessite un redémarrage de la chatière pour s'appliquer.
              </p>

              <hr className="my-4 border-white/10" />
              <label className="mb-3 flex items-center gap-2 text-sm">
                <input
                  type="checkbox"
                  checked={doorControl.use_camera_for_cat_detection}
                  onChange={(e) => setDoorControl({ ...doorControl, use_camera_for_cat_detection: e.target.checked })}
                />
                Utiliser la caméra pour la détection du chat
              </label>
              <label className="mb-3 flex items-center gap-2 text-sm">
                <input
                  type="checkbox"
                  checked={doorControl.use_camera_for_motion_detection}
                  onChange={(e) =>
                    setDoorControl({ ...doorControl, use_camera_for_motion_detection: e.target.checked })
                  }
                />
                Utiliser la caméra pour la détection de mouvement (désactive le PIR)
              </label>
              {doorControl.use_camera_for_motion_detection && (
                <>
                  <label className="mb-3 ml-5 flex items-center gap-2 text-sm">
                    <input
                      type="checkbox"
                      checked={doorControl.combine_pir_and_camera_outside_motion}
                      onChange={(e) =>
                        setDoorControl({ ...doorControl, combine_pir_and_camera_outside_motion: e.target.checked })
                      }
                    />
                    Utiliser aussi le capteur PIR en plus de la caméra
                  </label>
                  <label className="mb-3 ml-5 flex items-center gap-2 text-sm">
                    <input
                      type="checkbox"
                      checked={doorControl.pause_camera_when_idle}
                      onChange={(e) => setDoorControl({ ...doorControl, pause_camera_when_idle: e.target.checked })}
                    />
                    Mettre la caméra en pause au repos (reprise sur mouvement PIR)
                  </label>
                </>
              )}
              <p className="mb-3 text-xs text-white/60">
                Les réglages caméra ci-dessus nécessitent un modèle entraîné pour tes chats.
              </p>

              <hr className="my-4 border-white/10" />
              <Field label="Ouvrir la direction intérieure pour :">
                <select
                  value={doorControl.allowed_to_enter}
                  onChange={(e) => setDoorControl({ ...doorControl, allowed_to_enter: e.target.value })}
                  className="w-full rounded-md border border-white/15 bg-neutral-900 px-3 py-2"
                >
                  <option value="all">Tous les chats (à chaque mouvement détecté)</option>
                  <option value="all_rfids">Tous les chats avec puce RFID</option>
                  <option value="known">Chats enregistrés uniquement</option>
                  <option value="none">Aucun chat</option>
                  <option value="configure_per_cat">Configuration individuelle par chat</option>
                </select>
              </Field>

              <Field label="Direction extérieure :">
                <select
                  value={doorControl.allowed_to_exit}
                  onChange={(e) => setDoorControl({ ...doorControl, allowed_to_exit: e.target.value })}
                  className="w-full rounded-md border border-white/15 bg-neutral-900 px-3 py-2"
                >
                  <option value="allow">Autoriser la sortie</option>
                  <option value="deny">Ne pas autoriser la sortie</option>
                  <option value="configure_per_cat">Configuration individuelle par chat</option>
                </select>
              </Field>
              <p className="mb-3 text-xs text-white/60">
                "Configuration individuelle" nécessite que tous les chats aient une puce RFID enregistrée.
              </p>

              <h3 className="mb-2 font-medium">Plages horaires de sortie (jusqu'à 3)</h3>
              <p className="mb-3 text-xs text-white/60">
                Si aucune plage n'est activée, la sortie est autorisée à toute heure (sous réserve des autres
                réglages). Si au moins une est activée, la sortie n'est autorisée que dans les plages actives.
              </p>
              {doorControl.exit_ranges.map((r, i) => (
                <div key={i} className="mb-3 flex items-center gap-2">
                  <label className="flex items-center gap-2 text-sm">
                    <input
                      type="checkbox"
                      checked={r.enabled}
                      onChange={(e) => setExitRange(i, { enabled: e.target.checked })}
                    />
                    Plage {i + 1}
                  </label>
                  <input
                    value={r.from}
                    onChange={(e) => setExitRange(i, { from: e.target.value })}
                    placeholder="00:00"
                    className="w-20 rounded-md border border-white/15 bg-transparent px-2 py-1 text-sm"
                  />
                  <span className="text-white/40">→</span>
                  <input
                    value={r.to}
                    onChange={(e) => setExitRange(i, { to: e.target.value })}
                    placeholder="00:00"
                    className="w-20 rounded-md border border-white/15 bg-transparent px-2 py-1 text-sm"
                  />
                </div>
              ))}

              <hr className="my-4 border-white/10" />
              <Slider
                label="Vitesse de réaction du capteur de mouvement intérieur"
                value={doorControl.pir_inside_threshold}
                min={0.1}
                max={6}
                step={0.1}
                unit="s"
                onChange={(v) => setDoorControl({ ...doorControl, pir_inside_threshold: v })}
              />

              <label className="mb-3 flex items-center gap-2 text-sm">
                <input
                  type="checkbox"
                  checked={doorControl.immediate_lock_after_passage}
                  onChange={(e) => setDoorControl({ ...doorControl, immediate_lock_after_passage: e.target.checked })}
                />
                Verrouillage immédiat après passage
              </label>
              <p className="mb-3 text-xs text-white/60">
                Reverrouille dès qu'un mouvement est détecté de l'autre côté. Utile pour un chat rapide ; à éviter
                si ton chat hésite souvent avant de passer.
              </p>

              <button
                onClick={saveDoorControl}
                disabled={savingDoorControl}
                className="mt-2 w-full rounded-lg bg-white py-3 text-center font-medium text-black disabled:opacity-50"
              >
                💾 Enregistrer
              </button>
              {doorControlMsg && <p className="mt-2 text-center text-sm text-white/70">{doorControlMsg}</p>}
            </>
          )}
        </Section>
        <Section title="Réglages Live view" description="Intervalle de rafraîchissement de la caméra en direct.">
          {liveView && (
            <>
              <Field label="Intervalle de rafraîchissement du Live-View">
                <select
                  value={liveView.refresh_interval}
                  onChange={(e) => setLiveView({ refresh_interval: Number(e.target.value) })}
                  className="w-full rounded-md border border-white/15 bg-neutral-900 px-3 py-2"
                >
                  <option value={0.1}>100ms</option>
                  <option value={0.2}>200ms</option>
                  <option value={0.5}>500ms</option>
                  <option value={1.0}>1s</option>
                  <option value={2.0}>2s</option>
                  <option value={3.0}>3s</option>
                  <option value={5.0}>5s</option>
                  <option value={10.0}>10s</option>
                </select>
              </Field>
              <p className="mb-3 text-xs text-white/60">
                Un rafraîchissement rapide peut ralentir les performances, surtout avec plusieurs
                utilisateurs connectés. En-dessous de 1s, une connexion rapide et stable est nécessaire.
              </p>
              <button
                onClick={saveLiveView}
                disabled={savingLiveView}
                className="w-full rounded-lg bg-white py-3 text-center font-medium text-black disabled:opacity-50"
              >
                💾 Enregistrer
              </button>
              {liveViewMsg && <p className="mt-2 text-center text-sm text-white/70">{liveViewMsg}</p>}
            </>
          )}
        </Section>

        <Section title="Réglages Photos" description="Nombre maximum de photos et limites par événement.">
          {picturesView && (
            <>
              <Field label="Nombre maximum de photos conservées en base">
                <input
                  type="number"
                  min={100}
                  value={picturesView.max_photos_count}
                  onChange={(e) => setPicturesView({ ...picturesView, max_photos_count: Number(e.target.value) })}
                  className={inputClass}
                />
              </Field>
              <p className="mb-3 text-xs text-white/60">
                Les photos les plus anciennes sont supprimées au-delà de cette valeur (~200 Mo pour 1000 photos).
              </p>

              <Field label="Photos max. par événement avec RFID">
                <input
                  type="number"
                  min={0}
                  value={picturesView.max_pictures_per_event_with_rfid}
                  onChange={(e) =>
                    setPicturesView({ ...picturesView, max_pictures_per_event_with_rfid: Number(e.target.value) })
                  }
                  className={inputClass}
                />
              </Field>

              <Field label="Photos max. par événement sans RFID">
                <input
                  type="number"
                  min={0}
                  value={picturesView.max_pictures_per_event_without_rfid}
                  onChange={(e) =>
                    setPicturesView({ ...picturesView, max_pictures_per_event_without_rfid: Number(e.target.value) })
                  }
                  className={inputClass}
                />
              </Field>
              <p className="mb-3 text-xs text-white/60">
                La détection de proie reste active sur toutes les photos de l'événement, même au-delà de cette
                limite - elle ne limite que ce qui est conservé en base. À 0, l'événement n'est pas enregistré du
                tout.
              </p>

              <Field label="Photos max. par page (vue Photos non groupée)">
                <input
                  type="number"
                  min={1}
                  value={picturesView.elements_per_page}
                  onChange={(e) => setPicturesView({ ...picturesView, elements_per_page: Number(e.target.value) })}
                  className={inputClass}
                />
              </Field>

              <button
                onClick={savePicturesView}
                disabled={savingPicturesView}
                className="w-full rounded-lg bg-white py-3 text-center font-medium text-black disabled:opacity-50"
              >
                💾 Enregistrer
              </button>
              {picturesViewMsg && <p className="mt-2 text-center text-sm text-white/70">{picturesViewMsg}</p>}
            </>
          )}
        </Section>
        <Section title="Home Assistant / MQTT" description="Intégration MQTT pour Home Assistant.">
          {mqtt && (
            <>
              <label className="mb-3 flex items-center gap-2 text-sm">
                <input
                  type="checkbox"
                  checked={mqtt.mqtt_enabled}
                  onChange={(e) => setMqtt({ ...mqtt, mqtt_enabled: e.target.checked })}
                />
                Activer MQTT
              </label>

              <div className="grid grid-cols-3 gap-3">
                <div className="col-span-2">
                  <Field label="Adresse du broker MQTT">
                    <input
                      value={mqtt.mqtt_broker_address}
                      onChange={(e) => setMqtt({ ...mqtt, mqtt_broker_address: e.target.value })}
                      placeholder="ex: 192.168.1.10"
                      className={inputClass}
                    />
                  </Field>
                </div>
                <Field label="Port">
                  <input
                    type="number"
                    min={1}
                    max={65535}
                    value={mqtt.mqtt_broker_port}
                    onChange={(e) => setMqtt({ ...mqtt, mqtt_broker_port: Number(e.target.value) })}
                    className={inputClass}
                  />
                </Field>
              </div>

              <div className="grid grid-cols-2 gap-3">
                <Field label="Utilisateur MQTT">
                  <input
                    value={mqtt.mqtt_username}
                    onChange={(e) => setMqtt({ ...mqtt, mqtt_username: e.target.value })}
                    className={inputClass}
                  />
                </Field>
                <Field label="Mot de passe MQTT">
                  <input
                    type="password"
                    value={mqtt.mqtt_password}
                    onChange={(e) => setMqtt({ ...mqtt, mqtt_password: e.target.value })}
                    className={inputClass}
                  />
                </Field>
              </div>

              <Field label="Intervalle de mise à jour caméra">
                <select
                  value={mqtt.mqtt_image_publish_interval}
                  onChange={(e) => setMqtt({ ...mqtt, mqtt_image_publish_interval: Number(e.target.value) })}
                  className="w-full rounded-md border border-white/15 bg-neutral-900 px-3 py-2"
                >
                  <option value={2}>2s</option>
                  <option value={3}>3s</option>
                  <option value={5}>5s</option>
                  <option value={10}>10s</option>
                  <option value={20}>20s</option>
                  <option value={30}>30s</option>
                  <option value={60}>60s</option>
                </select>
              </Field>

              <button
                onClick={saveMqtt}
                disabled={savingMqtt}
                className="mb-4 w-full rounded-lg bg-white py-3 text-center font-medium text-black disabled:opacity-50"
              >
                💾 Enregistrer
              </button>
              {mqttMsg && <p className="mb-4 text-center text-sm text-white/70">{mqttMsg}</p>}

              <hr className="mb-4 border-white/10" />
              <h3 className="mb-2 font-medium">Exemple de carte Home Assistant</h3>
              <p className="mb-2 text-sm text-white/60">
                Copie cette configuration pour créer une carte de tableau de bord dans Home Assistant :
              </p>
              <pre className="overflow-x-auto rounded-md bg-black/40 p-3 text-xs text-white/70">
{`type: vertical-stack
title: KittyHack Cat Flap
cards:
  - show_state: false
    show_name: false
    camera_view: live
    fit_mode: cover
    type: picture-entity
    entity: camera.${mqtt.mqtt_device_id}_camera
  - type: tile
    entity: sensor.${mqtt.mqtt_device_id}_events
    features_position: bottom
    vertical: false
    name: Last Event
    show_entity_picture: false
    hide_state: false
    state_content:
      - event
      - detected_cat
  - type: entities
    show_header_toggle: false
    entities:
      - entity: lock.${mqtt.mqtt_device_id}_inside_lock
        name: Inside
        icon: ""
        secondary_info: none
      - entity: binary_sensor.${mqtt.mqtt_device_id}_outside_lock
        name: Outside
        icon: ""
    state_color: false
    title: Magnetic Locks
  - type: entities
    title: Configuration
    show_header_toggle: false
    entities:
      - entity: select.${mqtt.mqtt_device_id}_allow_enter
        name: Open entrance for...
        icon: ""
        secondary_info: none
      - entity: select.${mqtt.mqtt_device_id}_allow_exit
        name: Allow cats to exit
        icon: ""
        secondary_info: none
    state_color: false
  - type: entities
    entities:
      - entity: binary_sensor.${mqtt.mqtt_device_id}_motion_outside
        name: Motion outside
        secondary_info: last-changed
      - entity: binary_sensor.${mqtt.mqtt_device_id}_motion_inside
        name: Motion inside
        secondary_info: last-changed
      - entity: binary_sensor.${mqtt.mqtt_device_id}_prey_detected
        name: Prey detected
        secondary_info: last-changed
    title: Status
    state_color: false
    show_header_toggle: false`}
              </pre>
            </>
          )}
        </Section>
        <Section title="Avancé" description="Niveau de log, inférence distante, et options matérielles.">
          {advanced && (
            <>
              <Field label="Niveau de log">
                <select
                  value={advanced.loglevel}
                  onChange={(e) => setAdvanced({ ...advanced, loglevel: e.target.value })}
                  className="w-full rounded-md border border-white/15 bg-neutral-900 px-3 py-2"
                >
                  <option value="DEBUG">DEBUG</option>
                  <option value="INFO">INFO</option>
                  <option value="WARN">WARN</option>
                  <option value="ERROR">ERROR</option>
                  <option value="CRITICAL">CRITICAL</option>
                </select>
              </Field>
              <p className="mb-3 text-xs text-white/60">
                INFO est le niveau par défaut. N'utilise DEBUG que si vraiment nécessaire.
              </p>

              <Field label="Limite FPS de l'inférence distante">
                <input
                  type="number"
                  min={1}
                  max={60}
                  value={advanced.remote_inference_max_fps}
                  onChange={(e) => setAdvanced({ ...advanced, remote_inference_max_fps: Number(e.target.value) })}
                  className={inputClass}
                />
              </Field>
              <p className="mb-3 text-xs text-white/60">
                Limite la boucle d'inférence du modèle pour réduire la charge CPU (défaut : 10).
              </p>

              <label className="mb-3 flex items-center gap-2 text-sm">
                <input
                  type="checkbox"
                  checked={advanced.restart_ip_camera_stream_on_failure}
                  onChange={(e) =>
                    setAdvanced({ ...advanced, restart_ip_camera_stream_on_failure: e.target.checked })
                  }
                />
                Surveillance du flux caméra IP (watchdog)
              </label>
              <p className="mb-3 text-xs text-white/60">
                Redémarre automatiquement le flux de la caméra externe si des images corrompues sont détectées.
              </p>

              <Field label="Appareil d'inférence (expérimental)">
                <select
                  value={advanced.inference_device}
                  onChange={(e) => setAdvanced({ ...advanced, inference_device: e.target.value })}
                  className="w-full rounded-md border border-white/15 bg-neutral-900 px-3 py-2"
                >
                  <option value="cpu">CPU (par défaut)</option>
                  <option value="cuda:0">GPU NVIDIA (CUDA)</option>
                  <option value="intel:gpu">GPU Intel (OpenVINO)</option>
                </select>
              </Field>
              <p className="mb-3 text-xs text-white/60">
                Ne fonctionne qu'avec des modèles YOLO personnalisés, pas avec les modèles TFLite d'origine.
                Fonctionnalité expérimentale, peut causer de l'instabilité sur du matériel non supporté.
              </p>

              <hr className="my-4 border-white/10" />
              <label className="mb-3 flex items-center gap-2 text-sm">
                <input
                  type="checkbox"
                  checked={advanced.disable_rfid_reader}
                  onChange={(e) => setAdvanced({ ...advanced, disable_rfid_reader: e.target.checked })}
                />
                Désactiver le lecteur RFID
              </label>
              <p className="mb-3 text-xs text-white/60">
                Pour le dépannage matériel uniquement. Si activé, seule "Ouvrir pour tous les chats" ou la
                détection par caméra permettra l'entrée.
              </p>

              <button
                onClick={saveAdvanced}
                disabled={savingAdvanced}
                className="w-full rounded-lg bg-white py-3 text-center font-medium text-black disabled:opacity-50"
              >
                💾 Enregistrer
              </button>
              {advancedMsg && <p className="mt-2 text-center text-sm text-white/70">{advancedMsg}</p>}
            </>
          )}
        </Section>
        <Section title="Ajouter un chat" description="Enregistrer un nouveau chat avec sa puce RFID.">
          {addCatState && (
            <>
              <Field label="Nom">
                <input
                  value={newCatName}
                  onChange={(e) => setNewCatName(e.target.value)}
                  className={inputClass}
                />
              </Field>

              <Field label="RFID">
                <input
                  value={newCatRfid}
                  onChange={(e) => setNewCatRfid(e.target.value)}
                  className={`w-full rounded-md border bg-transparent px-3 py-2 ${
                    newCatRfidValid ? "border-white/15" : "border-red-400"
                  }`}
                />
              </Field>
              <p className={`mb-3 text-xs ${newCatRfidValid ? "text-white/60" : "text-red-300"}`}>
                16 caractères hexadécimaux (0-9, A-F) - celui lu par la chatière.
              </p>

              <Field label="Photo (optionnel)">
                <input
                  ref={newCatFileRef}
                  type="file"
                  accept=".jpg,.jpeg,.png"
                  onChange={(e) => setNewCatPhoto(e.target.files?.[0] ?? null)}
                  className="w-full text-sm"
                />
              </Field>

              <label
                className={`mb-3 flex items-center gap-2 text-sm ${
                  !addCatState.prey_check_enabled ? "opacity-40" : ""
                }`}
              >
                <input
                  type="checkbox"
                  checked={newCatPrey}
                  disabled={!addCatState.prey_check_enabled}
                  onChange={(e) => setNewCatPrey(e.target.checked)}
                />
                Activer la détection de proie
              </label>
              {!addCatState.prey_check_enabled && (
                <p className="mb-3 -mt-2 text-xs text-white/60">
                  Désactivé : la détection de proie globale est coupée (section Contrôle de la porte).
                </p>
              )}

              <label
                className={`mb-3 flex items-center gap-2 text-sm ${
                  !addCatState.entry_mode_per_cat ? "opacity-40" : ""
                }`}
              >
                <input
                  type="checkbox"
                  checked={newCatAllowEntry}
                  disabled={!addCatState.entry_mode_per_cat}
                  onChange={(e) => setNewCatAllowEntry(e.target.checked)}
                />
                Autoriser l'entrée
              </label>
              {!addCatState.entry_mode_per_cat && (
                <p className="mb-3 -mt-2 text-xs text-white/60">
                  Disponible seulement en mode "Configuration individuelle par chat" pour l'entrée.
                </p>
              )}

              <label
                className={`mb-3 flex items-center gap-2 text-sm ${
                  !addCatState.exit_mode_per_cat ? "opacity-40" : ""
                }`}
              >
                <input
                  type="checkbox"
                  checked={newCatAllowExit}
                  disabled={!addCatState.exit_mode_per_cat}
                  onChange={(e) => setNewCatAllowExit(e.target.checked)}
                />
                Autoriser la sortie
              </label>
              {!addCatState.exit_mode_per_cat && (
                <p className="mb-3 -mt-2 text-xs text-white/60">
                  Disponible seulement en mode "Configuration individuelle par chat" pour la sortie.
                </p>
              )}

              <button
                onClick={saveNewCat}
                disabled={savingNewCat || !newCatName.trim() || !newCatRfidValid}
                className="w-full rounded-lg bg-white py-3 text-center font-medium text-black disabled:opacity-50"
              >
                🐱 Ajouter le chat
              </button>
              {newCatMsg && <p className="mt-2 text-center text-sm text-white/70">{newCatMsg}</p>}
            </>
          )}
        </Section>
        <Section title="Système" description="Tokens d'API, redémarrage/arrêt, actions distantes.">
          <h3 className="mb-2 font-medium">Actions chatière (Kittyflap)</h3>
          <div className="mb-4 flex gap-2">
            <button
              onClick={() =>
                systemAction("target-reboot", false, "Redémarrer la Kittyflap ?")
              }
              disabled={systemBusy}
              className="flex-1 rounded-md border border-white/20 px-4 py-2 text-sm hover:bg-white/10 disabled:opacity-40"
            >
              🔄 Redémarrer la Kittyflap
            </button>
            <button
              onClick={() =>
                systemAction("target-shutdown", false, "Éteindre la Kittyflap ?")
              }
              disabled={systemBusy}
              className="flex-1 rounded-md border border-white/20 px-4 py-2 text-sm hover:bg-white/10 disabled:opacity-40"
            >
              ⏻ Éteindre la Kittyflap
            </button>
          </div>
          <p className="mb-4 text-xs text-white/60">
            Pour éviter toute perte de données, attends toujours 30s après un arrêt avant de débrancher
            l'alimentation.
          </p>

          <hr className="mb-4 border-white/10" />
          <h3 className="mb-2 font-medium text-amber-300">⚠️ Cet appareil (NAS entier)</h3>
          <p className="mb-3 text-xs text-white/65">
            Ces boutons redémarrent/éteignent tout l'appareil qui héberge ce conteneur - pas seulement
            Kittyhack. Tous tes autres services (Sofa, Meelo, Pooky, etc.) seraient aussi coupés.
          </p>
          <div className="mb-4 flex gap-2">
            <button
              onClick={() =>
                systemAction(
                  "local-reboot",
                  true,
                  "Redémarrer CET APPAREIL (tout le NAS, pas que Kittyhack) ?"
                )
              }
              disabled={systemBusy}
              className="flex-1 rounded-md border border-amber-400/40 px-4 py-2 text-sm text-amber-200 hover:bg-amber-400/10 disabled:opacity-40"
            >
              🔄 Redémarrer cet appareil
            </button>
            <button
              onClick={() =>
                systemAction(
                  "local-shutdown",
                  true,
                  "Éteindre CET APPAREIL (tout le NAS, pas que Kittyhack) ?"
                )
              }
              disabled={systemBusy}
              className="flex-1 rounded-md border border-red-400/40 px-4 py-2 text-sm text-red-300 hover:bg-red-400/10 disabled:opacity-40"
            >
              ⏻ Éteindre cet appareil
            </button>
          </div>
          {systemMsg && <p className="mb-4 text-sm text-white/70">{systemMsg}</p>}

          <hr className="mb-4 border-white/10" />
          <h3 className="mb-2 font-medium">Tokens d'API</h3>
          <p className="mb-3 text-xs text-white/65">
            Authentifient les appels à l'API REST (voir doc/api.md). Chaque token n'est montré qu'une seule
            fois, à sa création — note-le en lieu sûr. Envoie-le via l'en-tête `Authorization: Bearer
            &lt;token&gt;`, ou en `?token=&lt;token&gt;` pour les clients URL-only (Stream Deck).
          </p>

          {tokens && tokens.length > 0 && (
            <table className="mb-4 w-full text-left text-sm">
              <thead>
                <tr className="text-white/65">
                  <th className="pb-1 font-normal">Label</th>
                  <th className="pb-1 font-normal">Créé</th>
                  <th className="pb-1 font-normal">Dernier usage</th>
                </tr>
              </thead>
              <tbody>
                {tokens.map((t) => (
                  <tr key={t.id} className="border-t border-white/10">
                    <td className="py-1">{t.label}</td>
                    <td className="py-1 text-white/60">{t.created_at?.slice(0, 19).replace("T", " ")}</td>
                    <td className="py-1 text-white/60">
                      {t.last_used_at ? t.last_used_at.slice(0, 19).replace("T", " ") : "jamais"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          {tokens && tokens.length === 0 && (
            <p className="mb-4 text-sm text-white/65">Aucun token pour l'instant.</p>
          )}

          <h4 className="mb-2 text-sm font-medium">Créer un nouveau token</h4>
          <div className="mb-4 flex gap-2">
            <input
              value={newTokenLabel}
              onChange={(e) => setNewTokenLabel(e.target.value)}
              placeholder="ex: stream-deck"
              className={inputClass}
            />
            <button
              onClick={createToken}
              disabled={tokenBusy || !newTokenLabel.trim()}
              className="shrink-0 rounded-md border border-white/20 px-4 py-2 text-sm hover:bg-white/10 disabled:opacity-40"
            >
              + Créer
            </button>
          </div>

          {createdToken && (
            <div className="mb-4 rounded-md border border-green-400/30 bg-green-400/10 p-3">
              <p className="mb-2 text-sm font-medium">
                Copie ce token maintenant — il ne sera plus jamais affiché.
              </p>
              <pre className="select-all overflow-x-auto rounded bg-black/40 p-2 text-xs">{createdToken}</pre>
              <button
                onClick={() => setCreatedToken(null)}
                className="mt-2 rounded-md bg-white/10 px-3 py-1 text-xs hover:bg-white/20"
              >
                Fermer
              </button>
            </div>
          )}

          <h4 className="mb-2 text-sm font-medium">Révoquer un token</h4>
          <div className="mb-2 flex gap-2">
            <select
              value={revokeId}
              onChange={(e) => setRevokeId(e.target.value)}
              className="w-full rounded-md border border-white/15 bg-neutral-900 px-3 py-2 text-sm"
            >
              <option value="">— choisir un token —</option>
              {tokens?.map((t) => (
                <option key={t.id} value={t.id}>
                  {t.label} ({t.id})
                </option>
              ))}
            </select>
            <button
              onClick={revokeToken}
              disabled={tokenBusy || !revokeId}
              className="shrink-0 rounded-md border border-red-400/40 px-4 py-2 text-sm text-red-300 hover:bg-red-400/10 disabled:opacity-40"
            >
              🗑️ Révoquer
            </button>
          </div>
          {tokenMsg && <p className="text-sm text-white/70">{tokenMsg}</p>}
        </Section>

        <Section title="Info" description="Versions et espace disque.">
          {sysInfo && (
            <div className="grid grid-cols-2 gap-3 text-sm">
              <div>
                <div className="text-white/65">Version de cet appareil</div>
                <div className="font-mono">{sysInfo.this_device_version}</div>
              </div>
              {sysInfo.is_remote_mode && (
                <div>
                  <div className="text-white/65">Version de la Kittyflap</div>
                  <div className="font-mono">{sysInfo.target_device_version}</div>
                </div>
              )}
              <div>
                <div className="text-white/65">Dernière version disponible</div>
                <div className="font-mono">{sysInfo.latest_version}</div>
              </div>
              <div>
                <div className="text-white/65">Espace disque libre</div>
                <div>
                  {sysInfo.free_disk_mb.toFixed(0)} / {sysInfo.total_disk_mb.toFixed(0)} Mo
                </div>
              </div>
            </div>
          )}

          {/* 08.10, Sid ("je vais jamais reprendre les màj de la page
              officielle, vu qu'on a tout forké. Ce qu'il faut qu'on sache
              c'est quand il y a une màj, pour voir ce qui est intéressant
              dedans et reprendre cet élément dans notre fork"): read-only
              by design - no update button. Just "is there something new
              upstream, what does it say". */}
          <hr className="my-4 border-white/10" />
          <h3 className="mb-2 font-medium">Nouveautés côté dépôt officiel</h3>
          <button
            onClick={checkUpstream}
            disabled={checkingUpstream}
            className="mb-3 rounded-md border border-white/20 px-4 py-2 text-sm hover:bg-white/10 disabled:opacity-40"
          >
            {checkingUpstream ? "Vérification..." : "🔄 Vérifier les nouveautés"}
          </button>

          {upstream && (
            <div className="mb-3 text-sm">
              {upstream.latest_version === "unknown" ? (
                <p className="text-white/65">
                  Impossible de joindre GitHub pour l'instant. Réessaie plus tard.
                </p>
              ) : !upstream.has_update ? (
                <p className="text-white/60">
                  À jour avec le dépôt officiel ({upstream.current_version}).
                </p>
              ) : (
                <>
                  <p className="mb-2">
                    Nouvelle version disponible : <span className="font-mono">{upstream.latest_version}</span>{" "}
                    (toi : <span className="font-mono">{upstream.current_version}</span>)
                  </p>
                  <div className="whitespace-pre-wrap rounded-md bg-black/40 p-3 text-xs text-white/80">
                    {upstream.release_notes || "Pas de notes de version disponibles."}
                  </div>
                </>
              )}
            </div>
          )}

          <p className="mt-3 text-sm">
            <a href="/remote-mode-doc" className="text-blue-300 underline">
              📖 Doc du mode distant
            </a>
          </p>
        </Section>

        <Section
          title="Historique de connexion distante"
          description="Connexions/déconnexions avec la Kittyflap, durées."
        >
          {connectionLog?.empty && (
            <p className="text-sm text-white/65">Pas encore d'historique de connexion.</p>
          )}
          {connectionLog && !connectionLog.empty && (
            <>
              <div className="mb-4 grid grid-cols-3 gap-2">
                <div className="rounded-xl bg-white/5 p-3 text-center">
                  <div className="text-xl font-semibold">{connectionLog.uptime_pct}%</div>
                  <div className="text-xs text-white/60">Disponibilité</div>
                </div>
                <div className="rounded-xl bg-white/5 p-3 text-center">
                  <div className="text-xl font-semibold">{connectionLog.disconnect_count}</div>
                  <div className="text-xs text-white/60">Déconnexions</div>
                </div>
                <div className="rounded-xl bg-white/5 p-3 text-center">
                  <div className="text-base font-semibold">{connectionLog.longest_streak_label}</div>
                  <div className="text-xs text-white/60">Plus longue série stable</div>
                </div>
              </div>
              <p className="mb-2 text-xs text-white/60">
                Depuis le {connectionLog.since_label} · plus récent en premier
              </p>
              <div className="flex flex-col gap-1">
                {connectionLog.rows?.map((r, i) => (
                  <div key={i} className="flex items-start gap-2 rounded-lg bg-white/5 px-3 py-2 text-sm">
                    <span className="mt-0.5">{r.connected ? "📶" : "⚠️"}</span>
                    <div>
                      <div className="text-xs text-white/65">{r.at}</div>
                      <div>
                        <span className={r.connected ? "text-green-300" : "text-red-300"}>
                          {r.connected ? "Connecté" : "Déconnecté"}
                        </span>
                        {" · "}
                        {r.duration_label}
                        {r.ongoing ? " (en cours)" : ""}
                      </div>
                      {r.reason && <div className="text-xs text-white/60">{r.reason}</div>}
                    </div>
                  </div>
                ))}
              </div>
            </>
          )}
        </Section>
      </div>
      <BottomNav active="configuration" />
    </>
  );
}
