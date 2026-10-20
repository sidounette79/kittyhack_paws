import { useState } from "react";

// 10.10, Sid ("au lieu d'avoir le texte complet... on peut juste avoir un
// petit rond de couleur? Comme le rond de connexion dans la bannière, avec
// le texte qui s'affiche si on clique... comme ça je vois qu'il y a quelque
// chose mais sans que ça pollue"): same pattern as TopBanner's own
// connection dot (a small colored circle, tap to reveal). Unlike
// TopBanner's `title` attribute (a hover tooltip, unreliable on touch),
// this toggles a real inline text on tap - works the same on phone and
// desktop.
export default function InfoDot({
  color,
  label,
}: {
  color: string;
  label: string;
}) {
  const [open, setOpen] = useState(false);
  return (
    <span className="inline-flex items-center gap-1.5 align-middle">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-label={label}
        title={label}
        className="inline-flex h-4 w-4 items-center justify-center"
      >
        <span className={`h-2.5 w-2.5 rounded-full ${color}`} />
      </button>
      {open && <span className="text-[0.85rem] text-white/70">{label}</span>}
    </span>
  );
}
