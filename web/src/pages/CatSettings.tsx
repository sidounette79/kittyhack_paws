import { useEffect, useRef, useState } from "react";
import { useNavigate, useParams } from "react-router";
import BottomNav from "../components/BottomNav";
import { fetchJson } from "../lib/api";
import { useBackButton } from "../lib/useBackButton";
import { usePageTitle } from "../lib/usePageTitle";

interface CatBio {
  species: string;
  sex: string;
  sterilized: boolean | null;
  birth_date: string;
  weight_kg: number | null;
  breed_primary: string;
  breed_secondary: string;
  bio_comments: string;
}

interface CatSettings {
  id: number;
  name: string;
  rfid: string;
  photo_url: string | null;
  enable_prey_detection: boolean;
  block_exit_after_prey: boolean;
  allow_entry: boolean;
  allow_exit: boolean;
  entry_editable: boolean;
  exit_editable: boolean;
  prey_editable: boolean;
  block_exit_editable: boolean;
  bio: CatBio;
}

const RFID_RE = /^[0-9A-Fa-f]{16}$/;

function Toggle({
  label,
  checked,
  disabled,
  disabledNote,
  onChange,
}: {
  label: string;
  checked: boolean;
  disabled?: boolean;
  disabledNote?: string;
  onChange: (v: boolean) => void;
}) {
  return (
    <div className={disabled ? "opacity-50" : ""}>
      <label className="flex items-center justify-between gap-3 py-2">
        <span>{label}</span>
        <input
          type="checkbox"
          checked={checked}
          disabled={disabled}
          onChange={(e) => onChange(e.target.checked)}
          className="h-5 w-9 accent-white/80"
        />
      </label>
      {disabled && disabledNote && (
        <p className="pb-2 text-xs text-white/60">{disabledNote}</p>
      )}
    </div>
  );
}

export default function CatSettingsPage() {
  const navigate = useNavigate();
  const { rfid } = useParams<{ rfid: string }>();
  const [settings, setSettings] = useState<CatSettings | null>(null);
  usePageTitle(settings ? `Réglages de ${settings.name}` : "Réglages du chat");
  const [name, setName] = useState("");
  const [rfidValue, setRfidValue] = useState("");
  const [preyDetection, setPreyDetection] = useState(true);
  const [blockExitAfterPrey, setBlockExitAfterPrey] = useState(true);
  const [allowEntry, setAllowEntry] = useState(true);
  const [allowExit, setAllowExit] = useState(true);
  const [bioSpecies, setBioSpecies] = useState("");
  const [bioSex, setBioSex] = useState("");
  const [bioSterilized, setBioSterilized] = useState("");
  const [bioBirthDate, setBioBirthDate] = useState("");
  const [bioWeight, setBioWeight] = useState("");
  const [bioBreedPrimary, setBioBreedPrimary] = useState("");
  const [bioBreedSecondary, setBioBreedSecondary] = useState("");
  const [bioComments, setBioComments] = useState("");
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);

  useBackButton(
    () => false,
    () => navigate(`/presence/cat/${rfid}`)
  );

  useEffect(() => {
    if (!rfid) return;
    fetchJson<CatSettings>(`/app-api/v1/presence/cats/${rfid}/settings`)
      .then((s) => {
        setSettings(s);
        setName(s.name);
        setRfidValue(s.rfid);
        setPreyDetection(s.enable_prey_detection);
        setBlockExitAfterPrey(s.block_exit_after_prey);
        setAllowEntry(s.allow_entry);
        setAllowExit(s.allow_exit);
        setBioSpecies(s.bio.species);
        setBioSex(s.bio.sex);
        setBioSterilized(s.bio.sterilized === null ? "" : s.bio.sterilized ? "yes" : "no");
        setBioBirthDate(s.bio.birth_date);
        setBioWeight(s.bio.weight_kg === null ? "" : String(s.bio.weight_kg));
        setBioBreedPrimary(s.bio.breed_primary);
        setBioBreedSecondary(s.bio.breed_secondary);
        setBioComments(s.bio.bio_comments);
      })
      .catch(() => navigate(`/presence/cat/${rfid}`));
  }, [rfid, navigate]);

  const rfidValid = rfidValue.trim() === "" || RFID_RE.test(rfidValue.trim());

  const save = async () => {
    if (!rfid || !rfidValid) return;
    setSaving(true);
    setMessage(null);
    try {
      const result = await fetchJson<{ ok: boolean; rfid: string }>(
        `/app-api/v1/presence/cats/${rfid}/settings`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            name,
            rfid: rfidValue.trim().toUpperCase(),
            enable_prey_detection: preyDetection,
            block_exit_after_prey: blockExitAfterPrey,
            allow_entry: allowEntry,
            allow_exit: allowExit,
            bio: {
              species: bioSpecies.trim(),
              sex: bioSex,
              sterilized: bioSterilized === "" ? null : bioSterilized === "yes",
              birth_date: bioBirthDate,
              weight_kg: bioWeight.trim() === "" ? null : Number(bioWeight),
              breed_primary: bioBreedPrimary.trim(),
              breed_secondary: bioBreedSecondary.trim(),
              bio_comments: bioComments.trim(),
            },
          }),
        }
      );
      setMessage("Enregistré.");
      // The RFID (part of the URL) may have just changed - follow it so the
      // page keeps pointing at a cat that still actually exists at this URL.
      if (result.rfid !== rfid) {
        navigate(`/presence/cat/${result.rfid}/edit`, { replace: true });
      }
    } catch {
      setMessage("Échec de l'enregistrement.");
    } finally {
      setSaving(false);
    }
  };

  const uploadPhoto = async (file: File) => {
    if (!rfid) return;
    const form = new FormData();
    form.append("photo", file);
    setSaving(true);
    try {
      await fetchJson(`/app-api/v1/presence/cats/${rfid}/photo`, {
        method: "POST",
        body: form,
      });
      // Bust the cached /cat-photo/<rfid>.jpg image.
      setSettings((s) => (s ? { ...s, photo_url: `/cat-photo/${rfid}.jpg?t=${Date.now()}` } : s));
    } catch {
      setMessage("Échec de l'envoi de la photo.");
    } finally {
      setSaving(false);
    }
  };

  const doDelete = async () => {
    if (!rfid) return;
    setSaving(true);
    try {
      await fetchJson(`/app-api/v1/presence/cats/${rfid}`, { method: "DELETE" });
      navigate("/presence", { replace: true });
    } catch {
      setMessage("Échec de la suppression.");
      setSaving(false);
    }
  };

  if (!settings) {
    return (
      <>
        <div className="container mx-auto max-w-2xl px-3 py-4">
          <p className="text-center opacity-70">Chargement…</p>
        </div>
        <BottomNav active="presence" />
      </>
    );
  }

  return (
    <>
      <div className="container mx-auto max-w-2xl px-3 py-4">
        <div className="mb-4 flex items-center gap-3">
          <button
            onClick={() => navigate(`/presence/cat/${rfid}`)}
            aria-label="Retour"
            className="rounded-md border border-white/15 px-3 py-1.5 text-sm hover:bg-white/10"
          >
            ‹
          </button>
          <div className="text-lg font-semibold">Paramètres de {settings.name}</div>
        </div>

        <div className="mb-4 flex flex-col items-center gap-2">
          {settings.photo_url ? (
            <img
              src={settings.photo_url}
              alt=""
              className="h-28 w-28 rounded-full object-cover"
            />
          ) : (
            <div className="flex h-28 w-28 items-center justify-center rounded-full bg-white/10 text-4xl">
              🐱
            </div>
          )}
          <input
            ref={fileInputRef}
            type="file"
            accept=".jpg,.jpeg,.png"
            className="hidden"
            onChange={(e) => {
              const file = e.target.files?.[0];
              if (file) uploadPhoto(file);
            }}
          />
          <button
            onClick={() => fileInputRef.current?.click()}
            className="rounded-md border border-white/15 px-3 py-1.5 text-sm hover:bg-white/10"
          >
            Changer la photo
          </button>
        </div>

        <label className="mb-3 block">
          <span className="mb-1 block text-sm text-white/70">Nom</span>
          <input
            value={name}
            onChange={(e) => setName(e.target.value)}
            className="w-full rounded-md border border-white/15 bg-transparent px-3 py-2"
          />
        </label>

        <label className="mb-1 block">
          <span className="mb-1 block text-sm text-white/70">RFID</span>
          <input
            value={rfidValue}
            onChange={(e) => setRfidValue(e.target.value)}
            className={`w-full rounded-md border bg-transparent px-3 py-2 ${
              rfidValid ? "border-white/15" : "border-red-400"
            }`}
          />
        </label>
        <p className={`mb-1 text-xs ${rfidValid ? "text-white/60" : "text-red-300"}`}>
          {rfidValid
            ? "16 caractères hexadécimaux (0-9, A-F) - celui lu par la chatière, pas le numéro du carnet vétérinaire."
            : "RFID invalide : 16 caractères hexadécimaux (0-9, A-F) attendus."}
        </p>
        <p className="mb-4 text-xs text-white/60">
          Si le RFID est correct, le nom du chat s'affichera dans la section PHOTOS.
        </p>

        <div className="mb-4 rounded-xl border border-white/10 bg-white/5 p-4">
          <div className="mb-1 text-center text-sm font-semibold text-white/80">
            Réglages pour ce chat
          </div>
          <Toggle
            label="Activer la détection de proie"
            checked={preyDetection}
            disabled={!settings.prey_editable}
            disabledNote="Désactivé : la détection de proie globale est coupée dans CONFIGURATION."
            onChange={setPreyDetection}
          />
          <Toggle
            label="Bloquer la sortie après une entrée avec proie"
            checked={blockExitAfterPrey}
            disabled={!settings.block_exit_editable}
            disabledNote="Désactivé globalement dans CONFIGURATION."
            onChange={setBlockExitAfterPrey}
          />
          <Toggle
            label="Autoriser l'entrée"
            checked={allowEntry}
            disabled={!settings.entry_editable}
            disabledNote="Disponible seulement en mode « Configuration individuelle par chat » pour l'entrée."
            onChange={setAllowEntry}
          />
          <Toggle
            label="Autoriser la sortie"
            checked={allowExit}
            disabled={!settings.exit_editable}
            disabledNote="Disponible seulement en mode « Configuration individuelle par chat » pour la sortie."
            onChange={setAllowExit}
          />
        </div>

        {/* 07.10, Sid ("un petit espace biographie"): free-form profile
            fields, referenced on a screenshot of another pet-tracker app's
            "Biographie" form - same field set, adapted to our dark theme. */}
        <div className="mb-4 rounded-xl border border-white/10 bg-white/5 p-4">
          <div className="mb-3 text-center text-sm font-semibold text-white/80">
            Biographie
          </div>

          <label className="mb-3 block">
            <span className="mb-1 block text-sm text-white/70">Espèce</span>
            <input
              value={bioSpecies}
              onChange={(e) => setBioSpecies(e.target.value)}
              placeholder="Chat"
              className="w-full rounded-md border border-white/15 bg-transparent px-3 py-2"
            />
          </label>

          <div className="mb-3 grid grid-cols-2 gap-3">
            <label className="block">
              <span className="mb-1 block text-sm text-white/70">Sexe</span>
              <select
                value={bioSex}
                onChange={(e) => setBioSex(e.target.value)}
                className="w-full rounded-md border border-white/15 bg-neutral-900 px-3 py-2"
              >
                <option value="">—</option>
                <option value="male">Mâle</option>
                <option value="female">Femelle</option>
              </select>
            </label>
            <label className="block">
              <span className="mb-1 block text-sm text-white/70">Stérilisé</span>
              <select
                value={bioSterilized}
                onChange={(e) => setBioSterilized(e.target.value)}
                className="w-full rounded-md border border-white/15 bg-neutral-900 px-3 py-2"
              >
                <option value="">—</option>
                <option value="yes">Oui</option>
                <option value="no">Non</option>
              </select>
            </label>
          </div>

          <div className="mb-3 grid grid-cols-2 gap-3">
            <label className="block">
              <span className="mb-1 block text-sm text-white/70">Date de naissance</span>
              <input
                type="date"
                value={bioBirthDate}
                onChange={(e) => setBioBirthDate(e.target.value)}
                className="w-full rounded-md border border-white/15 bg-transparent px-3 py-2"
              />
            </label>
            <label className="block">
              <span className="mb-1 block text-sm text-white/70">Poids (kg)</span>
              <input
                type="number"
                step="0.1"
                min="0"
                value={bioWeight}
                onChange={(e) => setBioWeight(e.target.value)}
                className="w-full rounded-md border border-white/15 bg-transparent px-3 py-2"
              />
            </label>
          </div>

          <div className="mb-3 grid grid-cols-2 gap-3">
            <label className="block">
              <span className="mb-1 block text-sm text-white/70">Race principale</span>
              <input
                value={bioBreedPrimary}
                onChange={(e) => setBioBreedPrimary(e.target.value)}
                className="w-full rounded-md border border-white/15 bg-transparent px-3 py-2"
              />
            </label>
            <label className="block">
              <span className="mb-1 block text-sm text-white/70">Race secondaire</span>
              <input
                value={bioBreedSecondary}
                onChange={(e) => setBioBreedSecondary(e.target.value)}
                className="w-full rounded-md border border-white/15 bg-transparent px-3 py-2"
              />
            </label>
          </div>

          <label className="block">
            <span className="mb-1 block text-sm text-white/70">Commentaires</span>
            <textarea
              value={bioComments}
              onChange={(e) => setBioComments(e.target.value)}
              rows={3}
              className="w-full rounded-md border border-white/15 bg-transparent px-3 py-2"
            />
          </label>
        </div>

        {message && <p className="mb-3 text-center text-sm">{message}</p>}

        <button
          onClick={save}
          disabled={saving || !rfidValid}
          className="mb-6 w-full rounded-lg bg-white py-3 text-center font-medium text-black disabled:opacity-50"
        >
          💾 Enregistrer les modifications
        </button>

        <div className="rounded-xl border border-red-500/20 bg-red-500/5 p-4">
          <label className="flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              checked={confirmDelete}
              onChange={(e) => setConfirmDelete(e.target.checked)}
            />
            Supprimer {settings.name} de la base de données
          </label>
          {confirmDelete && (
            <button
              onClick={doDelete}
              disabled={saving}
              className="mt-3 w-full rounded-lg bg-red-500/20 py-2.5 text-center text-sm font-medium text-red-200 hover:bg-red-500/30"
            >
              Confirmer la suppression
            </button>
          )}
        </div>
      </div>
      <BottomNav active="presence" />
    </>
  );
}
