export type CookingProgress = {
  active: number;
  finished: boolean;
  prepared: number[];
};

export const emptyCookingProgress: CookingProgress = {
  active: 0,
  finished: false,
  prepared: [],
};

export function hasCookingProgress(state: CookingProgress) {
  return state.active > 0 || state.finished || state.prepared.length > 0;
}

export function cookingProgressKey(
  userId: string,
  orgId: string,
  recipeId: string,
  versionId: string,
) {
  return (
    "recipe:cooking:v1:" + JSON.stringify([userId, orgId, recipeId, versionId])
  );
}

type StoragePort = Pick<Storage, "getItem" | "setItem" | "removeItem">;

function decode(
  raw: string | null,
  steps: number,
  ingredients: number,
): CookingProgress {
  if (!raw) return emptyCookingProgress;
  let value;
  try {
    value = JSON.parse(raw);
  } catch {
    return emptyCookingProgress;
  }
  if (
    !value ||
    !Number.isInteger(value.active) ||
    value.active < 0 ||
    value.active >= steps ||
    typeof value.finished !== "boolean" ||
    !Array.isArray(value.prepared)
  ) {
    return emptyCookingProgress;
  }
  const prepared = value.prepared.filter(
    (i: unknown): i is number =>
      typeof i === "number" && Number.isInteger(i) && i >= 0 && i < ingredients,
  );
  return {
    active: value.active,
    finished: value.finished && value.active === steps - 1,
    prepared: [...new Set<number>(prepared)],
  };
}

// A failed write (private browsing, full storage) must never undo this session's marks.
export function openCookingProgress(
  key: string | undefined,
  steps: number,
  ingredients: number,
  getStorage: () => StoragePort,
) {
  let memory = emptyCookingProgress;
  let storage: StoragePort | undefined;
  let unsaved = false;
  try {
    if (key) storage = getStorage();
  } catch {
    // Cooking remains available when site storage is blocked.
  }
  function read() {
    if (key && storage && !unsaved) {
      try {
        memory = decode(storage.getItem(key), steps, ingredients);
      } catch {
        storage = undefined;
      }
    }
    return memory;
  }
  function update(change: (previous: CookingProgress) => CookingProgress) {
    memory = change(read());
    if (key && storage) {
      try {
        if (hasCookingProgress(memory))
          storage.setItem(key, JSON.stringify(memory));
        else storage.removeItem(key);
        unsaved = false;
      } catch {
        // Keep the latest edits in memory and retry on the next action, including reset.
        unsaved = true;
      }
    }
    return memory;
  }
  return { read, update, canPersist: () => !!storage && !unsaved };
}
