import { useEffect } from "react";

// 08.10, Sid ("je suis dans photos mais la page affiche chronologie ?"):
// every migrated page shares the same index.html (one bundle, see
// vite.config.ts's base comment), whose <title> was hardcoded to
// "Chronologie - Kittyhack" from when that was the only page - every other
// route silently kept that same browser-tab title. Each page now sets its
// own.
export function usePageTitle(title: string) {
  useEffect(() => {
    document.title = `${title} - Kittyhack`;
  }, [title]);
}
