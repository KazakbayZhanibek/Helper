import type { CaptureResult } from "./camera";

export const RECOVERY_TTL_MS = 24 * 60 * 60 * 1000;
const DATABASE = "master-camera-recovery-v1";
const STORE = "recordings";
export type RecoveryRecording = CaptureResult & {
  key: string;
  token: string;
  savedAt: number;
};
export class RecoveryConflictError extends Error {
  constructor() {
    super(
      "В другой вкладке уже сохранена запись этой инструкции. Она не заменена. Скачайте текущую запись перед закрытием страницы.",
    );
    this.name = "RecoveryConflictError";
  }
}
export function recoveryKey(userId: string, orgId: string, base: string) {
  return JSON.stringify([userId, orgId, base]);
}
export function validRecovery(
  value: unknown,
  key: string,
  now = Date.now(),
): value is RecoveryRecording {
  if (!value || typeof value !== "object") return false;
  const r = value as RecoveryRecording;
  return (
    r.key === key &&
    typeof r.token === "string" &&
    !!r.token &&
    Number.isFinite(r.savedAt) &&
    r.savedAt <= now &&
    now - r.savedAt < RECOVERY_TTL_MS &&
    r.file instanceof Blob &&
    typeof r.file.name === "string" &&
    r.file.type.startsWith("video/") &&
    r.file.size > 0 &&
    r.file.size <= 500 * 1024 * 1024 &&
    Number.isFinite(r.durationMs) &&
    r.durationMs >= 0 &&
    r.durationMs <= 600000 &&
    typeof r.truncated === "boolean"
  );
}
function openDatabase(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    if (typeof indexedDB === "undefined") {
      reject(new Error("Локальное хранилище недоступно."));
      return;
    }
    const request = indexedDB.open(DATABASE, 1);
    let failed = false;
    request.onupgradeneeded = () =>
      request.result.createObjectStore(STORE, { keyPath: "key" });
    request.onerror = () => reject(request.error);
    request.onblocked = () => {
      failed = true;
      reject(new Error("Закройте другие вкладки приложения и повторите."));
    };
    request.onsuccess = () => {
      const db = request.result;
      if (failed) {
        db.close();
        return;
      }
      db.onversionchange = () => db.close();
      resolve(db);
    };
  });
}
async function transaction<T>(
  mode: IDBTransactionMode,
  run: (
    store: IDBObjectStore,
    done: (result: T) => void,
    fail: (error: Error) => void,
  ) => void,
): Promise<T> {
  const db = await openDatabase();
  return new Promise<T>((resolve, reject) => {
    let result: T;
    let tx: IDBTransaction | undefined;
    let failure: Error | undefined;
    try {
      tx = db.transaction(STORE, mode);
      tx.oncomplete = () => {
        db.close();
        resolve(result);
      };
      tx.onabort = () => {
        db.close();
        reject(
          failure || tx?.error || new Error("Не удалось сохранить запись."),
        );
      };
      run(
        tx.objectStore(STORE),
        (value) => {
          result = value;
        },
        (error) => {
          failure = error;
          tx?.abort();
        },
      );
    } catch (error) {
      // Closing a connection does not abort queued writes.
      try {
        tx?.abort();
      } catch {
        /* A finished transaction has nothing to roll back. */
      }
      db.close();
      reject(error);
    }
  });
}
export async function loadRecovery(
  key: string,
): Promise<RecoveryRecording | null> {
  return transaction("readwrite", (store, done) => {
    const request = store.get(key);
    request.onsuccess = () => {
      if (validRecovery(request.result, key)) done(request.result);
      else {
        if (request.result) store.delete(key);
        done(null);
      }
    };
    // Expired local copies are removed when the camera panel is next opened.
    const cursor = store.openCursor();
    cursor.onsuccess = () => {
      const item = cursor.result;
      if (!item) return;
      if (
        !Number.isFinite(item.value.savedAt) ||
        Date.now() - item.value.savedAt >= RECOVERY_TTL_MS
      )
        item.delete();
      item.continue();
    };
  });
}
export async function saveRecovery(
  key: string,
  capture: CaptureResult,
): Promise<RecoveryRecording> {
  const record = {
    ...capture,
    key,
    token: crypto.randomUUID(),
    savedAt: Date.now(),
  };
  if (!validRecovery(record, key))
    throw new Error("Запись не подходит для локального восстановления.");
  await transaction<void>("readwrite", (store, done, fail) => {
    const existing = store.get(key);
    existing.onsuccess = () => {
      // Two tabs can both start with no saved copy. Never silently overwrite
      // the first recording when the other tab finishes later.
      if (validRecovery(existing.result, key)) {
        fail(new RecoveryConflictError());
        return;
      }
      store.put(record);
      done(undefined);
    };
  });
  return record;
}
export async function removeRecovery(
  key: string,
  token: string,
): Promise<void> {
  await transaction<void>("readwrite", (store, done) => {
    const request = store.get(key);
    request.onsuccess = () => {
      // A second tab may have saved a newer recording for this instruction.
      if (request.result?.token === token) store.delete(key);
      done(undefined);
    };
  });
}
