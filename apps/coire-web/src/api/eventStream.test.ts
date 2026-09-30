import { expect, test } from "vitest";
import { SseParser } from "./eventStream";

const bytes = (value: string) => new TextEncoder().encode(value);

test("parses split UTF-8, CRLF, comments, multiline data and event IDs", () => {
  const parser = new SseParser();
  const encoded = bytes(
    ': keepalive\r\nid: conversation:3\r\nevent: message.delta\r\ndata: {"text":"é",\r\ndata: "next":1}\r\n\r\n',
  );
  const accent = encoded.indexOf(0xc3);
  expect(parser.push(encoded.slice(0, accent + 1))).toEqual([]);
  expect(parser.push(encoded.slice(accent + 1))).toEqual([
    {
      id: "conversation:3",
      event: "message.delta",
      data: '{"text":"é",\n"next":1}',
    },
  ]);
  expect(parser.finish()).toEqual([]);
});

test("ignores comments and empty dispatches, then resets event fields", () => {
  const parser = new SseParser();
  expect(parser.push(bytes(": ping\n\nid: 4\ndata: one\n\ndata: two\n\n"))).toEqual([
    { id: "4", event: null, data: "one" },
    { id: null, event: null, data: "two" },
  ]);
});

test("rejects oversized frames before unbounded buffering", () => {
  const parser = new SseParser();
  expect(() => parser.push(bytes("x".repeat(2 * 1024 * 1024 + 1)))).toThrow("limit");
});
