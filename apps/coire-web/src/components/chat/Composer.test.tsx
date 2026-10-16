import { fireEvent, render, screen } from "@testing-library/react";
import { expect, test, vi } from "vitest";
import { Composer } from "./Composer";

test("keeps the draft through cold, queued, failure and retry-ready states", () => {
  const onChange = vi.fn();
  const onSend = vi.fn();
  const props = { value: "Explain this", onChange, onSend };
  const view = render(
    <Composer {...props} disabled status="Warming up the model · estimate unavailable" />,
  );
  const input = screen.getByRole("textbox", { name: "Message" });
  expect(input).toHaveValue("Explain this");
  expect(screen.getByRole("status")).toHaveTextContent("estimate unavailable");
  expect(screen.getByRole("button", { name: "Send" })).toBeDisabled();

  view.rerender(<Composer {...props} disabled status="Waiting for capacity…" />);
  expect(screen.getByRole("status")).toHaveTextContent("Waiting for capacity");
  expect(input).toHaveValue("Explain this");

  view.rerender(<Composer {...props} disabled status="Generation failed; retry this turn" />);
  expect(screen.getByRole("status")).toHaveTextContent("Generation failed");
  expect(onSend).not.toHaveBeenCalled();

  view.rerender(<Composer {...props} disabled={false} status={null} />);
  fireEvent.keyDown(input, { key: "Enter", code: "Enter" });
  expect(onSend).toHaveBeenCalledTimes(1);
});

test("Shift+Enter remains available for multiline drafts", () => {
  const onSend = vi.fn();
  render(
    <Composer value="Line one" onChange={vi.fn()} onSend={onSend} disabled={false} status={null} />,
  );
  fireEvent.keyDown(screen.getByRole("textbox", { name: "Message" }), {
    key: "Enter",
    code: "Enter",
    shiftKey: true,
  });
  expect(onSend).not.toHaveBeenCalled();
});

test("contains attachment upload, preserves the draft while active and replaces Send with Stop", () => {
  const stop = vi.fn(),
    upload = vi.fn(),
    send = vi.fn(),
    change = vi.fn();
  const view = render(
    <Composer
      value="Next question"
      onChange={change}
      onSend={send}
      disabled
      active
      status="Generating…"
      onStop={stop}
      onUpload={upload}
      files={<span>notes.txt</span>}
    />,
  );
  const form = screen.getByRole("form", { name: "Message composer" });
  expect(screen.getByText("notes.txt").closest("form")).toBe(form);
  expect(screen.getByRole("button", { name: "Attach files" }).closest("form")).toBe(form);
  expect(screen.queryByRole("button", { name: "Send" })).not.toBeInTheDocument();
  fireEvent.change(screen.getByRole("textbox", { name: "Message" }), {
    target: { value: "Editable while generating" },
  });
  expect(change).toHaveBeenCalledWith("Editable while generating");
  fireEvent.keyDown(screen.getByRole("textbox", { name: "Message" }), { key: "Enter" });
  expect(send).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "Stop response" }));
  expect(stop).toHaveBeenCalledOnce();
  view.rerender(
    <Composer
      value="Next question"
      onChange={change}
      onSend={send}
      disabled={false}
      status={null}
      onUpload={upload}
    />,
  );
  const file = new File(["hello"], "notes.txt", { type: "text/plain" });
  fireEvent.change(screen.getByLabelText("Add file"), { target: { files: [file] } });
  expect(upload).toHaveBeenCalledWith(file);
});

test("does not submit an IME composition", () => {
  const send = vi.fn();
  render(<Composer value="入力" onChange={vi.fn()} onSend={send} disabled={false} status={null} />);
  fireEvent.keyDown(screen.getByRole("textbox", { name: "Message" }), {
    key: "Enter",
    isComposing: true,
  });
  expect(send).not.toHaveBeenCalled();
});

test("accepts a dropped or pasted file inside the composer and rejects multiple files without silently dropping them", () => {
  const upload = vi.fn();
  render(
    <Composer
      value=""
      onChange={vi.fn()}
      onSend={vi.fn()}
      disabled={false}
      status={null}
      onUpload={upload}
    />,
  );
  const form = screen.getByRole("form", { name: "Message composer" });
  const file = new File(["hello"], "notes.txt", { type: "text/plain" });
  fireEvent.drop(form, { dataTransfer: { files: [file] } });
  expect(upload).toHaveBeenCalledWith(file);
  fireEvent.paste(screen.getByRole("textbox", { name: "Message" }), {
    clipboardData: { files: [file] },
  });
  expect(upload).toHaveBeenCalledTimes(2);
  fireEvent.drop(form, { dataTransfer: { files: [file, file] } });
  expect(screen.getByRole("alert")).toHaveTextContent("Attach one file at a time");
  expect(upload).toHaveBeenCalledTimes(2);
});
