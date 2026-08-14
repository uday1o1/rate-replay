import { FormEvent, useMemo, useState } from "react";

import { api } from "./api";

export type AccountFacts = {
  schema_version: "account-facts-v1";
  service_window: { start: string; end: string };
  service_provider: "PG&E";
  service_mode: "BUNDLED";
  meter_count: 1;
  primary_meter_only: true;
  income_tier: "TIER_3";
  care_enrolled: false;
  fera_enrolled: false;
  medical_baseline: false;
  cca_service: false;
  direct_access_service: false;
  active_bill_protection: false;
  solar_or_export: false;
  baseline_territory: "T";
  baseline_quantity_code: "BASIC";
  qualifying_technologies: ["EV"];
  user_attested_at: string;
};

export type TariffSummary = {
  tariff_version_id: string;
  plan_code: string;
  admission_status: "ADMITTED";
  comparison_admitted: boolean;
  optimization_admitted: boolean;
};

type SourceCoverage = {
  source_id: string;
  source_sha256: string;
  source_url: string;
};

type ComponentCoverage = {
  component_key: string;
  status: "SUPPORTED" | "NOT_APPLICABLE" | "BLOCKED";
  reason_code: string | null;
  contributing_rule_ids: string[];
};

type Candidate = {
  tariff_version_id: string;
  plan_code: string;
  eligibility: { status: string; reason_codes: string[] };
  component_coverage: ComponentCoverage[];
  alternative_plan: null | {
    supported_calculated_cents: number;
    component_coverage: ComponentCoverage[];
    provenance_sources: SourceCoverage[];
    result_sha256: string;
  };
};

type ComparisonExclusion = {
  code: string;
  tariff_version_id: string;
  component_key: string | null;
  eligibility_reason_codes: string[];
};

type ComparisonResource = {
  comparison_id: string;
  repeated: boolean;
  result: {
    candidates: Candidate[];
    exclusions: ComparisonExclusion[];
    required_component_keys: string[];
    common_supported_component_keys: string[];
    rankable: boolean;
    ranked_tariff_version_ids: string[];
    winner_tariff_version_ids: string[];
    savings_against_current_supported_cents: number | null;
    comparison_sha256: string;
  };
};

type ComparisonWorkspaceProps = {
  replayId: string;
  csrf: string;
  accountFacts: AccountFacts;
  tariffs: TariffSummary[];
  onMessage: (message: string) => void;
};

export function ComparisonWorkspace({
  replayId,
  csrf,
  accountFacts,
  tariffs,
  onMessage,
}: ComparisonWorkspaceProps) {
  const [selectedIds, setSelectedIds] = useState<string[]>(() =>
    tariffs.map((tariff) => tariff.tariff_version_id),
  );
  const [comparison, setComparison] = useState<ComparisonResource | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const displayedCandidates = useMemo(() => {
    if (comparison === null || !comparison.result.rankable) {
      return comparison?.result.candidates ?? [];
    }
    const candidatesById = new Map(
      comparison.result.candidates.map((candidate) => [
        candidate.tariff_version_id,
        candidate,
      ]),
    );
    return comparison.result.ranked_tariff_version_ids.flatMap((id) => {
      const candidate = candidatesById.get(id);
      return candidate === undefined ? [] : [candidate];
    });
  }, [comparison]);

  async function createComparison(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (selectedIds.length < 2) {
      onMessage("Select E-1 and at least one alternative plan.");
      return;
    }
    const data = new FormData(event.currentTarget);
    setSubmitting(true);
    try {
      const annualUsageWh = optionalKwhToWh(data.get("annual_usage_kwh"));
      const annualBaselineWh = optionalKwhToWh(data.get("annual_baseline_kwh"));
      const value = await api<ComparisonResource>("/v1/comparisons", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Idempotency-Key": `browser-comparison-${crypto.randomUUID()}`,
          "X-CSRF-Token": csrf,
        },
        body: JSON.stringify({
          request_schema_version: "comparison-operation-v1",
          replay_id: replayId,
          candidate_tariff_version_ids: [...selectedIds].sort(),
          account_facts: accountFacts,
          dated_eligibility_facts: {
            facts_as_of: entryText(data.get("facts_as_of")),
            ev_registered_and_charged_at_premises:
              data.get("ev_registered") === "on",
            whole_house_metering: data.get("whole_house_metering") === "on",
            annual_usage_period: {
              start: entryText(data.get("annual_period_start")),
              end: entryText(data.get("annual_period_end")),
            },
            annual_usage_wh: annualUsageWh,
            annual_baseline_allowance_wh: annualBaselineWh,
          },
        }),
      });
      setComparison(value);
      onMessage(
        value.result.rankable
          ? "Comparable historical plan replay created."
          : "Plan replay completed with ranking blocked by the displayed exclusions.",
      );
    } catch (error) {
      onMessage(
        error instanceof Error ? error.message : "Plan comparison failed.",
      );
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <section className="panel wide" aria-labelledby="comparison-heading">
      <p className="step">04</p>
      <h2 id="comparison-heading">Compare July plans</h2>
      <p>
        Replay the same immutable usage timestamps under admitted historical
        tariffs. Eligibility and comparable-component coverage must both pass
        before RateReplay ranks the supported subtotals.
      </p>
      <form
        className="comparison-form"
        onSubmit={(event) => void createComparison(event)}
      >
        <fieldset className="candidate-picker">
          <legend>Explicit candidate plans</legend>
          {tariffs.map((tariff) => {
            const isCurrent = tariff.tariff_version_id === "pge-e1-2026-07";
            return (
              <label key={tariff.tariff_version_id}>
                <input
                  type="checkbox"
                  checked={selectedIds.includes(tariff.tariff_version_id)}
                  disabled={isCurrent}
                  onChange={(event) => {
                    setSelectedIds((current) =>
                      event.currentTarget.checked
                        ? [...current, tariff.tariff_version_id]
                        : current.filter(
                            (candidateId) =>
                              candidateId !== tariff.tariff_version_id,
                          ),
                    );
                  }}
                />
                <span>
                  {tariff.plan_code}
                  {isCurrent ? " - current plan" : ""}
                </span>
              </label>
            );
          })}
        </fieldset>
        <div className="eligibility-fields">
          <label>
            Facts valid as of
            <input
              name="facts_as_of"
              type="date"
              defaultValue="2026-07-01"
              required
            />
          </label>
          <label>
            Trailing usage period start
            <input
              name="annual_period_start"
              type="date"
              defaultValue="2025-07-01"
              required
            />
          </label>
          <label>
            Trailing usage period end
            <input
              name="annual_period_end"
              type="date"
              defaultValue="2026-07-01"
              required
            />
          </label>
          <label>
            Trailing 12-month usage, kWh
            <input
              name="annual_usage_kwh"
              inputMode="decimal"
              pattern="[0-9]+(\.[0-9]{1,3})?"
              defaultValue="6000"
              placeholder="Leave blank if unknown"
            />
          </label>
          <label>
            Trailing baseline allowance, kWh
            <input
              name="annual_baseline_kwh"
              inputMode="decimal"
              pattern="[0-9]+(\.[0-9]{1,3})?"
              defaultValue="2000"
              placeholder="Leave blank if unknown"
            />
          </label>
        </div>
        <div className="comparison-attestations">
          <label>
            <input name="ev_registered" type="checkbox" defaultChecked />
            The qualifying EV was registered and charged at the premises on the
            facts date.
          </label>
          <label>
            <input name="whole_house_metering" type="checkbox" defaultChecked />
            The service used one whole-house meter on the facts date.
          </label>
        </div>
        <button className="primary" type="submit" disabled={submitting}>
          {submitting ? "Replaying candidates…" : "Replay selected plans"}
        </button>
      </form>

      {comparison !== null && (
        <article className="comparison-result" aria-live="polite">
          {comparison.result.rankable &&
          comparison.result.savings_against_current_supported_cents !== null &&
          comparison.result.winner_tariff_version_ids.length > 0 ? (
            <div className="comparison-outcome comparable-outcome">
              <div>
                <p className="eyebrow">Lowest supported subtotal</p>
                <h3>{winnerNames(comparison.result, tariffs).join(" and ")}</h3>
              </div>
              <div>
                <p className="eyebrow">Supported-charge savings from E-1</p>
                <strong className="comparison-delta">
                  {formatMoney(
                    comparison.result.savings_against_current_supported_cents,
                  )}
                </strong>
              </div>
            </div>
          ) : (
            <div className="comparison-outcome blocked-outcome" role="alert">
              <p className="eyebrow">Ranking blocked</p>
              <h3>No comparable winner</h3>
              <p>
                The supported subtotals below are separate calculations. They
                are not ranked because eligibility or component coverage is
                incomplete.
              </p>
            </div>
          )}

          <div className="table-scroll">
            <table>
              <caption>
                Historical tariff eligibility and supported subtotal coverage
              </caption>
              <thead>
                <tr>
                  <th scope="col">Plan</th>
                  <th scope="col">Eligibility</th>
                  <th scope="col">Supported subtotal</th>
                  <th scope="col">Component coverage</th>
                  <th scope="col">Filed sources</th>
                </tr>
              </thead>
              <tbody>
                {displayedCandidates.map((candidate) => (
                  <tr key={candidate.tariff_version_id}>
                    <th scope="row">{candidate.plan_code}</th>
                    <td>{candidate.eligibility.status}</td>
                    <td>
                      {candidate.alternative_plan === null
                        ? "Not calculated"
                        : formatMoney(
                            candidate.alternative_plan
                              .supported_calculated_cents,
                          )}
                    </td>
                    <td>{coverageSummary(candidate.component_coverage)}</td>
                    <td>
                      {candidate.alternative_plan?.provenance_sources.length ??
                        0}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          {comparison.result.exclusions.length > 0 && (
            <div className="exclusion-report">
              <h4>Why ranking is blocked</h4>
              <ul>
                {comparison.result.exclusions.map((exclusion, index) => (
                  <li
                    key={`${exclusion.tariff_version_id}-${exclusion.code}-${index}`}
                  >
                    <strong>
                      {planName(exclusion.tariff_version_id, tariffs)}:
                    </strong>{" "}
                    {humanize(exclusion.code)}
                    {exclusion.component_key === null
                      ? ""
                      : ` - ${humanize(exclusion.component_key)}`}
                    {exclusion.eligibility_reason_codes.length === 0
                      ? ""
                      : ` (${exclusion.eligibility_reason_codes
                          .map(humanize)
                          .join(", ")})`}
                  </li>
                ))}
              </ul>
            </div>
          )}

          <div className="candidate-evidence">
            {displayedCandidates.map((candidate) => (
              <details key={candidate.tariff_version_id}>
                <summary>{candidate.plan_code} coverage and provenance</summary>
                <dl className="coverage-grid">
                  {candidate.component_coverage.map((component) => (
                    <div key={component.component_key}>
                      <dt>{humanize(component.component_key)}</dt>
                      <dd>{component.status}</dd>
                      {component.reason_code !== null && (
                        <dd>{humanize(component.reason_code)}</dd>
                      )}
                    </div>
                  ))}
                </dl>
                {candidate.alternative_plan !== null && (
                  <ul className="source-list compact-source-list">
                    {candidate.alternative_plan.provenance_sources.map(
                      (source) => (
                        <li key={source.source_id}>
                          <a
                            href={source.source_url}
                            rel="noreferrer"
                            target="_blank"
                          >
                            {source.source_id}
                          </a>
                          <code>{source.source_sha256}</code>
                        </li>
                      ),
                    )}
                  </ul>
                )}
              </details>
            ))}
          </div>
          <p className="coverage-note">
            Common supported components:{" "}
            {comparison.result.common_supported_component_keys
              .map(humanize)
              .join(", ") || "None"}
            . Comparison hash:{" "}
            <code>{comparison.result.comparison_sha256}</code>
          </p>
        </article>
      )}
    </section>
  );
}

function coverageSummary(coverage: ComponentCoverage[]): string {
  const supported = coverage.filter(
    (item) => item.status === "SUPPORTED",
  ).length;
  const blocked = coverage.filter((item) => item.status === "BLOCKED").length;
  return blocked === 0
    ? `${supported} supported, complete`
    : `${supported} supported, ${blocked} blocked`;
}

function optionalKwhToWh(value: FormDataEntryValue | null): number | null {
  const text = entryText(value).trim();
  if (text.length === 0) return null;
  const match = /^(\d+)(?:\.(\d{1,3}))?$/.exec(text);
  if (match === null) {
    throw new Error("Energy must use no more than three decimal places.");
  }
  const whole = Number(match[1]);
  const fractionalWh = Number((match[2] ?? "").padEnd(3, "0"));
  const wattHours = whole * 1000 + fractionalWh;
  if (!Number.isSafeInteger(whole) || !Number.isSafeInteger(wattHours)) {
    throw new Error("Energy is outside the supported exact range.");
  }
  return wattHours;
}

function entryText(value: FormDataEntryValue | null): string {
  if (value === null) return "";
  if (typeof value !== "string") {
    throw new Error("This field must contain text, not a file.");
  }
  return value;
}

function winnerNames(
  result: ComparisonResource["result"],
  tariffs: TariffSummary[],
): string[] {
  return result.winner_tariff_version_ids.map((id) => planName(id, tariffs));
}

function planName(id: string, tariffs: TariffSummary[]): string {
  return (
    tariffs.find((tariff) => tariff.tariff_version_id === id)?.plan_code ?? id
  );
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
    .replace(/\b\w/g, (letter) => letter.toUpperCase());
}
