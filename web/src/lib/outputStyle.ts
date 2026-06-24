export const OUTPUT_SURFACE =
  "overflow-hidden rounded-2xl border border-white/[0.1] bg-surface-1/95 shadow-[0_18px_42px_rgba(0,0,0,0.26)] backdrop-blur-sm";

export const OUTPUT_SURFACE_SOFT =
  "overflow-hidden rounded-2xl border border-white/[0.1] bg-zinc-900/65 shadow-[0_14px_30px_rgba(0,0,0,0.22)] backdrop-blur-sm";

export const OUTPUT_SURFACE_ACCENT =
  "overflow-hidden rounded-2xl border border-oracle-500/20 bg-zinc-900/65 shadow-[0_12px_28px_rgba(0,0,0,0.2)] backdrop-blur-sm";

export const OUTPUT_SECTION_HEADER =
  "flex w-full items-center justify-between gap-2 px-4 py-3 text-[14px] font-medium transition-colors duration-150 hover:bg-white/[0.04]";

export const OUTPUT_META_BAR =
  "flex flex-col gap-2.5 border-t border-white/[0.08] bg-white/[0.03] px-4 py-3.5 sm:flex-row sm:items-center sm:justify-between";

export const OUTPUT_META_PILL =
  "rounded-full border border-white/[0.12] bg-white/[0.06] px-2.5 py-1.5 text-[13px] font-medium uppercase tracking-wide text-zinc-300";

export const STATUS_BADGE =
  "inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1.5 text-[13px] font-medium";

export const STATUS_BADGE_WARNING =
  `${STATUS_BADGE} border-amber-500/20 bg-amber-500/[0.08] text-amber-300`;

export const STATUS_BADGE_ACCENT =
  `${STATUS_BADGE} border-oracle-500/20 bg-oracle-500/[0.08] text-oracle-300`;

export const STATUS_BADGE_SUCCESS =
  `${STATUS_BADGE} border-emerald-500/20 bg-emerald-500/[0.08] text-emerald-300`;

export const STATUS_PANEL =
  "rounded-xl border px-3.5 py-3 text-[14px] shadow-[0_8px_20px_rgba(0,0,0,0.18)]";

export const STATUS_PANEL_INFO =
  `${STATUS_PANEL} border-sky-500/20 bg-sky-500/[0.06] text-sky-100/90`;

export const STATUS_PANEL_WARNING =
  `${STATUS_PANEL} border-amber-500/22 bg-amber-500/[0.08] text-amber-100/90`;

export const STATUS_PANEL_ERROR =
  `${STATUS_PANEL} border-red-500/22 bg-red-500/[0.08] text-red-100/90`;

export const STATUS_PANEL_ACCENT =
  `${STATUS_PANEL} border-oracle-500/22 bg-oracle-500/[0.08] text-oracle-100/90`;

export const STATUS_PANEL_SUCCESS =
  `${STATUS_PANEL} border-emerald-500/22 bg-emerald-500/[0.08] text-emerald-100/90`;

export const STATUS_PANEL_NEUTRAL =
  `${STATUS_PANEL} border-white/[0.06] bg-white/[0.03] text-zinc-200`;

export const ACTION_ICON_BUTTON =
  "flex h-10 w-10 items-center justify-center rounded-xl border border-white/[0.08] bg-white/[0.02] text-zinc-400 transition-all duration-150 hover:border-white/[0.16] hover:bg-white/[0.06] hover:text-zinc-100 disabled:cursor-not-allowed disabled:opacity-30 touch-manipulation sm:h-8 sm:w-8 sm:rounded-lg";

export const ACTION_ICON_BUTTON_ACCENT =
  `${ACTION_ICON_BUTTON} border-oracle-500/25 bg-oracle-500/[0.12] text-oracle-300 hover:border-oracle-400/35 hover:bg-oracle-500/[0.18] hover:text-oracle-100`;

export const ACTION_ICON_BUTTON_SUCCESS =
  `${ACTION_ICON_BUTTON} border-emerald-500/25 bg-emerald-500/[0.12] text-emerald-300 hover:border-emerald-400/35 hover:bg-emerald-500/[0.18] hover:text-emerald-100`;

export const ACTION_ICON_BUTTON_DESTRUCTIVE =
  `${ACTION_ICON_BUTTON} border-red-500/25 bg-red-500/[0.12] text-red-300 hover:border-red-400/35 hover:bg-red-500/[0.18] hover:text-red-100`;

export const ACTION_MENU =
  "absolute right-0 bottom-full mb-2 rounded-xl border border-white/[0.08] bg-surface-2/95 p-1.5 shadow-[0_18px_42px_rgba(0,0,0,0.34)] backdrop-blur-md z-50 animate-fade-in";

export const ACTION_MENU_ITEM =
  "flex w-full items-center gap-2 rounded-lg px-3 py-2 text-sm text-zinc-300 transition-colors hover:bg-white/[0.05] hover:text-zinc-100 sm:gap-1.5 sm:rounded-md sm:px-2 sm:py-1.5 sm:text-[13px]";

export const ACTION_CHIP_PRIMARY =
  "inline-flex items-center justify-center gap-1.5 rounded-xl border border-oracle-500/30 bg-oracle-500/[0.12] px-3 py-2 text-[13px] font-medium text-oracle-200 transition-all duration-150 hover:border-oracle-400/40 hover:bg-oracle-500/[0.18] touch-manipulation sm:rounded-lg sm:px-2.5 sm:py-1.5 sm:text-xs";

export const ACTION_CHIP_SECONDARY =
  "inline-flex items-center justify-center gap-1.5 rounded-xl border border-white/[0.08] bg-zinc-800/85 px-3 py-2 text-[13px] font-medium text-zinc-200 transition-all duration-150 hover:border-white/[0.14] hover:bg-zinc-700/80 touch-manipulation sm:rounded-lg sm:px-2.5 sm:py-1.5 sm:text-xs";

export const ACTION_CHIP_GHOST =
  "inline-flex items-center justify-center gap-1.5 rounded-xl px-3 py-2 text-[13px] font-medium text-zinc-400 transition-all duration-150 hover:bg-white/[0.04] hover:text-zinc-200 touch-manipulation sm:rounded-lg sm:px-2.5 sm:py-1.5 sm:text-xs";

export const ACTION_CHIP_DESTRUCTIVE =
  "inline-flex items-center justify-center gap-1.5 rounded-xl border border-red-500/25 bg-red-500/[0.12] px-3 py-2 text-[13px] font-medium text-red-200 transition-all duration-150 hover:border-red-400/35 hover:bg-red-500/[0.18] touch-manipulation sm:rounded-lg sm:px-2.5 sm:py-1.5 sm:text-xs";

export const PROGRESS_CARD =
  "relative overflow-hidden rounded-2xl border border-white/[0.12] bg-zinc-900/82 shadow-[0_16px_30px_rgba(0,0,0,0.22)] backdrop-blur-sm transition-all duration-200";
