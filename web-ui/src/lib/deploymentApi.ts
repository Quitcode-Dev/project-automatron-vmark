/**
 * API client helpers for the Docker Deployment Intelligence layer.
 * These wrap the 14 endpoints added in docker_deployment_ai.
 */

const BASE = "/api";

async function request<T = unknown>(
  path: string,
  options: RequestInit = {}
): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    headers: { "Content-Type": "application/json", ...(options.headers ?? {}) },
    credentials: "same-origin",
    ...options,
  });
  if (!res.ok) {
    const detail = await res.text().catch(() => res.statusText);
    throw new Error(`${res.status} ${detail}`);
  }
  return res.json() as Promise<T>;
}

// ---- Types ----

export interface DeploymentTarget {
  id: string;
  project_id: string;
  name: string;
  host: string;
  ssh_user: string;
  ssh_port: number;
  environment: string;
  domain: string | null;
  app_name: string;
  deploy_path: string;
  preferred_strategy: string;
  auth_mode: string;
  auth_reference: string | null;
  created_at: string;
  updated_at: string;
  last_inventory_snapshot_id: string | null;
}

export interface DeploymentTargetCreate {
  name: string;
  host: string;
  ssh_user: string;
  ssh_port?: number;
  environment?: string;
  domain?: string;
  app_name: string;
  deploy_path: string;
  preferred_strategy?: string;
  auth_mode?: string;
  auth_reference?: string;
}

export interface InventorySnapshot {
  id: string;
  target_id: string;
  project_id: string;
  status: string;
  raw_json: Record<string, unknown>;
  summary_json: {
    detected_reverse_proxy?: string;
    detected_deployment_manager?: string;
    confidence?: number;
    container_count?: number;
    error?: string | null;
  };
  detected_reverse_proxy: string | null;
  detected_deployment_manager: string | null;
  confidence: number;
  error_message: string | null;
  created_at: string;
}

export interface DockerAIAnalysis {
  id: string;
  provider: string;
  analysis_type: string;
  status: string;
  error_message: string | null;
  created_at: string;
}

export interface DeploymentPlan {
  id: string;
  project_id: string;
  target_id: string;
  status: string;
  plan_json: Record<string, unknown>;
  summary_markdown: string | null;
  risk_level: string;
  blocking_questions_json: string[];
  created_at: string;
}

export interface ValidationResult {
  status: "passed" | "warning" | "blocked";
  checks: Array<{ name: string; passed: boolean; message: string; blocking: boolean }>;
  warnings: string[];
  blocking_errors: string[];
}

export interface DeploymentRun {
  id: string;
  plan_id: string;
  project_id: string;
  target_id: string;
  status: string;
  started_by: string | null;
  started_at: string | null;
  finished_at: string | null;
  health_status: string;
  rollback_available: number;
}

// ---- Target API ----

export function createDeploymentTarget(
  projectId: string,
  data: DeploymentTargetCreate
): Promise<DeploymentTarget> {
  return request(`/projects/${projectId}/deployment-targets`, {
    method: "POST",
    body: JSON.stringify(data),
  });
}

export function listDeploymentTargets(projectId: string): Promise<DeploymentTarget[]> {
  return request(`/projects/${projectId}/deployment-targets`);
}

export function getDeploymentTarget(targetId: string): Promise<DeploymentTarget> {
  return request(`/deployment-targets/${targetId}`);
}

// ---- Inventory API ----

export function runInventory(targetId: string): Promise<{ status: string; target_id: string }> {
  return request(`/deployment-targets/${targetId}/inventory`, { method: "POST" });
}

export function getLatestInventory(targetId: string): Promise<InventorySnapshot> {
  return request(`/deployment-targets/${targetId}/inventory/latest`);
}

// ---- Docker AI API ----

export function runDockerAIAnalysis(
  targetId: string,
  analysisType = "analyze_inventory"
): Promise<{ status: string }> {
  return request(
    `/deployment-targets/${targetId}/docker-ai/analyze?analysis_type=${analysisType}`,
    { method: "POST" }
  );
}

export function listDockerAIAnalyses(targetId: string): Promise<DockerAIAnalysis[]> {
  return request(`/deployment-targets/${targetId}/docker-ai/analyses`);
}

// ---- Plan API ----

export function createDeploymentPlan(
  projectId: string,
  data: { target_id: string; desired_domain?: string; preferred_strategy?: string }
): Promise<{ status: string }> {
  return request(`/projects/${projectId}/deployment-plans`, {
    method: "POST",
    body: JSON.stringify(data),
  });
}

export function getDeploymentPlan(planId: string): Promise<DeploymentPlan> {
  return request(`/deployment-plans/${planId}`);
}

export function validateDeploymentPlan(planId: string): Promise<ValidationResult> {
  return request(`/deployment-plans/${planId}/validate`, { method: "POST" });
}

export function approveDeploymentPlan(
  planId: string,
  approvalNote?: string
): Promise<{ approval_id: string }> {
  return request(`/deployment-plans/${planId}/approve`, {
    method: "POST",
    body: JSON.stringify({ approval_note: approvalNote }),
  });
}

export function executeDeploymentPlan(planId: string): Promise<{ status: string }> {
  return request(`/deployment-plans/${planId}/execute`, { method: "POST" });
}

// ---- Run API ----

export function getDeploymentRun(runId: string): Promise<DeploymentRun> {
  return request(`/deployment-runs/${runId}`);
}

export function getDeploymentRunSteps(
  runId: string
): Promise<Array<Record<string, unknown>>> {
  return request(`/deployment-runs/${runId}/steps`);
}

export function rollbackDeploymentRun(runId: string): Promise<{ status: string }> {
  return request(`/deployment-runs/${runId}/rollback`, { method: "POST" });
}
