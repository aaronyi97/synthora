export type AppLanguage = "zh-CN" | "en-US";

export const LANGUAGE_STORAGE_KEY = "synthora_language";
export const LEGACY_LANGUAGE_STORAGE_KEY = "synthora-language";
export const SUPPORTED_LANGUAGES: AppLanguage[] = ["zh-CN", "en-US"];

function hasLocalStorage(): boolean {
  return typeof localStorage !== "undefined" && typeof localStorage.getItem === "function";
}

export function normalizeAppLanguage(raw?: string | null): AppLanguage {
  const value = (raw || "").trim().replace("_", "-").toLowerCase();
  if (value.startsWith("en")) return "en-US";
  if (value.startsWith("zh")) return "zh-CN";
  return "zh-CN";
}

export function persistClientLanguage(language: string): AppLanguage {
  const normalized = normalizeAppLanguage(language);
  if (!hasLocalStorage()) return normalized;
  localStorage.setItem(LANGUAGE_STORAGE_KEY, normalized);
  if (typeof localStorage.removeItem === "function") {
    localStorage.removeItem(LEGACY_LANGUAGE_STORAGE_KEY);
  }
  return normalized;
}

export function readStoredLanguage(): AppLanguage | null {
  if (!hasLocalStorage()) return null;

  const stored = localStorage.getItem(LANGUAGE_STORAGE_KEY);
  if (stored) return normalizeAppLanguage(stored);

  const legacyStored = localStorage.getItem(LEGACY_LANGUAGE_STORAGE_KEY);
  if (!legacyStored) return null;

  return persistClientLanguage(legacyStored);
}

export function hydrateLegacyLanguageStorage(): AppLanguage | null {
  return readStoredLanguage();
}
