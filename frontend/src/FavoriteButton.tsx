import { useState } from "react";
import { api, instructionApi } from "./api";
import { Icon } from "./components";

type Props = {
  orgId: string;
  id: string;
  title: string;
  saved: boolean;
  compact?: boolean;
  onChange: (saved: boolean) => void;
  onError: (message: string) => void;
};

export function FavoriteButton({
  orgId,
  id,
  title,
  saved,
  compact,
  onChange,
  onError,
}: Props) {
  const [busy, setBusy] = useState(false);
  const action = saved ? "Убрать из избранного" : "Добавить в избранное";
  async function toggle() {
    if (busy) return;
    setBusy(true);
    try {
      const result = await api<{ favorite: boolean }>(
        instructionApi(orgId, id) + "/favorite",
        {
          method: saved ? "DELETE" : "PUT",
        },
      );
      onChange(result.favorite);
    } catch (error) {
      onError((error as Error).message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <button
      type="button"
      className={compact ? "card-favorite" : "recipe-favorite"}
      aria-pressed={saved}
      aria-label={action + " · " + title}
      title={action}
      disabled={busy}
      onClick={toggle}
    >
      <Icon name="bookmark" size={19} />
      {!compact && (saved ? "В избранном" : "В избранное")}
    </button>
  );
}
