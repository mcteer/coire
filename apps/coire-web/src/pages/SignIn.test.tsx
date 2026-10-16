import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { SignIn } from "./SignIn";

afterEach(() => vi.restoreAllMocks());

test("signs in without storing credentials and uses password-manager fields", async () => {
  const user = { id: "owner", email: "owner@example.test" };
  const fetcher = vi
    .spyOn(globalThis, "fetch")
    .mockResolvedValueOnce(new Response(JSON.stringify({ enabled: true, access_enabled: false })))
    .mockResolvedValueOnce(
      new Response(JSON.stringify({ user, expires_at: "2026-10-16T00:00:00Z" })),
    );
  const signedIn = vi.fn();
  render(<SignIn onSignedIn={signedIn} />);
  const email = await screen.findByLabelText("Email");
  const password = screen.getByLabelText("Password");
  expect(email).toHaveAttribute("autocomplete", "username");
  expect(password).toHaveAttribute("autocomplete", "current-password");
  fireEvent.change(email, { target: { value: "owner@example.test" } });
  fireEvent.change(password, { target: { value: "private password for test" } });
  fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
  await waitFor(() => expect(signedIn).toHaveBeenCalledWith(user));
  expect(fetcher).toHaveBeenLastCalledWith(
    "/api/v1/auth/local/login",
    expect.objectContaining({ method: "POST" }),
  );
  expect(password).toHaveValue("");
});

test("shows refusal and permits retry", async () => {
  vi.spyOn(globalThis, "fetch")
    .mockResolvedValueOnce(new Response(JSON.stringify({ enabled: true, access_enabled: false })))
    .mockResolvedValueOnce(
      new Response(JSON.stringify({ detail: "email or password is incorrect" }), { status: 401 }),
    );
  render(<SignIn onSignedIn={vi.fn()} />);
  fireEvent.change(await screen.findByLabelText("Email"), {
    target: { value: "owner@example.test" },
  });
  fireEvent.change(screen.getByLabelText("Password"), {
    target: { value: "private password for test" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("email or password is incorrect");
  expect(screen.getByRole("button", { name: "Sign in" })).toBeEnabled();
});

test("explains an unconfigured deployment", async () => {
  vi.spyOn(globalThis, "fetch").mockResolvedValue(
    new Response(JSON.stringify({ enabled: false, access_enabled: false })),
  );
  render(<SignIn onSignedIn={vi.fn()} />);
  expect(await screen.findByText(/Local sign-in has not been configured/)).toBeInTheDocument();
  expect(screen.queryByLabelText("Password")).not.toBeInTheDocument();
});
