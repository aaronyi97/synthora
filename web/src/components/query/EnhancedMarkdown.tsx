/**
 * Enhanced Markdown renderer with Mermaid diagrams, chart support, and inline citations.
 *
 * Detects ```mermaid code blocks and renders them as SVG diagrams.
 * Detects ```chart code blocks (JSON) and renders them as Recharts visualizations.
 * Renders [N] inline citation markers as clickable numbered badges.
 * Falls back to standard ReactMarkdown for everything else.
 */

import React, { useEffect, useRef, useState, memo, useCallback } from "react";
import { useTranslation } from "react-i18next";
import DOMPurify from "dompurify";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import rehypeKatex from "rehype-katex";
import rehypeHighlight from "rehype-highlight";
import "highlight.js/styles/github-dark.css";
import "katex/dist/katex.min.css";
import {
  BarChart, Bar, LineChart, Line, PieChart, Pie, Cell,
  XAxis, YAxis, CartesianGrid, Tooltip, Legend, ResponsiveContainer,
} from "recharts";

// Lazy-load mermaid to avoid module-level crash
let mermaidReady: Promise<typeof import("mermaid")["default"]> | null = null;
function getMermaid() {
  if (!mermaidReady) {
    mermaidReady = import("mermaid").then((m) => {      // AUDIT-008: reset on failure handled in .catch below
      m.default.initialize({
        startOnLoad: false,
        theme: "dark",
        themeVariables: {
          darkMode: true,
          background: "#18181b",
          primaryColor: "#a78bfa",
          primaryTextColor: "#e4e4e7",
          primaryBorderColor: "#3f3f46",
          lineColor: "#71717a",
          secondaryColor: "#1e1e2e",
          tertiaryColor: "#27272a",
        },
        flowchart: { curve: "basis" },
        securityLevel: "strict",
      });
      return m.default;
    }).catch((e) => {
      mermaidReady = null; // AUDIT-008: allow retry on load failure
      throw e;
    });
  }
  return mermaidReady;
}

const CHART_COLORS = ["#a78bfa", "#34d399", "#fbbf24", "#f87171", "#60a5fa", "#f472b6", "#2dd4bf", "#fb923c"];

function sanitizeHref(url: string | undefined): string {
  if (!url) return "#";
  const trimmed = url.trim().toLowerCase();
  if (trimmed.startsWith("javascript:") || trimmed.startsWith("data:") || trimmed.startsWith("vbscript:")) {
    return "#";
  }
  return url;
}

// ── Mermaid Block ──
const MermaidBlock = memo(({ code }: { code: string }) => {
  const { t } = useTranslation();
  const ref = useRef<HTMLDivElement>(null);
  const [svg, setSvg] = useState<string>("");
  const [error, setError] = useState<string>("");

  useEffect(() => {
    const id = `mermaid-${Math.random().toString(36).slice(2, 8)}`;
    getMermaid()
      .then((m) => m.render(id, code.trim()))
      .then((result: { svg: string }) => setSvg(result.svg))
      .catch(() => setError("render_failed"));
  }, [code]);

  if (error) {
    return (
      <div className="my-3 rounded-xl border border-red-500/30 bg-red-500/[0.12] p-3 text-[13px] text-red-200">
        {t("components.enhancedMarkdown.mermaid.renderFailed")}
      </div>
    );
  }

  return (
    <div
      ref={ref}
      className="my-4 flex justify-center overflow-x-auto rounded-2xl border border-white/[0.12] bg-surface-1/70 p-5"
      dangerouslySetInnerHTML={{ __html: DOMPurify.sanitize(svg, { USE_PROFILES: { svg: true, svgFilters: true } }) }}
    />
  );
});
MermaidBlock.displayName = "MermaidBlock";

// ── Chart Block ──
interface ChartData {
  type: "bar" | "line" | "pie";
  title?: string;
  data: Record<string, string | number>[];
  xKey?: string;
  yKeys?: string[];
}

const ChartBlock = memo(({ code }: { code: string }) => {
  const { t } = useTranslation();
  const [error, setError] = useState<string>("");
  const [chartData, setChartData] = useState<ChartData | null>(null);

  useEffect(() => {
    try {
      const parsed = JSON.parse(code.trim()) as ChartData;
      if (!parsed.data || !Array.isArray(parsed.data)) {
        setError(t("components.enhancedMarkdown.chart.dataMissing"));
        return;
      }
      // Auto-detect keys if not specified
      if (!parsed.xKey || !parsed.yKeys) {
        const keys = Object.keys(parsed.data[0] || {});
        const numericKeys = keys.filter((k) =>
          parsed.data.some((d) => typeof d[k] === "number")
        );
        const stringKeys = keys.filter((k) => !numericKeys.includes(k));
        parsed.xKey = parsed.xKey || stringKeys[0] || keys[0];
        parsed.yKeys = parsed.yKeys || numericKeys;
      }
      setChartData(parsed);
    } catch (e) {
      setError(t("components.enhancedMarkdown.chart.parseFailed", { error: String(e) }));
    }
  }, [code, t]);

  if (error) {
    return (
      <div className="my-3 rounded-xl border border-amber-500/30 bg-amber-500/[0.12] p-3 text-[13px] text-amber-200">
        {error}
      </div>
    );
  }

  if (!chartData) return null;

  const { type, title, data, xKey, yKeys } = chartData;

  return (
    <div className="my-4 rounded-2xl border border-white/[0.12] bg-surface-1/70 p-5">
      {title && <div className="mb-3 text-center text-[14px] font-semibold text-zinc-100">{title}</div>}
      <ResponsiveContainer width="100%" height={280}>
        {type === "pie" ? (
          <PieChart>
            <Pie
              data={data}
              dataKey={yKeys?.[0] || "value"}
              nameKey={xKey || "name"}
              cx="50%" cy="50%"
              outerRadius={100}
              label={({ name, percent }) => `${name} ${((percent ?? 0) * 100).toFixed(0)}%`}
              labelLine={false}
            >
              {data.map((_, i) => (
                <Cell key={i} fill={CHART_COLORS[i % CHART_COLORS.length]} />
              ))}
            </Pie>
            <Tooltip
              contentStyle={{ background: "#27272a", border: "1px solid #52525b", borderRadius: "10px", fontSize: "13px" }}
              labelStyle={{ color: "#a1a1aa" }}
            />
            <Legend wrapperStyle={{ fontSize: "13px", color: "#d4d4d8" }} />
          </PieChart>
        ) : type === "line" ? (
          <LineChart data={data}>
            <CartesianGrid strokeDasharray="3 3" stroke="#3f3f46" />
            <XAxis dataKey={xKey} tick={{ fill: "#d4d4d8", fontSize: 12 }} />
            <YAxis tick={{ fill: "#d4d4d8", fontSize: 12 }} />
            <Tooltip
              contentStyle={{ background: "#27272a", border: "1px solid #52525b", borderRadius: "10px", fontSize: "13px" }}
              labelStyle={{ color: "#a1a1aa" }}
            />
            <Legend wrapperStyle={{ fontSize: "13px", color: "#d4d4d8" }} />
            {yKeys?.map((key, i) => (
              <Line key={key} type="monotone" dataKey={key} stroke={CHART_COLORS[i % CHART_COLORS.length]} strokeWidth={2} dot={{ r: 3 }} />
            ))}
          </LineChart>
        ) : (
          <BarChart data={data}>
            <CartesianGrid strokeDasharray="3 3" stroke="#3f3f46" />
            <XAxis dataKey={xKey} tick={{ fill: "#d4d4d8", fontSize: 12 }} />
            <YAxis tick={{ fill: "#d4d4d8", fontSize: 12 }} />
            <Tooltip
              contentStyle={{ background: "#27272a", border: "1px solid #52525b", borderRadius: "10px", fontSize: "13px" }}
              labelStyle={{ color: "#a1a1aa" }}
            />
            <Legend wrapperStyle={{ fontSize: "13px", color: "#d4d4d8" }} />
            {yKeys?.map((key, i) => (
              <Bar key={key} dataKey={key} fill={CHART_COLORS[i % CHART_COLORS.length]} radius={[4, 4, 0, 0]} />
            ))}
          </BarChart>
        )}
      </ResponsiveContainer>
    </div>
  );
});
ChartBlock.displayName = "ChartBlock";

// ── Code Block with copy button + language label ──
const CodeBlock = memo(({ className, children }: { className?: string; children?: React.ReactNode }) => {
  const { t } = useTranslation();
  const [copied, setCopied] = useState(false);
  const lang = className?.replace("language-", "") || "";
  const code = String(children).replace(/\n$/, "");

  const handleCopy = useCallback(() => {
    navigator.clipboard.writeText(code).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    });
  }, [code]);

  return (
    <div className="relative group my-2">
      <div className="flex items-center justify-between rounded-t-xl border border-white/[0.12] border-b-0 bg-surface-1 px-3.5 py-2">
        <span className="font-mono text-[12px] text-zinc-300">{lang || "code"}</span>
        <button
          onClick={handleCopy}
          className="rounded-md px-2 py-1 text-[12px] text-zinc-300 transition-colors hover:bg-white/[0.06] hover:text-zinc-100"
        >
          {copied ? t("components.enhancedMarkdown.code.copied") : t("components.enhancedMarkdown.code.copy")}
        </button>
      </div>
      <pre className="!mt-0 !rounded-t-none !rounded-b-lg overflow-x-auto">
        <code className={className}>{children}</code>
      </pre>
    </div>
  );
});
CodeBlock.displayName = "CodeBlock";

// ── Citation types ──
export interface Citation {
  url: string;
  title: string;
  date?: string;
}

// ── Inline [N] citation badge ──
function CitationBadge({ num, citation, onClick }: { num: number; citation?: Citation; onClick?: () => void }) {
  const { t } = useTranslation();
  return (
    <a
      href={sanitizeHref(citation?.url)}
      target={citation?.url ? "_blank" : undefined}
      rel="noopener noreferrer"
      onClick={(e) => { e.stopPropagation(); onClick?.(); }}
      title={citation?.title || t("components.enhancedMarkdown.sources.sourceNumber", { num })}
      className="ml-0.5 inline-flex h-5 w-5 cursor-pointer items-center justify-center rounded-full border border-oracle-500/40 bg-oracle-500/[0.18] text-[11px] font-semibold text-oracle-200 align-super no-underline transition-colors hover:bg-oracle-500/[0.3] hover:text-oracle-100"
      style={{ lineHeight: 1, verticalAlign: "super", fontSize: "11px" }}
    >
      {num}
    </a>
  );
}

// ── Sources footer ──
function SourcesFooter({ citations }: { citations: Citation[] }) {
  const { t } = useTranslation();
  const [expanded, setExpanded] = useState(false);
  if (!citations.length) return null;

  // Compact inline preview (Perplexity-style): show first few as small pills
  const previewCount = Math.min(citations.length, 4);

  return (
    <div className="mt-4 border-t border-white/[0.1] pt-3">
      <div className="flex flex-wrap items-center gap-2">
        <span className="mr-1 text-[13px] font-medium text-zinc-300">{t("components.enhancedMarkdown.sources.label")}</span>
        {citations.slice(0, previewCount).map((c, i) => (
          <a
            key={i}
            href={sanitizeHref(c.url)}
            target="_blank"
            rel="noopener noreferrer"
            className="inline-flex max-w-[170px] items-center gap-1.5 rounded-full border border-white/[0.12] bg-surface-2 px-2.5 py-1.5 text-[13px] text-zinc-200 transition-colors no-underline hover:border-oracle-500/35 hover:text-oracle-200 sm:max-w-[220px]"
            title={c.title || c.url}
          >
            <span className="shrink-0 font-semibold text-oracle-300">{i + 1}</span>
            <span className="truncate">{(() => { try { return new URL(c.url).hostname.replace(/^www\./, ""); } catch { return t("components.enhancedMarkdown.sources.fallback"); } })()}</span>
          </a>
        ))}
        {citations.length > previewCount && (
          <button
            onClick={() => setExpanded(!expanded)}
            className="px-2 py-1 text-[13px] text-zinc-300 transition-colors hover:text-zinc-100"
          >
            {t("components.enhancedMarkdown.sources.moreCount", { count: citations.length - previewCount })}
          </button>
        )}
        {citations.length <= previewCount && citations.length > 1 && (
          <button
            onClick={() => setExpanded(!expanded)}
            className="text-[13px] text-zinc-300 transition-colors hover:text-zinc-100"
          >
            {expanded ? t("components.enhancedMarkdown.sources.collapse") : t("components.enhancedMarkdown.sources.expand")}
          </button>
        )}
      </div>
      {expanded && (
        <div className="mt-3 grid grid-cols-1 gap-2 sm:grid-cols-2">
          {citations.map((c, i) => (
            <a
              key={i}
              href={sanitizeHref(c.url)}
              target="_blank"
              rel="noopener noreferrer"
              className="group flex items-center gap-2 rounded-xl border border-white/[0.12] bg-surface-1 px-3 py-2.5 transition-colors no-underline hover:border-oracle-500/35"
            >
              <span className="flex h-5 w-5 shrink-0 items-center justify-center rounded-full border border-oracle-500/40 bg-oracle-500/[0.18] text-[11px] font-semibold text-oracle-200">{i + 1}</span>
              <div className="min-w-0">
                <p className="truncate text-[13px] text-zinc-100 transition-colors group-hover:text-oracle-200">{c.title || c.url}</p>
              </div>
            </a>
          ))}
        </div>
      )}
    </div>
  );
}

// ── Main Enhanced Markdown ──
interface Props {
  content: string;
  citations?: Citation[];
  className?: string;
}

// Replace [N] text markers with citation badge placeholders that ReactMarkdown passes through.
// Avoids mismatching:
//   - Markdown link reference definitions: [1]: http://...
//   - Reference-style links: [text][1]
//   - Lines that start with [N]: (definition lines)
function injectCitationMarkers(text: string): string {
  return text.replace(/(?<!\])\[(\d+)\](?![:\[\(])/g, (_, n) => `[[CITE:${n}]]`);
}

export default function EnhancedMarkdown({ content, citations = [], className }: Props) {
  const segments = splitContentSegments(content);

  return (
    <div className={className || "prose-oracle text-[15px] leading-8 text-zinc-100"}>
      {segments.map((seg, i) => {
        if (seg.type === "mermaid") {
          return <MermaidBlock key={i} code={seg.content} />;
        }
        if (seg.type === "chart") {
          return <ChartBlock key={i} code={seg.content} />;
        }
        const processedContent = citations.length ? injectCitationMarkers(seg.content) : seg.content;
        return (
          <ReactMarkdown
            key={i}
            remarkPlugins={[remarkGfm, remarkMath]}
            rehypePlugins={[rehypeHighlight, rehypeKatex]}
            components={{
              a: ({ href, children }) => (
                <a
                  href={sanitizeHref(href)}
                  target="_blank"
                  rel="noopener noreferrer"
                  onClick={(e) => e.stopPropagation()}
                >
                  {children}
                </a>
              ),
              code: ({ className: cls, children, ...props }) => {
                const isBlock = cls?.startsWith("language-");
                if (isBlock) {
                  return <CodeBlock className={cls}>{children}</CodeBlock>;
                }
                return (
                  <code
                    className={`rounded-md border border-white/[0.1] bg-white/[0.06] px-1.5 py-0.5 text-[0.92em] text-oracle-100 ${cls || ""}`}
                    {...props}
                  >
                    {children}
                  </code>
                );
              },
              p: ({ children }) => (
                <p className="my-3 leading-8">{renderWithCitations(children, citations)}</p>
              ),
              li: ({ children }) => (
                <li className="marker:text-zinc-500">{renderWithCitations(children, citations)}</li>
              ),
              ul: ({ children }) => (
                <ul className="my-3 space-y-1.5 pl-6">{children}</ul>
              ),
              ol: ({ children }) => (
                <ol className="my-3 space-y-1.5 pl-6">{children}</ol>
              ),
              blockquote: ({ children }) => (
                <blockquote className="my-4 rounded-r-2xl border-l-2 border-oracle-400/45 bg-oracle-500/[0.06] px-4 py-3 text-zinc-200">
                  {children}
                </blockquote>
              ),
              table: ({ children }) => (
                <div className="overflow-x-auto my-3">
                  <table className="w-full border-collapse text-[13px]">{children}</table>
                </div>
              ),
              thead: ({ children }) => (
                <thead className="bg-zinc-800/60">{children}</thead>
              ),
              th: ({ children }) => (
                <th className="whitespace-nowrap border border-zinc-700/70 px-3 py-2 text-left text-[13px] font-semibold text-zinc-100">{children}</th>
              ),
              td: ({ children }) => (
                <td className="align-top border border-zinc-800/80 px-3 py-2 text-zinc-200">{children}</td>
              ),
              tr: ({ children }) => (
                <tr className="hover:bg-zinc-800/20 transition-colors even:bg-zinc-900/30">{children}</tr>
              ),
            }}
          >
            {processedContent}
          </ReactMarkdown>
        );
      })}
      {citations.length > 0 && <SourcesFooter citations={citations} />}
    </div>
  );
}

// Recursively walk React children and replace [[CITE:N]] text with CitationBadge
function renderWithCitations(children: React.ReactNode, citations: Citation[]): React.ReactNode {
  if (!citations.length) return children;
  return processChildren(children, citations);
}

function processChildren(children: React.ReactNode, citations: Citation[]): React.ReactNode {
  return React.Children.map(children, (child) => {
    if (typeof child === "string") {
      return splitCitationText(child, citations);
    }
    if (React.isValidElement(child)) {
      const props = child.props as Record<string, unknown>;
      if (props.children) {
        return React.cloneElement(child as React.ReactElement<Record<string, unknown>>, {
          children: processChildren(props.children as React.ReactNode, citations),
        });
      }
    }
    return child;
  });
}

function splitCitationText(text: string, citations: Citation[]): React.ReactNode {
  const parts = text.split(/(\[\[CITE:\d+\]\])/g);
  if (parts.length === 1) return text;
  return parts.map((part, i) => {
    const m = part.match(/\[\[CITE:(\d+)\]\]/);
    if (!m) return part;
    const num = parseInt(m[1], 10);
    const citation = citations[num - 1];
    return <CitationBadge key={i} num={num} citation={citation} />;
  });
}

interface Segment {
  type: "markdown" | "mermaid" | "chart";
  content: string;
}

function splitContentSegments(content: string): Segment[] {
  const segments: Segment[] = [];
  const regex = /```(mermaid|chart)\n([\s\S]*?)```/g;
  let lastIndex = 0;
  let match;

  while ((match = regex.exec(content)) !== null) {
    // Add preceding markdown
    if (match.index > lastIndex) {
      const md = content.slice(lastIndex, match.index).trim();
      if (md) segments.push({ type: "markdown", content: md });
    }
    // Add special block
    segments.push({ type: match[1] as "mermaid" | "chart", content: match[2] });
    lastIndex = match.index + match[0].length;
  }

  // Add remaining markdown
  if (lastIndex < content.length) {
    const md = content.slice(lastIndex).trim();
    if (md) segments.push({ type: "markdown", content: md });
  }

  if (segments.length === 0) {
    segments.push({ type: "markdown", content });
  }

  return segments;
}
