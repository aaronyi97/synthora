import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { OUTPUT_SURFACE_SOFT, STATUS_BADGE_ACCENT } from "@/lib/outputStyle";

interface Props {
  socratic?: boolean;
}

export default function LoadingOracle({ socratic = false }: Props) {
  const { t } = useTranslation();
  const stages = socratic
    ? [
        t("components.loadingOracle.stages.socratic.dispatching"),
        t("components.loadingOracle.stages.socratic.analyzing"),
        t("components.loadingOracle.stages.socratic.mapping"),
        t("components.loadingOracle.stages.socratic.identifying"),
        t("components.loadingOracle.stages.socratic.generating"),
        t("components.loadingOracle.stages.socratic.preparing"),
      ]
    : [
        t("components.loadingOracle.stages.default.dispatching"),
        t("components.loadingOracle.stages.default.analyzing"),
        t("components.loadingOracle.stages.default.comparing"),
        t("components.loadingOracle.stages.default.synthesizing"),
        t("components.loadingOracle.stages.default.validating"),
        t("components.loadingOracle.stages.default.polishing"),
      ];
  const [stageIdx, setStageIdx] = useState(0);
  const [dots, setDots] = useState("");

  useEffect(() => {
    const interval = setInterval(() => {
      setStageIdx((prev) => (prev + 1) % stages.length);
    }, 8000);
    return () => clearInterval(interval);
  }, [stages.length]);

  useEffect(() => {
    const interval = setInterval(() => {
      setDots((prev) => (prev.length >= 3 ? "" : prev + "."));
    }, 500);
    return () => clearInterval(interval);
  }, []);

  return (
    <div className="animate-fade-in px-4 py-12">
      <div className={`${OUTPUT_SURFACE_SOFT} mx-auto flex max-w-lg flex-col items-center justify-center px-6 py-8 text-center`}>
        <span className={STATUS_BADGE_ACCENT}>{t("components.loadingOracle.badge")}</span>

        {/* Pulsing oracle */}
        <div className="relative mb-7 mt-5">
          <div className="flex h-16 w-16 animate-pulse-slow items-center justify-center rounded-full bg-oracle-500/10">
            <div className="flex h-10 w-10 items-center justify-center rounded-full bg-oracle-500/20">
              <div className="h-5 w-5 rounded-full bg-oracle-500/60" />
            </div>
          </div>
          {/* Orbiting dots */}
          <div className="absolute inset-0 animate-spin" style={{ animationDuration: "6s" }}>
            <div className="absolute left-1/2 top-0 h-1.5 w-1.5 -translate-x-1/2 -translate-y-1 rounded-full bg-oracle-400" />
          </div>
          <div className="absolute inset-0 animate-spin" style={{ animationDuration: "8s", animationDirection: "reverse" }}>
            <div className="absolute bottom-0 left-1/2 h-1 w-1 -translate-x-1/2 translate-y-1 rounded-full bg-oracle-300/60" />
          </div>
        </div>

        <p className="text-sm text-zinc-300 transition-all duration-500">
          {stages[stageIdx]}{dots}
        </p>
        <div className="mt-5 flex gap-1">
          {stages.map((_, i) => (
            <div
              key={i}
              className={`h-0.5 w-6 rounded-full transition-colors duration-500 ${
                i <= stageIdx ? "bg-oracle-500/60" : "bg-surface-4"
              }`}
            />
          ))}
        </div>
      </div>
    </div>
  );
}
