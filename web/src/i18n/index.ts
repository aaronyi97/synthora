import i18n from "i18next";
import LanguageDetector from "i18next-browser-languagedetector";
import { initReactI18next } from "react-i18next";
import enUS from "./locales/en-US.json";
import zhCN from "./locales/zh-CN.json";
import {
  hydrateLegacyLanguageStorage,
  LANGUAGE_STORAGE_KEY,
  SUPPORTED_LANGUAGES,
} from "./language";

const resources = {
  "zh-CN": {
    common: zhCN,
  },
  "en-US": {
    common: enUS,
  },
} as const;

hydrateLegacyLanguageStorage();

const hasBundledResources =
  typeof i18n.getResourceBundle === "function"
  && Boolean(i18n.getResourceBundle("zh-CN", "common"))
  && Boolean(i18n.getResourceBundle("en-US", "common"));

if (!i18n.isInitialized || !hasBundledResources) {
  void i18n
    .use(LanguageDetector)
    .use(initReactI18next)
    .init({
      resources,
      fallbackLng: "zh-CN",
      supportedLngs: SUPPORTED_LANGUAGES,
      defaultNS: "common",
      ns: ["common"],
      load: "currentOnly",
      returnNull: false,
      initAsync: false,
      interpolation: {
        escapeValue: false,
      },
      detection: {
        order: ["localStorage", "navigator"],
        lookupLocalStorage: LANGUAGE_STORAGE_KEY,
        caches: ["localStorage"],
      },
    });
}

export default i18n;
