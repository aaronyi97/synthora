// 源真相: 后端 GET /modes 端点 (从 config.yaml modes 节生成)
// 前端不硬编码后端行为描述，只渲染后端元数据。
// 防线 #1: 运行时对齐
import { useState, useEffect, useMemo, useRef } from "react";
import { createPortal } from "react-dom";
import { useTranslation } from "react-i18next";
import {
  Check,
  ChevronDown,
  Globe,
  Zap,
  Brain,
  FlaskConical,
  GraduationCap,
  Sparkles,
} from "lucide-react";
import type { Mode, ModeInfo } from "@/types";
import { api } from "@/api/client";

interface Props {
  selected: Mode;
  onChange: (mode: Mode) => void;
  disabled?: boolean;
  webSearch?: boolean;
  onWebSearchChange?: (v: boolean) => void;
}

const ICON_MAP: Record<string, typeof Zap> = {
  zap: Zap,
  brain: Brain,
  flask: FlaskConical,
  "graduation-cap": GraduationCap,
  sparkles: Sparkles,
};

const LEGACY_SMART_MODE_LABEL = "\u667A\u80FD";
const HIDDEN_MODES = new Set(["light", LEGACY_SMART_MODE_LABEL]);

function buildFallbackModes(t: (key: string) => string): ModeInfo[] {
  return [
    {
      id: "auto",
      label: "Auto",
      desc: t("components.modeSelector.fallback.auto.desc"),
      detail: t("components.modeSelector.fallback.auto.detail"),
      icon: "sparkles",
      order: 0,
      contributor_count: 0,
      n_of_m: 0,
      has_judge: false,
      has_critique: false,
      has_preflight: false,
      max_timeout_seconds: 60,
    },
    {
      id: "deep",
      label: "Deep",
      desc: t("components.modeSelector.fallback.deep.desc"),
      detail: t("components.modeSelector.fallback.deep.detail"),
      icon: "brain",
      order: 2,
      contributor_count: 6,
      n_of_m: 5,
      has_judge: true,
      has_critique: true,
      has_preflight: true,
      max_timeout_seconds: 180,
    },
    {
      id: "research",
      label: "Research",
      desc: t("components.modeSelector.fallback.research.desc"),
      detail: t("components.modeSelector.fallback.research.detail"),
      icon: "flask",
      order: 3,
      contributor_count: 6,
      n_of_m: 5,
      has_judge: true,
      has_critique: true,
      has_preflight: true,
      max_timeout_seconds: 900,
    },
    {
      id: "socratic",
      label: t("common.modes.socraticName"),
      desc: t("components.modeSelector.fallback.socratic.desc"),
      detail: t("components.modeSelector.fallback.socratic.detail"),
      icon: "graduation-cap",
      order: 4,
      contributor_count: 3,
      n_of_m: 2,
      has_judge: true,
      has_critique: false,
      has_preflight: false,
      max_timeout_seconds: 120,
    },
  ];
}

function getDisplayLabel(mode: Pick<ModeInfo, "id" | "label">, t: (key: string) => string): string {
  if (mode.id === "socratic") return t("common.modes.socraticName");
  if (mode.id === "roundtable") return t("common.modes.roundtableName");
  return mode.label;
}

// v5.1: Auto is now the default mode (Dispatcher-driven). Light removed from user selection.

export default function ModeSelector({ selected, onChange, disabled, webSearch = true, onWebSearchChange }: Props) {
  const { t } = useTranslation();
  const fallbackModes = buildFallbackModes(t);
  const [modeList, setModeList] = useState<ModeInfo[] | null>(null);
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);
  const desktopDropdownRef = useRef<HTMLDivElement>(null);
  const [desktopDropdownPosition, setDesktopDropdownPosition] = useState<{
    left: number;
    bottom: number;
  } | null>(null);

  useEffect(() => {
    api.modes()
      .then((res) => setModeList(res.modes.filter((m) => !HIDDEN_MODES.has(m.id) && !HIDDEN_MODES.has(m.label))))
      .catch(() => { setModeList(null); });
  }, []);

  useEffect(() => {
    if (!open) return;
    const onPointerDown = (evt: MouseEvent) => {
      const target = evt.target as Node | null;
      const clickedTrigger = !!(rootRef.current && target && rootRef.current.contains(target));
      const clickedDesktopDropdown = !!(
        desktopDropdownRef.current
        && target
        && desktopDropdownRef.current.contains(target)
      );
      if (!clickedTrigger && !clickedDesktopDropdown) {
        setOpen(false);
      }
    };
    window.addEventListener("mousedown", onPointerDown);
    return () => window.removeEventListener("mousedown", onPointerDown);
  }, [open]);

  useEffect(() => {
    if (!open) return;

    const updateDesktopDropdownPosition = () => {
      if (!rootRef.current) return;
      const rect = rootRef.current.getBoundingClientRect();
      setDesktopDropdownPosition({
        left: Math.max(12, rect.right - 192),
        bottom: Math.max(12, window.innerHeight - rect.top + 8),
      });
    };

    updateDesktopDropdownPosition();
    window.addEventListener("resize", updateDesktopDropdownPosition);
    window.addEventListener("scroll", updateDesktopDropdownPosition, true);
    return () => {
      window.removeEventListener("resize", updateDesktopDropdownPosition);
      window.removeEventListener("scroll", updateDesktopDropdownPosition, true);
    };
  }, [open]);

  const visibleModes = modeList ?? fallbackModes;
  const selectedMode = useMemo(
    () => visibleModes.find((m) => m.id === selected) ?? fallbackModes.find((m) => m.id === selected) ?? fallbackModes[0],
    [fallbackModes, selected, visibleModes],
  );

  const chooseMode = (id: string) => {
    onChange(id as Mode);
    setOpen(false);
  };

  const modeItems = (
    <div className="space-y-0.5">
      {visibleModes.map((m) => {
        const Icon = ICON_MAP[m.icon] || Sparkles;
        return (
          <button
            key={m.id}
            type="button"
            onClick={() => chooseMode(m.id)}
            disabled={disabled}
            className={`w-full flex items-center gap-2.5 rounded-xl px-3 py-2.5 text-left transition-all duration-150 disabled:opacity-40 ${
              selected === m.id
                ? "bg-oracle-500/[0.14] text-zinc-50"
                : "text-zinc-300 hover:bg-surface-3 hover:text-zinc-100"
            }`}
          >
            <Icon size={15} className={selected === m.id ? "text-oracle-300" : "text-zinc-500"} />
            <span className="text-[14px] font-medium flex-1">{getDisplayLabel(m, t)}</span>
            {selected === m.id && <Check size={14} className="text-oracle-300" />}
          </button>
        );
      })}
    </div>
  );

  return (
    <div ref={rootRef} className="relative">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        disabled={disabled}
        className="flex h-11 min-w-[132px] items-center justify-between gap-2 rounded-2xl border border-white/[0.14] bg-white/[0.04] px-3.5 text-[14px] font-medium text-zinc-100 transition-all duration-150 hover:border-white/[0.24] hover:bg-white/[0.08] active:scale-[0.98] disabled:opacity-40"
      >
        <span className="inline-flex items-center gap-1.5">
          {(() => {
            const Icon = ICON_MAP[selectedMode.icon] || Sparkles;
            return <Icon className="h-4 w-4 text-oracle-300" />;
          })()}
          <span>{getDisplayLabel(selectedMode, t)}</span>
        </span>
        <ChevronDown className={`h-4 w-4 text-zinc-400 transition-transform ${open ? "rotate-180" : ""}`} />
      </button>

      {open && desktopDropdownPosition && typeof document !== "undefined" && createPortal(
        <div
          ref={desktopDropdownRef}
          className="fixed z-[9999] hidden w-48 animate-fade-in rounded-xl border border-white/[0.12] bg-surface-2 p-1.5 shadow-2xl shadow-black/45 md:block"
          style={{
            left: `${desktopDropdownPosition.left}px`,
            bottom: `${desktopDropdownPosition.bottom}px`,
          }}
        >
          {modeItems}
          {onWebSearchChange && (
            <div className="mt-1 flex items-center gap-1.5 border-t border-zinc-800/70 px-2.5 pt-1.5 pb-0.5 text-[12px] text-emerald-300/90">
              <Globe size={12} />
              <span>{t(webSearch ? "components.modeSelector.webSearch.enabled" : "components.modeSelector.webSearch.disabled")}</span>
            </div>
          )}
        </div>,
        document.body,
      )}

      {open && (
        <div className="md:hidden fixed inset-0 z-[70]">
          <button
            type="button"
            onClick={() => setOpen(false)}
            className="absolute inset-0 bg-black/60"
            aria-label={t("components.modeSelector.close")}
          />
          <div className="absolute bottom-0 left-0 right-0 max-h-[70vh] overflow-y-auto rounded-t-2xl border-t border-white/[0.12] bg-surface-1 p-3 pb-[max(0.85rem,env(safe-area-inset-bottom))] animate-slide-up">
            <div className="mx-auto mb-2 h-1 w-10 rounded-full bg-zinc-700/70" />
            {modeItems}
            {onWebSearchChange && (
              <div className="mt-1.5 flex items-center gap-1.5 border-t border-zinc-800/70 px-2.5 pt-1.5 text-[12px] text-emerald-300/90">
                <Globe size={12} />
                <span>{t(webSearch ? "components.modeSelector.webSearch.enabled" : "components.modeSelector.webSearch.disabled")}</span>
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
