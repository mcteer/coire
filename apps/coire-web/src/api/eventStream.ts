/** Shared authenticated SSE transport and incremental frame parser. */
export type SseFrame = { id: string | null; event: string | null; data: string };

export class SseParser {
  private readonly decoder = new TextDecoder("utf-8", { fatal: true });
  private line = "";
  private data: string[] = [];
  private id: string | null = null;
  private event: string | null = null;
  private afterCr = false;
  private readonly limit = 2 * 1024 * 1024;

  push(bytes: Uint8Array): SseFrame[] {
    return this.accept(this.decoder.decode(bytes, { stream: true }));
  }

  finish(): SseFrame[] {
    return this.accept(this.decoder.decode());
  }

  private accept(text: string): SseFrame[] {
    const frames: SseFrame[] = [];
    for (const char of text) {
      if (this.afterCr) {
        this.afterCr = false;
        if (char === "\n") continue;
      }
      if (char === "\r" || char === "\n") {
        this.consumeLine(frames);
        this.afterCr = char === "\r";
      } else {
        this.line += char;
        if (this.line.length > this.limit) throw new Error("stream frame exceeds limit");
      }
    }
    return frames;
  }

  private consumeLine(frames: SseFrame[]): void {
    const line = this.line;
    this.line = "";
    if (line === "") {
      if (this.data.length)
        frames.push({ id: this.id, event: this.event, data: this.data.join("\n") });
      this.data = [];
      this.id = null;
      this.event = null;
      return;
    }
    if (line.startsWith(":")) return;
    const colon = line.indexOf(":");
    const field = colon < 0 ? line : line.slice(0, colon);
    const raw = colon < 0 ? "" : line.slice(colon + 1);
    const value = raw.startsWith(" ") ? raw.slice(1) : raw;
    if (field === "data") {
      this.data.push(value);
      if (this.data.join("\n").length > this.limit) throw new Error("stream event exceeds limit");
    } else if (field === "id" && !value.includes("\0")) {
      this.id = value;
    } else if (field === "event") {
      this.event = value;
    }
  }
}

export async function readEventStream(
  response: Response,
  signal: AbortSignal,
  onFrame: (frame: SseFrame) => void,
): Promise<void> {
  if (!response.body) throw new Error("stream body unavailable");
  const reader = response.body.getReader();
  const parser = new SseParser();
  try {
    while (!signal.aborted) {
      const { value, done } = await reader.read();
      if (done) break;
      if (value) for (const frame of parser.push(value)) onFrame(frame);
    }
    if (!signal.aborted) for (const frame of parser.finish()) onFrame(frame);
  } finally {
    await reader.cancel().catch(() => undefined);
  }
}

export function openEventStream(url: string, cursor: string | null, signal: AbortSignal) {
  return fetch(url, {
    credentials: "same-origin",
    headers: cursor ? { "Last-Event-ID": cursor } : {},
    signal,
  });
}
