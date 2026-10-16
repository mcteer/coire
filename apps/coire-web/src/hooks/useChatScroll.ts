import { useEffect, useRef, useState } from "react";

/** Follow live output only while the reader remains near the end of the thread. */
export function useChatScroll(conversationId: string | null, revision: string) {
  const end = useRef<HTMLDivElement>(null);
  const nearEnd = useRef(true);
  const selected = useRef<string | null>(null);
  const [away, setAway] = useState(false);
  const jump = () => {
    nearEnd.current = true;
    setAway(false);
    end.current?.scrollIntoView?.({ block: "end", behavior: "instant" });
  };
  useEffect(() => {
    const measure = () => {
      const distance = document.documentElement.scrollHeight - innerHeight - scrollY;
      nearEnd.current = distance < 360;
      setAway(!nearEnd.current);
    };
    window.addEventListener("scroll", measure, { passive: true });
    return () => window.removeEventListener("scroll", measure);
  }, []);
  useEffect(() => {
    if (!conversationId || !revision) return;
    if (selected.current !== conversationId) {
      selected.current = conversationId;
      nearEnd.current = true;
    }
    if (nearEnd.current) {
      const frame = requestAnimationFrame(jump);
      return () => cancelAnimationFrame(frame);
    }
  }, [conversationId, revision]);
  return { end, away, jump };
}
