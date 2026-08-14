import { useEffect, useRef, useState } from "react";

import { DemoRedactedReport } from "./demoArtifacts";
import { RedactedReport } from "./PublicDemo";
import { ApiError, api, pollApi } from "./api";

const POLL_INTERVAL_MS = 1000;

type JobResource = {
  job_id: string;
  kind: string;
  state: "QUEUED" | "LEASED" | "RUNNING" | "SUCCEEDED" | "FAILED" | "CANCELLED";
  failure_code: string | null;
  terminal_result_type: string | null;
  terminal_result_id: string | null;
};

type ReportResource = {
  schema_version: "report-resource-v1";
  export_id: string;
  scenario_id: string;
  scenario_result_id: string;
  job_id: string;
  created_at: string;
  report: DemoRedactedReport;
};

type ReportExportProps = {
  scenarioId: string;
  csrf: string;
  onMessage: (message: string) => void;
};

export function ReportExport({
  scenarioId,
  csrf,
  onMessage,
}: ReportExportProps) {
  const operationToken = useRef(0);
  const [job, setJob] = useState<JobResource | null>(null);
  const [report, setReport] = useState<ReportResource | null>(null);
  const [issue, setIssue] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(
    () => () => {
      operationToken.current += 1;
    },
    [],
  );

  async function requestReport() {
    const token = operationToken.current + 1;
    operationToken.current = token;
    setBusy(true);
    setIssue(null);
    try {
      const submitted = await api<JobResource>(
        `/v1/reports/${scenarioId}/exports`,
        {
          method: "POST",
          headers: {
            "Idempotency-Key": `browser-report-${crypto.randomUUID()}`,
            "X-CSRF-Token": csrf,
          },
        },
      );
      if (operationToken.current !== token) return;
      setJob(submitted);
      await finishReport(submitted, token);
    } catch (error) {
      if (operationToken.current === token) showError(error);
    } finally {
      if (operationToken.current === token) setBusy(false);
    }
  }

  async function finishReport(initial: JobResource, token: number) {
    let current = initial;
    for (
      let attempt = 0;
      !isTerminal(current.state) && attempt < 120;
      attempt += 1
    ) {
      current = await pollApi<JobResource>(`/v1/jobs/${current.job_id}`);
      if (operationToken.current !== token) return;
      setJob(current);
      if (!isTerminal(current.state)) await wait(POLL_INTERVAL_MS);
    }
    if (operationToken.current !== token) return;
    if (!isTerminal(current.state)) {
      setIssue(
        "The durable report job is still running. Check its status again without creating another export.",
      );
      return;
    }
    if (
      current.state !== "SUCCEEDED" ||
      current.terminal_result_type !== "REPORT"
    ) {
      throw new ApiError(
        409,
        current.failure_code ?? "REPORT_JOB_UNSUCCESSFUL",
        "The report worker published no export.",
        [],
        { job_id: current.job_id, job_state: current.state },
      );
    }
    const resource = await api<ReportResource>(`/v1/reports/${scenarioId}`);
    if (operationToken.current !== token) return;
    setReport(resource);
    setIssue(null);
    onMessage("The deny-by-default redacted report is ready.");
  }

  async function resumeReport() {
    if (job === null) return;
    const token = operationToken.current + 1;
    operationToken.current = token;
    setBusy(true);
    setIssue(null);
    try {
      await finishReport(job, token);
    } catch (error) {
      if (operationToken.current === token) showError(error);
    } finally {
      if (operationToken.current === token) setBusy(false);
    }
  }

  function showError(error: unknown) {
    const message =
      error instanceof ApiError
        ? `${error.code}: ${error.message}`
        : error instanceof Error
          ? error.message
          : "Report generation failed safely.";
    setIssue(message);
    onMessage(message);
  }

  function downloadReport() {
    if (report === null) return;
    const blob = new Blob([`${JSON.stringify(report.report, null, 2)}\n`], {
      type: "application/json",
    });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `ratereplay-redacted-${report.export_id}.json`;
    link.click();
    URL.revokeObjectURL(url);
  }

  return (
    <section className="report-export" aria-labelledby="report-export-heading">
      <p className="step">06</p>
      <h3 id="report-export-heading">Create the redacted report</h3>
      <p>
        The durable export runs from the accepted scenario result. Its preview
        and downloaded JSON use the same aggregate-only schema.
      </p>
      {report === null ? (
        <div className="actions">
          <button
            className="primary"
            type="button"
            disabled={busy}
            onClick={() => void requestReport()}
          >
            {busy && job === null
              ? "Submitting report..."
              : "Generate redacted report"}
          </button>
          {job !== null && !isTerminal(job.state) && (
            <button
              type="button"
              disabled={busy}
              onClick={() => void resumeReport()}
            >
              Check report status
            </button>
          )}
        </div>
      ) : (
        <>
          <RedactedReport
            report={report.report}
            headingId="private-redacted-report"
            stepLabel="06 - Aggregate-only export"
          />
          <div className="actions">
            <button type="button" onClick={downloadReport}>
              Download displayed redacted JSON
            </button>
          </div>
        </>
      )}
      {job !== null && report === null && (
        <p className="coverage-note" aria-live="polite">
          Durable report job {job.job_id}: {job.state}
        </p>
      )}
      {issue !== null && (
        <p className="quality-error" role="alert">
          {issue}
        </p>
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
