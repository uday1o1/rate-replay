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

const profile = {
  profile_version_id: "profile-one",
  content_hash: "d".repeat(64),
  billing_period_start_utc_ns: 1,
  billing_period_end_utc_ns: 2,
};

const tariffDetail = {
  admission: {
    tariff_version_id: "pge-e1-2026-07",
    plan_code: "E-1",
    admitted_service_windows: [["2026-07-01", "2026-08-01"]],
    compiler_content_sha256: "a".repeat(64),
    scope: {
      calculation_time_mode: "HISTORICAL_REPLAY",
      comparison_admitted: false,
      optimization_admitted: false,
    },
  },
  compilation: {
    reports: {
      source_coverage: [
        {
          source_id: "pge-advice-7921-e",
          source_sha256: "b".repeat(64),
          source_url: "https://example.test/pge-advice-7921-e.pdf",
          linked_rule_ids: ["E1_TOTAL_ENERGY_2026_06_01"],
        },
      ],
    },
  },
};

const replayResource = {
  replay_id: "replay-one",
  repeated: false,
  result: {
    eligibility: { status: "ELIGIBLE", reason_codes: [] },
    supported_calculated_cents: 9819,
    line_items: [
      {
        rule_id: "E1_TOTAL_ENERGY_2026_06_01",
        source_id: "pge-advice-7921-e",
        line_item_key: "bundled_energy.tier_1",
        quantity_numerator: 201500,
        quantity_denominator: 1,
        quantity_unit: "Wh",
        rate_numerator_microdollars: 325610,
        rate_denominator: 1,
        rate_unit: "microdollars/kWh",
        rounded_cents: 6561,
      },
    ],
    user_unsupported_lines: [
      {
        line_item_key: "user_entered_other_1",
        description: "Local tax",
        amount_cents: 200,
      },
    ],
    reconciliation: {
      user_unsupported_cents: 200,
      unexplained_residual_cents: 981,
      entered_bill_total_cents: 11000,
      classification: "REVIEW_REQUIRED",
    },
    provenance_sources: tariffDetail.compilation.reports.source_coverage,
    manifest: {
      calculation_time_mode: "HISTORICAL_REPLAY",
      tariff_compiler_content_sha256: "a".repeat(64),
      replay_input_sha256: "c".repeat(64),
      reconciliation_input_sha256: "d".repeat(64),
      reconciliation_policy_sha256: "e".repeat(64),
      calculation_sha256: "f".repeat(64),
    },
    result_sha256: "1".repeat(64),
  },
};

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
      .mockResolvedValueOnce(response(200, { items: [] }))
      .mockResolvedValueOnce(response(200, tariffDetail))
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
      .mockResolvedValueOnce(response(200, profile))
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
    const uploadCall = fetchMock.mock.calls[4] as [string, RequestInit];
    expect(uploadCall[0]).toBe("/v1/imports");
    expect(uploadCall[1].method).toBe("POST");
    expect(uploadCall[1].body).toBeInstanceOf(FormData);
    expect(
      (uploadCall[1].headers as Record<string, string>)["X-CSRF-Token"],
    ).toBe("csrf-token");
    const confirmationCall = fetchMock.mock.calls[6] as [string, RequestInit];
    expect(JSON.parse(confirmationCall[1].body as string)).toMatchObject({
      acknowledged_warning_ids: ["warning-one"],
      pge_service_attested: true,
    });
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(8));
  });

  it("renders an auditable E-1 replay without recommendation language", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(response(401, { message: "Sign in" }))
      .mockResolvedValueOnce(
        response(201, {
          user: { user_id: "owner", username: "owner_one" },
          csrf_token: "csrf-token",
        }),
      )
      .mockResolvedValueOnce(response(200, { items: [profile] }))
      .mockResolvedValueOnce(response(200, tariffDetail))
      .mockResolvedValueOnce(response(201, replayResource));
    vi.stubGlobal("fetch", fetchMock);
    vi.stubGlobal("crypto", { randomUUID: () => "replay-request-id" });
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

    expect(
      await screen.findByText(/E-1 is admitted only for historical replay/i),
    ).toBeVisible();
    fireEvent.change(screen.getByLabelText(/Current bill total in dollars/i), {
      target: { value: "110.00" },
    });
    fireEvent.change(screen.getByLabelText(/Unsupported line description/i), {
      target: { value: "Local tax" },
    });
    fireEvent.change(screen.getByLabelText(/Unsupported line amount/i), {
      target: { value: "2.00" },
    });
    fireEvent.click(
      screen.getByLabelText(/I attest that every locked account fact/i),
    );
    fireEvent.click(
      screen.getByRole("button", { name: "Create historical replay" }),
    );

    expect(await screen.findByText("$98.19")).toBeVisible();
    expect(screen.getByText("$9.81")).toBeVisible();
    expect(
      screen.getByText(/User-entered unsupported: Local tax/i),
    ).toBeVisible();
    expect(
      screen.getByText(/does not move it into a supported charge/i),
    ).toBeVisible();
    expect(screen.queryByText(/recommended plan/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/savings/i)).not.toBeInTheDocument();
    const replayCall = fetchMock.mock.calls[4] as [string, RequestInit];
    expect(replayCall[0]).toBe("/v1/replays");
    const replayBody = JSON.parse(replayCall[1].body as string) as {
      current_bill_total_cents: number;
      user_unsupported_lines: { amount_cents: number }[];
      account_facts: { income_tier: string; baseline_territory: string };
    };
    expect(replayBody.current_bill_total_cents).toBe(11000);
    expect(replayBody.user_unsupported_lines[0]?.amount_cents).toBe(200);
    expect(replayBody.account_facts).toMatchObject({
      income_tier: "TIER_3",
      baseline_territory: "T",
    });
  });
});
