import { useState } from "react";
import { Shield, X } from "lucide-react";
import { useTranslation } from "react-i18next";

const CONSENT_KEY = "synthora_data_consent_v1";

export function useConsentBanner() {
  const [dismissed, setDismissed] = useState(
    () => localStorage.getItem(CONSENT_KEY) === "accepted"
  );

  const accept = () => {
    localStorage.setItem(CONSENT_KEY, "accepted");
    setDismissed(true);
  };

  return { show: !dismissed, accept };
}

interface Props {
  onAccept: () => void;
}

export default function ConsentBanner({ onAccept }: Props) {
  const { t } = useTranslation();
  return (
    <div className="fixed bottom-0 left-0 right-0 z-50 p-3 sm:p-4">
      <div className="max-w-2xl mx-auto bg-surface-1 border border-surface-4/40 rounded-xl shadow-2xl p-4 flex gap-3 items-start">
        <div className="shrink-0 w-7 h-7 rounded-lg bg-oracle-500/10 flex items-center justify-center mt-0.5">
          <Shield size={14} className="text-oracle-400" />
        </div>
        <div className="flex-1 min-w-0">
          <p className="text-xs text-zinc-300 leading-relaxed">
            {t("components.consentBanner.body")}
          </p>
          <p className="text-xs text-zinc-600 mt-1">
            {t("components.consentBanner.noticePrefix")}{" "}
            <a href="/privacy" className="text-oracle-400/80 hover:text-oracle-400 underline underline-offset-2">
              {t("components.consentBanner.privacyPolicy")}
            </a>
            {t("components.consentBanner.noticeSuffix")}
          </p>
        </div>
        <button
          onClick={onAccept}
          className="shrink-0 flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-oracle-500/15 text-oracle-300 border border-oracle-500/30 text-xs hover:bg-oracle-500/25 transition-colors whitespace-nowrap"
        >
          <X size={11} />
          {t("components.consentBanner.acknowledge")}
        </button>
      </div>
    </div>
  );
}
