import { useState, useRef, useEffect, useCallback } from "react";
import { ArrowUp, Square, Paperclip, X, Image, FileText, Loader2, Check } from "lucide-react";
import { useTranslation } from "react-i18next";
import { api } from "@/api/client";
import type { Mode } from "@/types";

interface UploadedFile {
  file_id: string;
  filename: string;
  content_type: string;
  is_image: boolean;
  preview_url?: string;
  size_bytes?: number;
}

interface Props {
  onSubmit: (question: string, fileIds?: string[]) => void;
  loading: boolean;
  mode: Mode;
  onCancel?: () => void;
  initialValue?: string;
  placeholder?: string;
  submitRequest?: { id: number; question: string } | null;
  footerRight?: React.ReactNode;
}

function FileChip({ f, onRemove }: { f: UploadedFile; onRemove: (id: string) => void }) {
  const { t } = useTranslation();
  return (
    <div className="group flex items-center gap-2 rounded-2xl border border-white/[0.12] bg-surface-2/70 pl-1.5 pr-2.5 py-2 transition-colors hover:border-white/[0.2] hover:bg-surface-3">
      {f.is_image && f.preview_url ? (
        <img src={f.preview_url} alt={f.filename} className="w-8 h-8 rounded-lg object-cover flex-shrink-0" />
      ) : f.is_image ? (
        <div className="w-8 h-8 rounded-lg bg-blue-500/10 flex items-center justify-center flex-shrink-0">
          <Image size={15} className="text-blue-400" />
        </div>
      ) : (
        <div className="w-8 h-8 rounded-lg bg-amber-500/10 flex items-center justify-center flex-shrink-0">
          <FileText size={15} className="text-amber-400" />
        </div>
      )}
      <div className="flex flex-col min-w-0">
        <span className="max-w-[120px] truncate text-[13px] font-medium leading-tight text-zinc-100">{f.filename}</span>
        <span className="text-[13px] leading-tight text-zinc-300">
          {f.size_bytes
            ? f.size_bytes < 1024 * 1024
              ? `${(f.size_bytes / 1024).toFixed(0)} KB`
              : `${(f.size_bytes / 1024 / 1024).toFixed(1)} MB`
            : f.is_image ? t("components.queryInput.fileChip.image") : t("components.queryInput.fileChip.document")}
        </span>
      </div>
      <button
        onClick={() => onRemove(f.file_id)}
        className="ml-1 flex h-7 w-7 items-center justify-center rounded-full text-zinc-400 transition-colors hover:bg-surface-4 hover:text-zinc-100 touch-manipulation"
        aria-label={t("components.queryInput.fileChip.remove", { filename: f.filename })}
      >
        <X size={12} />
      </button>
    </div>
  );
}

export default function QueryInput({ onSubmit, loading, mode, onCancel, initialValue, placeholder, submitRequest, footerRight }: Props) {
  const { t } = useTranslation();
  const [value, setValue] = useState(() => {
    try { return sessionStorage.getItem('synthora_draft_input') || ''; } catch { return ''; }
  });
  const [files, setFiles] = useState<UploadedFile[]>([]);
  const [uploading, setUploading] = useState(false);
  const [uploadSuccess, setUploadSuccess] = useState(false);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [dragOver, setDragOver] = useState(false);
  const [uploadMenuOpen, setUploadMenuOpen] = useState(false);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const uploadMenuRef = useRef<HTMLDivElement>(null);
  const handledSubmitRequestRef = useRef<number | null>(null);
  const uploadSuccessTimerRef = useRef<number | null>(null);

  const revealInput = useCallback((behavior: ScrollBehavior = "smooth") => {
    window.setTimeout(() => {
      textareaRef.current?.scrollIntoView({ block: "nearest", behavior });
    }, 80);
  }, []);

  useEffect(() => {
    if (initialValue) {
      setValue(initialValue);
      setTimeout(() => {
        textareaRef.current?.focus();
        revealInput("auto");
      }, 50);
    }
  }, [initialValue, revealInput]);

  useEffect(() => {
    const timer = window.setTimeout(() => {
      textareaRef.current?.focus();
      textareaRef.current?.scrollIntoView({ block: "nearest", behavior: "auto" });
    }, 100);
    return () => window.clearTimeout(timer);
  }, []);

  useEffect(() => {
    if (!submitRequest || handledSubmitRequestRef.current === submitRequest.id) return;
    handledSubmitRequestRef.current = submitRequest.id;
    setValue(submitRequest.question);
    onSubmit(submitRequest.question);
    try { sessionStorage.removeItem('synthora_draft_input'); } catch { /* */ }
    setValue("");
    setTimeout(() => textareaRef.current?.focus(), 50);
  }, [submitRequest, onSubmit]);

  // Persist draft to sessionStorage so it survives navigation
  useEffect(() => {
    try { sessionStorage.setItem('synthora_draft_input', value); } catch { /* */ }
  }, [value]);

  useEffect(() => {
    if (textareaRef.current) {
      textareaRef.current.style.height = "auto";
      textareaRef.current.style.height = Math.min(textareaRef.current.scrollHeight, 160) + "px";
    }
  }, [value]);

  useEffect(() => {
    if (!uploadMenuOpen) return;
    const onPointerDown = (evt: MouseEvent) => {
      const target = evt.target as Node | null;
      if (uploadMenuRef.current && target && !uploadMenuRef.current.contains(target)) {
        setUploadMenuOpen(false);
      }
    };
    window.addEventListener("mousedown", onPointerDown);
    return () => window.removeEventListener("mousedown", onPointerDown);
  }, [uploadMenuOpen]);

  useEffect(() => {
    return () => {
      if (uploadSuccessTimerRef.current !== null) {
        window.clearTimeout(uploadSuccessTimerRef.current);
      }
    };
  }, []);

  const uploadFiles = useCallback(async (fileList: File[]) => {
    const batch = fileList.slice(0, 5 - files.length);
    if (batch.length === 0) return;
    setUploading(true);
    try {
      for (const file of batch) {
        try {
          const result = await api.upload(file);
          const preview_url = result.is_image ? URL.createObjectURL(file) : undefined;
          setFiles((prev) => [...prev, { ...result, preview_url, size_bytes: file.size }]);
          setUploadSuccess(true);
          if (uploadSuccessTimerRef.current !== null) {
            window.clearTimeout(uploadSuccessTimerRef.current);
          }
          uploadSuccessTimerRef.current = window.setTimeout(() => {
            setUploadSuccess(false);
            uploadSuccessTimerRef.current = null;
          }, 2000);
        } catch (err) {
          console.error("Upload failed:", err);
          const msg = err instanceof Error ? err.message : t("components.queryInput.upload.errorFallback");
          setUploadError(msg.length > 60 ? msg.slice(0, 60) + "…" : msg);
          setTimeout(() => setUploadError(null), 4000);
        }
      }
    } finally {
      setUploading(false);
    }
  }, [files.length, t]);

  const handleSubmit = useCallback(() => {
    const q = value.trim();
    if ((!q && files.length === 0) || loading) return;
    const fileIds = files.map((f) => f.file_id);
    onSubmit(q || t("components.queryInput.defaultFileQuestion"), fileIds.length > 0 ? fileIds : undefined);
    setValue("");
    try { sessionStorage.removeItem('synthora_draft_input'); } catch { /* */ }
    files.forEach((f) => f.preview_url && URL.revokeObjectURL(f.preview_url));
    setFiles([]);
    setTimeout(() => textareaRef.current?.focus(), 50);
  }, [files, loading, onSubmit, t, value]);

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
      e.preventDefault();
      handleSubmit();
      return;
    }
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      handleSubmit();
    }
  };

  const handleFileSelect = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const selected = e.target.files;
    if (!selected || selected.length === 0) return;
    e.target.value = "";
    setUploadMenuOpen(false);
    await uploadFiles(Array.from(selected));
  };

  const removeFile = useCallback((fileId: string) => {
    setFiles((prev) => {
      const removed = prev.find((f) => f.file_id === fileId);
      if (removed?.preview_url) URL.revokeObjectURL(removed.preview_url);
      return prev.filter((f) => f.file_id !== fileId);
    });
  }, []);

  const handleDragOver = (e: React.DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    if (e.dataTransfer.types.includes("Files")) setDragOver(true);
  };

  const handleDragLeave = (e: React.DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    if (!e.currentTarget.contains(e.relatedTarget as Node)) setDragOver(false);
  };

  const handleDrop = async (e: React.DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    setDragOver(false);
    setUploadMenuOpen(false);
    if (e.dataTransfer.files.length > 0) {
      await uploadFiles(Array.from(e.dataTransfer.files));
    }
  };

  const canSend = (value.trim().length > 0 || files.length > 0) && !loading;
  const modeLabels: Record<Mode, string> = {
    auto: "Auto",
    light: "Light",
    deep: "Deep",
    research: "Research",
    socratic: t("common.modes.socraticName"),
    roundtable: t("common.modes.roundtableName"),
  };
  const modeHelpText: Record<Mode, string> = {
    auto: t("components.queryInput.modeHelp.auto"),
    light: t("components.queryInput.modeHelp.light"),
    deep: t("components.queryInput.modeHelp.deep"),
    research: t("components.queryInput.modeHelp.research"),
    socratic: t("components.queryInput.modeHelp.socratic"),
    roundtable: t("components.queryInput.modeHelp.roundtable"),
  };

  return (
    <div
      className={`relative overflow-hidden rounded-[1.5rem] border shadow-[0_18px_44px_rgba(0,0,0,0.28)] transition-all duration-200 bg-[linear-gradient(180deg,rgba(255,255,255,0.03),rgba(255,255,255,0.01))] ${
        dragOver
          ? "border-oracle-400/65 bg-oracle-500/[0.08]"
          : "border-white/[0.12] focus-within:border-oracle-400/60 focus-within:bg-surface-1"
      }`}
      onDragOver={handleDragOver}
      onDragLeave={handleDragLeave}
      onDrop={handleDrop}
    >
      {/* Drag overlay */}
      {dragOver && (
        <div className="pointer-events-none absolute inset-0 z-10 flex items-center justify-center rounded-[1.5rem] bg-oracle-500/[0.08]">
          <div className="flex flex-col items-center gap-2">
            <div className="rounded-full border border-oracle-400/40 bg-oracle-500/[0.16] p-3">
              <Paperclip size={18} className="text-oracle-300" />
            </div>
            <span className="text-[15px] font-semibold text-oracle-200">{t("components.queryInput.upload.dragToUpload")}</span>
          </div>
        </div>
      )}

      <div className="flex flex-col gap-0 p-4">
        <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
          <div className="inline-flex items-center gap-2 rounded-full border border-white/[0.12] bg-white/[0.05] px-3 py-1.5 text-[13px] font-medium text-zinc-200">
            <span className="text-zinc-400">{t("common.modes.currentMode")}</span>
            <span className="text-oracle-200">{modeLabels[mode] ?? mode}</span>
          </div>
          <div className="text-[13px] leading-6 text-zinc-400">
            {t("components.queryInput.keyboardHints")}
          </div>
        </div>

        <p className="mb-3 text-[13px] leading-6 text-zinc-400">
          {modeHelpText[mode]}
        </p>

        {/* File chips */}
        {(files.length > 0 || uploading || uploadSuccess || uploadError) && (
          <div className="mb-3">
            <div className="flex flex-wrap gap-2">
              {files.map((f) => (
                <FileChip key={f.file_id} f={f} onRemove={removeFile} />
              ))}
              {uploading && (
                <div className="flex items-center gap-2 rounded-xl border border-white/[0.12] bg-surface-2/70 px-3 py-2 text-[13px] text-zinc-300">
                  <Loader2 size={14} className="animate-spin text-oracle-300" />
                  <span>{t("components.queryInput.upload.uploading")}</span>
                </div>
              )}
              {uploadSuccess && !uploading && (
                <div className="flex items-center gap-2 rounded-xl border border-emerald-500/30 bg-emerald-500/[0.12] px-3 py-2 text-[13px] text-emerald-200">
                  <Check size={14} className="text-emerald-300" />
                  <span>{t("components.queryInput.upload.success")}</span>
                </div>
              )}
            </div>
            {uploadError && (
              <div className="mt-2 rounded-xl border border-red-500/30 bg-red-500/[0.12] px-3 py-2 text-[13px] text-red-200">
                {uploadError}
              </div>
            )}
          </div>
        )}

        {/* Textarea */}
        <textarea
          ref={textareaRef}
          value={value}
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={handleKeyDown}
          onFocus={() => revealInput("smooth")}
          data-query-input
          placeholder={placeholder || t("components.queryInput.placeholder")}
          maxLength={10000}
          rows={1}
          className="w-full resize-none bg-transparent text-[16px] leading-8 text-zinc-100 placeholder-zinc-500 focus:outline-none min-h-[30px]"
        />

        {/* Bottom row */}
        <div className="mt-4 flex items-center justify-between gap-3">
          <div ref={uploadMenuRef} className="relative">
            <button
              type="button"
              onClick={() => setUploadMenuOpen((v) => !v)}
              disabled={loading || files.length >= 5}
              className="flex h-11 w-11 items-center justify-center rounded-2xl border border-white/[0.14] bg-surface-2/70 text-zinc-300 transition-all duration-200 hover:border-white/[0.22] hover:bg-white/[0.1] hover:text-zinc-100 active:scale-95 disabled:cursor-not-allowed disabled:opacity-20 touch-manipulation"
              title={t("components.queryInput.upload.button")}
              aria-label={t("components.queryInput.upload.button")}
            >
              <Paperclip size={15} />
            </button>
            {uploadMenuOpen && (
              <div className="absolute bottom-full left-0 z-40 mb-2 w-60 animate-fade-in rounded-2xl border border-white/[0.12] bg-surface-2 p-2.5 shadow-2xl shadow-black/45">
                <button
                  type="button"
                  onClick={() => fileInputRef.current?.click()}
                  className="flex w-full items-center gap-2 rounded-xl px-3 py-3 text-[14px] text-zinc-100 transition-colors hover:bg-surface-3"
                >
                  <Paperclip size={14} className="text-oracle-300" />
                  {t("components.queryInput.upload.selectFiles")}
                </button>
                <p className="px-2 pt-1.5 text-[13px] leading-7 text-zinc-300">
                  {t("components.queryInput.upload.supportFormats")}
                </p>
              </div>
            )}
            <input
              ref={fileInputRef}
              type="file"
              multiple
              accept="image/jpeg,image/png,image/gif,image/webp,.pdf,.txt,.md,.docx,.xlsx,.xls,.csv"
              onChange={handleFileSelect}
              className="hidden"
            />
          </div>

          <div className="ml-auto flex items-center gap-2.5">
            {files.length > 0 && !value.trim() && (
              <span className="text-[12px] text-zinc-500">
                {t("components.queryInput.upload.filesOnlyPrompt")}
              </span>
            )}
            {footerRight ? <div className="shrink-0">{footerRight}</div> : null}
            {/* Send / Stop */}
            {loading && onCancel ? (
              <button
                type="button"
                onClick={onCancel}
                className="flex h-11 w-11 items-center justify-center rounded-2xl border border-white/[0.14] bg-surface-4 text-zinc-200 transition-all duration-200 hover:bg-surface-5 active:scale-95 touch-manipulation"
                title={t("components.queryInput.stop")}
                aria-label={t("components.queryInput.stop")}
              >
                <Square size={12.5} fill="currentColor" />
              </button>
            ) : (
              <button
                type="button"
                onClick={handleSubmit}
                disabled={!canSend}
                className="flex h-11 w-11 items-center justify-center rounded-2xl bg-oracle-500 text-surface-0 transition-all duration-200 active:scale-95 hover:bg-oracle-400 disabled:cursor-not-allowed disabled:opacity-20 shadow-[0_12px_32px_rgba(234,179,8,0.28)] ring-1 ring-oracle-200/35 touch-manipulation"
                title={t("components.queryInput.send")}
                aria-label={t("components.queryInput.send")}
              >
                <ArrowUp size={14} />
              </button>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
