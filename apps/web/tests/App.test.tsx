import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { App } from "../src/App";

function response(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("App", () => {
  it("names the product and explains the private account boundary", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(response(401, { message: "Sign in" })),
    );
    render(<App />);
    expect(screen.getByRole("heading", { name: "RateReplay" })).toBeVisible();
    expect(
      await screen.findByRole("heading", { name: "Private local account" }),
    ).toBeVisible();
    expect(screen.getByText(/no password recovery/i)).toBeVisible();
    expect(
      screen.queryByLabelText(/utility password/i),
    ).not.toBeInTheDocument();
  });

  it("uses the public auth and upload workflow to render a quality report", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(response(401, { message: "Sign in" }))
      .mockResolvedValueOnce(
        response(201, {
          user: { user_id: "owner", username: "owner_one" },
          csrf_token: "csrf-token",
        }),
      )
      .mockResolvedValueOnce(
        response(202, {
          import_id: "import-one",
          state_url: "/v1/imports/import-one",
        }),
      )
      .mockResolvedValueOnce(
        response(200, {
          import_id: "import-one",
          state: "READY",
          job_state: "SUCCEEDED",
          reading_count: 362,
          interval_resolution_seconds: 3600,
          coverage_start_utc_ns: 1,
          coverage_end_utc_ns: 2,
          findings: [
            {
              code: "INTERVAL_GAP",
              severity: "WARNING",
              field_path: "readings",
              warning_id: "warning-one",
            },
          ],
          failure_code: null,
        }),
      )
      .mockResolvedValueOnce(response(200, { profile_version_id: "profile" }))
      .mockResolvedValueOnce(
        response(200, {
          import_id: "import-one",
          state: "CONFIRMED",
          job_state: "SUCCEEDED",
          reading_count: 362,
          interval_resolution_seconds: 3600,
          coverage_start_utc_ns: 1,
          coverage_end_utc_ns: 2,
          findings: [],
          failure_code: null,
        }),
      );
    vi.stubGlobal("fetch", fetchMock);
    vi.stubGlobal("crypto", { randomUUID: () => "request-id" });
    render(<App />);

    fireEvent.change(await screen.findByLabelText("Username"), {
      target: { value: "owner_one" },
    });
    fireEvent.change(screen.getByLabelText("Password"), {
      target: { value: "correct horse battery staple" },
    });
    fireEvent.click(
      screen.getByRole("button", { name: "Create private account" }),
    );

    const fileInput = await screen.findByLabelText(
      /choose one downloaded usage file/i,
    );
    const file = new File(["<feed />"], "usage.xml", {
      type: "application/xml",
    });
    fireEvent.change(fileInput, { target: { files: [file] } });
    const uploadForm = fileInput.closest("form");
    expect(uploadForm).not.toBeNull();
    fireEvent.submit(uploadForm as HTMLFormElement);

    expect(await screen.findByText("362")).toBeVisible();
    const confirm = screen.getByRole("button", {
      name: "Confirm complete period",
    });
    expect(confirm).toBeDisabled();
    fireEvent.click(screen.getByLabelText("Acknowledge INTERVAL_GAP"));
    expect(confirm).toBeDisabled();
    fireEvent.click(screen.getByLabelText(/I confirm this is my PG&E/i));
    expect(confirm).toBeEnabled();
    fireEvent.click(confirm);
    expect(
      await screen.findByText(/raw upload has entered immediate deletion/i),
    ).toBeVisible();
    const uploadCall = fetchMock.mock.calls[2] as [string, RequestInit];
    expect(uploadCall[0]).toBe("/v1/imports");
    expect(uploadCall[1].method).toBe("POST");
    expect(uploadCall[1].body).toBeInstanceOf(FormData);
    expect(
      (uploadCall[1].headers as Record<string, string>)["X-CSRF-Token"],
    ).toBe("csrf-token");
    const confirmationCall = fetchMock.mock.calls[4] as [string, RequestInit];
    expect(JSON.parse(confirmationCall[1].body as string)).toMatchObject({
      acknowledged_warning_ids: ["warning-one"],
      pge_service_attested: true,
    });
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(6));
  });
});
