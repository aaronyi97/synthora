import { Link } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { ArrowLeft, Shield } from "lucide-react";
import { SUPPORT_CONTACT } from "@/lib/constants";

export default function PrivacyPage() {
  const { t } = useTranslation();

  return (
    <div className="h-full overflow-y-auto max-w-2xl mx-auto px-4 py-8">
      <div className="mb-6 flex items-center gap-3">
        <Link
          to="/"
          className="flex items-center gap-1.5 text-sm text-zinc-500 hover:text-zinc-300 transition-colors"
        >
          <ArrowLeft size={14} />
          {t("pages.privacy.back")}
        </Link>
      </div>

      <div className="flex items-center gap-3 mb-8">
        <div className="w-8 h-8 rounded-lg bg-oracle-500/10 flex items-center justify-center">
          <Shield size={16} className="text-oracle-400" />
        </div>
        <h1 className="text-xl font-semibold text-zinc-100">{t("pages.privacy.title")}</h1>
      </div>

      <div className="prose prose-invert prose-sm max-w-none space-y-6 text-zinc-400 leading-relaxed">
        <section>
          <p className="text-xs text-zinc-600">{t("pages.privacy.lastUpdated")}</p>
        </section>

        <section>
          <h2 className="text-sm font-semibold text-zinc-300 mb-2">{t("pages.privacy.sections.about.title")}</h2>
          <p>{t("pages.privacy.sections.about.body")}</p>
        </section>

        <section>
          <h2 className="text-sm font-semibold text-zinc-300 mb-2">{t("pages.privacy.sections.dataCollected.title")}</h2>
          <ul className="list-disc list-inside space-y-1">
            <li>{t("pages.privacy.sections.dataCollected.items.accountInfo")}</li>
            <li>{t("pages.privacy.sections.dataCollected.items.queryContent")}</li>
            <li>{t("pages.privacy.sections.dataCollected.items.usageBehavior")}</li>
            <li>{t("pages.privacy.sections.dataCollected.items.technicalData")}</li>
          </ul>
        </section>

        <section>
          <h2 className="text-sm font-semibold text-zinc-300 mb-2">{t("pages.privacy.sections.usagePurpose.title")}</h2>
          <ul className="list-disc list-inside space-y-1">
            <li>{t("pages.privacy.sections.usagePurpose.items.answerService")}</li>
            <li>{t("pages.privacy.sections.usagePurpose.items.personalization")}</li>
            <li>{t("pages.privacy.sections.usagePurpose.items.security")}</li>
            <li>{t("pages.privacy.sections.usagePurpose.items.serviceImprovement")}</li>
          </ul>
        </section>

        <section>
          <h2 className="text-sm font-semibold text-zinc-300 mb-2">{t("pages.privacy.sections.thirdPartyModels.title")}</h2>
          <p>{t("pages.privacy.sections.thirdPartyModels.body")}</p>
        </section>

        <section>
          <h2 className="text-sm font-semibold text-zinc-300 mb-2">{t("pages.privacy.sections.storageSecurity.title")}</h2>
          <ul className="list-disc list-inside space-y-1">
            <li>{t("pages.privacy.sections.storageSecurity.items.serverLocation")}</li>
            <li>{t("pages.privacy.sections.storageSecurity.items.passwordHashing")}</li>
            <li>{t("pages.privacy.sections.storageSecurity.items.cookieStorage")}</li>
          </ul>
        </section>

        <section>
          <h2 className="text-sm font-semibold text-zinc-300 mb-2">{t("pages.privacy.sections.yourRights.title")}</h2>
          <ul className="list-disc list-inside space-y-1">
            <li><strong className="text-zinc-300">{t("pages.privacy.sections.yourRights.items.dataAccess.label")}</strong>{t("pages.privacy.sections.yourRights.items.dataAccess.detail")}</li>
            <li><strong className="text-zinc-300">{t("pages.privacy.sections.yourRights.items.disableTracking.label")}</strong>{t("pages.privacy.sections.yourRights.items.disableTracking.detail")}</li>
            <li><strong className="text-zinc-300">{t("pages.privacy.sections.yourRights.items.deleteCognitiveData.label")}</strong>{t("pages.privacy.sections.yourRights.items.deleteCognitiveData.detail")}</li>
            <li><strong className="text-zinc-300">{t("pages.privacy.sections.yourRights.items.deleteAccount.label")}</strong>{t("pages.privacy.sections.yourRights.items.deleteAccount.detail")}</li>
          </ul>
        </section>

        <section>
          <h2 className="text-sm font-semibold text-zinc-300 mb-2">{t("pages.privacy.sections.cookies.title")}</h2>
          <p>{t("pages.privacy.sections.cookies.body")}</p>
        </section>

        <section>
          <h2 className="text-sm font-semibold text-zinc-300 mb-2">{t("pages.privacy.sections.contact.title")}</h2>
          <p>{t("pages.privacy.sections.contact.body", { wechat: SUPPORT_CONTACT })}</p>
        </section>
      </div>
    </div>
  );
}
