/**
 * TypewriterMarkdown — reveals markdown content progressively (v3.3).
 *
 * Used for draft answers so they appear with a flowing effect
 * instead of suddenly popping in.  Once fully revealed, renders
 * normal EnhancedMarkdown for full formatting.
 *
 * Speed adapts to content length: short content reveals fast,
 * long content ramps up to avoid excessive wait.
 */

import { useState, useEffect, useRef } from "react";
import EnhancedMarkdown from "./EnhancedMarkdown";

interface Props {
  content: string;
  /** Characters per frame (~60fps). Higher = faster. Default 8. */
  speed?: number;
  /** If true, skip animation and show full content immediately. */
  immediate?: boolean;
  className?: string;
}

export default function TypewriterMarkdown({ content, speed = 8, immediate = false, className }: Props) {
  const [revealed, setRevealed] = useState(immediate ? content.length : 0);
  const rafRef = useRef<number>(0);
  const revealedRef = useRef(immediate ? content.length : 0);
  const contentRef = useRef(content);

  useEffect(() => {
    if (immediate) {
      contentRef.current = content;
      revealedRef.current = content.length;
      setRevealed(content.length);
      return;
    }
    if (content === contentRef.current) return;
    const previousContent = contentRef.current;
    const nextRevealed = content.startsWith(previousContent)
      ? Math.max(0, Math.min(revealedRef.current, previousContent.length))
      : 0;
    contentRef.current = content;
    revealedRef.current = nextRevealed;
    setRevealed(nextRevealed);
  }, [content, immediate]);

  useEffect(() => {
    if (immediate) return;

    const tick = () => {
      const cur = revealedRef.current;
      const len = contentRef.current.length;
      if (cur >= len) return; // done

      const remaining = len - cur;
      const adaptive = remaining > 2000 ? speed * 4
        : remaining > 500 ? speed * 2
        : speed;

      const next = Math.min(cur + adaptive, len);
      revealedRef.current = next;
      setRevealed(next);

      if (next < len) {
        rafRef.current = requestAnimationFrame(tick);
      }
    };

    const timer = setTimeout(() => {
      rafRef.current = requestAnimationFrame(tick);
    }, 60);

    return () => {
      clearTimeout(timer);
      cancelAnimationFrame(rafRef.current);
    };
  }, [content, speed, immediate]); // only restart on NEW content, not on revealed

  const done = revealed >= content.length;
  const visibleText = done ? content : content.slice(0, revealed);

  return (
    <div className={className}>
      <EnhancedMarkdown content={visibleText} />
      {!done && (
        <span className="inline-block w-1.5 h-3.5 bg-blue-400/50 animate-pulse ml-0.5 align-middle" />
      )}
    </div>
  );
}
