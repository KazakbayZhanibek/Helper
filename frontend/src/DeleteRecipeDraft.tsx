import { useState } from "react";
import { api, ApiError, instructionApi } from "./api";
import { Dialog, ErrorBanner } from "./components";

export function DeleteRecipeDraft({
  orgId,
  instructionId,
  title,
  etag,
  disabled,
  onDeleted,
}: {
  orgId: string;
  instructionId: string;
  title: string;
  etag: string;
  disabled: boolean;
  onDeleted: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");
  const [refreshNeeded, setRefreshNeeded] = useState(false);

  async function remove() {
    if (disabled || pending || refreshNeeded) return;
    setPending(true);
    setError("");
    try {
      await api(instructionApi(orgId, instructionId), {
        method: "DELETE",
        etag,
      });
      onDeleted();
    } catch (cause) {
      setError(
        cause instanceof Error ? cause.message : "Не удалось удалить черновик.",
      );
      setRefreshNeeded(
        cause instanceof ApiError && [404, 412].includes(cause.status),
      );
    } finally {
      setPending(false);
    }
  }

  return (
    <div className="button-row">
      <button
        type="button"
        className="text-button danger-text"
        disabled={disabled}
        onClick={() => {
          setError("");
          setRefreshNeeded(false);
          setOpen(true);
        }}
      >
        Удалить черновик
      </button>
      {open && (
        <Dialog title="Удалить черновик?" onClose={() => setOpen(false)}>
          <p>
            Рецепт «{title}» исчезнет из книги. В тарифе освободится одно место
            для рецепта.
          </p>
          {error && <ErrorBanner error={error} />}
          <div className="button-row">
            <button
              type="button"
              className="button secondary"
              disabled={pending}
              onClick={() => setOpen(false)}
            >
              Оставить черновик
            </button>
            {refreshNeeded ? (
              <button
                type="button"
                className="button"
                onClick={() => window.location.reload()}
              >
                Обновить страницу
              </button>
            ) : (
              <button
                type="button"
                className="button danger"
                disabled={disabled || pending}
                onClick={remove}
              >
                {pending ? "Удаляем…" : "Удалить черновик"}
              </button>
            )}
          </div>
        </Dialog>
      )}
    </div>
  );
}
