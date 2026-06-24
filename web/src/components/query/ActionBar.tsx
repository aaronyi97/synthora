import { useState, useCallback, useRef, useEffect } from "react";
import {
  Copy,
  Check,
  Download,
  Share2,
  FileText,
  FileType,
  Printer,
  X,
} from "lucide-react";
import { cn } from "@/lib/utils";
import {
  ACTION_ICON_BUTTON,
  ACTION_ICON_BUTTON_DESTRUCTIVE,
  ACTION_ICON_BUTTON_SUCCESS,
  ACTION_MENU,
  ACTION_MENU_ITEM,
} from "@/lib/outputStyle";
import DOMPurify from "dompurify";
import { marked } from "marked";
import { useTranslation } from "react-i18next";

interface ActionBarProps {
  /** Plain text content for copy/download */
  content: string;
  /** Title/question for the document header */
  title: string;
  /** Unique ID for filenames */
  queryId?: string;
  /** Optional: show thumbs up/down */
  showFeedback?: boolean;
  onFeedback?: (vote: "up" | "down") => void;
  feedbackState?: "up" | "down" | null;
}

// ── Security helpers ──

function escapeHtml(s: string): string {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

// ── Download helpers ──

function triggerDownload(content: string, filename: string, mime = "text/plain;charset=utf-8") {
  const blob = new Blob([content], { type: mime });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

function buildDocContent(title: string, content: string): string {
  // Word-compatible HTML document — C1: escape to prevent XSS
  const t = escapeHtml(title);
  const c = DOMPurify.sanitize(marked.parse(content) as string);
  return `<html xmlns:o="urn:schemas-microsoft-com:office:office" xmlns:w="urn:schemas-microsoft-com:office:word" xmlns="http://www.w3.org/TR/REC-html40">
<head><meta charset="utf-8"><title>${t}</title>
<style>body{font-family:"Microsoft YaHei",sans-serif;font-size:14px;line-height:1.8;padding:40px;}h1{font-size:18px;margin-bottom:16px;}p{margin:8px 0;}ul,ol{margin:12px 0;padding-left:24px;}li{margin:4px 0;}pre{background:#f5f5f5;border:1px solid #e5e7eb;border-radius:8px;padding:16px;overflow:auto;white-space:pre-wrap;}code{background:#f5f5f5;border-radius:4px;padding:0.1em 0.35em;font-family:"SFMono-Regular","Consolas",monospace;}pre code{background:transparent;padding:0;}table{width:100%;border-collapse:collapse;margin:16px 0;}th,td{border:1px solid #d4d4d8;padding:8px 10px;text-align:left;vertical-align:top;}th{background:#f4f4f5;}</style>
</head><body><h1>${t}</h1><div>${c}</div></body></html>`;
}

function printAsPdf(title: string, content: string) {
  const printWin = window.open("", "_blank", "width=800,height=600");
  if (!printWin) return;
  // C1: escape to prevent XSS in print window
  const t = escapeHtml(title);
  const c = DOMPurify.sanitize(marked.parse(content) as string);
  printWin.document.write(`<!DOCTYPE html><html><head><meta charset="utf-8"><title>${t}</title>
<style>body{font-family:"Microsoft YaHei","PingFang SC",sans-serif;font-size:14px;line-height:1.8;padding:40px;max-width:700px;margin:0 auto;}
h1{font-size:18px;margin-bottom:16px;color:#333;}p{margin:8px 0;}ul,ol{margin:12px 0;padding-left:24px;}li{margin:4px 0;}pre{background:#f5f5f5;border:1px solid #e5e7eb;border-radius:8px;padding:16px;overflow:auto;white-space:pre-wrap;}code{background:#f5f5f5;border-radius:4px;padding:0.1em 0.35em;font-family:"SFMono-Regular","Consolas",monospace;}pre code{background:transparent;padding:0;}table{width:100%;border-collapse:collapse;margin:16px 0;}th,td{border:1px solid #d4d4d8;padding:8px 10px;text-align:left;vertical-align:top;}th{background:#f4f4f5;}</style>
</head><body><h1>${t}</h1><div>${c}</div></body></html>`);
  printWin.document.close();
  setTimeout(() => { printWin.print(); printWin.close(); }, 300);
}

// ── Share helpers ──

function canNativeShare(): boolean {
  return typeof navigator !== "undefined" && !!navigator.share;
}

async function nativeShare(title: string, text: string) {
  try {
    await navigator.share({ title, text: text.slice(0, 500) + (text.length > 500 ? "..." : ""), url: window.location.href });
  } catch { /* user cancelled or not supported */ }
}

export default function ActionBar({ content, title, queryId, showFeedback, onFeedback, feedbackState }: ActionBarProps) {
  const { t } = useTranslation();
  const [copied, setCopied] = useState(false);
  const [downloadOpen, setDownloadOpen] = useState(false);
  const [shareOpen, setShareOpen] = useState(false);
  const [linkCopied, setLinkCopied] = useState(false);
  const downloadRef = useRef<HTMLDivElement>(null);
  const shareRef = useRef<HTMLDivElement>(null);
  const shareTargets = [
    {
      id: "weibo",
      label: t("components.actionBar.share.targets.weibo"),
      icon: "🔗",
      getUrl: (text: string, url: string) => `https://service.weibo.com/share/share.php?title=${encodeURIComponent(text)}&url=${encodeURIComponent(url)}`,
    },
    {
      id: "twitter",
      label: t("components.actionBar.share.targets.twitter"),
      icon: "𝕏",
      getUrl: (text: string, url: string) => `https://twitter.com/intent/tweet?text=${encodeURIComponent(text)}&url=${encodeURIComponent(url)}`,
    },
    {
      id: "copy_link",
      label: t("components.actionBar.share.targets.copyLink"),
      icon: "🔗",
      getUrl: () => "",
    },
  ];

  // Close menus on outside click
  useEffect(() => {
    const handler = (e: MouseEvent) => {
      const t = e.target as Node;
      if (downloadRef.current && !downloadRef.current.contains(t)) setDownloadOpen(false);
      if (shareRef.current && !shareRef.current.contains(t)) setShareOpen(false);
    };
    window.addEventListener("mousedown", handler);
    return () => window.removeEventListener("mousedown", handler);
  }, []);

  const handleCopy = useCallback(() => {
    navigator.clipboard.writeText(content).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    }).catch(() => {});
  }, [content]);

  const fileBase = `synthora-${queryId || Date.now()}`;

  const handleShare = useCallback(() => {
    if (canNativeShare()) {
      nativeShare(title, content);
    } else {
      setShareOpen((v) => !v);
    }
  }, [title, content]);

  const handleShareTarget = useCallback((targetId: string) => {
    if (targetId === "copy_link") {
      navigator.clipboard.writeText(window.location.href).then(() => {
        setLinkCopied(true);
        setTimeout(() => { setLinkCopied(false); setShareOpen(false); }, 1500);
      }).catch(() => {});
      return;
    }
    const target = shareTargets.find((item) => item.id === targetId);
    if (target) {
      const url = target.getUrl(title.slice(0, 100), window.location.href);
      window.open(url, "_blank", "noopener,noreferrer,width=600,height=500");
    }
    setShareOpen(false);
  }, [shareTargets, title]);

  return (
    <div className="flex flex-wrap items-center gap-1.5 sm:gap-1">
      {/* Copy */}
      <button
        onClick={handleCopy}
        title={t("components.actionBar.copyFullText")}
        className={cn(ACTION_ICON_BUTTON, copied && ACTION_ICON_BUTTON_SUCCESS)}
      >
        {copied ? <Check size={14} /> : <Copy size={14} />}
      </button>

      {/* Download */}
      <div ref={downloadRef} className="relative">
        <button onClick={() => setDownloadOpen((v) => !v)} title={t("components.actionBar.download.title")} className={ACTION_ICON_BUTTON}>
          <Download size={14} />
        </button>
        {downloadOpen && (
          <div className={cn(ACTION_MENU, "w-44 sm:w-40")}>
            <button type="button" onClick={() => { triggerDownload(`${title}\n\n${content}\n`, `${fileBase}.txt`); setDownloadOpen(false); }} className={ACTION_MENU_ITEM}>
              <FileText size={12} /> {t("components.actionBar.download.txt")}
            </button>
            <button type="button" onClick={() => { triggerDownload(buildDocContent(title, content), `${fileBase}.doc`, "application/msword;charset=utf-8"); setDownloadOpen(false); }} className={ACTION_MENU_ITEM}>
              <FileType size={12} /> {t("components.actionBar.download.doc")}
            </button>
            <button type="button" onClick={() => { printAsPdf(title, content); setDownloadOpen(false); }} className={ACTION_MENU_ITEM}>
              <Printer size={12} /> {t("components.actionBar.download.pdf")}
            </button>
          </div>
        )}
      </div>

      {/* Share */}
      <div ref={shareRef} className="relative">
        <button onClick={handleShare} title={t("components.actionBar.share.title")} className={ACTION_ICON_BUTTON}>
          <Share2 size={14} />
        </button>
        {shareOpen && (
          <div className={cn(ACTION_MENU, "w-44 sm:w-40")}>
            {shareTargets.map((target) => (
              <button key={target.id} type="button" onClick={() => handleShareTarget(target.id)} className={ACTION_MENU_ITEM}>
                <span className="text-[12px]">{target.icon}</span>
                {target.id === "copy_link" && linkCopied ? t("components.actionBar.share.copied") : target.label}
              </button>
            ))}
            <button type="button" onClick={() => setShareOpen(false)} className={cn(ACTION_MENU_ITEM, "text-zinc-500")}>
              <X size={12} /> {t("components.actionBar.share.close")}
            </button>
          </div>
        )}
      </div>

      {/* Feedback (optional) */}
      {showFeedback && onFeedback && (
        <>
          <button
            onClick={() => onFeedback("up")}
            title={t("components.actionBar.feedback.helpful")}
            className={cn(
              ACTION_ICON_BUTTON,
              feedbackState === "up" && ACTION_ICON_BUTTON_SUCCESS,
              feedbackState && feedbackState !== "up" && "opacity-30",
            )}
          >
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M7 10v12"/><path d="M15 5.88 14 10h5.83a2 2 0 0 1 1.92 2.56l-2.33 8A2 2 0 0 1 17.5 22H4a2 2 0 0 1-2-2v-8a2 2 0 0 1 2-2h2.76a2 2 0 0 0 1.79-1.11L12 2h0a3.13 3.13 0 0 1 3 3.88Z"/></svg>
          </button>
          <button
            onClick={() => onFeedback("down")}
            title={t("components.actionBar.feedback.problematic")}
            className={cn(
              ACTION_ICON_BUTTON,
              feedbackState === "down" && ACTION_ICON_BUTTON_DESTRUCTIVE,
              feedbackState && feedbackState !== "down" && "opacity-30",
            )}
          >
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M17 14V2"/><path d="M9 18.12 10 14H4.17a2 2 0 0 1-1.92-2.56l2.33-8A2 2 0 0 1 6.5 2H20a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2h-2.76a2 2 0 0 0-1.79 1.11L12 22h0a3.13 3.13 0 0 1-3-3.88Z"/></svg>
          </button>
        </>
      )}
    </div>
  );
}
