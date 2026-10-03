import type { ImagePresetList } from "../../api/images";

export function PresetRail({
  presets,
  selectedId,
  onSelect,
}: {
  presets: ImagePresetList["items"];
  selectedId: string | null;
  onSelect: (presetId: string | null) => void;
}) {
  if (presets.length === 0) return null;
  return (
    <div role="listbox" aria-label="Image presets">
      <button
        className="button"
        type="button"
        aria-selected={selectedId === null}
        onClick={() => onSelect(null)}
      >
        No preset
      </button>
      {presets.map((preset) => (
        <button
          key={`${preset.id}:${preset.revision}`}
          className="button"
          type="button"
          role="option"
          aria-selected={selectedId === preset.id}
          onClick={() => onSelect(preset.id)}
        >
          {preset.name}
        </button>
      ))}
    </div>
  );
}
