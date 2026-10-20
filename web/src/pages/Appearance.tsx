import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router";
import BottomNav from "../components/BottomNav";
import { fetchJson } from "../lib/api";
import { useBackButton } from "../lib/useBackButton";
import { usePageTitle } from "../lib/usePageTitle";

interface WallpaperImage {
  filename: string;
  url: string;
}

interface WallpaperState {
  active: string | null;
  images: WallpaperImage[];
}

const MAX_WALLPAPERS = 4;

// 07.10, Sid ("choisir parmi des wallpaper, voir télécharger depuis
// smartphone - comme pour meelo"): same gallery shape as Meelo's mobile
// wallpaper picker (apps/mobile/src/pages/settings/sections/wallpaper.tsx) -
// up to MAX_WALLPAPERS uploads, tap one to make it active, remove any,
// "+" tile to pick a new one from the phone.
export default function AppearancePage() {
  usePageTitle("Apparence");
  const navigate = useNavigate();
  const [state, setState] = useState<WallpaperState | null>(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [blurPx, setBlurPx] = useState(10);
  const fileInputRef = useRef<HTMLInputElement | null>(null);

  useBackButton(
    () => false,
    () => navigate(-1)
  );

  const load = () => {
    fetchJson<WallpaperState>("/app-api/v1/wallpaper")
      .then(setState)
      .catch(() => {});
  };

  useEffect(() => {
    fetchJson<{ blur_px: number }>("/app-api/v1/appearance/photo-blur")
      .then((d) => setBlurPx(d.blur_px))
      .catch(() => {});
  }, []);

  // 07.10, Sid ("encore de flou... rajoute un réglage dans apparence et je
  // modifie moi"): live preview as the slider moves, saved on release
  // (onMouseUp/onTouchEnd would need two handlers - onChange firing on
  // every tick already debounces naturally since a drag fires far fewer
  // POSTs than it would keystrokes).
  const onBlurChange = (value: number) => {
    setBlurPx(value);
    fetchJson("/app-api/v1/appearance/photo-blur", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ blur_px: value }),
    }).catch(() => {});
  };

  useEffect(load, []);

  const onChooseImage = () => fileInputRef.current?.click();

  const onFileSelected = async (file: File) => {
    const form = new FormData();
    form.append("image", file);
    setBusy(true);
    setMessage(null);
    try {
      await fetchJson("/app-api/v1/wallpaper", { method: "POST", body: form });
      load();
    } catch (err) {
      setMessage(err instanceof Error ? err.message : "Échec de l'envoi.");
    } finally {
      setBusy(false);
    }
  };

  const onSelect = async (filename: string) => {
    setBusy(true);
    try {
      await fetchJson("/app-api/v1/wallpaper/active", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ filename }),
      });
      load();
    } finally {
      setBusy(false);
    }
  };

  const onClear = async () => {
    setBusy(true);
    try {
      await fetchJson("/app-api/v1/wallpaper/active", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ filename: "" }),
      });
      load();
    } finally {
      setBusy(false);
    }
  };

  const onRemove = async (filename: string) => {
    setBusy(true);
    try {
      await fetchJson(`/app-api/v1/wallpaper/${filename}`, { method: "DELETE" });
      load();
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <div className="container mx-auto max-w-2xl px-3 py-4">
        <div className="mb-4 flex items-center gap-3">
          <button
            onClick={() => navigate(-1)}
            aria-label="Retour"
            className="rounded-md border border-white/15 px-3 py-1.5 text-sm hover:bg-white/10"
          >
            ‹
          </button>
          <div className="text-lg font-semibold">Apparence</div>
        </div>

        <div className="mb-4 rounded-xl border border-white/10 bg-white/5 p-4">
          <div className="mb-1 text-sm font-semibold text-white/80">Fond d'écran</div>
          <p className="mb-3 text-xs text-white/65">
            Choisis une photo parmi celles déjà envoyées, ou ajoutes-en une nouvelle depuis ton
            téléphone (max {MAX_WALLPAPERS}).
          </p>

          <input
            ref={fileInputRef}
            type="file"
            accept="image/*"
            className="hidden"
            onChange={(e) => {
              const file = e.target.files?.[0];
              if (file) onFileSelected(file);
              e.target.value = "";
            }}
          />

          <div className="flex flex-wrap gap-3">
            <button
              onClick={onClear}
              disabled={busy}
              className={`flex h-16 w-16 flex-col items-center justify-center rounded-lg border text-[0.65rem] ${
                state && !state.active
                  ? "border-white bg-white/20"
                  : "border-white/15 bg-white/5 hover:bg-white/10"
              }`}
            >
              Aucun
            </button>
            {state?.images.map((img) => (
              <div key={img.filename} className="relative h-16 w-16">
                <button
                  onClick={() => onSelect(img.filename)}
                  disabled={busy}
                  className={`h-16 w-16 overflow-hidden rounded-lg border-2 ${
                    state.active === img.filename ? "border-white" : "border-transparent"
                  }`}
                >
                  <img src={img.url} alt="" className="h-full w-full object-cover" />
                </button>
                <button
                  onClick={() => onRemove(img.filename)}
                  disabled={busy}
                  aria-label="Supprimer ce fond d'écran"
                  className="absolute -right-1.5 -top-1.5 flex h-5 w-5 items-center justify-center rounded-full bg-neutral-900 text-xs"
                >
                  ✕
                </button>
              </div>
            ))}
            {(state?.images.length ?? 0) < MAX_WALLPAPERS && (
              <button
                onClick={onChooseImage}
                disabled={busy}
                className="flex h-16 w-16 items-center justify-center rounded-lg border border-dashed border-white/25 text-xl text-white/60 hover:bg-white/5"
              >
                +
              </button>
            )}
          </div>

          {message && <p className="mt-3 text-sm text-red-300">{message}</p>}
        </div>

        <div className="mb-4 rounded-xl border border-white/10 bg-white/5 p-4">
          <div className="mb-1 text-sm font-semibold text-white/80">
            Flou de la photo (fiche chat)
          </div>
          <p className="mb-3 text-xs text-white/65">
            Netteté de la photo du chat en fond de la bande de dates.
          </p>
          <div className="flex items-center gap-3">
            <span className="text-xs text-white/65">Net</span>
            <input
              type="range"
              min={0}
              max={30}
              value={blurPx}
              onChange={(e) => onBlurChange(Number(e.target.value))}
              className="w-full accent-white"
            />
            <span className="text-xs text-white/65">Flou</span>
          </div>
          <div className="mt-1 text-center text-xs text-white/60">{blurPx}px</div>
        </div>

        {/* 07.10, Sid ("le thème peut aller dans apparence"): the old Shiny
            navbar's "Theme: Dark" badge wasn't a real toggle Sid ever used -
            she never wants light mode on any project (physically bothers
            her eyes), and index.css hardcodes the dark palette rather than
            making it switchable. Informational placeholder here rather than
            building a working light-mode switch nobody asked to actually
            use. */}
        <div className="mb-4 rounded-xl border border-white/10 bg-white/5 p-4">
          <div className="mb-1 text-sm font-semibold text-white/80">Thème</div>
          <p className="text-xs text-white/65">
            Sombre uniquement — pas de mode clair sur cette app.
          </p>
        </div>
      </div>
      <BottomNav active="appearance" />
    </>
  );
}
