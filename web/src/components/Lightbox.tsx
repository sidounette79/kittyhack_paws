// 07.10, Sid: extracted out of Chronology.tsx so the Presence events list
// (same "tap a row, see the photo" interaction) doesn't duplicate it - the
// swipe/touch-action fix only has to live in one place now.
export default function Lightbox({
  url,
  onClose,
}: {
  url: string | null;
  onClose: () => void;
}) {
  if (!url) return null;
  return (
    <div
      className="kh-photo-lightbox-backdrop fixed inset-0 z-50 flex items-center justify-center bg-black/85 p-4"
      style={{ touchAction: "pan-y" }}
      onClick={onClose}
    >
      <img src={url} alt="" className="max-h-full max-w-full rounded-lg" />
    </div>
  );
}
