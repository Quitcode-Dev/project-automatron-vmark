"use client";

/**
 * PlanDetail — full operator transparency view for a deployment plan.
 *
 * Shows everything an operator needs to review before approving:
 *   - selected target
 *   - detected server state (dm, rp, provider, confidence)
 *   - risk level
 *   - validation status + blocking errors + warnings
 *   - generated file paths + content previews (secrets redacted)
 *   - deployment_actions list
 *   - routing_plan, port_plan
 *   - required secrets
 *   - rollback status
 */

import React, { useState } from "react";
import type { DeploymentPlan, DeploymentTarget, ValidationResult } from "@/lib/deploymentApi";
import { redactSecretContent } from "@/lib/redactSecrets";

// Typed representation of plan_json so all fields are narrowed from `unknown`.
interface PlanJson {
  strategy?: string;
  risk_level?: string;
  summary?: string;
  docker_ai?: { provider?: string; analysis_id?: string | null; reasoning_summary?: string };
  detected_server_state?: {
    deployment_manager?: string;
    reverse_proxy?: string;
    confidence?: number;
    evidence?: string[];
  };
  port_plan?: {
    internal_app_port?: number | null;
    host_port?: number | null;
    uses_reverse_proxy?: boolean;
    reverse_proxy_type?: string;
  };
  routing_plan?: { domain?: string; router_name?: string; service_name?: string };
  deployment_actions?: Array<{ action_type?: string; params?: Record<string, string | number | boolean> }>;
  generated_files?: Array<{ path: string; purpose: string; content: string }>;
  secrets_required?: string[];
  rollback_plan?: { type?: string; previous_release_ref_required?: boolean };
  blocking_questions?: string[];
}

const RISK_COLORS: Record<string, string> = {
  low: "bg-green-100 text-green-800",
  medium: "bg-yellow-100 text-yellow-800",
  high: "bg-orange-100 text-orange-800",
  blocked: "bg-red-100 text-red-800",
};

function RiskBadge({ risk }: { risk: string }) {
  return (
    <span
      className={`rounded px-2 py-0.5 text-xs font-semibold ${RISK_COLORS[risk] ?? "bg-muted text-muted-foreground"}`}
      aria-label={`risk level: ${risk}`}
    >
      {risk.toUpperCase()}
    </span>
  );
}

function ProviderBadge({ provider }: { provider: string | undefined }) {
  const label =
    provider === "gordon" ? "Gordon (docker ai)"
    : provider === "docker_agent" ? "Docker Agent"
    : provider === "model_runner" ? "Model Runner"
    : provider === "litellm" ? "litellm (fallback)"
    : provider === "none" || !provider ? "No AI provider"
    : provider;
  const color =
    provider === "gordon" ? "bg-blue-100 text-blue-800"
    : provider === "litellm" ? "bg-gray-100 text-gray-600"
    : "bg-purple-100 text-purple-800";
  return (
    <span className={`rounded px-2 py-0.5 text-xs font-semibold ${color}`} aria-label={`provider: ${label}`}>
      {label}
    </span>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="space-y-1">
      <h4 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">{title}</h4>
      <div className="rounded border border-border p-2 text-xs">{children}</div>
    </div>
  );
}

function FilePreview({ file }: { file: { path: string; purpose: string; content: string } }) {
  const [expanded, setExpanded] = useState(false);
  const redacted = redactSecretContent(file.content);
  return (
    <div className="space-y-1">
      <div className="flex items-center justify-between gap-2">
        <code className="font-mono text-xs text-primary">{file.path}</code>
        <span className="text-muted-foreground">{file.purpose}</span>
        <button
          onClick={() => setExpanded((v) => !v)}
          className="rounded bg-secondary px-1.5 py-0.5 text-xs"
          aria-label={expanded ? `collapse ${file.path}` : `expand ${file.path}`}
        >
          {expanded ? "hide" : "preview"}
        </button>
      </div>
      {expanded && (
        <pre
          className="max-h-48 overflow-auto rounded bg-muted p-2 text-xs"
          aria-label={`content of ${file.path}`}
        >
          {redacted}
        </pre>
      )}
    </div>
  );
}

export interface PlanDetailProps {
  plan: DeploymentPlan;
  target: DeploymentTarget;
  validation: ValidationResult | null;
}

export function PlanDetail({ plan, target, validation }: PlanDetailProps) {
  const pj = plan.plan_json as PlanJson;
  const dockerAi = pj.docker_ai ?? {};
  const serverState = pj.detected_server_state ?? {};
  const portPlan = pj.port_plan ?? {};
  const routingPlan = pj.routing_plan ?? {};
  const actions = pj.deployment_actions ?? [];
  const generatedFiles = pj.generated_files ?? [];
  const secretsRequired = pj.secrets_required ?? [];
  const rollbackPlan = pj.rollback_plan ?? {};

  // Derive rollback status label
  const rollbackType = String(rollbackPlan.type ?? "");
  const rollbackStatus =
    rollbackType === "implemented" ? "implemented"
    : rollbackType === "metadata_only" ? "metadata_only"
    : "disabled";

  const rollbackLabel =
    rollbackStatus === "implemented" ? "✓ Rollback available"
    : rollbackStatus === "metadata_only" ? "⚠ Metadata captured, execution disabled (MVP)"
    : "✗ Rollback disabled (MVP)";

  return (
    <div className="mt-3 space-y-3">
      {/* Target */}
      <Section title="Target">
        <div className="space-y-0.5">
          <p><span className="text-muted-foreground">Name: </span><strong>{target.name}</strong></p>
          <p><span className="text-muted-foreground">Host: </span>{target.host}</p>
          <p><span className="text-muted-foreground">Environment: </span>
            <span className={target.environment === "production" ? "font-semibold text-orange-700" : ""}>
              {target.environment}
            </span>
          </p>
          <p><span className="text-muted-foreground">Deploy path: </span>{target.deploy_path}</p>
        </div>
      </Section>

      {/* Strategy + Risk */}
      <Section title="Plan">
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-muted-foreground">Strategy:</span>
          <strong>{pj.strategy ?? ""}</strong>
          <RiskBadge risk={plan.risk_level ?? "medium"} />
        </div>
        {plan.summary_markdown && (
          <p className="mt-1 text-muted-foreground">{plan.summary_markdown}</p>
        )}
      </Section>

      {/* Detected server state */}
      <Section title="Detected Server State">
        <div className="space-y-0.5">
          <p>
            <span className="text-muted-foreground">Deployment manager: </span>
            <strong>{serverState.deployment_manager ?? "unknown"}</strong>
          </p>
          <p>
            <span className="text-muted-foreground">Reverse proxy: </span>
            <strong>{serverState.reverse_proxy ?? "unknown"}</strong>
          </p>
          <p>
            <span className="text-muted-foreground">Confidence: </span>
            {((serverState.confidence ?? 0) * 100).toFixed(0)}%
          </p>
          {serverState.evidence?.length ? (
            <ul className="ml-2 mt-1 list-disc space-y-0.5 pl-3 text-muted-foreground">
              {serverState.evidence.slice(0, 5).map((ev, i) => (
                <li key={i}>{ev}</li>
              ))}
            </ul>
          ) : null}
          <div className="mt-1 flex items-center gap-2">
            <span className="text-muted-foreground">AI provider:</span>
            <ProviderBadge provider={dockerAi.provider} />
          </div>
        </div>
      </Section>

      {/* Validation */}
      <Section title="Validation">
        {validation ? (
          <div className="space-y-1">
            <p
              className={
                validation.status === "passed" ? "font-semibold text-green-700"
                : validation.status === "blocked" ? "font-semibold text-destructive"
                : "font-semibold text-yellow-700"
              }
              aria-label={`validation status: ${validation.status}`}
            >
              {validation.status.toUpperCase()}
            </p>
            {validation.blocking_errors.length > 0 && (
              <div aria-label="blocking errors">
                <p className="font-medium text-destructive">Blocking errors:</p>
                <ul className="mt-0.5 space-y-0.5">
                  {validation.blocking_errors.map((e, i) => (
                    <li key={i} className="flex items-start gap-1 text-destructive">
                      <span aria-hidden="true">✗</span>
                      <span>{e}</span>
                    </li>
                  ))}
                </ul>
              </div>
            )}
            {validation.warnings.length > 0 && (
              <div aria-label="warnings">
                <p className="font-medium text-yellow-700">Warnings:</p>
                <ul className="mt-0.5 space-y-0.5">
                  {validation.warnings.map((w, i) => (
                    <li key={i} className="flex items-start gap-1 text-yellow-700">
                      <span aria-hidden="true">⚠</span>
                      <span>{w}</span>
                    </li>
                  ))}
                </ul>
              </div>
            )}
          </div>
        ) : (
          <p className="text-muted-foreground">Not validated yet. Run validation before approving.</p>
        )}
      </Section>

      {/* Blocking questions — check plan_json first, fall back to the extracted column */}
      {(() => {
        const bqs: string[] = (pj.blocking_questions?.length)
          ? pj.blocking_questions
          : (plan.blocking_questions_json ?? []);
        return bqs.length > 0 ? (
          <div className="rounded border border-destructive/40 bg-destructive/5 p-2">
            <p className="font-semibold text-destructive" aria-label="blocking questions">
              Blocking questions (must resolve before approval):
            </p>
            <ul className="mt-1 list-disc pl-4 space-y-0.5">
              {bqs.map((q, i) => (
                <li key={i}>{q}</li>
              ))}
            </ul>
          </div>
        ) : null;
      })()}

      {/* Port plan */}
      {(portPlan.host_port || portPlan.internal_app_port) && (
        <Section title="Port Plan">
          <div className="space-y-0.5">
            {portPlan.internal_app_port != null && (
              <p><span className="text-muted-foreground">App port: </span>{String(portPlan.internal_app_port)}</p>
            )}
            {portPlan.host_port != null && (
              <p><span className="text-muted-foreground">Host port: </span>{String(portPlan.host_port)}</p>
            )}
            {portPlan.uses_reverse_proxy != null && (
              <p><span className="text-muted-foreground">Uses reverse proxy: </span>
                {portPlan.uses_reverse_proxy ? "yes" : "no"}
                {portPlan.reverse_proxy_type ? ` (${String(portPlan.reverse_proxy_type)})` : ""}
              </p>
            )}
          </div>
        </Section>
      )}

      {/* Routing plan */}
      {(routingPlan.domain || routingPlan.router_name || routingPlan.service_name) && (
        <Section title="Routing Plan">
          <div className="space-y-0.5">
            {routingPlan.domain && (
              <p><span className="text-muted-foreground">Domain: </span><strong>{String(routingPlan.domain)}</strong></p>
            )}
            {routingPlan.router_name && (
              <p><span className="text-muted-foreground">Router: </span>{String(routingPlan.router_name)}</p>
            )}
            {routingPlan.service_name && (
              <p><span className="text-muted-foreground">Service: </span>{String(routingPlan.service_name)}</p>
            )}
          </div>
        </Section>
      )}

      {/* Required secrets */}
      {secretsRequired.length > 0 && (
        <Section title="Required Secrets">
          <ul className="space-y-0.5">
            {secretsRequired.map((s, i) => (
              <li key={i} className="flex items-center gap-1">
                <span className="text-muted-foreground">•</span>
                <code className="font-mono">{s}</code>
              </li>
            ))}
          </ul>
        </Section>
      )}

      {/* Deployment actions */}
      {actions.length > 0 && (
        <Section title={`Deployment Actions (${actions.length})`}>
          <ol className="space-y-1" aria-label="deployment actions">
            {actions.map((action, i) => {
              const atype = action.action_type ?? "";
              const params = action.params ?? {};
              return (
                <li key={i} className="flex items-start gap-2">
                  <span className="mt-0.5 font-mono text-muted-foreground">{i + 1}.</span>
                  <div>
                    <span className="font-mono font-semibold">{atype}</span>
                    {Object.keys(params).length > 0 && (
                      <span className="ml-2 text-muted-foreground">
                        {Object.entries(params)
                          .slice(0, 3)
                          .map(([k, v]) => `${k}=${String(v)}`)
                          .join(", ")}
                      </span>
                    )}
                  </div>
                </li>
              );
            })}
          </ol>
        </Section>
      )}

      {/* Generated files */}
      {generatedFiles.length > 0 && (
        <Section title={`Generated Files (${generatedFiles.length})`}>
          <div className="space-y-2" aria-label="generated files">
            {generatedFiles.map((f, i) => (
              <FilePreview key={i} file={f} />
            ))}
          </div>
        </Section>
      )}

      {/* Rollback status */}
      <Section title="Rollback Status">
        <p
          className={
            rollbackStatus === "implemented" ? "text-green-700"
            : rollbackStatus === "metadata_only" ? "text-yellow-700"
            : "text-muted-foreground"
          }
          aria-label="rollback status"
        >
          {rollbackLabel}
        </p>
      </Section>
    </div>
  );
}
