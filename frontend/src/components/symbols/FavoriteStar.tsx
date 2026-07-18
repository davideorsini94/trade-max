import { useState } from "react";
import { apiPost, errorMessage } from "../../api/client";
import type { SymbolOut } from "../../api/types";

interface FavoriteStarProps {
  symbolId: number;
  isFavorite: boolean;
  onToggled?: (updated: SymbolOut) => void;
  onError?: (message: string) => void;
  size?: number;
  className?: string;
}

export default function FavoriteStar({
  symbolId,
  isFavorite,
  onToggled,
  onError,
  size = 20,
  className = "",
}: FavoriteStarProps) {
  const [busy, setBusy] = useState(false);

  async function handleClick(event: React.MouseEvent) {
    event.preventDefault();
    event.stopPropagation();
    if (busy) return;
    setBusy(true);
    try {
      const updated = await apiPost<SymbolOut>(`/symbols/${symbolId}/favorite`, {
        is_favorite: !isFavorite,
      });
      onToggled?.(updated);
    } catch (err) {
      onError?.(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  const label = isFavorite ? "Rimuovi dai preferiti" : "Aggiungi ai preferiti";

  return (
    <button
      type="button"
      onClick={handleClick}
      disabled={busy}
      title={label}
      aria-label={label}
      aria-pressed={isFavorite}
      className={`inline-flex items-center justify-center rounded-md p-1 transition hover:bg-slate-800 disabled:opacity-50 ${className}`}
    >
      <svg
        width={size}
        height={size}
        viewBox="0 0 24 24"
        fill={isFavorite ? "#f5b942" : "none"}
        stroke={isFavorite ? "#f5b942" : "#8b95ab"}
        strokeWidth="1.6"
        className={busy ? "tm-pulse" : ""}
      >
        <path
          d="M12 3.5l2.7 5.6 6.1.9-4.4 4.3 1 6.1L12 17.3l-5.4 2.9 1-6.1-4.4-4.3 6.1-.9L12 3.5z"
          strokeLinejoin="round"
          strokeLinecap="round"
        />
      </svg>
    </button>
  );
}
