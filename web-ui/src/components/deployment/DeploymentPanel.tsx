"use client";

/**
 * DeploymentPanel — orchestrates the full Docker Deployment Intelligence flow.
 *
 * Sub-components handle display; this component owns data fetching, polling,
 * WebSocket event subscriptions, and state wiring.
 */

import React, { useCallback, useEffect, useRef, useState } from "react";
import {
  approveDeploymentPlan,
  createDeploymentPlan,
  createDeploymentTarget,
  DeploymentPlan,
  DockerAIAnalysis,
  DeploymentRun,
  DeploymentTarget,
  executeDeploymentPlan,
  getDeploymentPlan,
  getDeploymentRun,
  getDeploymentRunSteps,
  getLatestInventory,
  InventorySnapshot,
  listDeploymentTargets,
  listDockerAIAnalyses,
  rollbackDeploymentRun,
  runDockerAIAnalysis,
  runInventory,
  RunStep,
  validateDeploymentPlan,
  ValidationResult,
} from "@/lib/deploymentApi";
import { PlanDetail } from "./PlanDetail";
import { ApprovalGates } from "./ApprovalGates";
import { RunView } from "./RunView";
import { getSocket } from "@/lib/socket";

// ---- Provider badge (inventory / analysis section) ----
function ProviderBadge({ provider }: { provider: string | undefined }) {
  const label =
    provider === "gordon" ? "Gordon (docker ai)"
    : provider === "docker_agent" ? "Docker Agent"
    : provider === "model_runner" ? "Model Runner"
    : provider === "litellm" ? "litellm (fallback)"
    : provider === "none" || !provider ? "No AI provider available"
    : provider;
  const color =
    provider === "gordon" ? "bg-blue-100 text-blue-800"
    : provider === "litellm" ? "bg-gray-100 text-gray-600"
    : "bg-purple-100 text-purple-800";
  return (
    <span className={`rounded px-2 py-0.5 text-xs font-semibold ${color}`}>{label}</span>
  );
}

// ---- Analysis result card (details, not just a status string) ----
function AnalysisCard({ analysis, expanded }: { analysis: DockerAIAnalysis; expanded: boolean }) {
  const [open, setOpen] = useState(expanded);
  const n = analysis.normalized ?? {};
  const ok = analysis.status === "ok";
  const confidencePct =
    typeof n.confidence === "number" ? `${(n.confidence * 100).toFixed(0)}%` : null;
  const hasDetails =
    !!(n.deployment_manager || n.reverse_proxy || n.recommended_strategy ||
       (n.evidence?.length) || (n.risks?.length) || n.notes || n.reasoning_summary);

  return (
    <div className="rounded-lg border border-border bg-background/40 p-2">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-2 text-left"
      >
        <span className="text-muted-foreground">{hasDetails ? (open ? "▾" : "▸") : "•"}</span>
        <ProviderBadge provider={analysis.provider} />
        <span className="text-muted-foreground">{analysis.analysis_type}</span>
        <span className={ok ? "text-green-600" : "text-destructive"}>{analysis.status}</span>
        {confidencePct && <span className="ml-auto text-muted-foreground">conf {confidencePct}</span>}
      </button>

      {!ok && analysis.error_message && (
        <p className="mt-1 pl-5 text-destructive">{analysis.error_message}</p>
      )}

      {open && hasDetails && (
        <div className="mt-2 space-y-1.5 pl-5">
          {(n.deployment_manager || n.reverse_proxy) && (
            <div className="flex flex-wrap gap-x-4">
              {n.deployment_manager && (
                <span><span className="text-muted-foreground">manager: </span><strong>{n.deployment_manager}</strong></span>
              )}
              {n.reverse_proxy && (
                <span><span className="text-muted-foreground">proxy: </span><strong>{n.reverse_proxy}</strong></span>
              )}
            </div>
          )}
          {n.recommended_strategy && (
            <p><span className="text-muted-foreground">recommended strategy: </span><strong>{n.recommended_strategy}</strong></p>
          )}
          {(n.notes || n.reasoning_summary) && (
            <p className="text-muted-foreground">{n.notes || n.reasoning_summary}</p>
          )}
          {n.evidence && n.evidence.length > 0 && (
            <div>
              <p className="text-muted-foreground">evidence:</p>
              <ul className="ml-3 list-disc">
                {n.evidence.map((e, i) => <li key={i}>{e}</li>)}
              </ul>
            </div>
          )}
          {n.risks && n.risks.length > 0 && (
            <div>
              <p className="text-amber-600">risks:</p>
              <ul className="ml-3 list-disc text-amber-700">
                {n.risks.map((r, i) => <li key={i}>{r}</li>)}
              </ul>
            </div>
          )}
          {n.warnings && n.warnings.length > 0 && (
            <div>
              <p className="text-amber-600">warnings:</p>
              <ul className="ml-3 list-disc text-amber-700">
                {n.warnings.map((w, i) => <li key={i}>{w}</li>)}
              </ul>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// ---- Target registration form ----
function TargetForm({
  projectId,
  onCreated,
}: {
  projectId: string;
  onCreated: (t: DeploymentTarget) => void;
}) {
  const [form, setForm] = useState({
    name: "prod-server",
    host: "",
    ssh_user: "deploy",
    ssh_port: 22,
    app_name: "",
    deploy_path: "/opt/automatron/apps",
    environment: "production",
    preferred_strategy: "auto_detect",
    domain: "",
    auth_mode: "ssh_key",
    auth_reference: "",
  });
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setSaving(true);
    setError(null);
    try {
      const target = await createDeploymentTarget(projectId, form);
      onCreated(target);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to create target");
    } finally {
      setSaving(false);
    }
  };

  const field = (label: string, key: keyof typeof form, type = "text") => (
    <label className="flex flex-col gap-1 text-xs">
      <span className="font-medium text-muted-foreground">{label}</span>
      <input
        type={type}
        value={String(form[key])}
        onChange={(e) =>
          setForm((f) => ({
            ...f,
            [key]: type === "number" ? Number(e.target.value) : e.target.value,
          }))
        }
        className="rounded border border-input bg-background px-2 py-1.5 text-sm"
      />
    </label>
  );

  return (
    <form onSubmit={(e) => void handleSubmit(e)} className="space-y-3">
      <h4 className="text-sm font-semibold">Register Deployment Target</h4>
      <div className="grid grid-cols-2 gap-3">
        {field("Name", "name")}
        {field("Host / IP", "host")}
        {field("SSH User", "ssh_user")}
        {field("SSH Port", "ssh_port", "number")}
        {field("App Name", "app_name")}
        {field("Deploy Path", "deploy_path")}
        {field("Domain (optional)", "domain")}
        {field("Auth Reference (key path)", "auth_reference")}
      </div>
      <div className="grid grid-cols-2 gap-3">
        <label className="flex flex-col gap-1 text-xs">
          <span className="font-medium text-muted-foreground">Environment</span>
          <select
            value={form.environment}
            onChange={(e) => setForm((f) => ({ ...f, environment: e.target.value }))}
            className="rounded border border-input bg-background px-2 py-1.5 text-sm"
          >
            {["preview", "staging", "production"].map((v) => (
              <option key={v} value={v}>{v}</option>
            ))}
          </select>
        </label>
        <label className="flex flex-col gap-1 text-xs">
          <span className="font-medium text-muted-foreground">Preferred Strategy</span>
          <select
            value={form.preferred_strategy}
            onChange={(e) => setForm((f) => ({ ...f, preferred_strategy: e.target.value }))}
            className="rounded border border-input bg-background px-2 py-1.5 text-sm"
          >
            {[
              "auto_detect", "docker_compose_private", "docker_compose_with_host_port",
              "existing_traefik", "kamal_v1", "kamal_v2", "existing_nginx",
              "existing_caddy", "no_public_exposure",
            ].map((v) => <option key={v} value={v}>{v}</option>)}
          </select>
        </label>
      </div>
      {error && <p className="text-xs text-destructive">{error}</p>}
      <button
        type="submit"
        disabled={saving}
        className="rounded bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground disabled:opacity-50"
      >
        {saving ? "Saving…" : "Register Target"}
      </button>
    </form>
  );
}

// ---- Inventory polling ----
// The POST /inventory endpoint kicks off a background SSH collection (can take
// 15-30s) and returns immediately with {status:"started"}. Polling the latest
// snapshot until a *new* one (different id) appears avoids the race where the
// UI fetches the stale/absent snapshot right after triggering the run.
async function pollForNewInventory(
  targetId: string,
  previousId: string | null,
  { intervalMs = 2000, timeoutMs = 90000 }: { intervalMs?: number; timeoutMs?: number } = {},
): Promise<InventorySnapshot | null> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const s = await getLatestInventory(targetId).catch(() => null);
    if (s && s.id !== previousId) return s;
    await new Promise((r) => setTimeout(r, intervalMs));
  }
  // Timed out — return whatever is latest (may still be the previous snapshot)
  return getLatestInventory(targetId).catch(() => null);
}

// Docker AI analysis is also a background task. Poll the list until a *new*
// analysis (different newest id) appears, mirroring the inventory flow.
async function pollForNewAnalysis(
  targetId: string,
  previousId: string | null,
  { intervalMs = 2000, timeoutMs = 120000 }: { intervalMs?: number; timeoutMs?: number } = {},
): Promise<DockerAIAnalysis[]> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const list = await listDockerAIAnalyses(targetId).catch(() => []);
    if (list.length > 0 && list[0].id !== previousId) return list;
    await new Promise((r) => setTimeout(r, intervalMs));
  }
  return listDockerAIAnalyses(targetId).catch(() => []);
}

// ---- Polling hook ----
const TERMINAL_STATUSES = new Set(["completed", "failed", "cancelled", "error"]);

function useRunPoller(
  runId: string | null,
  onUpdate: (run: DeploymentRun, steps: RunStep[]) => void,
) {
  const timer = useRef<ReturnType<typeof setInterval> | null>(null);

  useEffect(() => {
    if (!runId) return;
    const poll = async () => {
      try {
        const [run, steps] = await Promise.all([
          getDeploymentRun(runId),
          getDeploymentRunSteps(runId),
        ]);
        onUpdate(run, steps);
        if (TERMINAL_STATUSES.has(run.status)) {
          if (timer.current) clearInterval(timer.current);
          timer.current = null;
        }
      } catch {
        // network errors during polling are non-fatal
      }
    };
    void poll(); // immediate first poll
    timer.current = setInterval(() => void poll(), 3000);
    return () => {
      if (timer.current) clearInterval(timer.current);
    };
  }, [runId, onUpdate]);
}

// ---- Main panel ----
export default function DeploymentPanel({ projectId }: { projectId: string }) {
  const [targets, setTargets] = useState<DeploymentTarget[]>([]);
  const [selectedTarget, setSelectedTarget] = useState<DeploymentTarget | null>(null);
  const [snapshot, setSnapshot] = useState<InventorySnapshot | null>(null);
  const [analyses, setAnalyses] = useState<Awaited<ReturnType<typeof listDockerAIAnalyses>>>([]);
  const [plan, setPlan] = useState<DeploymentPlan | null>(null);
  const [validation, setValidation] = useState<ValidationResult | null>(null);
  const [approved, setApproved] = useState(false);
  const [rollbackAck, setRollbackAck] = useState(false);
  const [run, setRun] = useState<DeploymentRun | null>(null);
  const [runSteps, setRunSteps] = useState<RunStep[]>([]);
  const [activeRunId, setActiveRunId] = useState<string | null>(null);
  const [hasActiveRun, setHasActiveRun] = useState(false);
  const [showTargetForm, setShowTargetForm] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);

  // Reset plan/validation/approval when target changes
  useEffect(() => {
    setPlan(null);
    setValidation(null);
    setApproved(false);
    setRollbackAck(false);
    setRun(null);
    setRunSteps([]);
    setActiveRunId(null);
    setHasActiveRun(false);
  }, [selectedTarget?.id]);

  // Load targets on mount
  const loadTargets = useCallback(async () => {
    const ts = await listDeploymentTargets(projectId).catch(() => []);
    setTargets(ts);
    if (ts.length > 0 && !selectedTarget) {
      setSelectedTarget(ts[0]);
    }
  }, [projectId, selectedTarget]);

  useEffect(() => {
    void loadTargets();
  }, [loadTargets]);

  // Load inventory and analyses when target changes
  useEffect(() => {
    if (!selectedTarget) return;
    void getLatestInventory(selectedTarget.id)
      .then(setSnapshot)
      .catch(() => setSnapshot(null));
    void listDockerAIAnalyses(selectedTarget.id)
      .then(setAnalyses)
      .catch(() => setAnalyses([]));
  }, [selectedTarget]);

  // WebSocket: listen for deployment events on this project
  useEffect(() => {
    const socket = getSocket();
    if (!socket) return;

    const onRunCompleted = (data: { run_id?: string }) => {
      if (data.run_id && data.run_id === activeRunId) {
        void getDeploymentRun(data.run_id).then((r) => setRun(r)).catch(() => {});
        void getDeploymentRunSteps(data.run_id).then(setRunSteps).catch(() => {});
      }
    };
    const onStepCompleted = (data: { run_id?: string }) => {
      if (data.run_id && data.run_id === activeRunId) {
        void getDeploymentRunSteps(data.run_id).then(setRunSteps).catch(() => {});
      }
    };

    socket.on("deployment.run.completed", onRunCompleted);
    socket.on("deployment.run.step_completed", onStepCompleted);
    return () => {
      socket.off("deployment.run.completed", onRunCompleted);
      socket.off("deployment.run.step_completed", onStepCompleted);
    };
  }, [activeRunId]);

  // Polling fallback: update run + steps every 3s until terminal
  const handleRunUpdate = useCallback((updatedRun: DeploymentRun, steps: RunStep[]) => {
    setRun(updatedRun);
    setRunSteps(steps);
    if (TERMINAL_STATUSES.has(updatedRun.status)) {
      setHasActiveRun(false);
    }
  }, []);

  useRunPoller(activeRunId, handleRunUpdate);

  const action = async (label: string, fn: () => Promise<void>) => {
    setBusy(label);
    setMsg(null);
    try {
      await fn();
    } catch (err) {
      setMsg(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(null);
    }
  };

  const handleRunStarted = useCallback((runId: string) => {
    setActiveRunId(runId);
    setHasActiveRun(true);
    setMsg(`Deployment started — tracking run ${runId}`);
  }, []);

  const handleApprove = useCallback(async () => {
    if (!plan) return;
    await approveDeploymentPlan(plan.id);
    setApproved(true);
    setMsg("Plan approved");
  }, [plan]);

  const handleExecute = useCallback(async () => {
    if (!plan) throw new Error("No plan selected");
    return executeDeploymentPlan(plan.id);
  }, [plan]);

  const handleRollback = useCallback(async () => {
    if (!run) return;
    await action("Rollback", async () => {
      await rollbackDeploymentRun(run.id);
    });
  }, [run]);

  return (
    <div className="flex h-full flex-col gap-4 overflow-auto p-1 text-sm">
      {/* ---- 1. Targets ---- */}
      <section className="rounded-xl border border-border bg-card p-4">
        <div className="flex items-center justify-between">
          <h3 className="font-semibold">Deployment Targets</h3>
          <button
            onClick={() => setShowTargetForm((v) => !v)}
            className="rounded bg-secondary px-2 py-1 text-xs font-medium"
          >
            {showTargetForm ? "Cancel" : "+ Add Target"}
          </button>
        </div>
        {showTargetForm && (
          <div className="mt-3 rounded-lg border border-dashed border-border p-3">
            <TargetForm
              projectId={projectId}
              onCreated={(t) => {
                setTargets((ts) => [t, ...ts]);
                setSelectedTarget(t);
                setShowTargetForm(false);
              }}
            />
          </div>
        )}
        {targets.length === 0 && !showTargetForm && (
          <p className="mt-2 text-xs text-muted-foreground">
            No targets yet. Register one to get started.
          </p>
        )}
        <div className="mt-2 flex flex-wrap gap-2">
          {targets.map((t) => (
            <button
              key={t.id}
              onClick={() => setSelectedTarget(t)}
              className={`rounded-lg border px-3 py-1 text-xs transition ${
                selectedTarget?.id === t.id
                  ? "border-primary bg-primary/10 font-semibold"
                  : "border-border bg-background hover:bg-muted"
              }`}
            >
              {t.name} <span className="text-muted-foreground">({t.host})</span>
            </button>
          ))}
        </div>
      </section>

      {selectedTarget && (
        <>
          {/* ---- 2-4. Inventory + server state ---- */}
          <section className="rounded-xl border border-border bg-card p-4">
            <div className="flex items-center justify-between">
              <h3 className="font-semibold">Server Inventory</h3>
              <button
                onClick={() =>
                  void action("Run Inventory", async () => {
                    const previousId = snapshot?.id ?? null;
                    await runInventory(selectedTarget.id);
                    const s = await pollForNewInventory(selectedTarget.id, previousId);
                    setSnapshot(s);
                  })
                }
                disabled={busy !== null}
                className="rounded bg-primary px-2 py-1 text-xs font-medium text-primary-foreground disabled:opacity-50"
              >
                {busy === "Run Inventory" ? "Running…" : "Run Inventory"}
              </button>
            </div>
            {snapshot ? (
              <div className="mt-3 space-y-1 text-xs">
                <p>
                  <span className="text-muted-foreground">Deployment manager: </span>
                  <strong>{snapshot.detected_deployment_manager ?? "unknown"}</strong>
                </p>
                <p>
                  <span className="text-muted-foreground">Reverse proxy: </span>
                  <strong>{snapshot.detected_reverse_proxy ?? "unknown"}</strong>
                </p>
                <p>
                  <span className="text-muted-foreground">Confidence: </span>
                  {((snapshot.confidence ?? 0) * 100).toFixed(0)}%
                </p>
                <p>
                  <span className="text-muted-foreground">Containers: </span>
                  {snapshot.summary_json?.container_count ?? "?"}
                </p>
                {snapshot.error_message && (
                  <p className="text-destructive">{snapshot.error_message}</p>
                )}
              </div>
            ) : (
              <p className="mt-2 text-xs text-muted-foreground">No inventory yet.</p>
            )}
          </section>

          {/* ---- 5. Docker AI analysis ---- */}
          <section className="rounded-xl border border-border bg-card p-4">
            <div className="flex items-center justify-between">
              <h3 className="font-semibold">Docker AI Analysis</h3>
              <button
                onClick={() =>
                  void action("Analyze", async () => {
                    const previousId = analyses[0]?.id ?? null;
                    await runDockerAIAnalysis(selectedTarget.id);
                    const a = await pollForNewAnalysis(selectedTarget.id, previousId);
                    setAnalyses(a);
                  })
                }
                disabled={busy !== null || !snapshot}
                className="rounded bg-primary px-2 py-1 text-xs font-medium text-primary-foreground disabled:opacity-50"
              >
                {busy === "Analyze" ? "Analyzing…" : "Run Analysis"}
              </button>
            </div>
            {analyses.length > 0 ? (
              <div className="mt-3 space-y-3 text-xs">
                {analyses.slice(0, 3).map((a, i) => (
                  <AnalysisCard key={a.id} analysis={a} expanded={i === 0} />
                ))}
              </div>
            ) : (
              <p className="mt-2 text-xs text-muted-foreground">
                No analyses yet. Run inventory first.
              </p>
            )}
          </section>

          {/* ---- 7-8. Plan + validation ---- */}
          <section className="rounded-xl border border-border bg-card p-4">
            <div className="flex items-center justify-between">
              <h3 className="font-semibold">Deployment Plan</h3>
              <div className="flex gap-2">
                <button
                  onClick={() =>
                    void action("Create Plan", async () => {
                      const r = await createDeploymentPlan(projectId, {
                        target_id: selectedTarget.id,
                        preferred_strategy: selectedTarget.preferred_strategy,
                      });
                      if (r.plan_id) {
                        // Poll until the background task writes the plan
                        await new Promise((res) => setTimeout(res, 3000));
                        const fetched = await getDeploymentPlan(r.plan_id);
                        setPlan(fetched);
                        setValidation(null);
                        setApproved(false);
                      }
                    })
                  }
                  disabled={busy !== null || !snapshot}
                  className="rounded bg-primary px-2 py-1 text-xs font-medium text-primary-foreground disabled:opacity-50"
                >
                  {busy === "Create Plan" ? "Planning…" : "Create Plan"}
                </button>
                {plan && (
                  <button
                    onClick={() =>
                      void action("Validate", async () => {
                        const v = await validateDeploymentPlan(plan.id);
                        setValidation(v);
                      })
                    }
                    disabled={busy !== null}
                    className="rounded bg-secondary px-2 py-1 text-xs font-medium"
                  >
                    {busy === "Validate" ? "Validating…" : "Validate"}
                  </button>
                )}
              </div>
            </div>

            {plan ? (
              <PlanDetail
                plan={plan}
                target={selectedTarget}
                validation={validation}
              />
            ) : (
              <p className="mt-2 text-xs text-muted-foreground">
                No plan yet. Run inventory and analysis first.
              </p>
            )}
          </section>

          {/* ---- 10. Approval + execution ---- */}
          {plan && (
            <section className="rounded-xl border border-border bg-card p-4">
              <h3 className="mb-3 font-semibold">Approval &amp; Execution</h3>
              <ApprovalGates
                plan={plan}
                target={selectedTarget}
                validation={validation}
                approved={approved}
                hasActiveRun={hasActiveRun}
                rollbackAck={rollbackAck}
                onRollbackAckChange={setRollbackAck}
                onApprove={handleApprove}
                onExecute={handleExecute}
                onRunStarted={handleRunStarted}
              />
            </section>
          )}

          {/* ---- 12-13. Deployment run ---- */}
          {run && (
            <section className="rounded-xl border border-border bg-card p-4">
              <h3 className="mb-3 font-semibold">Deployment Run</h3>
              <RunView
                run={run}
                steps={runSteps}
                onRollback={() => void handleRollback()}
                busyRollback={busy === "Rollback"}
              />
            </section>
          )}
        </>
      )}

      {/* Status / feedback bar */}
      {msg && (
        <div className="rounded-lg border border-border bg-muted px-3 py-2 text-xs">
          {msg}
        </div>
      )}
    </div>
  );
}
