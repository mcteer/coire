import { act, renderHook } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { useChatScroll } from "./useChatScroll";
afterEach(() => vi.restoreAllMocks());
test("follows output near the end, preserves reading position away from it, and jumps explicitly", () => {
  const callbacks: FrameRequestCallback[] = [];
  vi.spyOn(window, "requestAnimationFrame").mockImplementation((fn) => {
    callbacks.push(fn);
    return callbacks.length;
  });
  vi.spyOn(window, "cancelAnimationFrame").mockImplementation(() => {});
  const height = vi.spyOn(document.documentElement, "scrollHeight", "get").mockReturnValue(2000);
  const scroll = vi.fn();
  const hook = renderHook(({ revision }) => useChatScroll("conversation", revision), {
    initialProps: { revision: "1" },
  });
  Object.assign(hook.result.current.end, { current: { scrollIntoView: scroll } });
  act(() => callbacks.splice(0).forEach((fn) => fn(0)));
  expect(scroll).toHaveBeenCalledOnce();
  act(() => window.dispatchEvent(new Event("scroll")));
  expect(hook.result.current.away).toBe(true);
  hook.rerender({ revision: "2" });
  act(() => callbacks.splice(0).forEach((fn) => fn(0)));
  expect(scroll).toHaveBeenCalledOnce();
  act(() => hook.result.current.jump());
  expect(scroll).toHaveBeenCalledTimes(2);
  height.mockReturnValue(700);
  act(() => window.dispatchEvent(new Event("scroll")));
  expect(hook.result.current.away).toBe(false);
});
