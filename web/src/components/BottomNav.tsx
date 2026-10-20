import { useEffect, useState, type ReactElement } from "react";

// 07.10, Sid ("j'ai pas la barre de navigation en bas... ca se mettra
// quand on aura terminé?"): no, it never would have on its own - each
// migrated page is its own standalone React app with no shared chrome.
// This is the reusable replacement for ui.py's _bottom_nav_bar(), meant to
// be dropped into every page as it migrates (`<BottomNav active="..." />`)
// so none of them end up without it again. Icon paths copied 1:1 from the
// Shiny side's faicons output (icon_svg("paw"/"tv"/"clock"/"route"/"bars"))
// for exact visual parity, not a different icon set.

function PawIcon() {
  return (
    <svg viewBox="0 0 512 512" className="h-5 w-5" fill="currentColor" aria-hidden="true">
      <path d="M226.5 92.9c14.3 42.9-.3 86.2-32.6 96.8s-70.1-15.6-84.4-58.5s.3-86.2 32.6-96.8s70.1 15.6 84.4 58.5zM100.4 198.6c18.9 32.4 14.3 70.1-10.2 84.1s-59.7-.9-78.5-33.3S-2.7 179.3 21.8 165.3s59.7 .9 78.5 33.3zM69.2 401.2C121.6 259.9 214.7 224 256 224s134.4 35.9 186.8 177.2c3.6 9.7 5.2 20.1 5.2 30.5v1.6c0 25.8-20.9 46.7-46.7 46.7c-11.5 0-22.9-1.4-34-4.2l-88-22c-15.3-3.8-31.3-3.8-46.6 0l-88 22c-11.1 2.8-22.5 4.2-34 4.2C84.9 480 64 459.1 64 433.3v-1.6c0-10.4 1.6-20.8 5.2-30.5zM421.8 282.7c-24.5-14-29.1-51.7-10.2-84.1s54-47.3 78.5-33.3s29.1 51.7 10.2 84.1s-54 47.3-78.5 33.3zM310.1 189.7c-32.3-10.6-46.9-53.9-32.6-96.8s52.1-69.1 84.4-58.5s46.9 53.9 32.6 96.8s-52.1 69.1-84.4 58.5z" />
    </svg>
  );
}

function TvIcon() {
  return (
    <svg viewBox="0 0 640 512" className="h-5 w-5" fill="currentColor" aria-hidden="true">
      <path d="M64 64V352H576V64H64zM0 64C0 28.7 28.7 0 64 0H576c35.3 0 64 28.7 64 64V352c0 35.3-28.7 64-64 64H64c-35.3 0-64-28.7-64-64V64zM128 448H512c17.7 0 32 14.3 32 32s-14.3 32-32 32H128c-17.7 0-32-14.3-32-32s14.3-32 32-32z" />
    </svg>
  );
}

function RouteIcon() {
  return (
    <svg viewBox="0 0 512 512" className="h-5 w-5" fill="currentColor" aria-hidden="true">
      <path d="M416 256s96-96 96-160c0-53-43-96-96-96s-96 43-96 96c0 29.4 20.2 65.5 42.1 96H320c-53 0-96 43-96 96s43 96 96 96h96c17.7 0 32 14.3 32 32s-14.3 32-32 32H188.6c-6.2 9.6-12.6 18.8-19 27.2c-10.7 14.2-21.3 26.9-30 36.8H416c53 0 96-43 96-96s-43-96-96-96H320c-17.7 0-32-14.3-32-32s14.3-32 32-32h96zm0-128c-17.7 0-32-14.3-32-32s14.3-32 32-32s32 14.3 32 32s-14.3 32-32 32zM149.9 448c21.9-30.5 42.1-66.6 42.1-96c0-53-43-96-96-96s-96 43-96 96c0 64 96 160 96 160s3.5-3.5 9.2-9.6c.4-.4 .7-.8 1.1-1.2c3.3-3.5 7.1-7.8 11.4-12.8c.2-.2 .4-.4 .6-.6c9.4-10.8 20.7-24.6 31.6-39.8zM96 384c-17.7 0-32-14.3-32-32s14.3-32 32-32s32 14.3 32 32s-14.3 32-32 32z" />
    </svg>
  );
}

function ImageIcon() {
  return (
    <svg viewBox="0 0 512 512" className="h-5 w-5" fill="currentColor" aria-hidden="true">
      <path d="M0 96C0 60.7 28.7 32 64 32H448c35.3 0 64 28.7 64 64V416c0 35.3-28.7 64-64 64H64c-35.3 0-64-28.7-64-64V96zm323.8 202.5c-4.5-6-11.6-9.5-19.1-9.5s-14.6 3.5-19.1 9.5l-54.7 72.9-20.5-27.3c-4.5-6-11.6-9.5-19.1-9.5s-14.6 3.5-19.1 9.5l-72 96c-5.4 7.2-6.3 16.8-2.2 24.9s12.1 13.3 21.1 13.3h88 48 160c9 0 17.2-5 21.2-13s3.1-17.6-2.2-24.9l-112-144zM112 192a48 48 0 1 0 0-96 48 48 0 1 0 0 96z" />
    </svg>
  );
}

function SunIcon() {
  return (
    <svg viewBox="0 0 512 512" className="h-5 w-5" fill="currentColor" aria-hidden="true">
      <path d="M361.5 1.2c5 2.1 8.6 6.6 9.6 11.9L391 121l107.9 19.8c5.3 1 9.8 4.6 11.9 9.6s1.5 10.7-1.6 15.2L446.9 256l62.3 90.3c3.1 4.5 3.7 10.2 1.6 15.2s-6.6 8.6-11.9 9.6L391 391 371.1 498.9c-1 5.3-4.6 9.8-9.6 11.9s-10.7 1.5-15.2-1.6L256 446.9l-90.3 62.3c-4.5 3.1-10.2 3.7-15.2 1.6s-8.6-6.6-9.6-11.9L121 391 13.1 371.1c-5.3-1-9.8-4.6-11.9-9.6s-1.5-10.7 1.6-15.2L65.1 256 2.8 165.7c-3.1-4.5-3.7-10.2-1.6-15.2s6.6-8.6 11.9-9.6L121 121 140.9 13.1c1-5.3 4.6-9.8 9.6-11.9s10.7-1.5 15.2 1.6L256 65.1 346.3 2.8c4.5-3.1 10.2-3.7 15.2-1.6z" />
    </svg>
  );
}

function ChipIcon() {
  return (
    <svg viewBox="0 0 512 512" className="h-5 w-5" fill="currentColor" aria-hidden="true">
      <rect x="128" y="128" width="256" height="256" rx="32" />
      <rect x="208" y="32" width="16" height="96" />
      <rect x="288" y="32" width="16" height="96" />
      <rect x="208" y="384" width="16" height="96" />
      <rect x="288" y="384" width="16" height="96" />
      <rect x="32" y="208" width="96" height="16" />
      <rect x="32" y="288" width="96" height="16" />
      <rect x="384" y="208" width="96" height="16" />
      <rect x="384" y="288" width="96" height="16" />
    </svg>
  );
}

function GearIcon() {
  return (
    <svg viewBox="0 0 512 512" className="h-5 w-5" fill="currentColor" aria-hidden="true">
      <path d="M495.9 166.6c3.2 8.7 .5 18.4-6.4 24.6l-43.3 39.4c1.1 8.3 1.7 16.8 1.7 25.4s-.6 17.1-1.7 25.4l43.3 39.4c6.9 6.2 9.6 15.9 6.4 24.6c-4.4 11.9-9.7 23.3-15.8 34.3l-4.7 8.1c-6.6 11-14 21.4-22.1 31.2c-5.9 7.2-15.7 9.6-24.5 6.8l-55.7-17.7c-13.4 10.3-28.2 18.9-44 25.4l-12.5 57.1c-2 9.1-9 16.3-18.2 17.8c-13.8 2.3-28 3.5-42.5 3.5s-28.7-1.2-42.5-3.5c-9.2-1.5-16.2-8.7-18.2-17.8l-12.5-57.1c-15.8-6.5-30.6-15.1-44-25.4L83.1 410.3c-8.8 2.8-18.6 .3-24.5-6.8c-8.1-9.8-15.5-20.2-22.1-31.2l-4.7-8.1c-6.1-11-11.4-22.4-15.8-34.3c-3.2-8.7-.5-18.4 6.4-24.6l43.3-39.4C64.6 273.1 64 264.6 64 256s.6-17.1 1.7-25.4L22.4 191.2c-6.9-6.2-9.6-15.9-6.4-24.6c4.4-11.9 9.7-23.3 15.8-34.3l4.7-8.1c6.6-11 14-21.4 22.1-31.2c5.9-7.2 15.7-9.6 24.5-6.8l55.7 17.7c13.4-10.3 28.2-18.9 44-25.4l12.5-57.1c2-9.1 9-16.3 18.2-17.8C227.3 1.2 241.5 0 256 0s28.7 1.2 42.5 3.5c9.2 1.5 16.2 8.7 18.2 17.8l12.5 57.1c15.8 6.5 30.6 15.1 44 25.4l55.7-17.7c8.8-2.8 18.6-.3 24.5 6.8c8.1 9.8 15.5 20.2 22.1 31.2l4.7 8.1c6.1 11 11.4 22.4 15.8 34.3zM256 336a80 80 0 1 0 0-160 80 80 0 1 0 0 160z" />
    </svg>
  );
}

function BarsIcon() {
  return (
    <svg viewBox="0 0 448 512" className="h-5 w-5" fill="currentColor" aria-hidden="true">
      <path d="M0 96C0 78.3 14.3 64 32 64H416c17.7 0 32 14.3 32 32s-14.3 32-32 32H32C14.3 128 0 113.7 0 96zM0 256c0-17.7 14.3-32 32-32H416c17.7 0 32 14.3 32 32s-14.3 32-32 32H32c-17.7 0-32-14.3-32-32zM448 416c0 17.7-14.3 32-32 32H32c-17.7 0-32-14.3-32-32s14.3-32 32-32H416c17.7 0 32 14.3 32 32z" />
    </svg>
  );
}

interface NavItem {
  value: string;
  href: string;
  label: string;
  // Shown in the main bar's narrow columns when this item surfaces there -
  // "Entraînement de l'IA" wraps/overflows at that width, "IA" doesn't.
  // Falls back to `label` when omitted; the "Plus" dropdown always uses
  // the full `label` (plenty of width there, one item per row).
  shortLabel?: string;
  icon: () => ReactElement;
}

// 09.10, Sid ("on peut aussi faire apparaitre photos dans la barre de
// navigation et meme faire apparaitre les autres en fonction de la largeur
// de l'appareil utilisé?"): ONE priority-ordered list now, not a fixed
// main/more split - how many show directly (the rest folding into "Plus")
// is computed from the real viewport width below, so a wider screen
// surfaces more of them automatically instead of hiding behind a tap no
// matter how much room there is. wlan-configuration deliberately omitted -
// already hidden server-side in remote mode (is_remote_mode()), and this
// deployment is always remote.
const ALL_ITEMS: NavItem[] = [
  { value: "presence", href: "/presence/", label: "Chats", icon: PawIcon },
  { value: "live-view", href: "/live-view/", label: "Live", icon: TvIcon },
  { value: "journey", href: "/journey/", label: "Parcours", icon: RouteIcon },
  { value: "pictures", href: "/pictures/", label: "Photos", icon: ImageIcon },
  { value: "ai-training", href: "/ai-training/", label: "Entraînement de l'IA", shortLabel: "IA", icon: ChipIcon },
  { value: "appearance", href: "/appearance/", label: "Apparence", icon: SunIcon },
  { value: "configuration", href: "/configuration/", label: "Configuration", icon: GearIcon },
];

// Roughly the width one item takes in the bar (icon + label, py-2 gaps
// included) - used only to decide how many fit, never for layout itself
// (that's still plain flex). Kept conservative on purpose: better to show
// one fewer item directly than to wrap/clip on a real device.
// 09.10, Sid ("une barre qui s'adapte automatiquement à la taille? avec 5
// icones en mobile, et plus sur navigateur et tablette"): fixed tiers tied
// to Tailwind's own standard breakpoints (same 640/768px used elsewhere in
// this app), not a hand-estimated pixel-width calculation - that approach
// (tried first) silently shrank the bar below its old full-width mobile
// behavior, exactly the kind of fragile guess this avoids.
const MOBILE_VISIBLE_COUNT = 5; // < sm (640px)
const SM_VISIBLE_COUNT = 6; // >= sm (640px)
// >= md (768px): every item, no "Plus" button at all.

function useVisibleItemCount(): number {
  const [count, setCount] = useState(MOBILE_VISIBLE_COUNT);

  useEffect(() => {
    const mdQuery = window.matchMedia("(min-width: 768px)");
    const smQuery = window.matchMedia("(min-width: 640px)");
    const compute = () => {
      if (mdQuery.matches) {
        setCount(ALL_ITEMS.length);
      } else if (smQuery.matches) {
        setCount(SM_VISIBLE_COUNT);
      } else {
        setCount(MOBILE_VISIBLE_COUNT);
      }
    };
    compute();
    mdQuery.addEventListener("change", compute);
    smQuery.addEventListener("change", compute);
    return () => {
      mdQuery.removeEventListener("change", compute);
      smQuery.removeEventListener("change", compute);
    };
  }, []);

  return count;
}

export default function BottomNav({ active }: { active: string }) {
  const [moreOpen, setMoreOpen] = useState(false);
  const visibleCount = useVisibleItemCount();
  const mainItems = ALL_ITEMS.slice(0, visibleCount);
  const moreItems = ALL_ITEMS.slice(visibleCount);
  const isMoreActive = moreItems.some((item) => item.value === active);

  return (
    <>
      {moreOpen && (
        <div
          className="fixed inset-0 z-40"
          onClick={() => setMoreOpen(false)}
        >
          <div
            className="absolute bottom-16 left-1/2 w-full max-w-md -translate-x-1/2 rounded-lg border border-white/15 bg-neutral-900 p-1 shadow-lg"
            style={{ paddingInline: "0.5rem" }}
            onClick={(e) => e.stopPropagation()}
          >
            {moreItems.map((item) => (
              <a
                key={item.value}
                href={item.href}
                className={`block rounded-md px-3 py-2.5 text-sm ${
                  active === item.value ? "bg-white/10" : "hover:bg-white/5"
                }`}
              >
                {item.label}
              </a>
            ))}
          </div>
        </div>
      )}

      {/* 07.10, Sid ("en mode web, j'ai perdu la barre de navigation"):
          these migrated pages have no top navbar at all to fall back on
          (unlike the still-Shiny tabs), so this can't be mobile-only
          (md:hidden) like the old Shiny bottom bar was - it's the ONLY nav
          on every screen size now.
          09.10, Sid: width now grows with however many items actually show
          directly (useVisibleItemCount above), instead of a fixed max-w-md
          that could never fit more than a handful regardless of real
          screen space - still centered so it doesn't stretch edge-to-edge
          on a very wide window. */}
      <nav
        // 09.10, Sid ("elle peut prendre toute la largeur" / "5 icones en
        // mobile, et plus sur navigateur et tablette"): max-width grows
        // through the same breakpoints useVisibleItemCount uses to decide
        // the item count (sm/md), so the bar's width and its item count
        // change together - never narrower than the old fixed max-w-md on
        // an ordinary phone (w-full already filled that before), only
        // wider once sm:/md: actually show more items.
        className="fixed bottom-0 left-1/2 z-30 flex w-full max-w-md sm:max-w-lg md:max-w-2xl -translate-x-1/2 border-t border-white/10 bg-neutral-950/95 backdrop-blur md:rounded-t-xl md:border-x"
        style={{
          paddingBottom: "env(safe-area-inset-bottom, 0px)",
        }}
      >
        {mainItems.map((item) => {
          const Icon = item.icon;
          return (
            <a
              key={item.value}
              href={item.href}
              className={`flex flex-1 flex-col items-center gap-0.5 py-2 text-[0.65rem] ${
                active === item.value ? "text-white" : "text-white/50"
              }`}
            >
              <Icon />
              <span>{item.shortLabel ?? item.label}</span>
            </a>
          );
        })}
        {moreItems.length > 0 && (
          <button
            type="button"
            onClick={() => setMoreOpen((v) => !v)}
            className={`flex flex-1 flex-col items-center gap-0.5 py-2 text-[0.65rem] ${
              isMoreActive ? "text-white" : "text-white/50"
            }`}
          >
            <BarsIcon />
            <span>Plus</span>
          </button>
        )}
      </nav>

      {/* Spacer so page content doesn't end up hidden behind the fixed bar.
          09.10, Sid ("du contenu est vraiment caché/inaccessible... même en
          scrollant à fond"): the bar itself adds env(safe-area-inset-bottom)
          as its OWN padding (Android gesture-nav devices), but this spacer
          was a flat h-16 that never accounted for that extra height - the
          last item(s) in a long scroll ended up genuinely stuck under the
          bar by exactly that gesture-bar amount, not just visually blurred
          through it. */}
      <div style={{ height: "calc(4rem + env(safe-area-inset-bottom, 0px))" }} />
    </>
  );
}
