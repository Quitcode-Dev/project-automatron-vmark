"use client";

/**
 * RunView — deployment run status display.
 *
 * Shows:
 *   - run status + health_status
 *   - each step's action_type, status, stdout/stderr excerpts (redacted)
 *   - rollback unavailable message if rollback_available !== 1
 *   - rollback button only if rollback_available === 1 (MVP: always shown as unavailable)
 */

import React from "react";
import type { DeploymentRun } from "@/lib/deploymentApi";
import { redactSecretContent } from "@/lib/redactSecrets";

export interface RunStep {
  id?: string;
  run_id?: string;
  step_index?: number;
  action_type: string;
  status: string;
  started_at?: string | null;
  finished_at?: string | null;
  stdout_excerpt?: string | null;
  stderr_excerpt?: string | null;
  error_message?: string | null;
}

export interface RunViewProps {
  run: DeploymentRun;
  steps: RunStep[];
  onRollback?: () => void;
  busyRollback?: boolean;
}

const STATUS_COLOR: Record<string, string> = {
  pending: "text-muted-foreground",
  running: "text-blue-600",
  completed: "text-green-700",
  failed: "text-destructive",
  cancelled: "text-muted-foreground",
  deploying: "text-blue-600",
  starting: "text-blue-600",
};

const HEALTH_COLOR: Record<string, string> = {
  healthy: "text-green-700",
  unhealthy: "text-destructive",
  degraded: "text-yellow-700",
  unknown: "text-muted-foreground",
  failed: "text-destructive",
};

function StepRow({ step }: { step: RunStep }) {
  const [showLogs, setShowLogs] = React.useState(false);
  const hasOutput = step.stdout_excerpt || step.stderr_excerpt || step.error_message;
  return (
    <li className="border-b border-border pb-1 last:border-none">
      <div className="flex items-center gap-2">
        <span className="w-5 text-center text-muted-foreground text-xs">
          {step.step_index != null ? step.step_index + 1 : "·"}
        </span>
        <code className="font-mono text-xs">{step.action_type}</code>
        <span className={`text-xs ${STATUS_COLOR[step.status] ?? "text-muted-foreground"}`}>
          {step.status}
        </span>
        {hasOutput && (
          <button
            onClick={() => setShowLogs((v) => !v)}
            className="ml-auto rounded bg-secondary px-1.5 py-0.5 text-xs"
            aria-label={showLogs ? "hide step output" : "show step output"}
          >
            {showLogs ? "hide" : "logs"}
          </button>
        )}
      </div>
      {showLogs && hasOutput && (
        <div className="mt-1 space-y-1 pl-7">
          {step.stdout_excerpt && (
            <pre className="max-h-24 overflow-auto rounded bg-muted p-1.5 text-xs" aria-label="stdout">
              {redactSecretContent(step.stdout_excerpt)}
            </pre>
          )}
          {step.stderr_excerpt && (
            <pre className="max-h-24 overflow-auto rounded bg-destructive/5 p-1.5 text-xs text-destructive" aria-label="stderr">
              {redactSecretContent(step.stderr_excerpt)}
            </pre>
          )}
          {step.error_message && (
            <p className="text-xs text-destructive" aria-label="step error">{step.error_message}</p>
          )}
        </div>
      )}
    </li>
  );
}

export function RunView({ run, steps, onRollback, busyRollback }: RunViewProps) {
  const rollbackAvailable = run.rollback_available === 1;

  return (
    <div className="space-y-3">
      {/* Run summary */}
      <div className="space-y-1 text-xs">
        <div className="flex items-center gap-2">
          <span className="text-muted-foreground">Status:</span>
          <span
            className={`font-semibold ${STATUS_COLOR[run.status] ?? "text-foreground"}`}
            aria-label={`run status: ${run.status}`}
          >
            {run.status}
          </span>
        </div>
        <div className="flex items-center gap-2">
          <span className="text-muted-foreground">Health:</span>
          <span
            className={HEALTH_COLOR[run.health_status] ?? "text-muted-foreground"}
            aria-label={`health status: ${run.health_status}`}
          >
            {run.health_status}
          </span>
        </div>
        {run.started_at && (
          <p><span className="text-muted-foreground">Started: </span>{run.started_at}</p>
        )}
        {run.finished_at && (
          <p><span className="text-muted-foreground">Finished: </span>{run.finished_at}</p>
        )}
      </div>

      {/* Steps */}
      {steps.length > 0 && (
        <div>
          <h4 className="mb-1 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
            Steps ({steps.length})
          </h4>
          <ul className="space-y-1" aria-label="deployment steps">
            {steps.map((step, i) => (
              <StepRow key={step.id ?? i} step={step} />
            ))}
          </ul>
        </div>
      )}

      {/* Rollback */}
      {rollbackAvailable ? (
        <button
          onClick={onRollback}
          disabled={busyRollback}
          className="rounded bg-destructive px-2 py-1 text-xs font-medium text-destructive-foreground disabled:opacity-50"
          aria-label="rollback deployment"
        >
          {busyRollback ? "Rolling back…" : "Rollback"}
        </button>
      ) : (
        <p
          className="text-xs text-muted-foreground"
          role="note"
          aria-label="rollback unavailable"
        >
          Rollback not available for this run (MVP limitation — automatic rollback is not yet
          implemented). To recover, redeploy a previous version manually.
        </p>
      )}
    </div>
  );
}
