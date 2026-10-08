import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { CredentialCaptureCard } from "@src/features/security/CredentialCaptureCard";


describe("CredentialCaptureCard", () => {
  afterEach(cleanup);

  it("submits through the narrow Electron bridge and clears the DOM value", async () => {
    const captureCredential = vi.fn().mockResolvedValue({
      stored: true, secret_ref: "provider:deepseek", generation: 2, authorization_revision: 2,
    });
    const onStored = vi.fn();
    render(<CredentialCaptureCard credentialKind="provider_api_key" credentialSubject="deepseek"
      label="API Key" bridge={{ captureCredential }} onStored={onStored} />);

    const input = screen.getByLabelText("API Key");
    fireEvent.change(input, { target: { value: "sensitive-local-value" } });
    fireEvent.click(screen.getByRole("button", { name: "安全保存" }));

    await waitFor(() => expect(captureCredential).toHaveBeenCalledTimes(1));
    expect(captureCredential.mock.calls[0].slice(0, 3)).toEqual([
      "provider_api_key", "deepseek", "sensitive-local-value",
    ]);
    await waitFor(() => expect(input).toHaveValue(""));
    expect(screen.queryByText("sensitive-local-value")).not.toBeInTheDocument();
    expect(onStored).toHaveBeenCalledWith(expect.objectContaining({ secret_ref: "provider:deepseek" }));
  });

  it("does not expose an input outside the Electron capture boundary", () => {
    render(<CredentialCaptureCard credentialKind="provider_api_key" credentialSubject="deepseek" label="API Key" bridge={{}} />);
    expect(screen.queryByLabelText("API Key")).not.toBeInTheDocument();
    expect(screen.getByText(/只能在 Chriptmas OS 桌面应用/)).toBeInTheDocument();
  });
});
