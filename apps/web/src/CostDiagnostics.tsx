type ChargeComponentKey =
  | "baseline_allowance"
  | "bundled_energy"
  | "baseline_adjustment"
  | "base_services_charge"
  | "california_climate_credit"
  | "minimum_bill_adjustment"
  | "explicit_unsupported";

type DailyEnergyChargeAllocation = {
  service_day: string;
  line_item_key: string;
  charge_component_key: ChargeComponentKey;
  allocation_weight_wh: number;
  allocated_cents: number;
};

type MonthlyEnergyChargeAllocation = {
  calendar_month: string;
  allocation_weight_wh: number;
  allocated_cents: number;
};

type BillingPeriodAdjustment = {
  adjustment_kind:
    | "SUPPORTED_PERIOD_CHARGE"
    | "USER_UNSUPPORTED"
    | "UNEXPLAINED_RESIDUAL"
    | "TIER_RESET_CONTEXT";
  line_item_key: string;
  charge_component_key: ChargeComponentKey | null;
  amount_cents: number;
};

export type DiagnosticCostAllocation = {
  allocation_version: "private-cost-allocation-v1";
  status: "AVAILABLE" | "INTERVAL_DATA_UNAVAILABLE";
  timezone: "America/Los_Angeles";
  daily_energy_charges: DailyEnergyChargeAllocation[];
  monthly_energy_charges: MonthlyEnergyChargeAllocation[];
  billing_period_adjustments: BillingPeriodAdjustment[];
  reconciliation: {
    daily_energy_charge_cents: number;
    supported_period_adjustment_cents: number;
    supported_calculated_cents: number;
    user_unsupported_cents: number;
    unexplained_residual_cents: number;
    displayed_total_cents: number;
  };
};

function formatMoney(cents: number): string {
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
  }).format(cents / 100);
}

function humanize(value: string): string {
  return value
    .replaceAll(".", " ")
    .replaceAll("_", " ")
    .replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function formatServiceDay(value: string): string {
  return new Intl.DateTimeFormat("en-US", {
    month: "short",
    day: "numeric",
    timeZone: "America/Los_Angeles",
  }).format(new Date(`${value}T12:00:00-07:00`));
}

function formatMonth(value: string): string {
  return new Intl.DateTimeFormat("en-US", {
    month: "long",
    year: "numeric",
    timeZone: "UTC",
  }).format(new Date(`${value}-01T12:00:00Z`));
}

function adjustmentLabel(adjustment: BillingPeriodAdjustment): string {
  if (adjustment.adjustment_kind === "USER_UNSUPPORTED") {
    return `User-entered unsupported: ${humanize(adjustment.line_item_key)}`;
  }
  if (adjustment.adjustment_kind === "UNEXPLAINED_RESIDUAL") {
    return "Signed unexplained residual";
  }
  if (adjustment.adjustment_kind === "TIER_RESET_CONTEXT") {
    return "Tier boundaries reset once for this billing period";
  }
  return humanize(adjustment.line_item_key);
}

function aggregateDays(
  allocations: DailyEnergyChargeAllocation[],
): { serviceDay: string; allocatedCents: number }[] {
  const totals = new Map<string, number>();
  for (const allocation of allocations) {
    totals.set(
      allocation.service_day,
      (totals.get(allocation.service_day) ?? 0) + allocation.allocated_cents,
    );
  }
  return [...totals.entries()]
    .sort(([left], [right]) => left.localeCompare(right))
    .map(([serviceDay, allocatedCents]) => ({ serviceDay, allocatedCents }));
}

function barHeight(value: number, maximum: number): string {
  if (maximum === 0) {
    return "0%";
  }
  return `${Math.max(4, Math.round((Math.abs(value) / maximum) * 100))}%`;
}

export function CostDiagnostics({
  allocation,
}: {
  allocation: DiagnosticCostAllocation | null;
}) {
  if (
    allocation === null ||
    allocation.status === "INTERVAL_DATA_UNAVAILABLE"
  ) {
    return (
      <section
        className="cost-diagnostics"
        aria-labelledby="cost-diagnostics-heading"
      >
        <h4 id="cost-diagnostics-heading">Private cost diagnostics</h4>
        <p className="coverage-note">
          This result contract does not contain canonical interval allocations.
          The authoritative billing-period line items remain available above.
        </p>
      </section>
    );
  }

  const days = aggregateDays(allocation.daily_energy_charges);
  const maximumDaily = Math.max(
    0,
    ...days.map((day) => Math.abs(day.allocatedCents)),
  );
  const maximumMonthly = Math.max(
    0,
    ...allocation.monthly_energy_charges.map((month) =>
      Math.abs(month.allocated_cents),
    ),
  );

  return (
    <section
      className="cost-diagnostics"
      aria-labelledby="cost-diagnostics-heading"
    >
      <p className="eyebrow">Private interval diagnostics</p>
      <h4 id="cost-diagnostics-heading">
        Where supported energy charges occurred
      </h4>
      <p>
        These are traceable allocations of rounded billing-period charges, not
        independent daily or monthly bills. Fixed charges, credits, tier
        context, unsupported amounts, and the signed residual stay in separately
        labeled billing-period rows.
      </p>

      <div className="cost-diagnostic-grid">
        <article>
          <h5>Daily energy-charge allocation</h5>
          <div className="daily-cost-chart" aria-hidden="true">
            {days.map((day) => (
              <span
                className="daily-cost-column"
                key={day.serviceDay}
                title={`${formatServiceDay(day.serviceDay)}: ${formatMoney(
                  day.allocatedCents,
                )}`}
              >
                <span
                  className={day.allocatedCents < 0 ? "negative" : ""}
                  style={{
                    height: barHeight(day.allocatedCents, maximumDaily),
                  }}
                />
              </span>
            ))}
          </div>
          <div className="table-scroll compact-diagnostic-table">
            <table>
              <caption>
                Accessible table for daily energy-charge allocations
              </caption>
              <thead>
                <tr>
                  <th scope="col">Service day</th>
                  <th scope="col">Allocated charge</th>
                </tr>
              </thead>
              <tbody>
                {days.map((day) => (
                  <tr key={day.serviceDay}>
                    <th scope="row">{formatServiceDay(day.serviceDay)}</th>
                    <td>{formatMoney(day.allocatedCents)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </article>

        <article>
          <h5>Monthly energy-charge allocation</h5>
          <div className="monthly-cost-chart" aria-hidden="true">
            {allocation.monthly_energy_charges.map((month) => (
              <div key={month.calendar_month}>
                <span>{formatMonth(month.calendar_month)}</span>
                <span className="monthly-cost-track">
                  <span
                    className={month.allocated_cents < 0 ? "negative" : ""}
                    style={{
                      width: barHeight(month.allocated_cents, maximumMonthly),
                    }}
                  />
                </span>
                <strong>{formatMoney(month.allocated_cents)}</strong>
              </div>
            ))}
          </div>
          <div className="table-scroll compact-diagnostic-table">
            <table>
              <caption>
                Accessible table for monthly energy-charge allocations
              </caption>
              <thead>
                <tr>
                  <th scope="col">Calendar month</th>
                  <th scope="col">Allocated charge</th>
                </tr>
              </thead>
              <tbody>
                {allocation.monthly_energy_charges.map((month) => (
                  <tr key={month.calendar_month}>
                    <th scope="row">{formatMonth(month.calendar_month)}</th>
                    <td>{formatMoney(month.allocated_cents)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </article>
      </div>

      <div className="table-scroll period-adjustment-table">
        <table>
          <caption>Billing-period adjustments and reconciliation rows</caption>
          <thead>
            <tr>
              <th scope="col">Period-level row</th>
              <th scope="col">Classification</th>
              <th scope="col">Amount</th>
            </tr>
          </thead>
          <tbody>
            {allocation.billing_period_adjustments.map((adjustment) => (
              <tr
                key={`${adjustment.adjustment_kind}-${adjustment.line_item_key}`}
              >
                <th scope="row">{adjustmentLabel(adjustment)}</th>
                <td>{humanize(adjustment.adjustment_kind)}</td>
                <td>{formatMoney(adjustment.amount_cents)}</td>
              </tr>
            ))}
          </tbody>
          <tfoot>
            <tr>
              <th scope="row" colSpan={2}>
                Reconciled displayed total
              </th>
              <td>
                <strong>
                  {formatMoney(allocation.reconciliation.displayed_total_cents)}
                </strong>
              </td>
            </tr>
          </tfoot>
        </table>
      </div>
      <p className="coverage-note diagnostic-reconciliation-note">
        Daily energy allocations{" "}
        {formatMoney(allocation.reconciliation.daily_energy_charge_cents)} plus
        supported period rows{" "}
        {formatMoney(
          allocation.reconciliation.supported_period_adjustment_cents,
        )}{" "}
        reconcile exactly to the authoritative supported total{" "}
        {formatMoney(allocation.reconciliation.supported_calculated_cents)}.
      </p>
    </section>
  );
}
