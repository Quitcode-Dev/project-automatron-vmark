"use client";

/**
 * ApprovalGates — Approve + Execute buttons with all pre-flight gate logic.
 *
 * Approve disabled when:
 *   - no validation result exists
 *   - validation status is "blocked"
 *   - plan has blocking_questions
 *   - plan risk_level is "blocked"
 *   - target is production AND rollback is disabled AND user has not acknowledged
 *
 * Execute disabled when:
 *   - plan is not approved
 *   - validation is missing or would not allow execution
 *   - active run exists for this target
 */

import React, { useState } from "react";
import type { DeploymentPlan, DeploymentTarget, ValidationResult } from "@/lib/deploymentApi";

export interface ExecuteResult {
  run_id: string;
  status: string;
}

export interface ApprovalGatesProps {
  plan: DeploymentPlan;
  target: DeploymentTarget;
  validation: ValidationResult | null;
  /** True once the backend has recorded an approval for this plan. */
  approved: boolean;
  /** True when an active (pending/starting/running/deploying) run exists for this target. */
  hasActiveRun: boolean;
  /** Whether the user has acknowledged that rollback is disabled. */
  rollbackAck: boolean;
  onRollbackAckChange: (v: boolean) => void;
  onApprove: () => Promise<void>;
  /** Must resolve to { run_id, status } — the run_id is used for tracking. */
  onExecute: () => Promise<ExecuteResult>;
  /** Called with the run_id returned by the execute endpoint. */
  onRunStarted: (runId: string) => void;
}

function validationAllowsExecution(v: ValidationResult | null): boolean {
  if (!v) return false;
  if (v.status === "blocked") return false;
  if (v.status === "warning" && v.blocking_errors.length > 0) return false;
  return true;
}

export function ApprovalGates({
  plan,
  target,
  validation,
  approved,
  hasActiveRun,
  rollbackAck,
  onRollbackAckChange,
  onApprove,
  onExecute,
  onRunStarted,
}: ApprovalGatesProps) {
  const [busyApprove, setBusyApprove] = useState(false);
  const [busyExecute, setBusyExecute] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const pj = plan.plan_json as Record<string, unknown>;
  const blockingQuestions = (pj.blocking_questions ?? plan.blocking_questions_json ?? []) as string[];
  const isProduction = target.environment === "production";

  // Rollback is always disabled in MVP — production requires explicit acknowledgment.
  const rollbackDisabled = true;
  const needsRollbackAck = isProduction && rollbackDisabled && !rollbackAck;

  // --- Approve gate ---
  const approveBlockReasons: string[] = [];
  if (validation === null) approveBlockReasons.push("Run validation first");
  else if (validation.status === "blocked") approveBlockReasons.push("Validation blocked");
  if (plan.risk_level === "blocked") approveBlockReasons.push("Plan risk level is blocked");
  if (blockingQuestions.length > 0) approveBlockReasons.push(`${blockingQuestions.length} blocking question(s) unresolved`);
  if (needsRollbackAck) approveBlockReasons.push("Acknowledge rollback disabled for production");
  const canApprove = approveBlockReasons.length === 0 && !busyApprove && !busyExecute;

  // --- Execute gate ---
  const executeBlockReasons: string[] = [];
  if (!approved) executeBlockReasons.push("Approval required");
  if (!validationAllowsExecution(validation)) executeBlockReasons.push("Validation must pass");
  if (hasActiveRun) executeBlockReasons.push("Active run already in progress for this target");
  const canExecute = executeBlockReasons.length === 0 && !busyApprove && !busyExecute;

  const handleApprove = async () => {
    setBusyApprove(true);
    setError(null);
    try {
      await onApprove();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusyApprove(false);
    }
  };

  const handleExecute = async () => {
    setBusyExecute(true);
    setError(null);
    try {
      const result = await onExecute();
      if (result.run_id) {
        onRunStarted(result.run_id);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusyExecute(false);
    }
  };

  return (
    <div className="space-y-3">
      {/* Production rollback acknowledgment */}
      {isProduction && rollbackDisabled && (
        <label className="flex items-start gap-2 rounded border border-yellow-400/50 bg-yellow-50 p-2 text-xs">
          <input
            type="checkbox"
            checked={rollbackAck}
            onChange={(e) => onRollbackAckChange(e.target.checked)}
            className="mt-0.5"
            aria-label="acknowledge rollback disabled"
          />
          <span>
            <strong>Production deployment:</strong> Rollback is not available in this release (MVP limitation).
            I acknowledge that this deployment cannot be automatically rolled back if it fails.
          </span>
        </label>
      )}

      <div className="flex flex-wrap gap-2">
        {/* Approve button */}
        <button
          onClick={() => void handleApprove()}
          disabled={!canApprove}
          aria-label="approve plan"
          title={canApprove ? undefined : approveBlockReasons.join("; ")}
          className="rounded bg-green-600 px-3 py-1.5 text-xs font-semibold text-white disabled:opacity-40"
        >
          {busyApprove ? "Approving…" : approved ? "Re-approve" : "Approve"}
        </button>

        {/* Execute button */}
        <button
          onClick={() => void handleExecute()}
          disabled={!canExecute}
          aria-label="execute deployment"
          title={canExecute ? undefined : executeBlockReasons.join("; ")}
          className="rounded bg-primary px-3 py-1.5 text-xs font-semibold text-primary-foreground disabled:opacity-40"
        >
          {busyExecute ? "Deploying…" : "Execute Deploy"}
        </button>
      </div>

      {/* Gate reasons */}
      {!canApprove && !approved && approveBlockReasons.length > 0 && (
        <ul className="space-y-0.5 text-xs text-muted-foreground" aria-label="approval blocked reasons">
          {approveBlockReasons.map((r, i) => (
            <li key={i} className="flex items-center gap-1">
              <span className="text-yellow-600">•</span>
              {r}
            </li>
          ))}
        </ul>
      )}

      {/* Execute blocked reasons (shown only when approved but blocked) */}
      {approved && !canExecute && executeBlockReasons.length > 0 && (
        <ul className="space-y-0.5 text-xs text-muted-foreground" aria-label="execute blocked reasons">
          {executeBlockReasons.map((r, i) => (
            <li key={i} className="flex items-center gap-1">
              <span className="text-destructive">•</span>
              {r}
            </li>
          ))}
        </ul>
      )}

      {/* Active run warning */}
      {hasActiveRun && (
        <p className="text-xs text-destructive" role="alert" aria-label="active run warning">
          A deployment is already running for this target. Wait for it to finish.
        </p>
      )}

      {error && (
        <p className="text-xs text-destructive" role="alert">{error}</p>
      )}
    </div>
  );
}
