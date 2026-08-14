import { FormEvent, useEffect, useState } from "react";

import {
  AccountFacts,
  ComparisonWorkspace,
  TariffSummary,
} from "./ComparisonWorkspace";
import { api } from "./api";
import { ScenarioWorkspace } from "./ScenarioWorkspace";
import "./styles.css";

type SessionUser = { user_id: string; username: string };
type Session = { user: SessionUser; csrf_token: string | null };
type Finding = {
  code: string;
  severity: string;
  field_path: string;
  warning_id: string | null;
};
type ImportStatus = {
  import_id: string;
  state: string;
  job_state: string;
  reading_count: number;
  interval_resolution_seconds: number | null;
  coverage_start_utc_ns: number | null;
  coverage_end_utc_ns: number | null;
  findings: Finding[];
  failure_code: string | null;
  profile_version_id?: string | null;
};
type Profile = {
  import_id: string;
  profile_version_id: string;
  content_hash: string;
  billing_period_start_utc_ns: number;
  billing_period_end_utc_ns: number;
};
type BuiltInSimulatedProfile = {
  simulated: true;
  label: string;
  source_artifact_sha256: string;
  repeated: boolean;
  profile: Profile;
};
type SourceCoverage = {
  source_id: string;
  source_sha256: string;
  source_url: string;
  linked_rule_ids: string[];
};
type TariffDetail = {
  admission: {
    tariff_version_id: string;
    plan_code: string;
    admitted_service_windows: [string, string][];
    compiler_content_sha256: string;
    scope: {
      calculation_time_mode: "HISTORICAL_REPLAY";
      comparison_admitted: boolean;
      optimization_admitted: boolean;
    };
  };
  compilation: {
    reports: { source_coverage: SourceCoverage[] };
  };
};
type ReplayLine = {
  rule_id: string;
  source_id: string;
  line_item_key: string;
  quantity_numerator: number;
  quantity_denominator: number;
  quantity_unit: string;
  rate_numerator_microdollars: number;
  rate_denominator: number;
  rate_unit: string;
  rounded_cents: number;
};
type UnsupportedLine = {
  line_item_key: string;
  description: string;
  amount_cents: number;
};
type ReplayResource = {
  replay_id: string;
  repeated: boolean;
  result: {
    eligibility: { status: string; reason_codes: string[] };
    supported_calculated_cents: number;
    line_items: ReplayLine[];
    user_unsupported_lines: UnsupportedLine[];
    reconciliation: null | {
      user_unsupported_cents: number;
      unexplained_residual_cents: number;
      entered_bill_total_cents: number;
      classification: string;
    };
    provenance_sources: SourceCoverage[];
    manifest: {
      calculation_time_mode: "HISTORICAL_REPLAY";
      tariff_compiler_content_sha256: string;
      replay_input_sha256: string;
      reconciliation_input_sha256: string | null;
      reconciliation_policy_sha256: string | null;
      calculation_sha256: string;
    };
    result_sha256: string;
  };
};

function csrfCookie(): string | null {
  const prefix = "__Host-ratereplay_csrf=";
  const value = document.cookie
    .split(";")
    .map((part) => part.trim())
    .find((part) => part.startsWith(prefix));
  return value === undefined
    ? null
    : decodeURIComponent(value.slice(prefix.length));
}

export function App() {
  const [session, setSession] = useState<Session | null>(null);
  const [checkingSession, setCheckingSession] = useState(true);
  const [authMode, setAuthMode] = useState<"register" | "login">("register");
  const [csrf, setCsrf] = useState<string | null>(null);
  const [importStatus, setImportStatus] = useState<ImportStatus | null>(null);
  const [profile, setProfile] = useState<Profile | null>(null);
  const [tariff, setTariff] = useState<TariffDetail | null>(null);
  const [tariffs, setTariffs] = useState<TariffSummary[]>([]);
  const [replay, setReplay] = useState<ReplayResource | null>(null);
  const [comparisonAccountFacts, setComparisonAccountFacts] =
    useState<AccountFacts | null>(null);
  const [acknowledgedWarnings, setAcknowledgedWarnings] = useState<Set<string>>(
    new Set(),
  );
  const [pgeAttested, setPgeAttested] = useState(false);
  const [message, setMessage] = useState<string | null>(null);

  useEffect(() => {
    void api<Session>("/v1/auth/session")
      .then((value) => {
        setSession(value);
        setCsrf(csrfCookie());
      })
      .catch(() => setSession(null))
      .finally(() => setCheckingSession(false));
  }, []);

  useEffect(() => {
    if (session === null) return;
    void Promise.all([
      api<{ items: Profile[] }>("/v1/profiles?page_size=1"),
      api<{ items: TariffSummary[] }>("/v1/tariffs"),
      api<TariffDetail>("/v1/tariffs/pge-e1-2026-07"),
    ])
      .then(([profiles, listedTariffs, detail]) => {
        setProfile(profiles.items[0] ?? null);
        setTariffs(listedTariffs.items);
        setTariff(detail);
      })
      .catch((error) => {
        setMessage(
          error instanceof Error
            ? error.message
            : "Tariff workspace could not be loaded.",
        );
      });
  }, [session]);

  async function authenticate(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setMessage(null);
    const data = new FormData(event.currentTarget);
    try {
      const value = await api<Session>(`/v1/auth/${authMode}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          username: data.get("username"),
          password: data.get("password"),
        }),
      });
      setSession(value);
      setCsrf(value.csrf_token ?? csrfCookie());
    } catch (error) {
      setMessage(
        error instanceof Error ? error.message : "Authentication failed.",
      );
    }
  }

  async function upload(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setMessage(null);
    if (csrf === null) {
      setMessage(
        "Your security token is unavailable. Sign in again before uploading.",
      );
      return;
    }
    const data = new FormData(event.currentTarget);
    try {
      const operation = await api<{ import_id: string; state_url: string }>(
        "/v1/imports",
        {
          method: "POST",
          headers: {
            "Idempotency-Key": `browser-${crypto.randomUUID()}`,
            "X-CSRF-Token": csrf,
          },
          body: data,
        },
      );
      const value = await api<ImportStatus>(operation.state_url);
      setImportStatus(value);
      setProfile(null);
      setReplay(null);
      setComparisonAccountFacts(null);
      setAcknowledgedWarnings(new Set());
      setPgeAttested(false);
      setMessage(
        "Upload accepted. Refresh the quality report while the worker processes it.",
      );
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Upload failed.");
    }
  }

  async function installBuiltInProfile() {
    setMessage(null);
    if (csrf === null) {
      setMessage(
        "Your security token is unavailable. Sign in again before importing.",
      );
      return;
    }
    try {
      const installed = await api<BuiltInSimulatedProfile>(
        "/v1/imports/built-in-simulated-profile",
        {
          method: "POST",
          headers: {
            "Idempotency-Key": `browser-demo-${crypto.randomUUID()}`,
            "X-CSRF-Token": csrf,
          },
        },
      );
      setImportStatus(null);
      setProfile(installed.profile);
      setReplay(null);
      setComparisonAccountFacts(null);
      setAcknowledgedWarnings(new Set());
      setPgeAttested(false);
      setMessage(
        installed.repeated
          ? "Your existing immutable simulated July profile is ready."
          : `${installed.label} imported as immutable account data.`,
      );
    } catch (error) {
      setMessage(
        error instanceof Error
          ? error.message
          : "The built-in simulated profile could not be imported.",
      );
    }
  }

  async function refreshImport(preserveMessage = false) {
    if (importStatus === null) return;
    try {
      const value = await api<ImportStatus>(
        `/v1/imports/${importStatus.import_id}`,
      );
      setImportStatus(value);
      if (!preserveMessage && value.state === "READY") {
        setMessage("Quality report is ready for review.");
      }
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Refresh failed.");
    }
  }

  async function confirmImport() {
    if (
      importStatus === null ||
      csrf === null ||
      importStatus.coverage_start_utc_ns === null ||
      importStatus.coverage_end_utc_ns === null
    ) {
      return;
    }
    try {
      const confirmed = await api<Profile>(
        `/v1/imports/${importStatus.import_id}/confirm`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf },
          body: JSON.stringify({
            billing_period_start_utc_ns: importStatus.coverage_start_utc_ns,
            billing_period_end_utc_ns: importStatus.coverage_end_utc_ns,
            acknowledged_warning_ids: [...acknowledgedWarnings].sort(),
            pge_service_attested: pgeAttested,
          }),
        },
      );
      setProfile(confirmed);
      setMessage(
        "Profile confirmed. The raw upload has entered immediate deletion.",
      );
      await refreshImport(true);
    } catch (error) {
      setMessage(
        error instanceof Error ? error.message : "Confirmation failed.",
      );
    }
  }

  async function createReplay(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setMessage(null);
    if (csrf === null || profile === null || tariff === null) {
      setMessage("Confirm a July 2026 profile before creating a replay.");
      return;
    }
    const data = new FormData(event.currentTarget);
    try {
      const currentBill = optionalDollarsToCents(
        data.get("current_bill_total"),
      );
      const unsupportedAmount = optionalDollarsToCents(
        data.get("unsupported_amount"),
      );
      const unsupportedDescription = formEntryText(
        data.get("unsupported_description"),
      ).trim();
      if (unsupportedAmount !== null && unsupportedDescription.length === 0) {
        throw new Error(
          "Describe an unsupported bill line before adding its amount.",
        );
      }
      if (unsupportedAmount !== null && currentBill === null) {
        throw new Error(
          "Enter the current bill total before adding unsupported lines.",
        );
      }
      const window = tariff.admission.admitted_service_windows[0];
      if (window === undefined) {
        throw new Error("The admitted E-1 service window is unavailable.");
      }
      const accountFacts = lockedAccountFacts(window, new Date().toISOString());
      const value = await api<ReplayResource>("/v1/replays", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Idempotency-Key": `browser-replay-${crypto.randomUUID()}`,
          "X-CSRF-Token": csrf,
        },
        body: JSON.stringify({
          request_schema_version: "replay-operation-v1",
          profile_version_id: profile.profile_version_id,
          tariff_version_id: tariff.admission.tariff_version_id,
          account_facts: accountFacts,
          current_bill_total_cents: currentBill,
          user_unsupported_lines:
            unsupportedAmount === null
              ? []
              : [
                  {
                    line_item_key: "user_entered_other_1",
                    description: unsupportedDescription,
                    amount_cents: unsupportedAmount,
                  },
                ],
        }),
      });
      setReplay(value);
      setComparisonAccountFacts(accountFacts);
      setMessage("Immutable E-1 historical replay created.");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Replay failed.");
    }
  }

  async function logout() {
    if (csrf === null) return;
    try {
      await api("/v1/auth/logout", {
        method: "POST",
        headers: { "X-CSRF-Token": csrf },
      });
      setSession(null);
      setCsrf(null);
      setImportStatus(null);
      setProfile(null);
      setTariff(null);
      setTariffs([]);
      setReplay(null);
      setComparisonAccountFacts(null);
      setAcknowledgedWarnings(new Set());
      setPgeAttested(false);
      setMessage("Signed out and revoked the application session.");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Sign out failed.");
    }
  }

  const warningIds =
    importStatus?.findings
      .map((finding) => finding.warning_id)
      .filter((warning): warning is string => warning !== null) ?? [];
  const confirmationReady =
    importStatus?.state === "READY" &&
    pgeAttested &&
    warningIds.every((warning) => acknowledgedWarnings.has(warning));

  return (
    <main>
      <header className="masthead">
        <a className="brand" href="#top" aria-label="RateReplay home">
          <span className="brand-mark">RR</span>
          <span>RateReplay</span>
        </a>
        {session === null ? (
          <span className="evidence-pill">Historical replay</span>
        ) : (
          <div className="session-summary">
            <span>{session.user.username}</span>
            <button type="button" onClick={() => void logout()}>
              Sign out
            </button>
          </div>
        )}
      </header>

      <section className="hero" id="top">
        <p className="eyebrow">Trace every supported charge</p>
        <h1>RateReplay</h1>
        <p className="lede">
          Import your Green Button history, review its quality, and create an
          immutable profile for auditable electricity analysis.
        </p>
      </section>

      {message !== null && (
        <p className="message" role="status">
          {message}
        </p>
      )}

      {checkingSession ? (
        <section className="panel" aria-live="polite">
          Checking your application session…
        </section>
      ) : session === null ? (
        <section className="panel auth-panel" aria-labelledby="account-heading">
          <div>
            <p className="step">01</p>
            <h2 id="account-heading">Private local account</h2>
            <p>
              RateReplay stores no email and offers no password recovery. Losing
              your password means this account cannot be recovered.
            </p>
          </div>
          <form onSubmit={(event) => void authenticate(event)}>
            <div className="mode-switch" aria-label="Account action">
              <button
                className={authMode === "register" ? "active" : ""}
                type="button"
                onClick={() => setAuthMode("register")}
              >
                Create account
              </button>
              <button
                className={authMode === "login" ? "active" : ""}
                type="button"
                onClick={() => setAuthMode("login")}
              >
                Sign in
              </button>
            </div>
            <label>
              Username
              <input
                name="username"
                pattern="[A-Za-z0-9_]{3,64}"
                required
                autoComplete="username"
              />
            </label>
            <label>
              Password
              <input
                name="password"
                type="password"
                minLength={12}
                maxLength={128}
                required
                autoComplete={
                  authMode === "register" ? "new-password" : "current-password"
                }
              />
            </label>
            <button className="primary" type="submit">
              {authMode === "register" ? "Create private account" : "Sign in"}
            </button>
          </form>
        </section>
      ) : (
        <div className="workspace">
          <section className="panel" aria-labelledby="upload-heading">
            <p className="step">01</p>
            <h2 id="upload-heading">Import interval data</h2>
            <p className="privacy-note">
              Interval usage can reveal household activity. Upload only a
              downloaded file. Never enter utility credentials here. Raw files
              expire within 24 hours and enter immediate deletion after
              confirmation.
            </p>
            <form
              className="upload-form"
              onSubmit={(event) => void upload(event)}
            >
              <label>
                File format
                <select name="adapter" defaultValue="ESPI_XML">
                  <option value="ESPI_XML">Green Button ESPI XML</option>
                  <option value="PGE_CSV">PG&amp;E Green Button CSV</option>
                </select>
              </label>
              <label className="file-drop">
                <span>Choose one downloaded usage file</span>
                <input
                  name="file"
                  type="file"
                  accept=".xml,.csv,text/csv,application/xml"
                  required
                />
              </label>
              <button className="primary" type="submit">
                Upload securely
              </button>
            </form>
            <div className="built-in-import">
              <p className="eyebrow">No private data required</p>
              <h3>Start with the simulated July profile</h3>
              <p>
                Install the locked NREL-derived 750 kWh profile in this private
                account. It is always labeled simulated and contains no utility
                credentials or household data.
              </p>
              <button
                type="button"
                onClick={() => void installBuiltInProfile()}
              >
                Use built-in simulated July profile
              </button>
            </div>
          </section>

          <section className="panel" aria-labelledby="quality-heading">
            <p className="step">02</p>
            <h2 id="quality-heading">Quality review</h2>
            {importStatus === null ? (
              <p>Your latest import quality report will appear here.</p>
            ) : (
              <div className="quality-report">
                <dl>
                  <div>
                    <dt>Import state</dt>
                    <dd>{importStatus.state}</dd>
                  </div>
                  <div>
                    <dt>Intervals</dt>
                    <dd>{importStatus.reading_count.toLocaleString()}</dd>
                  </div>
                  <div>
                    <dt>Resolution</dt>
                    <dd>
                      {importStatus.interval_resolution_seconds === null
                        ? "Pending"
                        : `${importStatus.interval_resolution_seconds / 60} minutes`}
                    </dd>
                  </div>
                </dl>
                {importStatus.findings.length === 0 ? (
                  <p className="quality-ok">
                    No quality warnings in the selected data.
                  </p>
                ) : (
                  <ul className="finding-list">
                    {importStatus.findings.map((finding) => (
                      <li key={`${finding.code}-${finding.field_path}`}>
                        {finding.warning_id === null ? (
                          <span>{finding.code}</span>
                        ) : (
                          <label>
                            <input
                              type="checkbox"
                              checked={acknowledgedWarnings.has(
                                finding.warning_id,
                              )}
                              onChange={(event) => {
                                const updated = new Set(acknowledgedWarnings);
                                if (event.currentTarget.checked) {
                                  updated.add(finding.warning_id as string);
                                } else {
                                  updated.delete(finding.warning_id as string);
                                }
                                setAcknowledgedWarnings(updated);
                              }}
                            />
                            Acknowledge {finding.code}
                          </label>
                        )}
                      </li>
                    ))}
                  </ul>
                )}
                {importStatus.failure_code !== null && (
                  <p className="quality-error" role="alert">
                    Import stopped with {importStatus.failure_code}.
                  </p>
                )}
                <p className="coverage-note">
                  Confirmation uses the complete imported interval from{" "}
                  {formatUtc(importStatus.coverage_start_utc_ns)} to{" "}
                  {formatUtc(importStatus.coverage_end_utc_ns)}.
                </p>
                {importStatus.state === "CONFIRMED" ? (
                  <>
                    <p className="quality-ok">
                      Immutable profile created and raw-file deletion started.
                    </p>
                    <div className="actions">
                      <button
                        type="button"
                        onClick={() => void refreshImport()}
                      >
                        Refresh report
                      </button>
                    </div>
                  </>
                ) : (
                  <>
                    <label className="attestation">
                      <input
                        type="checkbox"
                        checked={pgeAttested}
                        onChange={(event) =>
                          setPgeAttested(event.currentTarget.checked)
                        }
                      />
                      I confirm this is my PG&amp;E electric service data for
                      the displayed period.
                    </label>
                    <div className="actions">
                      <button
                        type="button"
                        onClick={() => void refreshImport()}
                      >
                        Refresh report
                      </button>
                      <button
                        className="primary"
                        type="button"
                        disabled={!confirmationReady}
                        onClick={() => void confirmImport()}
                      >
                        Confirm complete period
                      </button>
                    </div>
                  </>
                )}
              </div>
            )}
          </section>

          <section className="panel wide" aria-labelledby="replay-heading">
            <p className="step">03</p>
            <h2 id="replay-heading">Replay July E-1</h2>
            <p>
              This is a historical calculation for the locked July 2026 service
              window. It does not compare plans and is not a forecast.
            </p>
            {profile === null ? (
              <p className="coverage-note">
                Confirm one complete July 2026 profile to unlock replay.
              </p>
            ) : (
              <form
                className="replay-form"
                onSubmit={(event) => void createReplay(event)}
              >
                <div
                  className="account-locks"
                  aria-label="Locked account facts"
                >
                  <span>PG&amp;E bundled service</span>
                  <span>Income Tier 3</span>
                  <span>Territory T, basic baseline</span>
                  <span>One primary import-only meter</span>
                  <span>One qualifying EV at the premises</span>
                </div>
                <div className="reconciliation-fields">
                  <label>
                    Current bill total in dollars, optional
                    <input
                      name="current_bill_total"
                      inputMode="decimal"
                      pattern="[0-9]+(\.[0-9]{1,2})?"
                      placeholder="110.00"
                    />
                  </label>
                  <label>
                    Unsupported line description, optional
                    <input
                      name="unsupported_description"
                      maxLength={120}
                      placeholder="Local tax shown on current bill"
                    />
                  </label>
                  <label>
                    Unsupported line amount in dollars, optional
                    <input
                      name="unsupported_amount"
                      inputMode="decimal"
                      pattern="-?[0-9]+(\.[0-9]{1,2})?"
                      placeholder="2.00"
                    />
                  </label>
                </div>
                <label className="attestation">
                  <input name="account_attestation" type="checkbox" required />I
                  attest that every locked account fact above applies for the
                  displayed July 2026 service window, and that CARE, FERA,
                  medical baseline, CCA, direct access, solar or export, and
                  active bill protection do not apply. I also attest that an EV
                  was a qualifying technology at the premises.
                </label>
                <button className="primary" type="submit">
                  Create historical replay
                </button>
              </form>
            )}

            {replay !== null && (
              <article
                className="replay-result"
                aria-labelledby="result-heading"
              >
                <div className="result-summary">
                  <div>
                    <p className="eyebrow">Supported calculated charges</p>
                    <h3 id="result-heading">
                      {formatMoney(replay.result.supported_calculated_cents)}
                    </h3>
                  </div>
                  <span className="status-badge">
                    {replay.result.eligibility.status}
                  </span>
                </div>
                <div className="table-scroll">
                  <table>
                    <caption>Auditable E-1 charge lines</caption>
                    <thead>
                      <tr>
                        <th scope="col">Charge</th>
                        <th scope="col">Quantity</th>
                        <th scope="col">Rate</th>
                        <th scope="col">Amount</th>
                        <th scope="col">Source</th>
                      </tr>
                    </thead>
                    <tbody>
                      {replay.result.line_items.map((line) => (
                        <tr key={line.line_item_key}>
                          <th scope="row">{humanize(line.line_item_key)}</th>
                          <td>
                            {formatQuantity(line)} {line.quantity_unit}
                          </td>
                          <td>{formatRate(line)}</td>
                          <td>{formatMoney(line.rounded_cents)}</td>
                          <td>
                            <code>{line.source_id}</code>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
                {replay.result.reconciliation === null ? (
                  <p className="coverage-note">
                    No current-bill reconciliation was requested.
                  </p>
                ) : (
                  <div
                    className="reconciliation"
                    aria-label="Current bill reconciliation"
                  >
                    <dl>
                      <div>
                        <dt>Entered bill</dt>
                        <dd>
                          {formatMoney(
                            replay.result.reconciliation
                              .entered_bill_total_cents,
                          )}
                        </dd>
                      </div>
                      <div>
                        <dt>User unsupported</dt>
                        <dd>
                          {formatMoney(
                            replay.result.reconciliation.user_unsupported_cents,
                          )}
                        </dd>
                      </div>
                      <div>
                        <dt>Unexplained residual</dt>
                        <dd>
                          {formatMoney(
                            replay.result.reconciliation
                              .unexplained_residual_cents,
                          )}
                        </dd>
                      </div>
                      <div>
                        <dt>Review state</dt>
                        <dd>
                          {humanize(
                            replay.result.reconciliation.classification,
                          )}
                        </dd>
                      </div>
                    </dl>
                    {replay.result.user_unsupported_lines.map((line) => (
                      <p className="unsupported-line" key={line.line_item_key}>
                        User-entered unsupported: {line.description}{" "}
                        <strong>{formatMoney(line.amount_cents)}</strong>
                      </p>
                    ))}
                    <p className="coverage-note">
                      The residual remains signed and visible. RateReplay does
                      not move it into a supported charge to force a match.
                    </p>
                  </div>
                )}
                <details className="manifest">
                  <summary>Calculation manifest and exact hashes</summary>
                  <dl>
                    <div>
                      <dt>Calculation</dt>
                      <dd>
                        <code>{replay.result.manifest.calculation_sha256}</code>
                      </dd>
                    </div>
                    <div>
                      <dt>Replay input</dt>
                      <dd>
                        <code>
                          {replay.result.manifest.replay_input_sha256}
                        </code>
                      </dd>
                    </div>
                    <div>
                      <dt>Compiler</dt>
                      <dd>
                        <code>
                          {
                            replay.result.manifest
                              .tariff_compiler_content_sha256
                          }
                        </code>
                      </dd>
                    </div>
                    <div>
                      <dt>Result</dt>
                      <dd>
                        <code>{replay.result.result_sha256}</code>
                      </dd>
                    </div>
                  </dl>
                </details>
              </article>
            )}
          </section>

          {replay !== null &&
            comparisonAccountFacts !== null &&
            csrf !== null && (
              <>
                <ComparisonWorkspace
                  key={`comparison-${replay.replay_id}`}
                  replayId={replay.replay_id}
                  csrf={csrf}
                  accountFacts={comparisonAccountFacts}
                  tariffs={tariffs}
                  onMessage={setMessage}
                />
                <ScenarioWorkspace
                  key={`scenario-${replay.replay_id}`}
                  profileId={profile?.profile_version_id ?? ""}
                  csrf={csrf}
                  accountFacts={comparisonAccountFacts}
                  tariffs={tariffs}
                  onMessage={setMessage}
                />
              </>
            )}

          <section
            className="panel wide provenance"
            aria-labelledby="provenance-heading"
          >
            <p className="step">06</p>
            <h2 id="provenance-heading">Filed-source provenance</h2>
            {tariff === null ? (
              <p>Loading the admitted E-1 source vector.</p>
            ) : (
              <>
                <p>
                  This is the current E-1 filed-source vector for the locked
                  July 2026 account class. Candidate source vectors appear with
                  each completed plan replay. Optimization is admitted
                  separately.
                </p>
                <ul className="source-list">
                  {tariff.compilation.reports.source_coverage.map((source) => (
                    <li key={source.source_id}>
                      <a
                        href={source.source_url}
                        rel="noreferrer"
                        target="_blank"
                      >
                        {source.source_id}
                      </a>
                      <span>{source.linked_rule_ids.length} linked rules</span>
                      <code>{source.source_sha256}</code>
                    </li>
                  ))}
                </ul>
                <p className="coverage-note">
                  Compiler content hash:{" "}
                  <code>{tariff.admission.compiler_content_sha256}</code>
                </p>
              </>
            )}
          </section>
        </div>
      )}
    </main>
  );
}

function formatUtc(value: number | null): string {
  if (value === null) return "pending coverage";
  return new Date(value / 1_000_000).toISOString().replace(".000Z", "Z");
}

function lockedAccountFacts(
  window: [string, string],
  userAttestedAt: string,
): AccountFacts {
  return {
    schema_version: "account-facts-v1",
    service_window: { start: window[0], end: window[1] },
    service_provider: "PG&E",
    service_mode: "BUNDLED",
    meter_count: 1,
    primary_meter_only: true,
    income_tier: "TIER_3",
    care_enrolled: false,
    fera_enrolled: false,
    medical_baseline: false,
    cca_service: false,
    direct_access_service: false,
    active_bill_protection: false,
    solar_or_export: false,
    baseline_territory: "T",
    baseline_quantity_code: "BASIC",
    qualifying_technologies: ["EV"],
    user_attested_at: userAttestedAt,
  };
}

function optionalDollarsToCents(
  value: FormDataEntryValue | null,
): number | null {
  const text = formEntryText(value).trim();
  if (text.length === 0) return null;
  const match = /^(-?)(\d+)(?:\.(\d{1,2}))?$/.exec(text);
  if (match === null) {
    throw new Error("Dollar amounts must use no more than two decimal places.");
  }
  const sign = match[1] === "-" ? -1 : 1;
  const whole = Number(match[2]);
  const fractional = Number((match[3] ?? "").padEnd(2, "0"));
  const cents = whole * 100 + fractional;
  if (!Number.isSafeInteger(whole) || !Number.isSafeInteger(cents)) {
    throw new Error("Dollar amount is outside the supported exact range.");
  }
  return sign * cents;
}

function formEntryText(value: FormDataEntryValue | null): string {
  if (value === null) return "";
  if (typeof value !== "string") {
    throw new Error("This field must contain text, not a file.");
  }
  return value;
}

function formatMoney(cents: number): string {
  const sign = cents < 0 ? "-" : "";
  const absolute = Math.abs(cents);
  return `${sign}$${Math.floor(absolute / 100).toLocaleString("en-US")}.${String(
    absolute % 100,
  ).padStart(2, "0")}`;
}

function formatQuantity(line: ReplayLine): string {
  if (line.quantity_denominator === 1) {
    return line.quantity_numerator.toLocaleString("en-US");
  }
  return `${line.quantity_numerator}/${line.quantity_denominator}`;
}

function formatRate(line: ReplayLine): string {
  const value =
    line.rate_numerator_microdollars / line.rate_denominator / 1_000_000;
  const suffix = line.rate_unit.replace("microdollars", "USD");
  return `${value.toFixed(5)} ${suffix}`;
}

function humanize(value: string): string {
  return value
    .replaceAll("_", " ")
    .replace(/\b\w/g, (letter) => letter.toUpperCase());
}
