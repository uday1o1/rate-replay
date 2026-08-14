import type { Page, Route } from "@playwright/test";

export type PrivateApiRequest = {
  method: string;
  path: string;
  headers: Record<string, string>;
  body: string | null;
};

export type PrivateApiOptions = {
  comparison: "rankable" | "blocked";
  scenario: "optimal" | "best-found" | "existing" | "model-invalid";
};

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

const sourceCoverage = [
  {
    source_id: "pge-advice-7921-e",
    source_sha256: "b".repeat(64),
    source_url: "https://example.test/pge-advice-7921-e.pdf",
    linked_rule_ids: ["E1_TOTAL_ENERGY_2026_06_01"],
  },
];

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
  compilation: { reports: { source_coverage: sourceCoverage } },
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
    diagnostic_cost_allocation: {
      allocation_version: "private-cost-allocation-v1",
      status: "AVAILABLE",
      timezone: "America/Los_Angeles",
      daily_energy_charges: [
        {
          service_day: "2026-07-01",
          line_item_key: "bundled_energy.tier_1",
          charge_component_key: "bundled_energy",
          allocation_weight_wh: 1000,
          allocated_cents: 3600,
        },
        {
          service_day: "2026-07-02",
          line_item_key: "bundled_energy.tier_1",
          charge_component_key: "bundled_energy",
          allocation_weight_wh: 1000,
          allocated_cents: 3600,
        },
      ],
      monthly_energy_charges: [
        {
          calendar_month: "2026-07",
          allocation_weight_wh: 2000,
          allocated_cents: 7200,
        },
      ],
      billing_period_adjustments: [
        {
          adjustment_kind: "SUPPORTED_PERIOD_CHARGE",
          line_item_key: "base_services_charge",
          charge_component_key: "base_services_charge",
          amount_cents: 2619,
        },
        {
          adjustment_kind: "USER_UNSUPPORTED",
          line_item_key: "user_entered_other_1",
          charge_component_key: null,
          amount_cents: 200,
        },
        {
          adjustment_kind: "UNEXPLAINED_RESIDUAL",
          line_item_key: "unexplained_residual",
          charge_component_key: null,
          amount_cents: 981,
        },
      ],
      reconciliation: {
        daily_energy_charge_cents: 7200,
        supported_period_adjustment_cents: 2619,
        supported_calculated_cents: 9819,
        user_unsupported_cents: 200,
        unexplained_residual_cents: 981,
        displayed_total_cents: 11000,
      },
    },
    reconciliation: {
      user_unsupported_cents: 200,
      unexplained_residual_cents: 981,
      entered_bill_total_cents: 11000,
      classification: "REVIEW_REQUIRED",
    },
    provenance_sources: sourceCoverage,
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
  tariffVersionId: string,
  planCode: string,
  supportedCalculatedCents: number,
) {
  return {
    tariff_version_id: tariffVersionId,
    plan_code: planCode,
    eligibility: { status: "ELIGIBLE", reason_codes: [] },
    component_coverage: supportedCoverage,
    alternative_plan: {
      supported_calculated_cents: supportedCalculatedCents,
      component_coverage: supportedCoverage,
      provenance_sources: sourceCoverage,
      result_sha256: planCode.charCodeAt(0).toString().repeat(64).slice(0, 64),
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
        { occurrence_id: "scenario-uuid", slots: resultEnergySlots(values) },
      ],
    },
    verification: {
      status: "VALID",
      objective: {
        supported_cost_cents: supportedCostCents,
        changed_occurrence_slot_count: 2,
        completion_slot_index_sum: 4,
        stable_slot_order_score: 7200,
      },
      verification_sha256: supportedCostCents
        .toString()
        .padEnd(64, "0")
        .slice(0, 64),
    },
    billing_result: {
      supported_calculated_cents: supportedCostCents,
      result_sha256: supportedCostCents.toString().padEnd(64, "1").slice(0, 64),
    },
  };
}

const scenarioReference = [0, 0, 0, 7200, 0, 0, 0, 0];
const scenarioSelected = [7200, 0, 0, 0, 0, 0, 0, 0];
const referenceVerified = verifiedSchedule(scenarioReference, 2500);
const selectedVerified = verifiedSchedule(scenarioSelected, 2000);

const scenarioSubmission = {
  scenario_id: "scenario-one",
  job: {
    job_id: "scenario-job-one",
    state: "QUEUED",
    failure_code: null,
    terminal_result_type: null,
    terminal_result_id: null,
  },
};

const successfulScenarioJob = {
  job_id: "scenario-job-one",
  state: "SUCCEEDED",
  failure_code: null,
  terminal_result_type: "SCENARIO",
  terminal_result_id: "scenario-result-one",
};

const invalidScenarioJob = {
  ...successfulScenarioJob,
  state: "FAILED",
  failure_code: "EXACT_SOLVER_MODEL_INVALID",
  terminal_result_type: null,
  terminal_result_id: null,
};

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

const existingReference = [0, 0, 0, 100, 200, 200, 0, 0];
const existingScenario = {
  ...optimalScenario,
  result: {
    ...optimalScenario.result,
    decomposition: {
      fixed_background: resultEnergySlots(
        existingReference.map((value) => 500 - value),
      ),
      shift_existing_reference: resultEnergySlots(existingReference),
      historical_addition_reference: resultEnergySlots(
        Array<number>(8).fill(0),
      ),
      reconstructed_measured_profile: resultEnergySlots(
        Array<number>(8).fill(500),
      ),
      unchanged_reference_profile: resultEnergySlots(
        Array<number>(8).fill(500),
      ),
      exact_measured_reconstruction: true,
    },
  },
};

const reportSubmission = {
  job_id: "report-job-one",
  kind: "REPORT",
  state: "QUEUED",
  failure_code: null,
  terminal_result_type: null,
  terminal_result_id: null,
};

const successfulReportJob = {
  ...reportSubmission,
  state: "SUCCEEDED",
  terminal_result_type: "REPORT",
  terminal_result_id: "report-export-one",
};

const reportResource = {
  schema_version: "report-resource-v1",
  export_id: "report-export-one",
  scenario_id: "scenario-one",
  scenario_result_id: "scenario-result-one",
  job_id: "report-job-one",
  created_at: "2026-08-14T00:00:00Z",
  report: {
    schema_version: "redacted-report-v1",
    redaction_policy_version: "redacted-report-policy-v1",
    report_template_version: "redacted-report-template-v1",
    calculation_time_mode: "HISTORICAL_REPLAY",
    historical_addition_label: "HISTORICAL_COUNTERFACTUAL_NOT_FORECAST",
    billing_period: { start: "2026-07-01", end: "2026-08-01" },
    aggregate_measured_energy_wh: 4000,
    aggregate_reference_flexible_energy_wh: 7200,
    aggregate_shifted_energy_wh: 7200,
    selected_supported_cost_cents: 2000,
    reference_supported_cost_cents: 2500,
    supported_cost_difference_cents: 500,
    signed_unexplained_residual_cents: null,
    supported_charge_components: [
      { component_key: "bundled_energy", amount_cents: 2000 },
    ],
    unsupported_component_codes: [],
    tariff_provenance: {
      tariff_version_id: "pge-etoud-2026-07",
      tariff_ir_version: "tariff-ir-v2",
      compiler_content_sha256: "a".repeat(64),
    },
    solver: {
      search_status: "OPTIMAL",
      selected_source: "SOLVER_INCUMBENT",
      verification_status: "VALID",
      verifier_version: "independent-schedule-verifier-v1",
      highest_objective_stage_proved_optimal: 4,
      first_open_stage: null,
    },
    limitations: ["Historical counterfactual, not a forecast."],
    report_sha256: "7".repeat(64),
  },
};

const profileDeletionId = "c".repeat(32);

function deletionStatus(status: "DELETING" | "DELETED") {
  return {
    schema_version: "deletion-status-v1",
    deletion_id: profileDeletionId,
    status,
    artifact_counts: status === "DELETED" ? { profiles: 1 } : {},
    completed_at: status === "DELETED" ? "2026-08-14T00:00:00Z" : null,
  };
}

function json(route: Route, status: number, body: object): Promise<void> {
  return route.fulfill({
    status,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
}

export async function installPrivateApi(
  page: Page,
  options: PrivateApiOptions,
): Promise<PrivateApiRequest[]> {
  const requests: PrivateApiRequest[] = [];
  let importReadCount = 0;
  await page.route("**/v1/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = `${url.pathname}${url.search}`;
    const method = request.method();
    requests.push({
      method,
      path,
      headers: request.headers(),
      body: request.postData(),
    });

    if (method === "GET" && path === "/v1/auth/session") {
      await json(route, 401, { code: "AUTH_REQUIRED", message: "Sign in" });
      return;
    }
    if (method === "POST" && path === "/v1/auth/register") {
      await json(route, 201, {
        user: { user_id: "owner", username: "owner_one" },
        csrf_token: "csrf-token",
      });
      return;
    }
    if (method === "GET" && path === "/v1/profiles?page_size=1") {
      await json(route, 200, { items: [] });
      return;
    }
    if (method === "GET" && path === "/v1/tariffs") {
      await json(route, 200, tariffList);
      return;
    }
    if (method === "GET" && path === "/v1/tariffs/pge-e1-2026-07") {
      await json(route, 200, tariffDetail);
      return;
    }
    if (
      method === "POST" &&
      path === "/v1/imports/built-in-simulated-profile"
    ) {
      await json(route, 201, builtInProfile);
      return;
    }
    if (method === "POST" && path === "/v1/imports") {
      await json(route, 202, {
        import_id: "import-one",
        state_url: "/v1/imports/import-one",
      });
      return;
    }
    if (method === "GET" && path === "/v1/imports/import-one") {
      importReadCount += 1;
      await json(route, 200, {
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
        profile_version_id: importReadCount === 1 ? null : "profile-one",
      });
      return;
    }
    if (method === "POST" && path === "/v1/imports/import-one/confirm") {
      await json(route, 200, profile);
      return;
    }
    if (method === "POST" && path === "/v1/replays") {
      await json(route, 201, replayResource);
      return;
    }
    if (method === "POST" && path === "/v1/comparisons") {
      await json(
        route,
        201,
        options.comparison === "rankable"
          ? rankableComparison
          : blockedComparison,
      );
      return;
    }
    if (
      method === "GET" &&
      path === "/v1/profiles/profile-one/scenario-slots"
    ) {
      await json(route, 200, profileScenarioSlots);
      return;
    }
    if (method === "POST" && path === "/v1/scenarios") {
      await json(route, 202, scenarioSubmission);
      return;
    }
    if (method === "GET" && path === "/v1/jobs/scenario-job-one") {
      await json(
        route,
        200,
        options.scenario === "model-invalid"
          ? invalidScenarioJob
          : successfulScenarioJob,
      );
      return;
    }
    if (method === "GET" && path === "/v1/scenarios/scenario-one") {
      const scenarioResult =
        options.scenario === "optimal"
          ? optimalScenario
          : options.scenario === "existing"
            ? existingScenario
            : bestFoundScenario;
      await json(route, 200, scenarioResult);
      return;
    }
    if (method === "POST" && path === "/v1/reports/scenario-one/exports") {
      await json(route, 202, reportSubmission);
      return;
    }
    if (method === "GET" && path === "/v1/jobs/report-job-one") {
      await json(route, 200, successfulReportJob);
      return;
    }
    if (method === "GET" && path === "/v1/reports/scenario-one") {
      await json(route, 200, reportResource);
      return;
    }
    if (method === "DELETE" && path === "/v1/profiles/profile-one") {
      await json(route, 202, deletionStatus("DELETING"));
      return;
    }
    if (method === "GET" && path === `/v1/deletions/${profileDeletionId}`) {
      await json(route, 200, deletionStatus("DELETED"));
      return;
    }
    await json(route, 500, {
      code: "E2E_UNHANDLED_REQUEST",
      message: `${method} ${path} was not declared by the browser contract fixture.`,
    });
  });
  return requests;
}
