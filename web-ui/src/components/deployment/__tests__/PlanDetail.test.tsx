import React from "react";
import { describe, it, expect } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { PlanDetail } from "../PlanDetail";
import type { DeploymentPlan, DeploymentTarget, ValidationResult } from "@/lib/deploymentApi";

// ---- Fixtures ----

const mockTarget: DeploymentTarget = {
  id: "tgt-1",
  project_id: "proj-1",
  name: "prod-server",
  host: "10.0.0.1",
  ssh_user: "deploy",
  ssh_port: 22,
  environment: "production",
  domain: "myapp.example.com",
  app_name: "myapp",
  deploy_path: "/opt/myapp",
  preferred_strategy: "docker_compose_private",
  auth_mode: "ssh_key",
  auth_reference: null,
  created_at: "2024-01-01",
  updated_at: "2024-01-01",
  last_inventory_snapshot_id: null,
};

function makePlan(overrides: Partial<Record<string, unknown>> = {}): DeploymentPlan {
  return {
    id: "plan-1",
    project_id: "proj-1",
    target_id: "tgt-1",
    status: "approved",
    plan_json: {
      strategy: "docker_compose_private",
      risk_level: "low",
      summary: "Deploy myapp using Docker Compose",
      docker_ai: { provider: "gordon", analysis_id: "ai-1", reasoning_summary: "Clean host" },
      detected_server_state: {
        deployment_manager: "none",
        reverse_proxy: "none",
        confidence: 0.95,
        evidence: ["No proxy detected", "Docker daemon running"],
      },
      deployment_actions: [
        { action_type: "CREATE_DIRECTORY", params: { path: "/opt/myapp" } },
        { action_type: "DOCKER_COMPOSE_UP", params: { project_name: "myapp", compose_file: "/opt/myapp/docker-compose.yml" } },
      ],
      generated_files: [
        { path: "docker-compose.yml", purpose: "compose file", content: "version: '3'\nservices:\n  web:\n    image: myapp:latest" },
        { path: ".env", purpose: "environment", content: "APP_SECRET=super-secret-value\nDATABASE_URL=postgres://user:pass@db/myapp" },
      ],
      port_plan: { internal_app_port: 3000, host_port: null, uses_reverse_proxy: false },
      routing_plan: { domain: "myapp.example.com", router_name: "myapp-router" },
      secrets_required: ["DATABASE_URL", "APP_SECRET"],
      rollback_plan: { type: "none" },
      blocking_questions: [],
      ...overrides,
    },
    summary_markdown: null,
    risk_level: "low",
    blocking_questions_json: [],
    created_at: "2024-01-01",
  };
}

const passedValidation: ValidationResult = {
  status: "passed",
  checks: [],
  blocking_errors: [],
  warnings: [],
};

const blockedValidation: ValidationResult = {
  status: "blocked",
  checks: [],
  blocking_errors: ["Port 80 is already in use by nginx", "Domain already routed to existing container"],
  warnings: [],
};

const warningValidation: ValidationResult = {
  status: "warning",
  checks: [],
  blocking_errors: [],
  warnings: ["No rollback plan defined", "Memory usage is high"],
};

// ---- Tests ----

describe("PlanDetail – operator transparency", () => {
  it("renders selected target name and host", () => {
    render(<PlanDetail plan={makePlan()} target={mockTarget} validation={null} />);
    expect(screen.getByText("prod-server")).toBeInTheDocument();
    expect(screen.getByText("10.0.0.1")).toBeInTheDocument();
  });

  it("renders environment badge for production", () => {
    render(<PlanDetail plan={makePlan()} target={mockTarget} validation={null} />);
    expect(screen.getByText("production")).toBeInTheDocument();
  });

  it("renders detected deployment manager", () => {
    render(<PlanDetail plan={makePlan()} target={mockTarget} validation={null} />);
    expect(screen.getAllByText("none").length).toBeGreaterThan(0);
  });

  it("renders detected reverse proxy", () => {
    render(<PlanDetail plan={makePlan()} target={mockTarget} validation={null} />);
    // "none" appears for both dm and rp
    const noneElements = screen.getAllByText("none");
    expect(noneElements.length).toBeGreaterThanOrEqual(2);
  });

  it("renders AI provider badge", () => {
    render(<PlanDetail plan={makePlan()} target={mockTarget} validation={null} />);
    expect(screen.getByText("Gordon (docker ai)")).toBeInTheDocument();
  });

  it("renders risk level badge", () => {
    render(<PlanDetail plan={makePlan()} target={mockTarget} validation={null} />);
    expect(screen.getByLabelText("risk level: low")).toBeInTheDocument();
  });

  it("renders strategy", () => {
    render(<PlanDetail plan={makePlan()} target={mockTarget} validation={null} />);
    expect(screen.getByText("docker_compose_private")).toBeInTheDocument();
  });

  it("shows 'not validated' when validation is null", () => {
    render(<PlanDetail plan={makePlan()} target={mockTarget} validation={null} />);
    expect(screen.getByText(/not validated yet/i)).toBeInTheDocument();
  });

  it("shows validation PASSED status", () => {
    render(<PlanDetail plan={makePlan()} target={mockTarget} validation={passedValidation} />);
    expect(screen.getByLabelText("validation status: passed")).toBeInTheDocument();
  });

  it("shows blocking errors", () => {
    render(<PlanDetail plan={makePlan()} target={mockTarget} validation={blockedValidation} />);
    expect(screen.getByText("Port 80 is already in use by nginx")).toBeInTheDocument();
    expect(screen.getByText("Domain already routed to existing container")).toBeInTheDocument();
  });

  it("marks blocking errors section with aria-label", () => {
    render(<PlanDetail plan={makePlan()} target={mockTarget} validation={blockedValidation} />);
    expect(screen.getByLabelText("blocking errors")).toBeInTheDocument();
  });

  it("shows warnings", () => {
    render(<PlanDetail plan={makePlan()} target={mockTarget} validation={warningValidation} />);
    expect(screen.getByText("No rollback plan defined")).toBeInTheDocument();
    expect(screen.getByText("Memory usage is high")).toBeInTheDocument();
  });

  it("marks warnings section with aria-label", () => {
    render(<PlanDetail plan={makePlan()} target={mockTarget} validation={warningValidation} />);
    expect(screen.getByLabelText("warnings")).toBeInTheDocument();
  });

  it("renders deployment actions list", () => {
    render(<PlanDetail plan={makePlan()} target={mockTarget} validation={null} />);
    const actionsList = screen.getByLabelText("deployment actions");
    expect(actionsList).toBeInTheDocument();
    expect(screen.getByText("CREATE_DIRECTORY")).toBeInTheDocument();
    expect(screen.getByText("DOCKER_COMPOSE_UP")).toBeInTheDocument();
  });

  it("renders generated file paths", () => {
    render(<PlanDetail plan={makePlan()} target={mockTarget} validation={null} />);
    expect(screen.getByLabelText("generated files")).toBeInTheDocument();
    expect(screen.getByText("docker-compose.yml")).toBeInTheDocument();
    expect(screen.getByText(".env")).toBeInTheDocument();
  });

  it("redacts secrets in file content preview", () => {
    render(<PlanDetail plan={makePlan()} target={mockTarget} validation={null} />);
    // Click preview for the .env file
    const expandButtons = screen.getAllByText("preview");
    // .env is the second file
    fireEvent.click(expandButtons[1]);
    const content = screen.getByLabelText("content of .env");
    expect(content.textContent).toContain("[REDACTED]");
    expect(content.textContent).not.toContain("super-secret-value");
  });

  it("renders port plan information", () => {
    render(<PlanDetail plan={makePlan()} target={mockTarget} validation={null} />);
    expect(screen.getByText("3000")).toBeInTheDocument();
  });

  it("renders routing plan domain", () => {
    render(<PlanDetail plan={makePlan()} target={mockTarget} validation={null} />);
    expect(screen.getByText("myapp.example.com")).toBeInTheDocument();
  });

  it("renders required secrets", () => {
    render(<PlanDetail plan={makePlan()} target={mockTarget} validation={null} />);
    expect(screen.getByText("DATABASE_URL")).toBeInTheDocument();
    expect(screen.getByText("APP_SECRET")).toBeInTheDocument();
  });

  it("renders rollback disabled status", () => {
    render(<PlanDetail plan={makePlan()} target={mockTarget} validation={null} />);
    expect(screen.getByLabelText("rollback status")).toBeInTheDocument();
    expect(screen.getByText(/rollback disabled/i)).toBeInTheDocument();
  });

  it("shows blocking questions when present", () => {
    const plan = makePlan({ blocking_questions: ["What registry should be used?"] });
    render(<PlanDetail plan={plan} target={mockTarget} validation={null} />);
    expect(screen.getByLabelText("blocking questions")).toBeInTheDocument();
    expect(screen.getByText("What registry should be used?")).toBeInTheDocument();
  });

  it("does not show blocking questions section when empty", () => {
    render(<PlanDetail plan={makePlan()} target={mockTarget} validation={null} />);
    expect(screen.queryByLabelText("blocking questions")).not.toBeInTheDocument();
  });
});
