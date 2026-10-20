export interface TimelineEntry {
  time: string;
  message: string;
}

// 07.10, Sid ("oublie pas de reproduire les différents détails des
// mouvements quand on clique sur les petites icônes"): the old events table
// (live_view.py's get_events_table_html) let you click a row to expand the
// full backend decision log for that motion block - "Mouvement détecté",
// "Patoune détecté(e) par RFID", "Entrée autorisée pour Patoune (RFID)",
// etc. Chronology/Journey never got this. Shared between both since the row
// shape (and the /app-api/v1/events/{block_id}/timeline endpoint) is
// identical in each.
export default function EventTimelinePanel({ entries }: { entries: TimelineEntry[] | null }) {
  return (
    <div className="mt-2 border-t border-white/10 pt-2">
      {entries === null && <p className="text-xs text-white/65">Chargement…</p>}
      {entries !== null && entries.length === 0 && (
        <p className="text-xs text-white/65">Aucun détail disponible.</p>
      )}
      {entries?.map((e, i) => (
        <div key={i} className="flex gap-2 py-0.5 text-xs text-white/60">
          <span className="shrink-0 font-mono text-white/60">{e.time}</span>
          <span>{e.message}</span>
        </div>
      ))}
    </div>
  );
}
