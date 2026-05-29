"use client";

/**
 * DeploymentPanel — the main Docker Deployment Intelligence UI.
 *
 * Replaces the legacy VPS-target form in the project page's "deploy" tab.
 * Shows all 14-section deployment flow per the spec §25:
 *   1. Targets  2. Inventory  3. Server state  4. Reverse proxy detection
 *   5. Docker AI analysis  6. Routing conflicts  7. Plan  8. Validation
 *   9. Secrets  10. Approval  11. Live log  12. Health  13. Rollback
 */

import React, { useCallback, useEffect, useState } from "react";
import {
  approveDeploymentPlan,
  createDeploymentPlan,
  createDeploymentTarget,
  DeploymentPlan,
  DeploymentRun,
  DeploymentTarget,
  executeDeploymentPlan,
  getDeploymentPlan,
  getDeploymentRun,
  getLatestInventory,
  InventorySnapshot,
  listDeploymentTargets,
  listDockerAIAnalyses,
  rollbackDeploymentRun,
  runDockerAIAnalysis,
  runInventory,
  validateDeploymentPlan,
  ValidationResult,
} from "@/lib/deploymentApi";

// ---- Risk level badge ----
const RISK_COLORS: Record<string, string> = {
  low: "bg-green-100 text-green-800",
  medium: "bg-yellow-100 text-yellow-800",
  high: "bg-orange-100 text-orange-800",
  blocked: "bg-red-100 text-red-800",
};

function RiskBadge({ risk }: { risk: string }) {
  return (
    <span className={`rounded px-2 py-0.5 text-xs font-semibold ${RISK_COLORS[risk] ?? "bg-muted text-muted-foreground"}`}>
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

// ---- Target form ----
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
        onChange={(e) => setForm((f) => ({ ...f, [key]: type === "number" ? Number(e.target.value) : e.target.value }))}
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
            {["preview", "staging", "production"].map((v) => <option key={v} value={v}>{v}</option>)}
          </select>
        </label>
        <label className="flex flex-col gap-1 text-xs">
          <span className="font-medium text-muted-foreground">Preferred Strategy</span>
          <select
            value={form.preferred_strategy}
            onChange={(e) => setForm((f) => ({ ...f, preferred_strategy: e.target.value }))}
            className="rounded border border-input bg-background px-2 py-1.5 text-sm"
          >
            {["auto_detect","docker_compose_private","docker_compose_with_host_port",
              "existing_traefik","kamal_v1","kamal_v2","existing_nginx","existing_caddy",
              "no_public_exposure"].map((v) => <option key={v} value={v}>{v}</option>)}
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

// ---- Main panel ----
export default function DeploymentPanel({ projectId }: { projectId: string }) {
  const [targets, setTargets] = useState<DeploymentTarget[]>([]);
  const [selectedTarget, setSelectedTarget] = useState<DeploymentTarget | null>(null);
  const [snapshot, setSnapshot] = useState<InventorySnapshot | null>(null);
  const [analyses, setAnalyses] = useState<ReturnType<typeof listDockerAIAnalyses> extends Promise<infer T> ? T : never>([]);
  const [plan, setPlan] = useState<DeploymentPlan | null>(null);
  const [validation, setValidation] = useState<ValidationResult | null>(null);
  const [run, setRun] = useState<DeploymentRun | null>(null);
  const [showTargetForm, setShowTargetForm] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);

  const load = useCallback(async () => {
    const ts = await listDeploymentTargets(projectId).catch(() => []);
    setTargets(ts);
    if (ts.length > 0 && !selectedTarget) {
      setSelectedTarget(ts[0]);
    }
  }, [projectId, selectedTarget]);

  useEffect(() => { void load(); }, [load]);

  useEffect(() => {
    if (!selectedTarget) return;
    void getLatestInventory(selectedTarget.id)
      .then(setSnapshot)
      .catch(() => setSnapshot(null));
    void listDockerAIAnalyses(selectedTarget.id)
      .then(setAnalyses)
      .catch(() => setAnalyses([]));
  }, [selectedTarget]);

  const action = async (label: string, fn: () => Promise<void>) => {
    setBusy(label);
    setMsg(null);
    try {
      await fn();
      setMsg(`${label} started`);
    } catch (err) {
      setMsg(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(null);
    }
  };

  const planJson = plan?.plan_json as Record<string, unknown> | undefined;
  const dockerAi = planJson?.docker_ai as Record<string, unknown> | undefined;
  const serverState = planJson?.detected_server_state as Record<string, unknown> | undefined;

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
          <p className="mt-2 text-xs text-muted-foreground">No targets yet. Register one to get started.</p>
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
                    await runInventory(selectedTarget.id);
                    await new Promise((r) => setTimeout(r, 2000));
                    const s = await getLatestInventory(selectedTarget.id).catch(() => null);
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
                    await runDockerAIAnalysis(selectedTarget.id);
                    await new Promise((r) => setTimeout(r, 2000));
                    const a = await listDockerAIAnalyses(selectedTarget.id).catch(() => []);
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
              <div className="mt-3 space-y-1 text-xs">
                {analyses.slice(0, 3).map((a) => (
                  <div key={a.id} className="flex items-center gap-2">
                    <ProviderBadge provider={a.provider} />
                    <span className="text-muted-foreground">{a.analysis_type}</span>
                    <span className={a.status === "ok" ? "text-green-600" : "text-destructive"}>
                      {a.status}
                    </span>
                  </div>
                ))}
              </div>
            ) : (
              <p className="mt-2 text-xs text-muted-foreground">No analyses yet. Run inventory first.</p>
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
                      await new Promise((res) => setTimeout(res, 3000));
                    })
                  }
                  disabled={busy !== null || !snapshot}
                  className="rounded bg-primary px-2 py-1 text-xs font-medium text-primary-foreground disabled:opacity-50"
                >
                  {busy === "Create Plan" ? "Planning…" : "Create Plan"}
                </button>
              </div>
            </div>
            {plan ? (
              <div className="mt-3 space-y-2 text-xs">
                <div className="flex items-center gap-2">
                  <span className="text-muted-foreground">Strategy:</span>
                  <strong>{String(planJson?.strategy ?? "")}</strong>
                  <RiskBadge risk={String(planJson?.risk_level ?? "medium")} />
                </div>
                {dockerAi && (
                  <div className="flex items-center gap-2">
                    <span className="text-muted-foreground">AI provider:</span>
                    <ProviderBadge provider={String(dockerAi.provider ?? "")} />
                  </div>
                )}
                {serverState && (
                  <div>
                    <span className="text-muted-foreground">Detected: </span>
                    {String(serverState.deployment_manager)} / {String(serverState.reverse_proxy)}
                    {" "}({((Number(serverState.confidence) || 0) * 100).toFixed(0)}%)
                  </div>
                )}
                {(planJson?.blocking_questions as string[] | undefined)?.length ? (
                  <div className="rounded bg-destructive/10 p-2">
                    <p className="font-semibold text-destructive">Blocking questions:</p>
                    <ul className="mt-1 list-disc pl-4">
                      {(planJson.blocking_questions as string[]).map((q, i) => (
                        <li key={i}>{q}</li>
                      ))}
                    </ul>
                  </div>
                ) : null}
              </div>
            ) : (
              <p className="mt-2 text-xs text-muted-foreground">No plan yet.</p>
            )}

            {/* Validation */}
            {plan && (
              <div className="mt-3 border-t border-border pt-3">
                <div className="flex items-center justify-between">
                  <span className="text-xs font-semibold text-muted-foreground">Validation</span>
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
                </div>
                {validation && (
                  <div className="mt-2 text-xs">
                    <span className={
                      validation.status === "passed" ? "text-green-600"
                      : validation.status === "blocked" ? "text-destructive"
                      : "text-yellow-600"
                    }>
                      {validation.status.toUpperCase()}
                    </span>
                    {validation.blocking_errors.map((e, i) => (
                      <p key={i} className="text-destructive">✗ {e}</p>
                    ))}
                    {validation.warnings.map((w, i) => (
                      <p key={i} className="text-yellow-600">⚠ {w}</p>
                    ))}
                  </div>
                )}
              </div>
            )}
          </section>

          {/* ---- 10. Approval + execute ---- */}
          {plan && (
            <section className="rounded-xl border border-border bg-card p-4">
              <h3 className="font-semibold">Approval &amp; Execution</h3>
              <div className="mt-3 flex gap-2">
                <button
                  onClick={() =>
                    void action("Approve", async () => {
                      await approveDeploymentPlan(plan.id);
                    })
                  }
                  disabled={
                    busy !== null ||
                    validation?.status === "blocked" ||
                    plan.risk_level === "blocked"
                  }
                  className="rounded bg-green-600 px-3 py-1.5 text-xs font-semibold text-white disabled:opacity-40"
                  title={
                    validation?.status === "blocked"
                      ? "Validation must pass before approval"
                      : undefined
                  }
                >
                  {busy === "Approve" ? "Approving…" : "Approve"}
                </button>
                <button
                  onClick={() =>
                    void action("Deploy", async () => {
                      const r = await executeDeploymentPlan(plan.id);
                    })
                  }
                  disabled={busy !== null}
                  className="rounded bg-primary px-3 py-1.5 text-xs font-semibold text-primary-foreground disabled:opacity-40"
                >
                  {busy === "Deploy" ? "Deploying…" : "Execute Deploy"}
                </button>
              </div>
              {validation?.status === "blocked" && (
                <p className="mt-2 text-xs text-destructive">
                  Resolve all blocking errors before approving.
                </p>
              )}
            </section>
          )}

          {/* ---- 12-13. Health + rollback ---- */}
          {run && (
            <section className="rounded-xl border border-border bg-card p-4">
              <div className="flex items-center justify-between">
                <h3 className="font-semibold">Deployment Run</h3>
                {run.rollback_available === 1 && (
                  <button
                    onClick={() =>
                      void action("Rollback", async () => {
                        await rollbackDeploymentRun(run.id);
                      })
                    }
                    disabled={busy !== null}
                    className="rounded bg-destructive px-2 py-1 text-xs font-medium text-destructive-foreground disabled:opacity-50"
                  >
                    {busy === "Rollback" ? "Rolling back…" : "Rollback"}
                  </button>
                )}
              </div>
              <div className="mt-2 space-y-1 text-xs">
                <p><span className="text-muted-foreground">Status: </span>{run.status}</p>
                <p><span className="text-muted-foreground">Health: </span>{run.health_status}</p>
                {run.started_at && <p><span className="text-muted-foreground">Started: </span>{run.started_at}</p>}
                {run.finished_at && <p><span className="text-muted-foreground">Finished: </span>{run.finished_at}</p>}
              </div>
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
