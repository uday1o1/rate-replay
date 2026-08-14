import { FormEvent, useState } from "react";

import { ApiError, api } from "./api";

const RECOVERY_STORAGE_KEY = "ratereplay.deletion-recovery.v1";

type DeletionTarget = "ACCOUNT" | "IMPORT" | "PROFILE";

type RecoveryCredential = {
  schema_version: "browser-deletion-recovery-v1";
  target_kind: DeletionTarget;
  deletion_id: string | null;
  receipt_secret: string;
  idempotency_key: string;
  status: string;
};

type DeletionIntent = {
  schema_version: "deletion-intent-v1";
  deletion_id: string;
  status: string;
  expires_at: string;
};

type DeletionStatus = {
  schema_version: "deletion-status-v1";
  deletion_id: string;
  status: string;
  artifact_counts: Record<string, number>;
  completed_at: string | null;
};

type PrivacyControlsProps = {
  username: string | null;
  csrf: string | null;
  importId: string | null;
  profileId: string | null;
  onAccountDeletionStarted: () => void;
  onImportDeleted: () => void;
  onProfileDeleted: () => void;
  onMessage: (message: string) => void;
};

export function PrivacyControls({
  username,
  csrf,
  importId,
  profileId,
  onAccountDeletionStarted,
  onImportDeleted,
  onProfileDeleted,
  onMessage,
}: PrivacyControlsProps) {
  const [recovery, setRecovery] = useState<RecoveryCredential | null>(() =>
    loadRecovery(),
  );
  const [accountConfirmation, setAccountConfirmation] = useState("");
  const [accountBackupAcknowledged, setAccountBackupAcknowledged] =
    useState(false);
  const [profileConfirmation, setProfileConfirmation] = useState("");
  const [importConfirmation, setImportConfirmation] = useState("");
  const [issue, setIssue] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [manualDeletionId, setManualDeletionId] = useState("");
  const [manualSecret, setManualSecret] = useState("");

  const accountPrepared =
    recovery?.target_kind === "ACCOUNT" && recovery.deletion_id !== null;
  const deletionActive = recovery !== null && !isTerminal(recovery.status);

  function remember(next: RecoveryCredential | null) {
    setRecovery(next);
    try {
      if (next === null) {
        window.sessionStorage.removeItem(RECOVERY_STORAGE_KEY);
      } else {
        window.sessionStorage.setItem(
          RECOVERY_STORAGE_KEY,
          JSON.stringify(next),
        );
      }
    } catch {
      setIssue(
        "This browser could not retain the deletion receipt. Download it before continuing.",
      );
    }
  }

  async function prepareAccountDeletion() {
    if (csrf === null || username === null) return;
    setBusy(true);
    setIssue(null);
    const pending =
      recovery?.target_kind === "ACCOUNT" && recovery.deletion_id === null
        ? recovery
        : newRecovery("ACCOUNT");
    remember(pending);
    try {
      const intent = await api<DeletionIntent>("/v1/account/deletion-intents", {
        method: "POST",
        headers: deletionHeaders(pending, csrf),
      });
      const prepared: RecoveryCredential = {
        ...pending,
        deletion_id: intent.deletion_id,
        status: intent.status,
      };
      remember(prepared);
      onMessage(
        "Account deletion is prepared but has not started. Save the recovery credential before confirming.",
      );
    } catch (error) {
      showError(error);
    } finally {
      setBusy(false);
    }
  }

  async function deleteAccount() {
    if (
      csrf === null ||
      recovery?.target_kind !== "ACCOUNT" ||
      recovery.deletion_id === null
    ) {
      return;
    }
    setBusy(true);
    setIssue(null);
    try {
      const status = await api<DeletionStatus>("/v1/account", {
        method: "DELETE",
        headers: {
          ...deletionHeaders(recovery, csrf),
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ deletion_id: recovery.deletion_id }),
      });
      remember({ ...recovery, status: status.status });
      onAccountDeletionStarted();
    } catch (error) {
      showError(error);
    } finally {
      setBusy(false);
    }
  }

  async function deleteResource(
    targetKind: "IMPORT" | "PROFILE",
    resourceId: string | null,
  ) {
    if (csrf === null || resourceId === null) return;
    setBusy(true);
    setIssue(null);
    const pending = newRecovery(targetKind);
    const resourceName = targetKind.toLowerCase();
    try {
      const status = await api<DeletionStatus>(
        `/v1/${resourceName}s/${resourceId}`,
        {
          method: "DELETE",
          headers: deletionHeaders(pending, csrf),
        },
      );
      remember({
        ...pending,
        deletion_id: status.deletion_id,
        status: status.status,
      });
      onMessage(
        `${humanize(targetKind)} deletion started. Its receipt remains independently pollable.`,
      );
    } catch (error) {
      showError(error);
    } finally {
      setBusy(false);
    }
  }

  async function checkReceipt(credential = recovery) {
    if (credential?.deletion_id === null || credential === null) return;
    setBusy(true);
    setIssue(null);
    try {
      const status = await api<DeletionStatus>(
        `/v1/deletions/${credential.deletion_id}`,
        {
          headers: {
            "X-Deletion-Receipt-Secret": credential.receipt_secret,
          },
        },
      );
      const updated = { ...credential, status: status.status };
      remember(updated);
      if (status.status === "DELETED" && credential.target_kind === "PROFILE") {
        onProfileDeleted();
      }
      if (status.status === "DELETED" && credential.target_kind === "IMPORT") {
        onImportDeleted();
      }
      if (
        credential.target_kind === "ACCOUNT" &&
        !["INTENT_CREATED", "PREPARED", "ABORTED"].includes(status.status)
      ) {
        onAccountDeletionStarted();
      }
      onMessage(
        status.status === "DELETED"
          ? "Deletion receipt verified complete."
          : `Deletion remains ${humanize(status.status)}.`,
      );
    } catch (error) {
      showError(error);
    } finally {
      setBusy(false);
    }
  }

  function recoverReceipt(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (
      !/^[0-9a-f]{32}$/.test(manualDeletionId) ||
      !isReceiptSecret(manualSecret)
    ) {
      setIssue("Enter a valid deletion ID and 43-character receipt secret.");
      return;
    }
    const credential: RecoveryCredential = {
      schema_version: "browser-deletion-recovery-v1",
      target_kind: "ACCOUNT",
      deletion_id: manualDeletionId,
      receipt_secret: manualSecret,
      idempotency_key: "recovered-receipt",
      status: "RECOVERY_LOADED",
    };
    remember(credential);
    void checkReceipt(credential);
  }

  function downloadRecovery() {
    if (recovery?.deletion_id === null || recovery === null) return;
    const payload = {
      schema_version: recovery.schema_version,
      deletion_id: recovery.deletion_id,
      receipt_secret: recovery.receipt_secret,
    };
    const blob = new Blob([`${JSON.stringify(payload, null, 2)}\n`], {
      type: "application/json",
    });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = "ratereplay-deletion-recovery.json";
    link.click();
    URL.revokeObjectURL(url);
  }

  function showError(error: unknown) {
    const message =
      error instanceof ApiError
        ? `${error.code}: ${error.message}`
        : error instanceof Error
          ? error.message
          : "Deletion coordination failed safely.";
    setIssue(message);
    onMessage(message);
  }

  if (username === null && recovery === null) {
    return (
      <section
        className="panel lifecycle-panel"
        aria-labelledby="receipt-recovery-heading"
      >
        <p className="step">Deletion receipt</p>
        <h2 id="receipt-recovery-heading">Recover a deletion status</h2>
        <p>
          Receipt polling is independent of an account session. The secret is
          sent only in the dedicated authorization header.
        </p>
        <form className="receipt-recovery-form" onSubmit={recoverReceipt}>
          <label>
            Deletion ID
            <input
              value={manualDeletionId}
              onChange={(event) =>
                setManualDeletionId(event.currentTarget.value)
              }
              pattern="[0-9a-f]{32}"
              autoComplete="off"
              required
            />
          </label>
          <label>
            Receipt secret
            <input
              type="password"
              value={manualSecret}
              onChange={(event) => setManualSecret(event.currentTarget.value)}
              minLength={43}
              maxLength={43}
              autoComplete="off"
              required
            />
          </label>
          <button className="primary" type="submit">
            Check saved receipt
          </button>
        </form>
        {issue !== null && (
          <p className="quality-error" role="alert">
            {issue}
          </p>
        )}
      </section>
    );
  }

  return (
    <section
      className="panel lifecycle-panel"
      aria-labelledby="privacy-controls-heading"
    >
      <p className="step">07</p>
      <h2 id="privacy-controls-heading">Retention and deletion</h2>
      <div className="retention-grid">
        <div>
          <strong>Raw uploads</strong>
          <span>
            Immediate deletion after confirmation, with a fixed 24-hour maximum.
          </span>
        </div>
        <div>
          <strong>Live account data</strong>
          <span>
            Generation-fenced deletion removes rows, objects, jobs, and exports.
          </span>
        </div>
        <div>
          <strong>Encrypted backups</strong>
          <span>
            May retain deleted data for at most 30 days. Hosted validation is
            not claimed.
          </span>
        </div>
      </div>

      {username !== null && (
        <div className="deletion-control-grid">
          <article>
            <h3>Delete the current profile</h3>
            <p>
              This removes the profile and its dependent calculations while the
              local account remains active.
            </p>
            <label>
              Type DELETE PROFILE to confirm
              <input
                value={profileConfirmation}
                onChange={(event) =>
                  setProfileConfirmation(event.currentTarget.value)
                }
                autoComplete="off"
                disabled={profileId === null || deletionActive}
              />
            </label>
            <button
              type="button"
              disabled={
                busy ||
                profileId === null ||
                deletionActive ||
                profileConfirmation !== "DELETE PROFILE"
              }
              onClick={() => void deleteResource("PROFILE", profileId)}
            >
              Delete current profile
            </button>
          </article>

          <article>
            <h3>Delete the source import</h3>
            <p>
              This removes the imported dataset, its profile, and every
              dependent calculation while the local account remains active.
            </p>
            <label>
              Type DELETE IMPORT to confirm
              <input
                value={importConfirmation}
                onChange={(event) =>
                  setImportConfirmation(event.currentTarget.value)
                }
                autoComplete="off"
                disabled={importId === null || deletionActive}
              />
            </label>
            <button
              type="button"
              disabled={
                busy ||
                importId === null ||
                deletionActive ||
                importConfirmation !== "DELETE IMPORT"
              }
              onClick={() => void deleteResource("IMPORT", importId)}
            >
              Delete source import
            </button>
          </article>

          <article className="danger-zone">
            <h3>Delete the entire account</h3>
            <p>
              Preparation is reversible because it makes no lifecycle change.
              Final confirmation revokes every session and cannot be cancelled.
            </p>
            {!accountPrepared ? (
              <button
                type="button"
                disabled={
                  busy ||
                  (deletionActive && recovery?.target_kind !== "ACCOUNT")
                }
                onClick={() => void prepareAccountDeletion()}
              >
                {recovery?.target_kind === "ACCOUNT" &&
                recovery.deletion_id === null
                  ? "Retry deletion preparation"
                  : "Prepare account deletion"}
              </button>
            ) : (
              <>
                <p className="counterfactual-note">
                  Deletion is prepared but not started. Download the recovery
                  credential before continuing.
                </p>
                <button type="button" onClick={downloadRecovery}>
                  Download deletion recovery credential
                </button>
                <label>
                  Type your username to confirm
                  <input
                    value={accountConfirmation}
                    onChange={(event) =>
                      setAccountConfirmation(event.currentTarget.value)
                    }
                    autoComplete="off"
                  />
                </label>
                <label className="attestation">
                  <input
                    type="checkbox"
                    checked={accountBackupAcknowledged}
                    onChange={(event) =>
                      setAccountBackupAcknowledged(event.currentTarget.checked)
                    }
                  />
                  I understand encrypted backups may retain deleted data for up
                  to 30 days and every restore reapplies the deletion ledger.
                </label>
                <button
                  className="destructive"
                  type="button"
                  disabled={
                    busy ||
                    accountConfirmation !== username ||
                    !accountBackupAcknowledged
                  }
                  onClick={() => void deleteAccount()}
                >
                  Delete account permanently
                </button>
              </>
            )}
          </article>
        </div>
      )}

      {recovery?.deletion_id !== null && recovery !== null && (
        <div className="deletion-receipt" aria-live="polite">
          <p className="eyebrow">Session-independent deletion receipt</p>
          <h3>{humanize(recovery.status)}</h3>
          <p>
            The receipt contains no username, interval data, account fact, or
            profile hash.
          </p>
          <div className="actions">
            <button
              type="button"
              disabled={busy}
              onClick={() => void checkReceipt()}
            >
              Check deletion status
            </button>
            <button type="button" onClick={downloadRecovery}>
              Download recovery credential
            </button>
            {isTerminal(recovery.status) && (
              <button type="button" onClick={() => remember(null)}>
                Forget local receipt
              </button>
            )}
          </div>
        </div>
      )}
      {issue !== null && (
        <p className="quality-error" role="alert">
          {issue}
        </p>
      )}
    </section>
  );
}

function newRecovery(targetKind: DeletionTarget): RecoveryCredential {
  return {
    schema_version: "browser-deletion-recovery-v1",
    target_kind: targetKind,
    deletion_id: null,
    receipt_secret: randomReceiptSecret(),
    idempotency_key: `browser-deletion-${crypto.randomUUID()}`,
    status: "CLIENT_PREPARED",
  };
}

function randomReceiptSecret(): string {
  const bytes = crypto.getRandomValues(new Uint8Array(32));
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return window
    .btoa(binary)
    .replaceAll("+", "-")
    .replaceAll("/", "_")
    .replace(/=+$/, "");
}

function deletionHeaders(
  recovery: RecoveryCredential,
  csrf: string,
): Record<string, string> {
  return {
    "Idempotency-Key": recovery.idempotency_key,
    "X-CSRF-Token": csrf,
    "X-Deletion-Receipt-Secret": recovery.receipt_secret,
  };
}

function loadRecovery(): RecoveryCredential | null {
  try {
    const raw = window.sessionStorage.getItem(RECOVERY_STORAGE_KEY);
    if (raw === null) return null;
    const value = JSON.parse(raw) as Partial<RecoveryCredential>;
    if (
      value.schema_version !== "browser-deletion-recovery-v1" ||
      (value.target_kind !== "ACCOUNT" &&
        value.target_kind !== "IMPORT" &&
        value.target_kind !== "PROFILE") ||
      (value.deletion_id !== null &&
        (typeof value.deletion_id !== "string" ||
          !/^[0-9a-f]{32}$/.test(value.deletion_id))) ||
      typeof value.receipt_secret !== "string" ||
      !isReceiptSecret(value.receipt_secret) ||
      typeof value.idempotency_key !== "string" ||
      typeof value.status !== "string"
    ) {
      window.sessionStorage.removeItem(RECOVERY_STORAGE_KEY);
      return null;
    }
    return value as RecoveryCredential;
  } catch {
    return null;
  }
}

function isReceiptSecret(value: string): boolean {
  return /^[A-Za-z0-9_-]{43}$/.test(value);
}

function isTerminal(status: string): boolean {
  return status === "DELETED" || status === "ABORTED";
}

function humanize(value: string): string {
  return value
    .replaceAll("_", " ")
    .toLowerCase()
    .replace(/\b\w/g, (letter) => letter.toUpperCase());
}
