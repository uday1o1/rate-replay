import { ApiError, api } from "./api";

export type JobResource = {
  job_id: string;
  state: "QUEUED" | "LEASED" | "RUNNING" | "SUCCEEDED" | "FAILED" | "CANCELLED";
  failure_code: string | null;
  terminal_result_type: string | null;
  terminal_result_id: string | null;
};

export type CompletedJobResource = JobResource & {
  state: "SUCCEEDED";
  terminal_result_type: string;
  terminal_result_id: string;
};

export function isJobResource(value: unknown): value is JobResource {
  return (
    typeof value === "object" &&
    value !== null &&
    "job_id" in value &&
    "state" in value &&
    typeof value.job_id === "string" &&
    typeof value.state === "string"
  );
}

export async function finishDurableJob(
  initial: JobResource,
  expectedResultType: string,
): Promise<CompletedJobResource> {
  let current = initial;
  for (
    let attempt = 0;
    !isTerminal(current.state) && attempt < 120;
    attempt += 1
  ) {
    current = await api<JobResource>(`/v1/jobs/${current.job_id}`);
    if (!isTerminal(current.state)) {
      await wait(250);
    }
  }
  if (!isTerminal(current.state)) {
    throw new ApiError(
      504,
      `${expectedResultType}_JOB_TIMEOUT`,
      "The durable calculation is still running. Its existing job can be checked again safely.",
      [],
      { job_id: current.job_id },
    );
  }
  if (
    current.state !== "SUCCEEDED" ||
    current.terminal_result_type !== expectedResultType ||
    current.terminal_result_id === null
  ) {
    throw new ApiError(
      409,
      current.failure_code ?? `${expectedResultType}_JOB_UNSUCCESSFUL`,
      "The durable calculation published no result.",
      [],
      { job_id: current.job_id, job_state: current.state },
    );
  }
  return current as CompletedJobResource;
}

function isTerminal(state: JobResource["state"]): boolean {
  return state === "SUCCEEDED" || state === "FAILED" || state === "CANCELLED";
}

function wait(milliseconds: number): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, milliseconds));
}
