import { CSSProperties, useEffect, useRef, useState } from "react";

import {
  DemoArtifacts,
  DemoRedactedReport,
  HeatmapSlot,
  loadDemoArtifacts,
} from "./demoArtifacts";

const STEPS = [
  "Welcome",
  "Import review",
  "Bill replay",
  "Plan comparison",
  "Load scheduling",
  "Redacted report",
] as const;

const STEP_HEADING_IDS = [
  "demo-welcome",
  "demo-import",
  "demo-replay",
  "demo-comparison",
  "demo-scheduling",
  "demo-report",
] as const;

export function PublicDemo() {
  const [artifacts, setArtifacts] = useState<DemoArtifacts | null>(null);
  const [failure, setFailure] = useState<string | null>(null);
  const [step, setStep] = useState(0);
  const stepPanel = useRef<HTMLDivElement>(null);
  const progress = useRef<HTMLElement>(null);
  const progressButtons = useRef<Array<HTMLButtonElement | null>>([]);
  const navigationRequested = useRef(false);

  useEffect(() => {
    void loadDemoArtifacts()
      .then(setArtifacts)
      .catch((error: unknown) =>
        setFailure(
          error instanceof Error
            ? error.message
            : "PUBLIC_DEMO_INTEGRITY_FAILED",
        ),
      );
  }, []);

  useEffect(() => {
    if (!navigationRequested.current || artifacts === null) return;
    const activeButton = progressButtons.current[step];
    if (activeButton !== null && activeButton !== undefined) {
      const left = Math.max(
        0,
        activeButton.offsetLeft -
          ((progress.current?.clientWidth ?? activeButton.clientWidth) -
            activeButton.clientWidth) /
            2,
      );
      progress.current?.scrollTo?.({ left });
    }
    stepPanel.current?.focus({ preventScroll: true });
    stepPanel.current?.scrollIntoView?.({ block: "start" });
  }, [artifacts, step]);

  function navigate(nextStep: number) {
    navigationRequested.current = true;
    setStep(Math.max(0, Math.min(STEPS.length - 1, nextStep)));
  }

  if (failure !== null) {
    return (
      <section className="panel demo-failure" role="alert">
        <p className="eyebrow">Public demo unavailable</p>
        <h2>Artifact integrity check failed</h2>
        <p>{failure}</p>
      </section>
    );
  }
  if (artifacts === null) {
    return (
      <section className="panel" aria-live="polite">
        Verifying the immutable simulated demo artifacts…
      </section>
    );
  }
  return (
    <div className="demo-workspace">
      <nav
        ref={progress}
        className="demo-progress"
        aria-label="Public demo progress"
      >
        <ol>
          {STEPS.map((label, index) => (
            <li key={label} aria-current={index === step ? "step" : undefined}>
              <button
                ref={(element) => {
                  progressButtons.current[index] = element;
                }}
                type="button"
                onClick={() => navigate(index)}
              >
                <span>{String(index + 1).padStart(2, "0")}</span>
                {label}
              </button>
            </li>
          ))}
        </ol>
      </nav>
      <div
        ref={stepPanel}
        className="demo-step-shell"
        role="group"
        aria-live="polite"
        aria-labelledby={STEP_HEADING_IDS[step]}
        tabIndex={-1}
      >
        {step === 0 && <Welcome artifacts={artifacts} />}
        {step === 1 && <ImportReview artifacts={artifacts} />}
        {step === 2 && <BillReplay artifacts={artifacts} />}
        {step === 3 && <Comparison artifacts={artifacts} />}
        {step === 4 && <Scheduling artifacts={artifacts} />}
        {step === 5 && <RedactedReport report={artifacts.redactedReport} />}
      </div>
      <div className="demo-actions">
        <button
          type="button"
          disabled={step === 0}
          onClick={() => navigate(step - 1)}
        >
          Previous
        </button>
        {step < STEPS.length - 1 && (
          <button
            className="primary"
            type="button"
            onClick={() => navigate(step + 1)}
          >
            {step === 0 ? "Start the walkthrough" : "Continue"}
          </button>
        )}
      </div>
    </div>
  );
}

function Welcome({ artifacts }: { artifacts: DemoArtifacts }) {
  return (
    <section className="panel demo-panel" aria-labelledby="demo-welcome">
      <p className="step">Public demo</p>
      <h2 id="demo-welcome">One simulated July story, fully traceable</h2>
      <p className="demo-lede">
        Follow a locked 750 kWh household profile from quality review to a
        redacted report. Nothing here is your data, a future forecast, or an
        official utility bill.
      </p>
      <div className="demo-trust-grid">
        <div>
          <strong>No account</strong>
          <span>No cookies, shared tenant, or visitor state.</span>
        </div>
        <div>
          <strong>No API jobs</strong>
          <span>No upload, mutation, or authenticated request.</span>
        </div>
        <div>
          <strong>Content locked</strong>
          <span>Every artifact is verified before display.</span>
        </div>
      </div>
      <p className="artifact-lock">
        Demo manifest <code>{artifacts.manifestSha256}</code>
      </p>
    </section>
  );
}

function ImportReview({ artifacts }: { artifacts: DemoArtifacts }) {
  const value = artifacts.importReview;
  return (
    <section className="panel demo-panel" aria-labelledby="demo-import">
      <p className="step">01 - Import review</p>
      <h2 id="demo-import">The complete July profile is calculation ready</h2>
      <p>
        This NREL-derived vector is always labeled simulated. The production
        parser contract validated one contiguous start-inclusive and
        end-exclusive billing period before any calculation ran.
      </p>
      <dl className="scenario-metrics">
        <Metric label="Quality state" value={value.quality_status} />
        <Metric
          label="Intervals"
          value={value.reading_count.toLocaleString()}
        />
        <Metric
          label="Resolution"
          value={`${value.interval_resolution_seconds / 60} minutes`}
        />
        <Metric
          label="Total usage"
          value={formatEnergy(value.total_energy_wh)}
        />
        <Metric
          label="Coverage start"
          value={formatNanoseconds(value.coverage_start_utc_ns)}
        />
        <Metric
          label="Coverage end"
          value={formatNanoseconds(value.coverage_end_utc_ns)}
        />
      </dl>
      <p className="quality-ok">No fatal or warning findings.</p>
      <details className="manifest">
        <summary>Profile integrity</summary>
        <dl>
          <Metric
            label="Parser contract"
            value={value.parser_contract_version}
          />
          <Metric
            label="Profile content"
            value={value.profile_content_sha256}
          />
          <Metric
            label="Source artifact"
            value={value.source_artifact_sha256}
          />
        </dl>
      </details>
    </section>
  );
}

function BillReplay({ artifacts }: { artifacts: DemoArtifacts }) {
  const replay = artifacts.billReplay;
  return (
    <section className="panel demo-panel" aria-labelledby="demo-replay">
      <p className="step">02 - Historical bill replay</p>
      <h2 id="demo-replay">Supported charges are separate from the gap</h2>
      <div className="result-summary">
        <div>
          <p className="eyebrow">Supported calculated charges</p>
          <h3>{formatMoney(replay.supported_calculated_cents)}</h3>
        </div>
        <span className="status-badge">{replay.eligibility.status}</span>
      </div>
      <div className="table-scroll">
        <table>
          <caption>Source-linked supported charge lines</caption>
          <thead>
            <tr>
              <th scope="col">Charge</th>
              <th scope="col">Amount</th>
              <th scope="col">Filed source</th>
            </tr>
          </thead>
          <tbody>
            {replay.line_items.map((line) => (
              <tr key={line.line_item_key}>
                <th scope="row">{humanize(line.line_item_key)}</th>
                <td>{formatMoney(line.rounded_cents)}</td>
                <td>
                  <code>{line.source_id}</code>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div
        className="reconciliation"
        aria-label="Simulated bill reconciliation"
      >
        <dl>
          <Metric
            label="Entered bill"
            value={formatMoney(replay.reconciliation.entered_bill_total_cents)}
          />
          <Metric
            label="User-entered unsupported"
            value={formatMoney(replay.reconciliation.user_unsupported_cents)}
          />
          <Metric
            label="Unexplained residual"
            value={formatMoney(
              replay.reconciliation.unexplained_residual_cents,
            )}
          />
          <Metric
            label="Review state"
            value={humanize(replay.reconciliation.classification)}
          />
        </dl>
        <p className="unsupported-line">
          The simulated local tax is user-entered and unsupported. It applies
          only to reconciling this current bill and is never copied to another
          tariff.
        </p>
        <p className="coverage-note">
          The signed residual is the entered bill minus supported charges and
          the explicit unsupported line. RateReplay leaves that gap visible
          instead of inventing a charge to force a match.
        </p>
        <p className="reconciliation-equation">
          {formatMoney(replay.reconciliation.entered_bill_total_cents)} entered
          bill = {formatMoney(replay.supported_calculated_cents)} supported +{" "}
          {formatMoney(replay.reconciliation.user_unsupported_cents)} explicit
          unsupported +{" "}
          {formatMoney(replay.reconciliation.unexplained_residual_cents)}
          residual.
        </p>
      </div>
    </section>
  );
}

function Comparison({ artifacts }: { artifacts: DemoArtifacts }) {
  const comparison = artifacts.tariffComparison;
  const winner = comparison.candidates.find((candidate) =>
    comparison.winner_tariff_version_ids.includes(candidate.tariff_version_id),
  );
  return (
    <section className="panel demo-panel" aria-labelledby="demo-comparison">
      <p className="step">03 - Same timestamps, five plans</p>
      <h2 id="demo-comparison">
        The ranking passes every eligibility and coverage gate
      </h2>
      <div className="comparison-outcome comparable-outcome">
        <div>
          <p className="eyebrow">Lowest supported subtotal</p>
          <h3>{winner?.plan_code ?? "No winner"}</h3>
        </div>
        <div>
          <p className="eyebrow">Supported-charge difference from E-1</p>
          <strong className="comparison-delta">
            {comparison.savings_against_current_supported_cents === null
              ? "Not rankable"
              : formatMoney(comparison.savings_against_current_supported_cents)}
          </strong>
        </div>
      </div>
      <div className="table-scroll">
        <table>
          <caption>Historical supported subtotals and coverage</caption>
          <thead>
            <tr>
              <th scope="col">Plan</th>
              <th scope="col">Eligibility</th>
              <th scope="col">Supported subtotal</th>
              <th scope="col">Coverage</th>
            </tr>
          </thead>
          <tbody>
            {comparison.candidates.map((candidate) => (
              <tr key={candidate.tariff_version_id}>
                <th scope="row">{candidate.plan_code}</th>
                <td>{candidate.eligibility.status}</td>
                <td>
                  {candidate.alternative_plan === null
                    ? "Not calculated"
                    : formatMoney(
                        candidate.alternative_plan.supported_calculated_cents,
                      )}
                </td>
                <td>
                  {candidate.component_coverage.every(
                    (component) => component.status !== "BLOCKED",
                  )
                    ? "Complete"
                    : "Blocked"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="coverage-note">
        The current bill's unsupported tax and residual are excluded from every
        alternative plan. If any candidate were ineligible or incomplete, this
        view would show exclusions without a winner or savings value.
      </p>
    </section>
  );
}

function Scheduling({ artifacts }: { artifacts: DemoArtifacts }) {
  const scenario = artifacts.scenarioInputs;
  const exact = artifacts.solverResult;
  const heuristic = artifacts.heuristicResult;
  const reference = artifacts.referenceResult;
  const verification = artifacts.verificationRecord;
  return (
    <section className="panel demo-panel" aria-labelledby="demo-scheduling">
      <p className="step">04 - Historical flexible load</p>
      <h2 id="demo-scheduling">
        Move one simulated EV addition on July's actual timestamps
      </h2>
      <p className="counterfactual-note">
        Historical addition means a counterfactual on this past service window.
        It is not a future forecast, current-rate projection, or device-control
        instruction.
      </p>
      <dl className="scenario-metrics">
        <Metric label="Load" value={scenario.load.kind} />
        <Metric label="Treatment" value="Historical addition" />
        <Metric
          label="Required energy"
          value={formatEnergy(scenario.load.required_energy_wh)}
        />
        <Metric
          label="Reference validation"
          value={scenario.reference_validation.status}
        />
        <Metric
          label="Reference supported cost"
          value={formatMoney(reference.supported_cost_cents)}
        />
        <Metric
          label="Exact selected cost"
          value={formatMoney(exact.supported_cost_cents)}
        />
      </dl>
      <div className="schedule-language-grid">
        <article>
          <p className="eyebrow">Unchanged reference</p>
          <h3>Locked simulated baseline</h3>
          <p>
            The public demo artifact supplies this complete schedule as the
            comparison baseline. It is not visitor input or an inference from
            household behavior.
          </p>
        </article>
        <article>
          <p className="eyebrow">Off-peak proxy heuristic</p>
          <h3>{humanize(heuristic.search_status)}</h3>
          <p>
            {humanize(heuristic.selection_outcome)}. This deterministic proxy is
            a simple off-peak surrogate and makes no bill-optimality claim.
          </p>
        </article>
        <article>
          <p className="eyebrow">Exact solver</p>
          <h3>
            {exact.search_status === "OPTIMAL" ? "Optimal" : "Best found"}
          </h3>
          <p>
            {exact.search_status === "OPTIMAL"
              ? `All four objective stages were proved optimal, and independent verification returned ${humanize(verification.status)}.`
              : `Stage ${exact.first_open_stage ?? "unknown"} remains open, so this is not labeled optimal.`}
          </p>
        </article>
      </div>
      <ScheduleHeatmap slots={scenario.heatmap_slots} />
      <h3>Original decomposition</h3>
      <dl className="scenario-metrics">
        <Metric
          label="Fixed background"
          value={formatEnergy(scenario.decomposition.fixed_background_wh)}
        />
        <Metric
          label="Historical-addition reference"
          value={formatEnergy(
            scenario.decomposition.historical_addition_reference_wh,
          )}
        />
        <Metric
          label="Reconstructed measured profile"
          value={formatEnergy(
            scenario.decomposition.reconstructed_measured_profile_wh,
          )}
        />
        <Metric
          label="Unchanged reference profile"
          value={formatEnergy(
            scenario.decomposition.unchanged_reference_profile_wh,
          )}
        />
        <Metric label="Measured reconstruction" value="Exact" />
        <Metric label="Independent verification" value={verification.status} />
      </dl>
      <details className="manifest">
        <summary>Verification and calculation hashes</summary>
        <dl>
          <Metric label="Calculation" value={exact.calculation_sha256} />
          <Metric label="Scenario result" value={exact.result_sha256} />
          <Metric
            label="Verification"
            value={verification.verification_sha256}
          />
          <Metric
            label="Reference verification"
            value={reference.verification_sha256}
          />
        </dl>
      </details>
    </section>
  );
}

function ScheduleHeatmap({ slots }: { slots: HeatmapSlot[] }) {
  const maximum = Math.max(
    1,
    ...slots.flatMap((slot) => [
      slot.reference_energy_wh,
      slot.heuristic_energy_wh,
      slot.selected_energy_wh,
    ]),
  );
  return (
    <div
      className="demo-heatmap"
      aria-label="Reference, heuristic, and exact schedule heatmap"
    >
      <div className="heatmap-legend" aria-hidden="true">
        <span className="reference-key">Reference</span>
        <span className="heuristic-key">Heuristic</span>
        <span className="exact-key">Exact</span>
      </div>
      <div className="heatmap-scroll">
        {slots.map((slot) => (
          <div className="heatmap-slot" key={slot.slot_start_utc}>
            <span>{formatTime(slot.slot_start_utc)}</span>
            <div
              className="heat reference"
              style={
                {
                  "--heat": slot.reference_energy_wh / maximum,
                } as CSSProperties
              }
              title={`Reference ${slot.reference_energy_wh} Wh`}
            />
            <div
              className="heat heuristic"
              style={
                {
                  "--heat": slot.heuristic_energy_wh / maximum,
                } as CSSProperties
              }
              title={`Heuristic ${slot.heuristic_energy_wh} Wh`}
            />
            <div
              className="heat exact"
              style={
                {
                  "--heat": slot.selected_energy_wh / maximum,
                } as CSSProperties
              }
              title={`Exact ${slot.selected_energy_wh} Wh`}
            />
          </div>
        ))}
      </div>
      <details className="heatmap-table">
        <summary>Schedule values table</summary>
        <div className="table-scroll">
          <table>
            <caption>
              Reference, heuristic, and exact energy for every displayed slot
            </caption>
            <thead>
              <tr>
                <th scope="col">Slot start</th>
                <th scope="col">Reference Wh</th>
                <th scope="col">Heuristic Wh</th>
                <th scope="col">Exact Wh</th>
              </tr>
            </thead>
            <tbody>
              {slots.map((slot) => (
                <tr key={`table-${slot.slot_start_utc}`}>
                  <th scope="row">{formatTime(slot.slot_start_utc)}</th>
                  <td>{slot.reference_energy_wh.toLocaleString()}</td>
                  <td>{slot.heuristic_energy_wh.toLocaleString()}</td>
                  <td>{slot.selected_energy_wh.toLocaleString()}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </details>
    </div>
  );
}

export function RedactedReport({
  report,
  headingId = "demo-report",
  stepLabel = "05 - Aggregate-only export",
}: {
  report: DemoRedactedReport;
  headingId?: string;
  stepLabel?: string;
}) {
  return (
    <section
      className="panel demo-panel report-paper"
      aria-labelledby={headingId}
    >
      <p className="step">{stepLabel}</p>
      <h2 id={headingId}>Redacted historical scheduling report</h2>
      <p>
        This export is deny-by-default. It contains no utility identifier, raw
        interval history, daily series, occurrence window, or exact reference or
        optimized load slot.
      </p>
      <dl className="scenario-metrics">
        <Metric
          label="Billing period"
          value={`${report.billing_period.start} to ${report.billing_period.end}`}
        />
        <Metric
          label="Measured energy"
          value={formatEnergy(report.aggregate_measured_energy_wh)}
        />
        <Metric
          label="Reference flexible energy"
          value={formatEnergy(report.aggregate_reference_flexible_energy_wh)}
        />
        <Metric
          label="Aggregate shifted energy"
          value={formatEnergy(report.aggregate_shifted_energy_wh)}
        />
        <Metric
          label="Reference supported cost"
          value={formatMoney(report.reference_supported_cost_cents)}
        />
        <Metric
          label="Selected supported cost"
          value={formatMoney(report.selected_supported_cost_cents)}
        />
        <Metric
          label="Supported cost difference"
          value={formatMoney(report.supported_cost_difference_cents)}
        />
        <Metric
          label="Solver status"
          value={humanize(report.solver.search_status)}
        />
        <Metric
          label="Verification"
          value={report.solver.verification_status}
        />
      </dl>
      <div className="table-scroll">
        <table>
          <caption>Allowlisted supported charge aggregates</caption>
          <thead>
            <tr>
              <th scope="col">Component</th>
              <th scope="col">Amount</th>
            </tr>
          </thead>
          <tbody>
            {report.supported_charge_components.map((component) => (
              <tr key={component.component_key}>
                <th scope="row">{humanize(component.component_key)}</th>
                <td>{formatMoney(component.amount_cents)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="counterfactual-note">
        Historical counterfactual, not a forecast. Supported charges only.
      </p>
      <details className="manifest">
        <summary>Redaction contract and report hash</summary>
        <dl>
          <Metric label="Policy" value={report.redaction_policy_version} />
          <Metric label="Template" value={report.report_template_version} />
          <Metric label="Report" value={report.report_sha256} />
        </dl>
      </details>
    </section>
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

function formatMoney(cents: number): string {
  const sign = cents < 0 ? "-" : "";
  const absolute = Math.abs(cents);
  return `${sign}$${Math.floor(absolute / 100).toLocaleString("en-US")}.${String(
    absolute % 100,
  ).padStart(2, "0")}`;
}

function formatEnergy(wattHours: number): string {
  return `${(wattHours / 1000).toLocaleString("en-US", {
    maximumFractionDigits: 3,
  })} kWh`;
}

function formatNanoseconds(nanoseconds: number): string {
  return new Date(nanoseconds / 1_000_000).toISOString().replace(".000Z", "Z");
}

function formatTime(value: string): string {
  return new Date(value).toLocaleTimeString("en-US", {
    hour: "numeric",
    minute: "2-digit",
    timeZone: "UTC",
  });
}

function humanize(value: string): string {
  return value
    .replaceAll("_", " ")
    .toLowerCase()
    .replace(/\b\w/g, (letter) => letter.toUpperCase());
}
