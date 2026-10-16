import { useEffect, useRef, useState } from "react";
import type { useTheme } from "../hooks/useTheme";
import {
  colorsError,
  validHex,
  type Appearance,
  type ColorTheme,
  type ThemeColors,
} from "../theme/colorThemes";
import { ConfirmAction } from "./ConfirmAction";
import { Icon } from "./Icons";
import "../styles/color-themes.css";

type Settings = ReturnType<typeof useTheme>;
const fields: [keyof ThemeColors, string][] = [
  ["page", "Page"],
  ["surface", "Panels"],
  ["sidebar", "Sidebar"],
  ["accent", "Accent"],
];

export function ColorThemePicker({ settings }: { settings: Settings }) {
  const [open, setOpen] = useState(false);
  const [draft, setDraft] = useState<ColorTheme | null>(null);
  const [error, setError] = useState("");
  const dialog = useRef<HTMLDialogElement>(null);
  const opener = useRef<HTMLButtonElement>(null);
  const nameInput = useRef<HTMLInputElement>(null);
  const createButton = useRef<HTMLButtonElement>(null);
  const editing = draft !== null;
  const { previewPalette } = settings;
  const draftError = draft ? colorsError(draft.light) || colorsError(draft.dark) : null;
  useEffect(() => {
    const element = dialog.current;
    if (!element) return;
    if (open && !element.open) {
      if (element.showModal) element.showModal();
      else element.setAttribute("open", "");
      element.querySelector<HTMLButtonElement>("button")?.focus();
    } else if (!open && element.open) {
      if (element.close) element.close();
      else element.removeAttribute("open");
      opener.current?.focus();
    }
  }, [open]);
  useEffect(() => {
    previewPalette(
      draft && !draftError ? { ...draft, name: draft.name.trim() || "Preview" } : null,
    );
  }, [draft, draftError, previewPalette]);
  useEffect(() => {
    if (editing) nameInput.current?.focus();
    else if (dialog.current?.open) createButton.current?.focus();
  }, [editing]);
  const close = () => {
    setDraft(null);
    setError("");
    previewPalette(null);
    setOpen(false);
  };
  const edit = (create: boolean) => {
    const current = settings.selectedPalette;
    setDraft({
      id: create ? `custom-${crypto.randomUUID()}` : current.id,
      name: create ? `My ${current.name}` : current.name,
      light: { ...current.light },
      dark: { ...current.dark },
    });
    setError("");
  };
  const updateColor = (appearance: Appearance, field: keyof ThemeColors, value: string) => {
    setDraft((current) =>
      current ? { ...current, [appearance]: { ...current[appearance], [field]: value } } : null,
    );
    setError("");
  };
  return (
    <>
      <button
        ref={opener}
        className="icon-button color-theme-toggle"
        type="button"
        aria-label="Color themes"
        aria-haspopup="dialog"
        onClick={() => setOpen(true)}
      >
        <Icon name="palette" />
      </button>
      <dialog
        ref={dialog}
        className="color-theme-dialog"
        aria-labelledby="color-themes-title"
        onKeyDown={(event) => {
          if (event.key !== "Tab" || event.ctrlKey || event.metaKey || event.altKey) return;
          const items = [
            ...event.currentTarget.querySelectorAll<HTMLElement>(
              "button:not([disabled]),input:not([disabled]),select:not([disabled]),textarea:not([disabled]),a[href]",
            ),
          ];
          if (!items.length) return;
          event.preventDefault();
          const index = items.findIndex((item) => item === document.activeElement);
          const next =
            index < 0
              ? event.shiftKey
                ? items.length - 1
                : 0
              : (index + (event.shiftKey ? -1 : 1) + items.length) % items.length;
          items[next]?.focus();
        }}
        onCancel={(event) => {
          event.preventDefault();
          close();
        }}
      >
        <header className="color-theme-header">
          <div>
            <h2 id="color-themes-title">Color themes</h2>
            <p>Choose a palette or make it your own.</p>
          </div>
          <button
            className="icon-button"
            type="button"
            aria-label="Close color themes"
            onClick={close}
          >
            <Icon name="close" />
          </button>
        </header>
        <div className="color-theme-appearance" role="group" aria-label="Theme appearance">
          <span>Appearance</span>
          <button className="button secondary" type="button" onClick={settings.toggleTheme}>
            {settings.theme === "light" ? "Switch to dark theme" : "Switch to light theme"}
          </button>
        </div>
        {!settings.preferencesSaved && (
          <p role="status" className="color-theme-storage-note">
            Changes apply now, but this browser is blocking saved preferences.
          </p>
        )}
        {draft ? (
          <form
            className="color-theme-editor"
            onSubmit={(event) => {
              event.preventDefault();
              if (draftError) return;
              const failure = settings.saveCustomTheme(draft);
              if (failure) setError(failure);
              else {
                setDraft(null);
                setError("");
              }
            }}
          >
            <h3>
              {settings.colorThemes.some((t) => t.id === draft.id)
                ? "Edit custom theme"
                : "New custom theme"}
            </h3>
            <label className="field">
              Theme name
              <input
                ref={nameInput}
                aria-label="Theme name"
                required
                maxLength={40}
                value={draft.name}
                onChange={(event) => {
                  setDraft((current) =>
                    current ? { ...current, name: event.target.value } : null,
                  );
                  setError("");
                }}
              />
            </label>
            <div className="color-theme-editor-modes">
              {(["light", "dark"] as const).map((appearance) => (
                <fieldset key={appearance}>
                  <legend>{appearance === "light" ? "Light colors" : "Dark colors"}</legend>
                  {fields.map(([field, label]) => (
                    <div className="color-theme-field" key={field}>
                      <label htmlFor={`theme-${appearance}-${field}`}>{label}</label>
                      <div>
                        <input
                          type="color"
                          aria-label={`${appearance === "light" ? "Light" : "Dark"} ${field} color`}
                          value={
                            validHex(draft[appearance][field])
                              ? draft[appearance][field]
                              : "#000000"
                          }
                          onChange={(event) => updateColor(appearance, field, event.target.value)}
                        />
                        <input
                          id={`theme-${appearance}-${field}`}
                          aria-label={`${appearance === "light" ? "Light" : "Dark"} ${field} hex`}
                          value={draft[appearance][field]}
                          maxLength={7}
                          pattern="#[0-9a-fA-F]{6}"
                          required
                          spellCheck={false}
                          onChange={(event) => updateColor(appearance, field, event.target.value)}
                        />
                      </div>
                    </div>
                  ))}
                </fieldset>
              ))}
            </div>
            <p className="muted">
              Preview is live. Text colors adjust automatically for readability.
            </p>
            {(draftError || error) && (
              <p className="error" role="alert">
                {draftError || error}
              </p>
            )}
            <div className="actions">
              <button
                className="button"
                type="submit"
                disabled={!!draftError || !draft.name.trim()}
              >
                Save theme
              </button>
              <button
                className="button secondary"
                type="button"
                onClick={() => {
                  setDraft(null);
                  setError("");
                }}
              >
                Cancel preview
              </button>
            </div>
          </form>
        ) : (
          <>
            <div className="color-theme-presets" role="group" aria-label="Color palettes">
              {settings.colorThemes.map((palette) => (
                <button
                  key={palette.id}
                  className="color-theme-card"
                  type="button"
                  aria-label={`Use ${palette.name} theme`}
                  aria-pressed={settings.selectedPalette.id === palette.id}
                  onClick={() => settings.selectPalette(palette.id)}
                >
                  <span className="color-theme-swatches" aria-hidden="true">
                    {fields.map(([field]) => (
                      <i key={field} style={{ backgroundColor: palette[settings.theme][field] }} />
                    ))}
                  </span>
                  <span>{palette.name}</span>
                  <small>
                    {palette.id.startsWith("custom-") ? "Custom" : "Preset"}
                    {settings.selectedPalette.id === palette.id ? " · Selected" : ""}
                  </small>
                </button>
              ))}
            </div>
            <div className="actions color-theme-actions">
              <button
                ref={createButton}
                className="button"
                type="button"
                onClick={() => edit(true)}
              >
                Create custom theme
              </button>
              {settings.selectedPalette.id.startsWith("custom-") && (
                <>
                  <button className="button secondary" type="button" onClick={() => edit(false)}>
                    Edit theme
                  </button>
                  <ConfirmAction
                    target={settings.selectedPalette.name}
                    label="Delete theme"
                    ariaLabel="Delete selected theme"
                    onConfirm={async () => settings.deleteCustomTheme(settings.selectedPalette.id)}
                  />
                </>
              )}
              <button
                className="button secondary"
                type="button"
                onClick={() => settings.selectPalette("original")}
              >
                Reset colors
              </button>
            </div>
          </>
        )}
        <footer>
          Saved in this browser. Colors apply across Coire; the layout stays the same.
        </footer>
      </dialog>
    </>
  );
}
