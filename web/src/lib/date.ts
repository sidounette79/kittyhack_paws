// 07.10, Sid ("ça descend de 2 jours en 2 jours... chevron pour avancer ne
// déclenche rien"): shared after the exact same bug (and fix) was needed on
// both Chronology and Presence - toISOString() converts to UTC, so in a
// timezone ahead of UTC (CEST) local midnight is still the previous day in
// UTC, silently eating/cancelling a day on every round-trip. Everything
// here stays in local-getter land, no UTC conversion anywhere.

export function todayLocalIso(): string {
  const now = new Date();
  const tzOffsetMs = now.getTimezoneOffset() * 60000;
  return new Date(now.getTime() - tzOffsetMs).toISOString().slice(0, 10);
}

export function shiftDate(iso: string, days: number): string {
  const d = new Date(iso + "T00:00:00");
  d.setDate(d.getDate() + days);
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${day}`;
}

export function weekdayAbbr(iso: string): string {
  const abbr = ["di", "lu", "ma", "me", "je", "ve", "sa"];
  return abbr[new Date(iso + "T00:00:00").getDay()];
}
