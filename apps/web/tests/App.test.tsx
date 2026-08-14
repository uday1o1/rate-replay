import {
  cleanup,
  fireEvent,
  render,
  screen,
  within,
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
  import_id: "import-one",
  profile_version_id: "profile-one",
  content_hash: "d".repeat(64),
  billing_period_start_utc_ns: 1,
  billing_period_end_utc_ns: 2,
};

const builtInProfile = {
  schema_version: "built-in-simulated-import-v1",
  simulated: true,
  label: "SIMULATED NREL-derived California household",
  source_artifact_sha256: "4".repeat(64),
  repeated: false,
  profile,
};

const tariffDetail = {
  admission: {
    tariff_version_id: "pge-e1-2026-07",
    plan_code: "E-1",
    admitted_service_windows: [["2026-07-01", "2026-08-01"]],
    compiler_content_sha256: "a".repeat(64),
    scope: {
      calculation_time_mode: "HISTORICAL_REPLAY",
      comparison_admitted: true,
      optimization_admitted: true,
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

const tariffList = {
  items: [
    ["pge-e1-2026-07", "E-1"],
    ["pge-eelec-2026-07", "E-ELEC"],
    ["pge-etouc-2026-07", "E-TOU-C"],
    ["pge-etoud-2026-07", "E-TOU-D"],
    ["pge-ev2a-2026-07", "EV2-A"],
  ].map(([tariff_version_id, plan_code]) => ({
    tariff_version_id,
    plan_code,
    admission_status: "ADMITTED",
    comparison_admitted: true,
    optimization_admitted: true,
  })),
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

const supportedCoverage = [
  {
    component_key: "base_services_charge",
    status: "SUPPORTED",
    reason_code: null,
    contributing_rule_ids: ["FIXED_CHARGE"],
  },
  {
    component_key: "bundled_energy",
    status: "SUPPORTED",
    reason_code: null,
    contributing_rule_ids: ["ENERGY_CHARGE"],
  },
];

function comparisonCandidate(
  tariff_version_id: string,
  plan_code: string,
  supported_calculated_cents: number,
) {
  return {
    tariff_version_id,
    plan_code,
    eligibility: { status: "ELIGIBLE", reason_codes: [] },
    component_coverage: supportedCoverage,
    alternative_plan: {
      supported_calculated_cents,
      component_coverage: supportedCoverage,
      provenance_sources: tariffDetail.compilation.reports.source_coverage,
      result_sha256: `${plan_code.charCodeAt(0)}`.repeat(64).slice(0, 64),
    },
  };
}

const comparisonCandidates = [
  comparisonCandidate("pge-e1-2026-07", "E-1", 27728),
  comparisonCandidate("pge-eelec-2026-07", "E-ELEC", 30278),
  comparisonCandidate("pge-etouc-2026-07", "E-TOU-C", 30253),
  comparisonCandidate("pge-etoud-2026-07", "E-TOU-D", 26021),
  comparisonCandidate("pge-ev2a-2026-07", "EV2-A", 26890),
];

const rankableComparison = {
  comparison_id: "comparison-one",
  repeated: false,
  result: {
    candidates: comparisonCandidates,
    exclusions: [],
    required_component_keys: ["base_services_charge", "bundled_energy"],
    common_supported_component_keys: ["base_services_charge", "bundled_energy"],
    rankable: true,
    ranked_tariff_version_ids: [
      "pge-etoud-2026-07",
      "pge-ev2a-2026-07",
      "pge-e1-2026-07",
      "pge-etouc-2026-07",
      "pge-eelec-2026-07",
    ],
    winner_tariff_version_ids: ["pge-etoud-2026-07"],
    savings_against_current_supported_cents: 1707,
    comparison_sha256: "9".repeat(64),
  },
};

const blockedComparison = {
  ...rankableComparison,
  comparison_id: "comparison-blocked",
  result: {
    ...rankableComparison.result,
    candidates: comparisonCandidates.map((candidate) =>
      candidate.tariff_version_id === "pge-ev2a-2026-07"
        ? {
            ...candidate,
            eligibility: {
              status: "UNKNOWN",
              reason_codes: ["ANNUAL_USAGE_REQUIRED"],
            },
            alternative_plan: null,
          }
        : candidate,
    ),
    exclusions: [
      {
        code: "CANDIDATE_ELIGIBILITY_UNKNOWN",
        tariff_version_id: "pge-ev2a-2026-07",
        component_key: null,
        eligibility_reason_codes: ["ANNUAL_USAGE_REQUIRED"],
      },
    ],
    rankable: false,
    ranked_tariff_version_ids: [],
    winner_tariff_version_ids: [],
    savings_against_current_supported_cents: null,
    comparison_sha256: "8".repeat(64),
  },
};

const scenarioSlots = Array.from({ length: 8 }, (_, index) => ({
  slot_start_utc: new Date(Date.UTC(2026, 6, 7, index)).toISOString(),
  duration_seconds: 3600,
  measured_energy_wh: 500,
}));

const profileScenarioSlots = {
  schema_version: "profile-scenario-slots-v1",
  profile_version_id: "profile-one",
  profile_content_sha256: "d".repeat(64),
  calculation_time_mode: "HISTORICAL_REPLAY",
  energy_basis: "METER_SIDE",
  slots: scenarioSlots,
};

function resultEnergySlots(values: number[]) {
  return scenarioSlots.map((slot, index) => ({
    slot_start_utc: slot.slot_start_utc,
    duration_seconds: slot.duration_seconds,
    energy_wh: values[index] ?? 0,
  }));
}

function verifiedSchedule(values: number[], supportedCostCents: number) {
  return {
    schedule: {
      occurrences: [
        {
          occurrence_id: "scenario-uuid",
          slots: resultEnergySlots(values),
        },
      ],
    },
    verification: {
      status: "VALID",
      objective: {
        supported_cost_cents: supportedCostCents,
        changed_occurrence_slot_count: 2,
        completion_slot_index_sum: values.reduce(
          (last, value, index) => (value > 0 ? index + 1 : last),
          0,
        ),
        stable_slot_order_score: values.reduce(
          (total, value, index) => total + value * (index + 1),
          0,
        ),
      },
      verification_sha256: `${supportedCostCents}`.padEnd(64, "0").slice(0, 64),
    },
    billing_result: {
      supported_calculated_cents: supportedCostCents,
      result_sha256: `${supportedCostCents}`.padEnd(64, "1").slice(0, 64),
    },
  };
}

const scenarioReference = [0, 0, 0, 7200, 0, 0, 0, 0];
const scenarioSelected = [7200, 0, 0, 0, 0, 0, 0, 0];
const referenceVerified = verifiedSchedule(scenarioReference, 2500);
const selectedVerified = verifiedSchedule(scenarioSelected, 2000);

const optimalScenario = {
  scenario_id: "scenario-one",
  state: "SUCCEEDED",
  repeated: false,
  result: {
    calculation_time_mode: "HISTORICAL_REPLAY",
    historical_addition_label: "HISTORICAL_COUNTERFACTUAL_NOT_FORECAST",
    reference_validation: {
      status: "VALID",
      load_count: 1,
      occurrence_count: 1,
      slot_count: 8,
    },
    decomposition: {
      fixed_background: resultEnergySlots(Array<number>(8).fill(500)),
      shift_existing_reference: resultEnergySlots(Array<number>(8).fill(0)),
      historical_addition_reference: resultEnergySlots(scenarioReference),
      reconstructed_measured_profile: resultEnergySlots(
        Array<number>(8).fill(500),
      ),
      unchanged_reference_profile: resultEnergySlots(
        scenarioReference.map((value) => value + 500),
      ),
      exact_measured_reconstruction: true,
    },
    exact: {
      search_status: "OPTIMAL",
      selected_source: "SOLVER_INCUMBENT",
      selection_reason: "INCUMBENT_STRICTLY_BETTER",
      selected: selectedVerified,
      reference: referenceVerified,
      highest_objective_stage_proved_optimal: 4,
      first_open_stage: null,
      best_supported_cost_bound: 2000,
      absolute_cost_gap_cents: 0,
      relative_cost_gap: 0,
    },
    heuristic: {
      search_status: "HEURISTIC_PROXY_OPTIMAL",
      selection_outcome: "HEURISTIC_INCUMBENT_SELECTED",
      bill_optimality_claim: false,
      selected: selectedVerified,
      fallback_reason: null,
    },
    manifest: {
      calculation_sha256: "2".repeat(64),
      solver_name: "OR-Tools CP-SAT",
      solver_version: "9.15.6755",
      rank_calendar_sha256: "3".repeat(64),
      selected_verification_sha256:
        selectedVerified.verification.verification_sha256,
      warning_codes: [],
    },
    result_sha256: "4".repeat(64),
  },
};

const bestFoundScenario = {
  ...optimalScenario,
  scenario_id: "scenario-best-found",
  result: {
    ...optimalScenario.result,
    exact: {
      ...optimalScenario.result.exact,
      search_status: "BEST_FOUND",
      highest_objective_stage_proved_optimal: 0,
      first_open_stage: 1,
      best_supported_cost_bound: 1999,
      absolute_cost_gap_cents: 1,
      relative_cost_gap: 0.0005,
    },
    manifest: {
      ...optimalScenario.result.manifest,
      warning_codes: ["EXACT_BEST_FOUND_OPEN_BOUND"],
    },
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
    let importReadCount = 0;
    const fetchMock = vi.fn(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        await Promise.resolve();
        const path =
          typeof input === "string"
            ? input
            : input instanceof URL
              ? input.toString()
              : input.url;
        const method = init?.method ?? "GET";
        if (path === "/v1/auth/session" && method === "GET") {
          return response(401, { message: "Sign in" });
        }
        if (path === "/v1/auth/register" && method === "POST") {
          return response(201, {
            user: { user_id: "owner", username: "owner_one" },
            csrf_token: "csrf-token",
          });
        }
        if (path === "/v1/profiles?page_size=1" && method === "GET") {
          return response(200, { items: [] });
        }
        if (path === "/v1/tariffs" && method === "GET") {
          return response(200, tariffList);
        }
        if (path === "/v1/tariffs/pge-e1-2026-07" && method === "GET") {
          return response(200, tariffDetail);
        }
        if (path === "/v1/imports" && method === "POST") {
          return response(202, {
            import_id: "import-one",
            state_url: "/v1/imports/import-one",
          });
        }
        if (path === "/v1/imports/import-one" && method === "GET") {
          importReadCount += 1;
          return response(200, {
            import_id: "import-one",
            state: importReadCount === 1 ? "READY" : "CONFIRMED",
            job_state: "SUCCEEDED",
            reading_count: 362,
            interval_resolution_seconds: 3600,
            coverage_start_utc_ns: 1,
            coverage_end_utc_ns: 2,
            findings:
              importReadCount === 1
                ? [
                    {
                      code: "INTERVAL_GAP",
                      severity: "WARNING",
                      field_path: "readings",
                      warning_id: "warning-one",
                    },
                  ]
                : [],
            failure_code: null,
          });
        }
        if (path === "/v1/imports/import-one/confirm" && method === "POST") {
          return response(200, profile);
        }
        throw new Error(`Unexpected request: ${method} ${path}`);
      },
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
    const uploadCall = fetchMock.mock.calls[5] as [string, RequestInit];
    expect(uploadCall[0]).toBe("/v1/imports");
    expect(uploadCall[1].method).toBe("POST");
    expect(uploadCall[1].body).toBeInstanceOf(FormData);
    expect(
      (uploadCall[1].headers as Record<string, string>)["X-CSRF-Token"],
    ).toBe("csrf-token");
    const confirmationCall = fetchMock.mock.calls[7] as [string, RequestInit];
    expect(JSON.parse(confirmationCall[1].body as string)).toMatchObject({
      acknowledged_warning_ids: ["warning-one"],
      pge_service_attested: true,
    });
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(9));
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
      .mockResolvedValueOnce(response(200, tariffList))
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
      await screen.findByText(/current E-1 filed-source vector/i),
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
    const replayCall = fetchMock.mock.calls[5] as [string, RequestInit];
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
      qualifying_technologies: ["EV"],
    });
  });

  it("renders a rankable comparison with coverage and filed-source evidence", async () => {
    const fetchMock = vi.fn(
      (input: string | URL | Request, init?: RequestInit) => {
        void init;
        const path =
          typeof input === "string"
            ? input
            : input instanceof URL
              ? input.toString()
              : input.url;
        if (path === "/v1/auth/session") {
          return Promise.resolve(response(401, { message: "Sign in" }));
        }
        if (path === "/v1/auth/register") {
          return Promise.resolve(
            response(201, {
              user: { user_id: "owner", username: "owner_one" },
              csrf_token: "csrf-token",
            }),
          );
        }
        if (path === "/v1/profiles?page_size=1") {
          return Promise.resolve(response(200, { items: [] }));
        }
        if (path === "/v1/tariffs/pge-e1-2026-07") {
          return Promise.resolve(response(200, tariffDetail));
        }
        if (path === "/v1/tariffs") {
          return Promise.resolve(response(200, tariffList));
        }
        if (path === "/v1/imports/built-in-simulated-profile") {
          return Promise.resolve(response(201, builtInProfile));
        }
        if (path === "/v1/replays") {
          return Promise.resolve(response(201, replayResource));
        }
        if (path === "/v1/comparisons") {
          return Promise.resolve(response(201, rankableComparison));
        }
        return Promise.reject(new Error(`Unexpected request: ${path}`));
      },
    );
    vi.stubGlobal("fetch", fetchMock);
    vi.stubGlobal("crypto", { randomUUID: () => "comparison-request-id" });
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
    fireEvent.click(
      await screen.findByRole("button", {
        name: "Use built-in simulated July profile",
      }),
    );
    expect(
      await screen.findByText(/imported as immutable account data/i),
    ).toBeVisible();
    fireEvent.click(
      await screen.findByLabelText(/I attest that every locked account fact/i),
    );
    fireEvent.click(
      screen.getByRole("button", { name: "Create historical replay" }),
    );

    expect(
      await screen.findByRole("heading", { name: "Compare July plans" }),
    ).toBeVisible();
    fireEvent.click(
      screen.getByRole("button", { name: "Replay selected plans" }),
    );
    expect(await screen.findByText("$17.07")).toBeVisible();
    expect(screen.getByRole("heading", { name: "E-TOU-D" })).toBeVisible();
    expect(screen.getByText("$260.21")).toBeVisible();
    expect(screen.getAllByText(/2 supported, complete/i)).toHaveLength(5);
    fireEvent.click(screen.getByText("E-TOU-D coverage and provenance"));
    expect(
      screen.getAllByRole("link", { name: "pge-advice-7921-e" }).length,
    ).toBeGreaterThan(0);

    const builtInCall = fetchMock.mock.calls.find(
      ([path]) => path === "/v1/imports/built-in-simulated-profile",
    );
    const replayCall = fetchMock.mock.calls.find(
      ([path]) => path === "/v1/replays",
    );
    const comparisonCall = fetchMock.mock.calls.find(
      ([path]) => path === "/v1/comparisons",
    );
    expect(builtInCall?.[0]).toBe("/v1/imports/built-in-simulated-profile");
    expect(builtInCall?.[1]?.method).toBe("POST");
    expect(comparisonCall?.[0]).toBe("/v1/comparisons");
    const replayBody = JSON.parse(replayCall?.[1]?.body as string) as {
      account_facts: unknown;
    };
    const comparisonBody = JSON.parse(comparisonCall?.[1]?.body as string) as {
      candidate_tariff_version_ids: string[];
      account_facts: unknown;
      dated_eligibility_facts: {
        annual_usage_wh: number;
        annual_baseline_allowance_wh: number;
      };
    };
    expect(comparisonBody.candidate_tariff_version_ids).toEqual(
      tariffList.items.map((item) => item.tariff_version_id).sort(),
    );
    expect(comparisonBody.account_facts).toEqual(replayBody.account_facts);
    expect(comparisonBody.dated_eligibility_facts).toMatchObject({
      annual_usage_wh: 6000000,
      annual_baseline_allowance_wh: 2000000,
    });
  });

  it("renders blocked exclusions without winner or savings language", async () => {
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
      .mockResolvedValueOnce(response(200, tariffList))
      .mockResolvedValueOnce(response(200, tariffDetail))
      .mockResolvedValueOnce(response(201, replayResource))
      .mockResolvedValueOnce(response(201, blockedComparison));
    vi.stubGlobal("fetch", fetchMock);
    vi.stubGlobal("crypto", { randomUUID: () => "blocked-request-id" });
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
    fireEvent.click(
      await screen.findByLabelText(/I attest that every locked account fact/i),
    );
    fireEvent.click(
      screen.getByRole("button", { name: "Create historical replay" }),
    );
    fireEvent.change(
      await screen.findByLabelText("Trailing 12-month usage, kWh"),
      { target: { value: "" } },
    );
    fireEvent.click(
      screen.getByRole("button", { name: "Replay selected plans" }),
    );

    expect(await screen.findByText("No comparable winner")).toBeVisible();
    expect(screen.getByText(/Candidate Eligibility Unknown/i)).toBeVisible();
    expect(screen.getByText(/Annual Usage Required/i)).toBeVisible();
    expect(screen.queryByText(/savings/i)).not.toBeInTheDocument();
    expect(screen.queryByText("$17.07")).not.toBeInTheDocument();
    const comparisonCall = fetchMock.mock.calls[6] as [string, RequestInit];
    const comparisonBody = JSON.parse(comparisonCall[1].body as string) as {
      dated_eligibility_facts: { annual_usage_wh: null };
    };
    expect(comparisonBody.dated_eligibility_facts.annual_usage_wh).toBeNull();
  });

  it("previews and runs a complete verified historical EV scenario", async () => {
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
      .mockResolvedValueOnce(response(200, tariffList))
      .mockResolvedValueOnce(response(200, tariffDetail))
      .mockResolvedValueOnce(response(201, replayResource))
      .mockResolvedValueOnce(response(200, profileScenarioSlots))
      .mockResolvedValueOnce(response(201, optimalScenario));
    vi.stubGlobal("fetch", fetchMock);
    vi.stubGlobal("crypto", { randomUUID: () => "scenario-uuid" });
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
    fireEvent.click(
      await screen.findByLabelText(/I attest that every locked account fact/i),
    );
    fireEvent.click(
      screen.getByRole("button", { name: "Create historical replay" }),
    );

    expect(
      await screen.findByRole("heading", {
        name: "Schedule a historical flexible load",
      }),
    ).toBeVisible();
    expect(screen.getByText(/counterfactual, not a forecast/i)).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "Preview reference" }));
    expect(
      await screen.findByRole("heading", {
        name: "Reference feasibility preview",
      }),
    ).toBeVisible();
    expect(screen.getByText("7.2 kWh")).toBeVisible();
    expect(
      screen.getByText(/server will independently validate every slot/i),
    ).toBeVisible();
    fireEvent.click(
      screen.getByRole("button", { name: "Run verified optimization" }),
    );

    expect(
      await screen.findByRole("heading", { name: "Optimal" }),
    ).toBeVisible();
    expect(
      screen.getByText(/All four objective stages were proved optimal/i),
    ).toBeVisible();
    expect(screen.getByText("$25.00")).toBeVisible();
    expect(screen.getByText("$20.00")).toBeVisible();
    expect(screen.getByText(/Exact measured reconstruction/i)).toBeVisible();
    expect(
      screen.getByText(/This heuristic is not bill-optimal/i),
    ).toBeVisible();
    expect(screen.getAllByText(/not a forecast/i).length).toBeGreaterThan(0);

    const slotCall = fetchMock.mock.calls[6] as [string, RequestInit];
    const scenarioCall = fetchMock.mock.calls[7] as [string, RequestInit];
    expect(slotCall[0]).toBe("/v1/profiles/profile-one/scenario-slots");
    expect(scenarioCall[0]).toBe("/v1/scenarios");
    const scenarioBody = JSON.parse(scenarioCall[1].body as string) as {
      tariff_version_id: string;
      loads: Array<{
        mode: string;
        occurrences: Array<{
          reference_schedule: Array<{ energy_wh: number }>;
        }>;
      }>;
      shift_existing_attestation_load_ids: string[];
    };
    expect(scenarioBody.tariff_version_id).toBe("pge-etoud-2026-07");
    expect(scenarioBody.loads[0]?.mode).toBe("HISTORICAL_ADDITION");
    const submittedReference =
      scenarioBody.loads[0]?.occurrences[0]?.reference_schedule ?? [];
    expect(submittedReference).toHaveLength(8);
    expect(
      submittedReference.reduce((total, slot) => total + slot.energy_wh, 0),
    ).toBe(7200);
    expect(scenarioBody.shift_existing_attestation_load_ids).toEqual([]);
  });

  it("explains aggregate-cap and reference-window failures before submission", async () => {
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
      .mockResolvedValueOnce(response(200, tariffList))
      .mockResolvedValueOnce(response(200, tariffDetail))
      .mockResolvedValueOnce(response(201, replayResource))
      .mockResolvedValueOnce(response(200, profileScenarioSlots))
      .mockResolvedValueOnce(response(200, profileScenarioSlots));
    vi.stubGlobal("fetch", fetchMock);
    vi.stubGlobal("crypto", { randomUUID: () => "scenario-uuid" });
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
    fireEvent.click(
      await screen.findByLabelText(/I attest that every locked account fact/i),
    );
    fireEvent.click(
      screen.getByRole("button", { name: "Create historical replay" }),
    );

    fireEvent.change(
      await screen.findByLabelText("Flexible-load aggregate cap, W, optional"),
      { target: { value: "100" } },
    );
    fireEvent.click(screen.getByRole("button", { name: "Preview reference" }));
    expect(
      await screen.findByRole("heading", {
        name: "REFERENCE_FLEXIBLE_LOAD_CAP_EXCEEDED",
      }),
    ).toBeVisible();
    expect(screen.getByText("3")).toBeVisible();
    expect(
      fetchMock.mock.calls.some((call) => call[0] === "/v1/scenarios"),
    ).toBe(false);

    fireEvent.change(
      screen.getByLabelText("Flexible-load aggregate cap, W, optional"),
      { target: { value: "7200" } },
    );
    fireEvent.change(
      screen.getByLabelText("Earliest start, exact UTC boundary"),
      {
        target: { value: "2026-07-07T02:00:00Z" },
      },
    );
    fireEvent.change(
      screen.getByLabelText("Unoptimized reference start, UTC"),
      {
        target: { value: "2026-07-07T01:00:00Z" },
      },
    );
    fireEvent.click(screen.getByRole("button", { name: "Preview reference" }));
    expect(
      await screen.findByRole("heading", {
        name: "REFERENCE_ENERGY_OUTSIDE_WINDOW",
      }),
    ).toBeVisible();
    expect(
      fetchMock.mock.calls.some((call) => call[0] === "/v1/scenarios"),
    ).toBe(false);
  });

  it("labels best-found schedules with an open bound instead of optimal", async () => {
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
      .mockResolvedValueOnce(response(200, tariffList))
      .mockResolvedValueOnce(response(200, tariffDetail))
      .mockResolvedValueOnce(response(201, replayResource))
      .mockResolvedValueOnce(response(200, profileScenarioSlots))
      .mockResolvedValueOnce(response(201, bestFoundScenario));
    vi.stubGlobal("fetch", fetchMock);
    vi.stubGlobal("crypto", { randomUUID: () => "scenario-uuid" });
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
    fireEvent.click(
      await screen.findByLabelText(/I attest that every locked account fact/i),
    );
    fireEvent.click(
      screen.getByRole("button", { name: "Create historical replay" }),
    );
    fireEvent.click(
      await screen.findByRole("button", { name: "Preview reference" }),
    );
    fireEvent.click(
      await screen.findByRole("button", { name: "Run verified optimization" }),
    );

    expect(
      await screen.findByRole("heading", { name: "Best found" }),
    ).toBeVisible();
    expect(screen.getByText(/first open stage is 1/i)).toBeVisible();
    expect(
      screen.queryByText(/All four objective stages were proved optimal/i),
    ).not.toBeInTheDocument();
  });

  it("shows internal model failures as unsuccessful structured errors", async () => {
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
      .mockResolvedValueOnce(response(200, tariffList))
      .mockResolvedValueOnce(response(200, tariffDetail))
      .mockResolvedValueOnce(response(201, replayResource))
      .mockResolvedValueOnce(response(200, profileScenarioSlots))
      .mockResolvedValueOnce(
        response(500, {
          schema_version: "problem-v1",
          code: "EXACT_SOLVER_MODEL_INVALID",
          message:
            "The exact model failed validation and no schedule was published.",
          request_id: "request-one",
          field_paths: [],
          witness: { solver_status: "MODEL_INVALID" },
        }),
      );
    vi.stubGlobal("fetch", fetchMock);
    vi.stubGlobal("crypto", { randomUUID: () => "scenario-uuid" });
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
    fireEvent.click(
      await screen.findByLabelText(/I attest that every locked account fact/i),
    );
    fireEvent.click(
      screen.getByRole("button", { name: "Create historical replay" }),
    );
    fireEvent.click(
      await screen.findByRole("button", { name: "Preview reference" }),
    );
    fireEvent.click(
      await screen.findByRole("button", { name: "Run verified optimization" }),
    );

    expect(
      await screen.findByRole("heading", {
        name: "EXACT_SOLVER_MODEL_INVALID",
      }),
    ).toBeVisible();
    expect(
      within(screen.getByRole("alert")).getByText(/no schedule was published/i),
    ).toBeVisible();
    expect(screen.getByText("MODEL_INVALID")).toBeVisible();
    expect(
      screen.queryByRole("heading", { name: "Optimal" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("heading", { name: "Best found" }),
    ).not.toBeInTheDocument();
  });
});
