import { useEffect, useState } from "react";
import { fetchJson } from "../lib/api";

interface WallpaperState {
  active: string | null;
  images: { filename: string; url: string }[];
}

// 07.10, Sid ("choisir parmi des wallpaper... comme pour meelo" / "les
// fonds de puce devaient rester foncés comme avant, le wallpaper doit juste
// faire une structure sur la vraie page de fond"): fixed, full-screen
// background behind every React page. First pass used only a 60%-black
// scrim, which left the photo bright enough to wash out every card's
// existing bg-white/5 (calibrated for a plain #121212 backdrop, not a
// colorful photo) - a much stronger scrim keeps cards reading exactly as
// dark as before, while the wallpaper still shows through as a faint,
// structural tint rather than a bright picture.
export default function WallpaperBackground() {
  const [url, setUrl] = useState<string | null>(null);

  useEffect(() => {
    fetchJson<WallpaperState>("/app-api/v1/wallpaper")
      .then((d) => {
        const active = d.images.find((i) => i.filename === d.active);
        setUrl(active ? active.url : null);
      })
      .catch(() => {});
  }, []);

  if (!url) return null;

  return (
    <div
      aria-hidden="true"
      className="fixed inset-0 -z-10 bg-cover bg-center"
      style={{ backgroundImage: `url(${url})` }}
    >
      <div className="absolute inset-0 bg-black/85" />
    </div>
  );
}
