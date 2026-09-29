import { fireEvent, render, screen } from "@testing-library/react";
import { expect, test, vi } from "vitest";
import type { ChatPickerEntry } from "../../api/chat";
import { ModelPicker } from "./ModelPicker";

const model: ChatPickerEntry = {
  id: "00000000-0000-0000-0000-000000000001",
  display_name: "Helpful model",
  description: "Answers everyday questions",
  tags: ["general"],
  context_window: 32768,
  size_class: "medium",
  load_state: "cold",
  estimated_warmup_seconds: null,
  verified: true,
  accepts_images: false,
};

test("shows an actionable empty state", () => {
  render(<ModelPicker models={[]} selectedId={null} onSelect={() => {}} />);
  expect(screen.getByText(/No chat models are available/)).toBeInTheDocument();
});

test("shows task group, metadata and honest unknown warm-up", () => {
  const onSelect = vi.fn();
  render(<ModelPicker models={[model]} selectedId={null} onSelect={onSelect} />);
  expect(screen.getByRole("heading", { name: "General" })).toBeInTheDocument();
  expect(screen.getByText("Answers everyday questions")).toBeInTheDocument();
  expect(screen.getByText(/33k context/)).toBeInTheDocument();
  expect(screen.getByText(/warm-up estimate unavailable/)).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: /Helpful model/ }));
  expect(onSelect).toHaveBeenCalledWith(model.id);
});

test("shows a measured warm-up estimate before selection", () => {
  render(
    <ModelPicker
      models={[{ ...model, estimated_warmup_seconds: 32.2 }]}
      selectedId={null}
      onSelect={() => {}}
    />,
  );
  expect(screen.getByText(/about 33 s warm-up/)).toBeInTheDocument();
});
