import { FormEvent, useEffect, useState } from "react";

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

async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, { credentials: "same-origin", ...init });
  const body = (await response.json().catch(() => null)) as
    (T & { message?: string }) | null;
  if (!response.ok) {
    throw new Error(
      body?.message ?? "RateReplay could not complete that request.",
    );
  }
  return body as T;
}

export function App() {
  const [session, setSession] = useState<Session | null>(null);
  const [checkingSession, setCheckingSession] = useState(true);
  const [authMode, setAuthMode] = useState<"register" | "login">("register");
  const [csrf, setCsrf] = useState<string | null>(null);
  const [importStatus, setImportStatus] = useState<ImportStatus | null>(null);
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
      setAcknowledgedWarnings(new Set());
      setPgeAttested(false);
      setMessage(
        "Upload accepted. Refresh the quality report while the worker processes it.",
      );
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Upload failed.");
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
      await api(`/v1/imports/${importStatus.import_id}/confirm`, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf },
        body: JSON.stringify({
          billing_period_start_utc_ns: importStatus.coverage_start_utc_ns,
          billing_period_end_utc_ns: importStatus.coverage_end_utc_ns,
          acknowledged_warning_ids: [...acknowledgedWarnings].sort(),
          pge_service_attested: pgeAttested,
        }),
      });
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
        </div>
      )}
    </main>
  );
}

function formatUtc(value: number | null): string {
  if (value === null) return "pending coverage";
  return new Date(value / 1_000_000).toISOString().replace(".000Z", "Z");
}
