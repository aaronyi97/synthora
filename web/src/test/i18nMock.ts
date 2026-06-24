import zhCN from "@/i18n/locales/zh-CN.json";

export function translateZh(key: string, options?: Record<string, unknown>): string {
  const value = key
    .split(".")
    .reduce<unknown>((current, segment) => (
      current && typeof current === "object"
        ? (current as Record<string, unknown>)[segment]
        : undefined
    ), zhCN);

  if (typeof value !== "string") return key;

  return value.replace(/\{\{(\w+)\}\}/g, (_match, name: string) => {
    const replacement = options?.[name];
    return replacement === undefined || replacement === null ? "" : String(replacement);
  });
}

export function createZhI18nMock() {
  return {
    changeLanguage: async () => {},
    getFixedT: () => translateZh,
    language: "zh-CN",
    resolvedLanguage: "zh-CN",
    t: translateZh,
  };
}
