import allowlistRaw from "../../../artifacts/demo/allowlist.v1.json?raw";
import manifestRaw from "../../../artifacts/demo/manifest.v1.json?raw";

import { DEMO_MANIFEST_SHA256 } from "./demoReleaseLock";

const objectModules = import.meta.glob<string>(
  "../../../artifacts/demo/objects/*.json",
  {
    eager: true,
    import: "default",
    query: "?raw",
  },
);

const LOGICAL_IDS = [
  "import-review",
  "bill-replay",
  "tariff-comparison",
  "scenario-inputs",
  "reference-result",
  "heuristic-result",
  "solver-result",
  "verification-record",
  "redacted-report",
] as const;

type LogicalId = (typeof LOGICAL_IDS)[number];

export type HeatmapSlot = {
  slot_start_utc: string;
  duration_seconds: number;
  reference_energy_wh: number;
  heuristic_energy_wh: number;
  selected_energy_wh: number;
};

export type DemoImportReview = {
  label: string;
  simulated: true;
  source_artifact_sha256: string;
  profile_content_sha256: string;
  parser_contract_version: string;
  reading_count: number;
  interval_resolution_seconds: number;
  coverage_start_utc_ns: number;
  coverage_end_utc_ns: number;
  total_energy_wh: number;
  findings: unknown[];
  quality_status: "READY";
};

type DemoReplayLine = {
  line_item_key: string;
  quantity_numerator: number;
  quantity_denominator: number;
  quantity_unit: string;
  rate_numerator_microdollars: number;
  rate_denominator: number;
  rate_unit: string;
  rounded_cents: number;
  source_id: string;
};

export type DemoReplay = {
  eligibility: { status: string; reason_codes: string[] };
  supported_calculated_cents: number;
  line_items: DemoReplayLine[];
  user_unsupported_lines: Array<{
    line_item_key: string;
    description: string;
    amount_cents: number;
  }>;
  reconciliation: {
    user_unsupported_cents: number;
    unexplained_residual_cents: number;
    entered_bill_total_cents: number;
    classification: string;
  };
  provenance_sources: Array<{
    source_id: string;
    source_sha256: string;
    source_url: string;
    linked_rule_ids: string[];
  }>;
  manifest: { calculation_sha256: string };
  result_sha256: string;
};

export type DemoComparison = {
  rankable: boolean;
  winner_tariff_version_ids: string[];
  savings_against_current_supported_cents: number | null;
  exclusions: Array<{
    code: string;
    tariff_version_id: string;
    component_key: string | null;
  }>;
  candidates: Array<{
    tariff_version_id: string;
    plan_code: string;
    eligibility: { status: string; reason_codes: string[] };
    component_coverage: Array<{
      component_key: string;
      status: string;
      reason_code: string | null;
    }>;
    alternative_plan: null | {
      supported_calculated_cents: number;
      provenance_sources: Array<{ source_id: string }>;
    };
  }>;
  comparison_sha256: string;
};

export type DemoScenarioInput = {
  calculation_time_mode: "HISTORICAL_REPLAY";
  historical_addition_label: "HISTORICAL_COUNTERFACTUAL_NOT_FORECAST";
  tariff_version_id: string;
  profile_content_sha256: string;
  heatmap_slots: HeatmapSlot[];
  load: {
    kind: string;
    mode: "HISTORICAL_ADDITION";
    execution_type: string;
    required_energy_wh: number;
    maximum_power_w: number;
    minimum_power_when_active_w: number;
    earliest_start_utc: string;
    deadline_utc: string;
  };
  reference_validation: {
    status: "VALID";
    load_count: number;
    occurrence_count: number;
    slot_count: number;
  };
  decomposition: {
    fixed_background_wh: number;
    existing_load_reference_wh: number;
    historical_addition_reference_wh: number;
    reconstructed_measured_profile_wh: number;
    unchanged_reference_profile_wh: number;
    exact_measured_reconstruction: true;
  };
};

export type DemoReferenceResult = {
  supported_cost_cents: number;
  verification_status: "VALID";
  verification_sha256: string;
};

export type DemoHeuristicResult = {
  search_status: string;
  selection_outcome: string;
  bill_optimality_claim: false;
  fallback_reason: string | null;
  supported_cost_cents: number;
  rank_calendar_sha256: string;
};

export type DemoSolverResult = {
  search_status: "OPTIMAL" | "BEST_FOUND";
  selected_source: string;
  selection_reason: string;
  supported_cost_cents: number;
  reference_supported_cost_cents: number;
  highest_objective_stage_proved_optimal: number;
  first_open_stage: number | null;
  absolute_cost_gap_cents: number | null;
  result_sha256: string;
  calculation_sha256: string;
};

export type DemoVerification = {
  status: "VALID";
  verification_version: string;
  verification_sha256: string;
  scenario_result_sha256: string;
  warning_codes: string[];
};

export type DemoRedactedReport = {
  schema_version: "redacted-report-v1";
  redaction_policy_version: string;
  report_template_version: string;
  calculation_time_mode: "HISTORICAL_REPLAY";
  historical_addition_label: "HISTORICAL_COUNTERFACTUAL_NOT_FORECAST";
  billing_period: { start: string; end: string };
  aggregate_measured_energy_wh: number;
  aggregate_reference_flexible_energy_wh: number;
  aggregate_shifted_energy_wh: number;
  selected_supported_cost_cents: number;
  reference_supported_cost_cents: number;
  supported_cost_difference_cents: number;
  signed_unexplained_residual_cents: number | null;
  supported_charge_components: Array<{
    component_key: string;
    amount_cents: number;
  }>;
  unsupported_component_codes: string[];
  tariff_provenance: {
    tariff_version_id: string;
    tariff_ir_version: string;
    compiler_content_sha256: string;
  };
  solver: {
    search_status: "OPTIMAL" | "BEST_FOUND";
    selected_source: string;
    verification_status: "VALID";
    verifier_version: string;
    highest_objective_stage_proved_optimal: number;
    first_open_stage: number | null;
  };
  limitations: string[];
  report_sha256: string;
};

export type DemoArtifacts = {
  importReview: DemoImportReview;
  billReplay: DemoReplay;
  tariffComparison: DemoComparison;
  scenarioInputs: DemoScenarioInput;
  referenceResult: DemoReferenceResult;
  heuristicResult: DemoHeuristicResult;
  solverResult: DemoSolverResult;
  verificationRecord: DemoVerification;
  redactedReport: DemoRedactedReport;
  manifestSha256: string;
};

type ManifestEntry = {
  logical_id: LogicalId;
  media_type: "application/json";
  path: string;
  sha256: string;
};

export async function loadDemoArtifacts(): Promise<DemoArtifacts> {
  if ((await sha256(manifestRaw)) !== DEMO_MANIFEST_SHA256) {
    throw new Error("PUBLIC_DEMO_MANIFEST_HASH_MISMATCH");
  }
  const manifest = parseObject(manifestRaw, "PUBLIC_DEMO_MANIFEST_INVALID");
  const allowlist = parseObject(allowlistRaw, "PUBLIC_DEMO_ALLOWLIST_INVALID");
  if (
    manifest.manifest_version !== "public-demo-manifest-v1" ||
    manifest.simulated_only !== true ||
    manifest.allowlist_sha256 !== (await sha256(allowlistRaw))
  ) {
    throw new Error("PUBLIC_DEMO_MANIFEST_INVALID");
  }
  const allowedIds = allowlist.logical_artifact_ids;
  if (
    !Array.isArray(allowedIds) ||
    allowedIds.length !== LOGICAL_IDS.length ||
    !LOGICAL_IDS.every((value, index) => value === allowedIds[index])
  ) {
    throw new Error("PUBLIC_DEMO_ALLOWLIST_INVALID");
  }
  const rawEntries = manifest.artifacts;
  if (!Array.isArray(rawEntries) || rawEntries.length !== LOGICAL_IDS.length) {
    throw new Error("PUBLIC_DEMO_MANIFEST_INVALID");
  }
  const payloads = new Map<LogicalId, unknown>();
  for (const rawEntry of rawEntries) {
    const entry = validateEntry(rawEntry);
    if (
      !LOGICAL_IDS.includes(entry.logical_id) ||
      payloads.has(entry.logical_id)
    ) {
      throw new Error("PUBLIC_DEMO_ARTIFACT_NOT_ALLOWED");
    }
    const modulePath = Object.keys(objectModules).find((path) =>
      path.endsWith(`/artifacts/demo/${entry.path}`),
    );
    const raw =
      modulePath === undefined ? undefined : objectModules[modulePath];
    if (
      raw === undefined ||
      (await sha256(raw)) !== entry.sha256 ||
      entry.path !== `objects/${entry.sha256}.json`
    ) {
      throw new Error("PUBLIC_DEMO_ARTIFACT_HASH_MISMATCH");
    }
    const artifact = parseObject(raw, "PUBLIC_DEMO_ARTIFACT_INVALID");
    if (
      artifact.schema_version !== "public-demo-artifact-v1" ||
      artifact.logical_id !== entry.logical_id ||
      artifact.simulated !== true ||
      !("payload" in artifact)
    ) {
      throw new Error("PUBLIC_DEMO_ARTIFACT_INVALID");
    }
    payloads.set(entry.logical_id, artifact.payload);
  }
  if (payloads.size !== LOGICAL_IDS.length) {
    throw new Error("PUBLIC_DEMO_ARTIFACT_SET_INCOMPLETE");
  }
  return {
    importReview: payload<DemoImportReview>(payloads, "import-review"),
    billReplay: payload<DemoReplay>(payloads, "bill-replay"),
    tariffComparison: payload<DemoComparison>(payloads, "tariff-comparison"),
    scenarioInputs: payload<DemoScenarioInput>(payloads, "scenario-inputs"),
    referenceResult: payload<DemoReferenceResult>(payloads, "reference-result"),
    heuristicResult: payload<DemoHeuristicResult>(payloads, "heuristic-result"),
    solverResult: payload<DemoSolverResult>(payloads, "solver-result"),
    verificationRecord: payload<DemoVerification>(
      payloads,
      "verification-record",
    ),
    redactedReport: payload<DemoRedactedReport>(payloads, "redacted-report"),
    manifestSha256: DEMO_MANIFEST_SHA256,
  };
}

function validateEntry(value: unknown): ManifestEntry {
  if (typeof value !== "object" || value === null) {
    throw new Error("PUBLIC_DEMO_MANIFEST_INVALID");
  }
  const entry = value as Record<string, unknown>;
  if (
    typeof entry.logical_id !== "string" ||
    entry.media_type !== "application/json" ||
    typeof entry.path !== "string" ||
    typeof entry.sha256 !== "string" ||
    !/^[0-9a-f]{64}$/.test(entry.sha256)
  ) {
    throw new Error("PUBLIC_DEMO_MANIFEST_INVALID");
  }
  return entry as ManifestEntry;
}

function parseObject(value: string, code: string): Record<string, unknown> {
  let parsed: unknown;
  try {
    parsed = JSON.parse(value);
  } catch {
    throw new Error(code);
  }
  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
    throw new Error(code);
  }
  return parsed as Record<string, unknown>;
}

function payload<T>(values: Map<LogicalId, unknown>, logicalId: LogicalId): T {
  const value = values.get(logicalId);
  if (typeof value !== "object" || value === null) {
    throw new Error("PUBLIC_DEMO_ARTIFACT_INVALID");
  }
  return value as T;
}

async function sha256(value: string): Promise<string> {
  if (globalThis.crypto?.subtle === undefined) {
    throw new Error("PUBLIC_DEMO_INTEGRITY_UNAVAILABLE");
  }
  const digest = await globalThis.crypto.subtle.digest(
    "SHA-256",
    new TextEncoder().encode(value),
  );
  return [...new Uint8Array(digest)]
    .map((byte) => byte.toString(16).padStart(2, "0"))
    .join("");
}
