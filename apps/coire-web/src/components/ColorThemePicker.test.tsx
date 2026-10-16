import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { useTheme } from "../hooks/useTheme";
import { colorPreferencesKey, defaultPreferences, presets } from "../theme/colorThemes";
import { ColorThemePicker } from "./ColorThemePicker";
const original = Object.getOwnPropertyDescriptor(window, "localStorage");
beforeEach(() => {
  const values = new Map<string, string>();
  Object.defineProperty(window, "localStorage", {
    configurable: true,
    value: {
      get length() {
        return values.size;
      },
      getItem: (key: string) => values.get(key) ?? null,
      setItem: (key: string, value: string) => {
        values.set(key, value);
      },
      removeItem: (key: string) => values.delete(key),
      clear: () => values.clear(),
      key: (i: number) => [...values.keys()][i] ?? null,
    },
  });
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  if (original) Object.defineProperty(window, "localStorage", original);
  else Reflect.deleteProperty(window, "localStorage");
  delete document.documentElement.dataset.theme;
});
function Harness() {
  const settings = useTheme();
  return <ColorThemePicker settings={settings} />;
}
async function open() {
  fireEvent.click(screen.getByRole("button", { name: "Color themes" }));
  return screen.findByRole("dialog", { name: "Color themes" });
}

test("preset choice persists across remount and does not touch private drafts", async () => {
  sessionStorage.setItem("coire.chat.palette-test", "private draft");
  const { unmount } = render(<Harness />);
  let dialog = await open();
  fireEvent.click(within(dialog).getByRole("button", { name: "Use Ocean theme" }));
  await waitFor(() => expect(document.documentElement.dataset.colorTheme).toBe("ocean"));
  expect(JSON.parse(window.localStorage.getItem(colorPreferencesKey)!).selectedId).toBe("ocean");
  unmount();
  render(<Harness />);
  dialog = await open();
  expect(within(dialog).getByRole("button", { name: "Use Ocean theme" })).toHaveAttribute(
    "aria-pressed",
    "true",
  );
  expect(sessionStorage.getItem("coire.chat.palette-test")).toBe("private draft");
  sessionStorage.removeItem("coire.chat.palette-test");
});

test("cancel and Escape discard live previews and restore focus without saving", async () => {
  render(<Harness />);
  const dialog = await open();
  fireEvent.click(within(dialog).getByRole("button", { name: "Create custom theme" }));
  fireEvent.change(within(dialog).getByLabelText("Light accent hex"), {
    target: { value: "#2457a7" },
  });
  await waitFor(() =>
    expect(document.documentElement.style.getPropertyValue("--accent")).toBe("#2457a7"),
  );
  fireEvent.click(within(dialog).getByRole("button", { name: "Cancel preview" }));
  await waitFor(() => expect(document.documentElement.style.getPropertyValue("--accent")).toBe(""));
  fireEvent.click(within(dialog).getByRole("button", { name: "Create custom theme" }));
  fireEvent.change(within(dialog).getByLabelText("Light accent hex"), {
    target: { value: "#35654d" },
  });
  fireEvent(dialog, new Event("cancel", { bubbles: false, cancelable: true }));
  await waitFor(() => expect(screen.getByRole("button", { name: "Color themes" })).toHaveFocus());
  expect(JSON.parse(window.localStorage.getItem(colorPreferencesKey)!)).toEqual(
    defaultPreferences(),
  );
  expect(document.documentElement.style.getPropertyValue("--accent")).toBe("");
});

test("custom light/dark colors can be saved, edited, reset and deleted", async () => {
  render(<Harness />);
  const dialog = await open();
  const q = within(dialog);
  fireEvent.click(q.getByRole("button", { name: "Create custom theme" }));
  fireEvent.change(q.getByLabelText("Theme name"), { target: { value: "My lab" } });
  fireEvent.change(q.getByLabelText("Light accent hex"), { target: { value: "#2457a7" } });
  fireEvent.change(q.getByLabelText("Dark accent hex"), { target: { value: "#aabbff" } });
  fireEvent.click(q.getByRole("button", { name: "Save theme" }));
  expect(await q.findByRole("button", { name: "Use My lab theme" })).toHaveAttribute(
    "aria-pressed",
    "true",
  );
  fireEvent.click(q.getByRole("button", { name: "Switch to dark theme" }));
  await waitFor(() =>
    expect(document.documentElement.style.getPropertyValue("--accent")).toBe("#aabbff"),
  );
  fireEvent.click(q.getByRole("button", { name: "Edit theme" }));
  fireEvent.change(q.getByLabelText("Theme name"), { target: { value: "My revised lab" } });
  fireEvent.click(q.getByRole("button", { name: "Save theme" }));
  expect(await q.findByRole("button", { name: "Use My revised lab theme" })).toBeInTheDocument();
  fireEvent.click(q.getByRole("button", { name: "Reset colors" }));
  expect(q.getByRole("button", { name: "Use Original theme" })).toHaveAttribute(
    "aria-pressed",
    "true",
  );
  fireEvent.click(q.getByRole("button", { name: "Use My revised lab theme" }));
  fireEvent.click(q.getByRole("button", { name: "Delete selected theme" }));
  fireEvent.click(q.getByRole("button", { name: "Delete selected theme" }));
  await waitFor(() =>
    expect(q.queryByRole("button", { name: "Use My revised lab theme" })).not.toBeInTheDocument(),
  );
  expect(JSON.parse(window.localStorage.getItem(colorPreferencesKey)!).customThemes).toHaveLength(
    0,
  );
});

test("invalid and conflicting colors cannot be saved; blocked storage remains usable", async () => {
  vi.spyOn(window.localStorage, "setItem").mockImplementation(() => {
    throw new Error("blocked");
  });
  render(<Harness />);
  const dialog = await open();
  const q = within(dialog);
  expect(await q.findByRole("status")).toHaveTextContent("blocking saved preferences");
  fireEvent.click(q.getByRole("button", { name: "Use Forest theme" }));
  await waitFor(() => expect(document.documentElement.dataset.colorTheme).toBe("forest"));
  fireEvent.click(q.getByRole("button", { name: "Create custom theme" }));
  fireEvent.change(q.getByLabelText("Light accent hex"), { target: { value: "#xyzxyz" } });
  expect(q.getByRole("button", { name: "Save theme" })).toBeDisabled();
  fireEvent.change(q.getByLabelText("Light accent hex"), {
    target: { value: presets[2].light.accent },
  });
  fireEvent.change(q.getByLabelText("Light surface hex"), { target: { value: "#000000" } });
  expect(q.getByRole("alert")).toHaveTextContent("readable");
  expect(q.getByRole("button", { name: "Save theme" })).toBeDisabled();
});

test("custom theme capacity is bounded and duplicate preset names are refused", async () => {
  const customThemes = Array.from({ length: 12 }, (_, i) => ({
    ...presets[1],
    id: `custom-00000000-0000-4000-8000-${String(i).padStart(12, "0")}`,
    name: `Custom ${i}`,
  }));
  window.localStorage.setItem(
    colorPreferencesKey,
    JSON.stringify({ version: 1, selectedId: "original", customThemes }),
  );
  const { unmount } = render(<Harness />);
  let dialog = await open();
  fireEvent.click(within(dialog).getByRole("button", { name: "Create custom theme" }));
  fireEvent.click(within(dialog).getByRole("button", { name: "Save theme" }));
  expect(await within(dialog).findByRole("alert")).toHaveTextContent("up to 12");
  expect(JSON.parse(window.localStorage.getItem(colorPreferencesKey)!).customThemes).toHaveLength(
    12,
  );
  unmount();
  window.localStorage.setItem(colorPreferencesKey, JSON.stringify(defaultPreferences()));
  render(<Harness />);
  dialog = await open();
  fireEvent.click(within(dialog).getByRole("button", { name: "Create custom theme" }));
  fireEvent.change(within(dialog).getByLabelText("Theme name"), { target: { value: "Ocean" } });
  fireEvent.click(within(dialog).getByRole("button", { name: "Save theme" }));
  expect(await within(dialog).findByRole("alert")).toHaveTextContent("different theme name");
});

test("Tab and Shift+Tab wrap within the color dialog", async () => {
  render(<Harness />);
  const dialog = await open();
  const q = within(dialog);
  const first = q.getByRole("button", { name: "Close color themes" });
  const last = q.getByRole("button", { name: "Reset colors" });
  first.focus();
  fireEvent.keyDown(first, { key: "Tab" });
  expect(q.getByRole("button", { name: "Switch to dark theme" })).toHaveFocus();
  first.focus();
  fireEvent.keyDown(first, { key: "Tab", shiftKey: true });
  expect(last).toHaveFocus();
  fireEvent.keyDown(last, { key: "Tab" });
  expect(first).toHaveFocus();
});
