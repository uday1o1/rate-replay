import { CSSProperties, FormEvent, useMemo, useState } from "react";

import { AccountFacts, TariffSummary } from "./ComparisonWorkspace";
import { ReportExport } from "./ReportExport";
import { ApiError, api } from "./api";

type ProfileSlot = {
  slot_start_utc: string;
  duration_seconds: number;
  measured_energy_wh: number;
};

type ProfileSlotsResource = {
  profile_version_id: string;
  profile_content_sha256: string;
  calculation_time_mode: "HISTORICAL_REPLAY";
  energy_basis: "METER_SIDE";
  slots: ProfileSlot[];
};

type ReferenceSlot = {
  slot_start_utc: string;
  duration_seconds: number;
  energy_wh: number;
};

type InterruptibleExecutionSpec = {
  execution_type: "INTERRUPTIBLE_MODULATING";
  maximum_power_w: number;
  minimum_power_when_active_w: number;
};

type FixedShapeExecutionSpec = {
  execution_type: "CONTIGUOUS_FIXED_SHAPE";
  fixed_slot_shape_wh: number[];
};

type ScenarioRequest = {
  request_schema_version: "scenario-operation-v1";
  profile_version_id: string;
  tariff_version_id: string;
  account_facts: AccountFacts;
  dated_eligibility_facts: {
    facts_as_of: string;
    ev_registered_and_charged_at_premises: true;
    whole_house_metering: true;
    annual_usage_period: { start: string; end: string };
    annual_usage_wh: number;
    annual_baseline_allowance_wh: number;
  };
  electrical_constraints: {
    site_import_cap_w: number | null;
    flexible_load_aggregate_cap_w: number | null;
    energy_basis: "METER_SIDE";
  };
  loads: Array<{
    load_id: string;
    physical_asset_key: string;
    kind: string;
    mode: "SHIFT_EXISTING" | "HISTORICAL_ADDITION";
    execution_spec: InterruptibleExecutionSpec | FixedShapeExecutionSpec;
    occurrences: Array<{
      occurrence_id: string;
      required_energy_wh: number;
      earliest_start_utc: string;
      deadline_utc: string;
      reference_schedule: ReferenceSlot[];
    }>;
  }>;
  shift_existing_attestation_load_ids: string[];
};

type Objective = {
  supported_cost_cents: number;
  changed_occurrence_slot_count: number;
  completion_slot_index_sum: number;
  stable_slot_order_score: number;
};

type Schedule = {
  occurrences: Array<{
    occurrence_id: string;
    slots: ReferenceSlot[];
  }>;
};

type VerifiedResult = {
  schedule: Schedule;
  verification: {
    status: "VALID";
    objective: Objective;
    verification_sha256: string;
  };
  billing_result: { supported_calculated_cents: number; result_sha256: string };
};

type EnergySlot = {
  slot_start_utc: string;
  duration_seconds: number;
  energy_wh: number;
};

type ScenarioResource = {
  scenario_id: string;
  state: string;
  repeated: boolean;
  result: {
    calculation_time_mode: "HISTORICAL_REPLAY";
    historical_addition_label: "HISTORICAL_COUNTERFACTUAL_NOT_FORECAST";
    reference_validation: {
      status: "VALID";
      load_count: number;
      occurrence_count: number;
      slot_count: number;
    };
    decomposition: {
      fixed_background: EnergySlot[];
      shift_existing_reference: EnergySlot[];
      historical_addition_reference: EnergySlot[];
      reconstructed_measured_profile: EnergySlot[];
      unchanged_reference_profile: EnergySlot[];
      exact_measured_reconstruction: true;
    };
    exact: {
      search_status: "OPTIMAL" | "BEST_FOUND";
      selected_source: "SOLVER_INCUMBENT" | "REFERENCE";
      selection_reason: string;
      selected: VerifiedResult;
      reference: VerifiedResult;
      highest_objective_stage_proved_optimal: number;
      first_open_stage: number | null;
      best_supported_cost_bound: number | null;
      absolute_cost_gap_cents: number | null;
      relative_cost_gap: number | null;
    };
    heuristic: {
      search_status: string;
      selection_outcome: string;
      bill_optimality_claim: false;
      selected: VerifiedResult;
      fallback_reason: string | null;
    };
    manifest: {
      calculation_sha256: string;
      solver_name: string;
      solver_version: string;
      rank_calendar_sha256: string;
      selected_verification_sha256: string;
      warning_codes: string[];
    };
    result_sha256: string;
  };
};

type JobResource = {
  job_id: string;
  state: "QUEUED" | "LEASED" | "RUNNING" | "SUCCEEDED" | "FAILED" | "CANCELLED";
  failure_code: string | null;
  terminal_result_type: string | null;
  terminal_result_id: string | null;
};

type ScenarioSubmission = {
  scenario_id: string;
  job: JobResource;
};

type Preview = {
  request: ScenarioRequest | null;
  issue: ScenarioIssue | null;
  slotCount: number;
  measuredWh: number;
  fixedBackgroundWh: number;
  flexibleReferenceWh: number;
  unchangedWh: number;
  positiveReferenceSlots: number;
};

type ScenarioIssue = {
  code: string;
  message: string;
  witness: Record<string, unknown>;
};

type ScenarioWorkspaceProps = {
  profileId: string;
  csrf: string;
  accountFacts: AccountFacts;
  tariffs: TariffSummary[];
  onMessage: (message: string) => void;
};

export function ScenarioWorkspace({
  profileId,
  csrf,
  accountFacts,
  tariffs,
  onMessage,
}: ScenarioWorkspaceProps) {
  const optimizableTariffs = useMemo(
    () => tariffs.filter((tariff) => tariff.optimization_admitted),
    [tariffs],
  );
  const defaultTariff =
    optimizableTariffs.find(
      (tariff) => tariff.tariff_version_id === "pge-etoud-2026-07",
    )?.tariff_version_id ??
    optimizableTariffs[0]?.tariff_version_id ??
    "";
  const [preview, setPreview] = useState<Preview | null>(null);
  const [result, setResult] = useState<ScenarioResource | null>(null);
  const [issue, setIssue] = useState<ScenarioIssue | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [executionType, setExecutionType] = useState<
    "INTERRUPTIBLE_MODULATING" | "CONTIGUOUS_FIXED_SHAPE"
  >("INTERRUPTIBLE_MODULATING");

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    setIssue(null);
    if (preview?.request === null) return;
    if (preview === null) {
      setSubmitting(true);
      try {
        const profile = await api<ProfileSlotsResource>(
          `/v1/profiles/${profileId}/scenario-slots`,
        );
        const next = buildPreview(data, profile, accountFacts);
        setPreview(next);
        setIssue(next.issue);
        onMessage(
          next.issue === null
            ? "Reference schedule reconstructed. Review the feasibility preview before running."
            : "Reference preview found a constraint violation before solver submission.",
        );
      } catch (error) {
        handleError(error, setIssue, onMessage);
      } finally {
        setSubmitting(false);
      }
      return;
    }
    setSubmitting(true);
    try {
      const submission = await api<ScenarioSubmission>("/v1/scenarios", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Idempotency-Key": `browser-scenario-${crypto.randomUUID()}`,
          "X-CSRF-Token": csrf,
        },
        body: JSON.stringify(preview.request),
      });
      let job = submission.job;
      for (
        let attempt = 0;
        !isTerminal(job.state) && attempt < 120;
        attempt += 1
      ) {
        job = await api<JobResource>(`/v1/jobs/${job.job_id}`);
        if (!isTerminal(job.state)) await wait(250);
      }
      if (!isTerminal(job.state)) {
        throw new ApiError(
          504,
          "SCENARIO_JOB_TIMEOUT",
          "The scenario is still running. Its durable job can be checked again safely.",
          [],
          { job_id: job.job_id },
        );
      }
      if (
        job.state !== "SUCCEEDED" ||
        job.terminal_result_type !== "SCENARIO"
      ) {
        throw new ApiError(
          409,
          job.failure_code ?? "SCENARIO_JOB_UNSUCCESSFUL",
          "The scenario worker rejected the calculation and published no schedule.",
          [],
          { job_id: job.job_id, job_state: job.state },
        );
      }
      const value = await api<ScenarioResource>(
        `/v1/scenarios/${submission.scenario_id}`,
      );
      setResult(value);
      onMessage(
        value.result.exact.search_status === "OPTIMAL"
          ? "Verified optimal historical scenario created."
          : "Verified best-found historical scenario created with an open bound.",
      );
    } catch (error) {
      handleError(error, setIssue, onMessage);
    } finally {
      setSubmitting(false);
    }
  }

  function invalidatePreview() {
    setPreview(null);
    setIssue(null);
  }

  return (
    <section className="panel wide" aria-labelledby="scenario-heading">
      <p className="step">05</p>
      <h2 id="scenario-heading">Schedule a historical flexible load</h2>
      <p>
        Describe one explicit load and its complete user-supplied reference.
        RateReplay changes only that declared energy on the admitted July 2026
        timestamps. A historical addition is a counterfactual, not a forecast.
      </p>
      <form
        className="scenario-form"
        onChange={invalidatePreview}
        onSubmit={(event) => void submit(event)}
      >
        <div className="scenario-fields">
          <label>
            Historical tariff
            <select
              name="tariff_version_id"
              defaultValue={defaultTariff}
              required
            >
              {optimizableTariffs.map((tariff) => (
                <option
                  key={tariff.tariff_version_id}
                  value={tariff.tariff_version_id}
                >
                  {tariff.plan_code}
                </option>
              ))}
            </select>
          </label>
          <label>
            Load treatment
            <select name="mode" defaultValue="HISTORICAL_ADDITION">
              <option value="HISTORICAL_ADDITION">Historical addition</option>
              <option value="SHIFT_EXISTING">
                Shift existing measured load
              </option>
            </select>
          </label>
          <label>
            Load kind
            <select name="kind" defaultValue="EV">
              <option value="EV">EV</option>
              <option value="DISHWASHER">Dishwasher</option>
              <option value="WASHER">Washer</option>
              <option value="DRYER">Dryer</option>
              <option value="POOL_PUMP">Pool pump</option>
              <option value="CUSTOM">Custom</option>
            </select>
          </label>
          <label>
            Execution model
            <select
              name="execution_type"
              value={executionType}
              onChange={(event) =>
                setExecutionType(
                  event.currentTarget.value as
                    "INTERRUPTIBLE_MODULATING" | "CONTIGUOUS_FIXED_SHAPE",
                )
              }
            >
              <option value="INTERRUPTIBLE_MODULATING">
                Interruptible or modulating
              </option>
              <option value="CONTIGUOUS_FIXED_SHAPE">
                Contiguous fixed appliance cycle
              </option>
            </select>
          </label>
          <label>
            Physical asset key
            <input
              name="physical_asset_key"
              defaultValue="primary-ev"
              required
            />
          </label>
          {executionType === "INTERRUPTIBLE_MODULATING" ? (
            <>
              <label>
                Required meter-side energy, kWh
                <input
                  name="required_energy_kwh"
                  inputMode="decimal"
                  pattern="[0-9]+(\.[0-9]{1,3})?"
                  defaultValue="7.2"
                  required
                />
              </label>
              <label>
                Maximum average power, W
                <input
                  name="maximum_power_w"
                  type="number"
                  min="1"
                  defaultValue="7200"
                  required
                />
              </label>
              <label>
                Minimum active power, W
                <input
                  name="minimum_power_w"
                  type="number"
                  min="0"
                  defaultValue="0"
                  required
                />
              </label>
            </>
          ) : (
            <label className="fixed-shape-field">
              Cycle energy by contiguous slot, Wh
              <input
                name="fixed_shape_wh"
                inputMode="numeric"
                pattern="[0-9]+(,[0-9]+)*"
                maxLength={512}
                defaultValue="500,1000,750"
                required
              />
              <span>
                Enter one nonnegative integer per canonical interval. The cycle
                keeps this exact shape and order at every allowed start.
              </span>
            </label>
          )}
          <label>
            Earliest start, exact UTC boundary
            <input
              name="earliest_start_utc"
              defaultValue="2026-07-07T00:00:00Z"
              required
            />
          </label>
          <label>
            Deadline, exact UTC boundary
            <input
              name="deadline_utc"
              defaultValue="2026-07-07T07:00:00Z"
              required
            />
          </label>
          <label>
            Unoptimized reference start, UTC
            <input
              name="reference_start_utc"
              defaultValue="2026-07-07T03:00:00Z"
              required
            />
          </label>
          <label>
            Site average import cap, W, optional
            <input
              name="site_import_cap_w"
              type="number"
              min="1"
              defaultValue="12000"
            />
          </label>
          <label>
            Flexible-load aggregate cap, W, optional
            <input
              name="flexible_load_cap_w"
              type="number"
              min="1"
              defaultValue="7200"
            />
          </label>
        </div>
        <label className="attestation">
          <input name="shift_existing_attestation" type="checkbox" />
          If I select shift existing measured load, I attest that this complete
          user-supplied reference represents energy already included in the
          imported meter profile.
        </label>
        <p className="coverage-note">
          V1 does not infer appliance usage, battery efficiency, convenience,
          automation, or future behavior. Average-power caps at interval
          resolution are not breaker-safety guarantees.
        </p>
        <button
          className="primary"
          type="submit"
          disabled={submitting || defaultTariff === ""}
        >
          {submitting
            ? "Checking exact constraints..."
            : preview === null
              ? "Preview reference"
              : "Run verified optimization"}
        </button>
      </form>

      {preview !== null && (
        <article className="scenario-preview" aria-live="polite">
          <h3>Reference feasibility preview</h3>
          <dl className="scenario-metrics">
            <Metric
              label="Canonical slots"
              value={preview.slotCount.toLocaleString()}
            />
            <Metric
              label="Measured profile"
              value={formatEnergy(preview.measuredWh)}
            />
            <Metric
              label="Fixed background"
              value={formatEnergy(preview.fixedBackgroundWh)}
            />
            <Metric
              label="Flexible reference"
              value={formatEnergy(preview.flexibleReferenceWh)}
            />
            <Metric
              label="Unchanged profile"
              value={formatEnergy(preview.unchangedWh)}
            />
            <Metric
              label="Positive reference slots"
              value={preview.positiveReferenceSlots.toLocaleString()}
            />
          </dl>
          {preview.issue === null ? (
            <p className="quality-ok">
              The client-side preview reconstructs the declared reference. The
              server will independently validate every slot before creating a
              job.
            </p>
          ) : (
            <IssueView issue={preview.issue} />
          )}
        </article>
      )}

      {issue !== null && preview?.issue !== issue && (
        <IssueView issue={issue} />
      )}

      {result !== null && (
        <>
          <ScenarioResultView resource={result} />
          <ReportExport
            scenarioId={result.scenario_id}
            csrf={csrf}
            onMessage={onMessage}
          />
        </>
      )}
    </section>
  );
}

function isTerminal(state: JobResource["state"]): boolean {
  return state === "SUCCEEDED" || state === "FAILED" || state === "CANCELLED";
}

function wait(milliseconds: number): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, milliseconds));
}

function buildPreview(
  data: FormData,
  profile: ProfileSlotsResource,
  accountFacts: AccountFacts,
): Preview {
  const slots = profile.slots;
  const mode = entryText(data.get("mode")) as
    "SHIFT_EXISTING" | "HISTORICAL_ADDITION";
  const executionType = entryText(data.get("execution_type")) as
    "INTERRUPTIBLE_MODULATING" | "CONTIGUOUS_FIXED_SHAPE";
  const fixedShape =
    executionType === "CONTIGUOUS_FIXED_SHAPE"
      ? parseFixedShape(data.get("fixed_shape_wh"))
      : null;
  const requiredEnergyWh =
    fixedShape === null
      ? exactKwhToWh(data.get("required_energy_kwh"))
      : sum(fixedShape);
  const maximumPowerW =
    fixedShape === null
      ? positiveInteger(data.get("maximum_power_w"), "Maximum power")
      : null;
  const minimumPowerW =
    fixedShape === null
      ? nonnegativeInteger(data.get("minimum_power_w"), "Minimum power")
      : null;
  if (
    maximumPowerW !== null &&
    minimumPowerW !== null &&
    minimumPowerW > maximumPowerW
  ) {
    return failedPreview(slots, "MINIMUM_POWER_EXCEEDS_MAXIMUM", {
      minimum_power_w: minimumPowerW,
      maximum_power_w: maximumPowerW,
    });
  }
  const earliest = boundaryIndex(
    slots,
    entryText(data.get("earliest_start_utc")),
    true,
  );
  const deadline = boundaryIndex(
    slots,
    entryText(data.get("deadline_utc")),
    false,
  );
  const referenceStart = boundaryIndex(
    slots,
    entryText(data.get("reference_start_utc")),
    true,
  );
  if (earliest === null || deadline === null || referenceStart === null) {
    return failedPreview(slots, "NON_ALIGNED_OCCURRENCE_BOUNDARY", {
      requirement:
        "All occurrence endpoints must exactly match a canonical UTC boundary.",
    });
  }
  if (
    earliest >= deadline ||
    referenceStart < earliest ||
    referenceStart >= deadline
  ) {
    return failedPreview(slots, "REFERENCE_ENERGY_OUTSIDE_WINDOW", {
      earliest_slot_index: earliest,
      deadline_slot_index: deadline,
      reference_start_slot_index: referenceStart,
    });
  }
  const referenceEnergy = Array<number>(slots.length).fill(0);
  if (fixedShape !== null) {
    if (referenceStart + fixedShape.length > deadline) {
      return failedPreview(slots, "FIXED_SHAPE_REFERENCE_MISMATCH", {
        reference_start_slot_index: referenceStart,
        fixed_shape_slot_count: fixedShape.length,
        deadline_slot_index: deadline,
      });
    }
    referenceEnergy.splice(referenceStart, fixedShape.length, ...fixedShape);
  } else {
    let remainingWh = requiredEnergyWh;
    for (
      let index = referenceStart;
      index < deadline && remainingWh > 0;
      index += 1
    ) {
      const slot = slots[index];
      if (slot === undefined || maximumPowerW === null) break;
      const capacityWh = Math.floor(
        (maximumPowerW * slot.duration_seconds) / 3600,
      );
      const energyWh = Math.min(remainingWh, capacityWh);
      if (
        energyWh > 0 &&
        minimumPowerW !== null &&
        energyWh * 3600 < minimumPowerW * slot.duration_seconds
      ) {
        return failedPreview(slots, "REFERENCE_MINIMUM_POWER_VIOLATED", {
          slot_index: index,
          energy_wh: energyWh,
        });
      }
      referenceEnergy[index] = energyWh;
      remainingWh -= energyWh;
    }
    if (remainingWh !== 0) {
      return failedPreview(slots, "REFERENCE_ENERGY_DOES_NOT_FIT_WINDOW", {
        remaining_energy_wh: remainingWh,
      });
    }
  }
  const siteCap = optionalPositiveInteger(
    data.get("site_import_cap_w"),
    "Site cap",
  );
  const flexibleCap = optionalPositiveInteger(
    data.get("flexible_load_cap_w"),
    "Flexible-load cap",
  );
  const fixedBackground = slots.map((slot, index) =>
    mode === "SHIFT_EXISTING"
      ? slot.measured_energy_wh - (referenceEnergy[index] ?? 0)
      : slot.measured_energy_wh,
  );
  const negativeIndex = fixedBackground.findIndex((energy) => energy < 0);
  if (negativeIndex >= 0) {
    return failedPreview(slots, "NEGATIVE_FIXED_BACKGROUND", {
      slot_index: negativeIndex,
    });
  }
  const unchanged = fixedBackground.map(
    (energy, index) => energy + (referenceEnergy[index] ?? 0),
  );
  for (let index = 0; index < slots.length; index += 1) {
    const slot = slots[index];
    if (slot === undefined) continue;
    const flexibleWh = referenceEnergy[index] ?? 0;
    if (
      flexibleCap !== null &&
      flexibleWh * 3600 > flexibleCap * slot.duration_seconds
    ) {
      return failedPreview(slots, "REFERENCE_FLEXIBLE_LOAD_CAP_EXCEEDED", {
        slot_index: index,
      });
    }
    if (
      siteCap !== null &&
      (unchanged[index] ?? 0) * 3600 > siteCap * slot.duration_seconds
    ) {
      return failedPreview(slots, "REFERENCE_SITE_IMPORT_CAP_EXCEEDED", {
        slot_index: index,
      });
    }
  }
  if (
    mode === "SHIFT_EXISTING" &&
    data.get("shift_existing_attestation") !== "on"
  ) {
    return failedPreview(slots, "SHIFT_EXISTING_ATTESTATION_MISMATCH", {
      missing: ["current load"],
    });
  }
  const loadId = crypto.randomUUID();
  const occurrenceId = crypto.randomUUID();
  let executionSpec: InterruptibleExecutionSpec | FixedShapeExecutionSpec;
  if (fixedShape === null) {
    if (maximumPowerW === null || minimumPowerW === null) {
      throw new Error("Interruptible power fields are unavailable.");
    }
    executionSpec = {
      execution_type: "INTERRUPTIBLE_MODULATING",
      maximum_power_w: maximumPowerW,
      minimum_power_when_active_w: minimumPowerW,
    };
  } else {
    executionSpec = {
      execution_type: "CONTIGUOUS_FIXED_SHAPE",
      fixed_slot_shape_wh: fixedShape,
    };
  }
  const request: ScenarioRequest = {
    request_schema_version: "scenario-operation-v1",
    profile_version_id: profile.profile_version_id,
    tariff_version_id: entryText(data.get("tariff_version_id")),
    account_facts: accountFacts,
    dated_eligibility_facts: {
      facts_as_of: "2026-07-01",
      ev_registered_and_charged_at_premises: true,
      whole_house_metering: true,
      annual_usage_period: { start: "2025-07-01", end: "2026-07-01" },
      annual_usage_wh: 6_000_000,
      annual_baseline_allowance_wh: 2_000_000,
    },
    electrical_constraints: {
      site_import_cap_w: siteCap,
      flexible_load_aggregate_cap_w: flexibleCap,
      energy_basis: "METER_SIDE",
    },
    loads: [
      {
        load_id: loadId,
        physical_asset_key: entryText(data.get("physical_asset_key")),
        kind: entryText(data.get("kind")),
        mode,
        execution_spec: executionSpec,
        occurrences: [
          {
            occurrence_id: occurrenceId,
            required_energy_wh: requiredEnergyWh,
            earliest_start_utc: slots[earliest]?.slot_start_utc ?? "",
            deadline_utc:
              deadline === slots.length
                ? finalBoundary(slots)
                : (slots[deadline]?.slot_start_utc ?? ""),
            reference_schedule: slots.map((slot, index) => ({
              slot_start_utc: slot.slot_start_utc,
              duration_seconds: slot.duration_seconds,
              energy_wh: referenceEnergy[index] ?? 0,
            })),
          },
        ],
      },
    ],
    shift_existing_attestation_load_ids:
      mode === "SHIFT_EXISTING" ? [loadId] : [],
  };
  return {
    request,
    issue: null,
    slotCount: slots.length,
    measuredWh: sum(slots.map((slot) => slot.measured_energy_wh)),
    fixedBackgroundWh: sum(fixedBackground),
    flexibleReferenceWh: sum(referenceEnergy),
    unchangedWh: sum(unchanged),
    positiveReferenceSlots: referenceEnergy.filter((energy) => energy > 0)
      .length,
  };
}

function failedPreview(
  slots: ProfileSlot[],
  code: string,
  witness: Record<string, unknown>,
): Preview {
  const measuredWh = sum(slots.map((slot) => slot.measured_energy_wh));
  return {
    request: null,
    issue: {
      code,
      message: humanize(code),
      witness,
    },
    slotCount: slots.length,
    measuredWh,
    fixedBackgroundWh: measuredWh,
    flexibleReferenceWh: 0,
    unchangedWh: measuredWh,
    positiveReferenceSlots: 0,
  };
}

function boundaryIndex(
  slots: ProfileSlot[],
  value: string,
  allowStartOnly: boolean,
): number | null {
  const target = Date.parse(value);
  if (!Number.isFinite(target)) return null;
  const index = slots.findIndex(
    (slot) => Date.parse(slot.slot_start_utc) === target,
  );
  if (index >= 0) return index;
  if (!allowStartOnly && Date.parse(finalBoundary(slots)) === target)
    return slots.length;
  return null;
}

function finalBoundary(slots: ProfileSlot[]): string {
  const finalSlot = slots.at(-1);
  if (finalSlot === undefined) return "";
  return new Date(
    Date.parse(finalSlot.slot_start_utc) + finalSlot.duration_seconds * 1000,
  ).toISOString();
}

function handleError(
  error: unknown,
  setIssue: (issue: ScenarioIssue | null) => void,
  onMessage: (message: string) => void,
) {
  if (error instanceof ApiError) {
    setIssue({
      code: error.code,
      message: error.message,
      witness: error.witness,
    });
    onMessage(`${error.code}: ${error.message}`);
    return;
  }
  const message =
    error instanceof Error
      ? error.message
      : typeof error === "object" &&
          error !== null &&
          "message" in error &&
          typeof error.message === "string"
        ? error.message
        : `Scenario request failed: ${String(error)}`;
  setIssue({ code: "SCENARIO_CLIENT_ERROR", message, witness: {} });
  onMessage(message);
}

function ScenarioResultView({ resource }: { resource: ScenarioResource }) {
  const result = resource.result;
  const exact = result.exact;
  const selected = exact.selected;
  const reference = exact.reference;
  const heuristic = result.heuristic.selected;
  const decomposition = result.decomposition;
  const changedSlots = selected.schedule.occurrences.flatMap((occurrence) => {
    const referenceOccurrence = reference.schedule.occurrences.find(
      (candidate) => candidate.occurrence_id === occurrence.occurrence_id,
    );
    const heuristicOccurrence = heuristic.schedule.occurrences.find(
      (candidate) => candidate.occurrence_id === occurrence.occurrence_id,
    );
    return occurrence.slots.flatMap((slot, index) => {
      const referenceSlot = referenceOccurrence?.slots[index];
      const heuristicSlot = heuristicOccurrence?.slots[index];
      const referenceEnergy = referenceSlot?.energy_wh ?? 0;
      const heuristicEnergy = heuristicSlot?.energy_wh ?? 0;
      return referenceEnergy === slot.energy_wh &&
        referenceEnergy === heuristicEnergy
        ? []
        : [
            {
              ...slot,
              reference_energy_wh: referenceEnergy,
              heuristic_energy_wh: heuristicEnergy,
            },
          ];
    });
  });
  return (
    <article className="scenario-result" aria-live="polite">
      <div className="scenario-outcome">
        <div>
          <p className="eyebrow">Verified exact solver result</p>
          <h3>
            {exact.search_status === "OPTIMAL" ? "Optimal" : "Best found"}
          </h3>
          <p>
            {exact.search_status === "OPTIMAL"
              ? "All four objective stages were proved optimal."
              : `The first open stage is ${exact.first_open_stage ?? "unreported"}; the cost bound remains open.`}
          </p>
        </div>
        <span className={`status-badge ${exact.search_status.toLowerCase()}`}>
          {humanize(exact.search_status)}
        </span>
      </div>
      <p className="counterfactual-note">
        Historical counterfactual, not a forecast. The result remains on the
        admitted service timestamps and does not claim future rates or behavior.
      </p>
      <dl className="scenario-metrics">
        <Metric
          label="Reference supported cost"
          value={formatMoney(
            reference.billing_result.supported_calculated_cents,
          )}
        />
        <Metric
          label="Selected supported cost"
          value={formatMoney(
            selected.billing_result.supported_calculated_cents,
          )}
        />
        <Metric
          label="Schedule source"
          value={humanize(exact.selected_source)}
        />
        <Metric
          label="Selection reason"
          value={humanize(exact.selection_reason)}
        />
        <Metric
          label="Changed occurrence slots"
          value={selected.verification.objective.changed_occurrence_slot_count.toLocaleString()}
        />
        <Metric
          label="Absolute open cost gap"
          value={
            exact.absolute_cost_gap_cents === null
              ? "Closed"
              : formatMoney(Math.ceil(exact.absolute_cost_gap_cents))
          }
        />
      </dl>
      <h4>Original decomposition and reconstructed unchanged profile</h4>
      <dl className="scenario-metrics">
        <Metric
          label="Fixed background"
          value={formatEnergy(sumEnergy(decomposition.fixed_background))}
        />
        <Metric
          label="Existing-load reference"
          value={formatEnergy(
            sumEnergy(decomposition.shift_existing_reference),
          )}
        />
        <Metric
          label="Historical-addition reference"
          value={formatEnergy(
            sumEnergy(decomposition.historical_addition_reference),
          )}
        />
        <Metric
          label="Reconstructed measured profile"
          value={formatEnergy(
            sumEnergy(decomposition.reconstructed_measured_profile),
          )}
        />
        <Metric
          label="Unchanged reference profile"
          value={formatEnergy(
            sumEnergy(decomposition.unchanged_reference_profile),
          )}
        />
        <Metric label="Exact measured reconstruction" value="Verified" />
      </dl>
      <div className="heuristic-note">
        <strong>
          Off-peak proxy heuristic: {humanize(result.heuristic.search_status)}
        </strong>
        <p>
          {humanize(result.heuristic.selection_outcome)}. This heuristic is not
          bill-optimal. Its displayed cost was recomputed by the reference
          billing engine.
        </p>
      </div>
      <ScheduleHeatmap
        reference={reference.schedule}
        heuristic={heuristic.schedule}
        exact={selected.schedule}
      />
      <div className="table-scroll">
        <table>
          <caption>
            Accessible schedule data for every slot changed by either result
          </caption>
          <thead>
            <tr>
              <th scope="col">UTC slot</th>
              <th scope="col">Reference Wh</th>
              <th scope="col">Heuristic Wh</th>
              <th scope="col">Selected Wh</th>
            </tr>
          </thead>
          <tbody>
            {changedSlots.length === 0 ? (
              <tr>
                <td colSpan={4}>The verified reference remained selected.</td>
              </tr>
            ) : (
              changedSlots.map((slot) => (
                <tr key={slot.slot_start_utc}>
                  <th scope="row">{slot.slot_start_utc}</th>
                  <td>{slot.reference_energy_wh.toLocaleString()}</td>
                  <td>{slot.heuristic_energy_wh.toLocaleString()}</td>
                  <td>{slot.energy_wh.toLocaleString()}</td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
      <details className="manifest">
        <summary>Scenario verification and calculation hashes</summary>
        <dl>
          <Metric
            label="Calculation"
            value={result.manifest.calculation_sha256}
          />
          <Metric label="Result" value={result.result_sha256} />
          <Metric
            label="Verification"
            value={result.manifest.selected_verification_sha256}
          />
          <Metric
            label="Rank calendar"
            value={result.manifest.rank_calendar_sha256}
          />
        </dl>
      </details>
    </article>
  );
}

function ScheduleHeatmap({
  reference,
  heuristic,
  exact,
}: {
  reference: Schedule;
  heuristic: Schedule;
  exact: Schedule;
}) {
  const referenceSlots = reference.occurrences.flatMap(
    (occurrence) => occurrence.slots,
  );
  const heuristicSlots = heuristic.occurrences.flatMap(
    (occurrence) => occurrence.slots,
  );
  const exactSlots = exact.occurrences.flatMap(
    (occurrence) => occurrence.slots,
  );
  const maximum = Math.max(
    1,
    ...referenceSlots.map((slot) => slot.energy_wh),
    ...heuristicSlots.map((slot) => slot.energy_wh),
    ...exactSlots.map((slot) => slot.energy_wh),
  );
  return (
    <div
      className="demo-heatmap"
      aria-label="Reference, heuristic, and exact private schedule heatmap"
    >
      <div className="heatmap-legend" aria-hidden="true">
        <span className="reference-key">Reference</span>
        <span className="heuristic-key">Heuristic</span>
        <span className="exact-key">Exact</span>
      </div>
      <div
        className="heatmap-scroll"
        style={{
          gridTemplateColumns: `repeat(${referenceSlots.length}, minmax(2.3rem, 1fr))`,
        }}
      >
        {referenceSlots.map((slot, index) => (
          <div className="heatmap-slot" key={`${slot.slot_start_utc}-${index}`}>
            <span>{formatSlotLabel(slot.slot_start_utc)}</span>
            <HeatCell
              kind="reference"
              value={slot.energy_wh}
              maximum={maximum}
            />
            <HeatCell
              kind="heuristic"
              value={heuristicSlots[index]?.energy_wh ?? 0}
              maximum={maximum}
            />
            <HeatCell
              kind="exact"
              value={exactSlots[index]?.energy_wh ?? 0}
              maximum={maximum}
            />
          </div>
        ))}
      </div>
    </div>
  );
}

function HeatCell({
  kind,
  value,
  maximum,
}: {
  kind: "reference" | "heuristic" | "exact";
  value: number;
  maximum: number;
}) {
  return (
    <div
      className={`heat ${kind}`}
      style={{ "--heat": value / maximum } as CSSProperties}
      title={`${humanize(kind)} ${value.toLocaleString()} Wh`}
    />
  );
}

function IssueView({ issue }: { issue: ScenarioIssue }) {
  const witnesses = Object.entries(issue.witness);
  return (
    <div className="scenario-issue" role="alert">
      <p className="eyebrow">Scenario not submitted</p>
      <h4>{issue.code}</h4>
      <p>{issue.message}</p>
      {witnesses.length > 0 && (
        <dl>
          {witnesses.map(([key, value]) => (
            <div key={key}>
              <dt>{humanize(key)}</dt>
              <dd>{formatWitness(value)}</dd>
            </div>
          ))}
        </dl>
      )}
    </div>
  );
}

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <dt>{label}</dt>
      <dd>{value}</dd>
    </div>
  );
}

function exactKwhToWh(value: FormDataEntryValue | null): number {
  const text = entryText(value).trim();
  const match = /^(\d+)(?:\.(\d{1,3}))?$/.exec(text);
  if (match === null)
    throw new Error("Energy must use no more than three decimal places.");
  const whole = Number(match[1]);
  const wattHours = whole * 1000 + Number((match[2] ?? "").padEnd(3, "0"));
  if (!Number.isSafeInteger(wattHours) || wattHours <= 0) {
    throw new Error("Required energy is outside the supported exact range.");
  }
  return wattHours;
}

function parseFixedShape(value: FormDataEntryValue | null): number[] {
  const text = entryText(value).trim();
  if (!/^\d+(?:,\d+)*$/.test(text)) {
    throw new Error(
      "Fixed shape must be a comma-separated list of nonnegative integer watt-hours.",
    );
  }
  const shape = text.split(",").map((entry) => Number(entry));
  if (
    shape.some((energy) => !Number.isSafeInteger(energy) || energy < 0) ||
    sum(shape) <= 0
  ) {
    throw new Error("Fixed shape must contain positive total energy.");
  }
  return shape;
}

function positiveInteger(
  value: FormDataEntryValue | null,
  label: string,
): number {
  const parsed = Number(entryText(value));
  if (!Number.isSafeInteger(parsed) || parsed <= 0)
    throw new Error(`${label} must be a positive integer.`);
  return parsed;
}

function nonnegativeInteger(
  value: FormDataEntryValue | null,
  label: string,
): number {
  const parsed = Number(entryText(value));
  if (!Number.isSafeInteger(parsed) || parsed < 0)
    throw new Error(`${label} must be a nonnegative integer.`);
  return parsed;
}

function optionalPositiveInteger(
  value: FormDataEntryValue | null,
  label: string,
): number | null {
  const text = entryText(value).trim();
  return text === "" ? null : positiveInteger(text, label);
}

function entryText(value: FormDataEntryValue | null): string {
  if (typeof value !== "string")
    throw new Error("Scenario fields must contain text.");
  return value;
}

function sum(values: number[]): number {
  return values.reduce((total, value) => total + value, 0);
}

function sumEnergy(values: EnergySlot[]): number {
  return sum(values.map((value) => value.energy_wh));
}

function formatEnergy(energyWh: number): string {
  return `${(energyWh / 1000).toLocaleString("en-US", { maximumFractionDigits: 3 })} kWh`;
}

function formatMoney(cents: number): string {
  const sign = cents < 0 ? "-" : "";
  const absolute = Math.abs(cents);
  return `${sign}$${Math.floor(absolute / 100).toLocaleString("en-US")}.${String(
    absolute % 100,
  ).padStart(2, "0")}`;
}

function humanize(value: string): string {
  return value
    .replaceAll("_", " ")
    .toLowerCase()
    .replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function formatWitness(value: unknown): string {
  if (Array.isArray(value)) return value.map(String).join(", ");
  if (typeof value === "object" && value !== null) return JSON.stringify(value);
  return String(value);
}

function formatSlotLabel(value: string): string {
  return new Date(value).toLocaleString("en-US", {
    month: "short",
    day: "numeric",
    hour: "numeric",
    timeZone: "UTC",
  });
}
