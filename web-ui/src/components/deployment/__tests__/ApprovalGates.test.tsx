import React from "react";
import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ApprovalGates } from "../ApprovalGates";
import type { DeploymentPlan, DeploymentTarget, ValidationResult } from "@/lib/deploymentApi";

// ---- Fixtures ----

const mockTarget: DeploymentTarget = {
  id: "tgt-1", project_id: "proj-1", name: "prod-server", host: "10.0.0.1",
  ssh_user: "deploy", ssh_port: 22, environment: "production", domain: null,
  app_name: "myapp", deploy_path: "/opt/myapp", preferred_strategy: "docker_compose_private",
  auth_mode: "ssh_key", auth_reference: null, created_at: "2024-01-01",
  updated_at: "2024-01-01", last_inventory_snapshot_id: null,
};

const stagingTarget: DeploymentTarget = { ...mockTarget, id: "tgt-2", environment: "staging" };

function makePlan(risk: string = "low", blockingQuestions: string[] = []): DeploymentPlan {
  return {
    id: "plan-1", project_id: "proj-1", target_id: "tgt-1", status: "approved",
    plan_json: {
      strategy: "docker_compose_private", risk_level: risk,
      blocking_questions: blockingQuestions,
    },
    summary_markdown: null, risk_level: risk,
    blocking_questions_json: blockingQuestions, created_at: "2024-01-01",
  };
}

const passedValidation: ValidationResult = {
  status: "passed", checks: [], blocking_errors: [], warnings: [],
};

const blockedValidation: ValidationResult = {
  status: "blocked", checks: [], blocking_errors: ["Port 80 in use"], warnings: [],
};

const warningValidation: ValidationResult = {
  status: "warning", checks: [], blocking_errors: [], warnings: ["Memory high"],
};

function defaultProps(overrides = {}) {
  return {
    plan: makePlan(),
    target: mockTarget,
    validation: passedValidation,
    approved: false,
    hasActiveRun: false,
    rollbackAck: true, // default: already acknowledged for production
    onRollbackAckChange: vi.fn(),
    onApprove: vi.fn().mockResolvedValue(undefined),
    onExecute: vi.fn().mockResolvedValue({ run_id: "run-xyz", status: "started" }),
    onRunStarted: vi.fn(),
    ...overrides,
  };
}

// ---- Tests ----

describe("ApprovalGates – approval button rules", () => {
  it("approve button is disabled when validation is null", () => {
    const props = defaultProps({ validation: null });
    render(<ApprovalGates {...props} />);
    expect(screen.getByRole("button", { name: /approve/i })).toBeDisabled();
  });

  it("approve button is disabled when validation status is blocked", () => {
    const props = defaultProps({ validation: blockedValidation });
    render(<ApprovalGates {...props} />);
    expect(screen.getByRole("button", { name: /approve/i })).toBeDisabled();
  });

  it("approve button is disabled when plan risk_level is blocked", () => {
    const props = defaultProps({ plan: makePlan("blocked") });
    render(<ApprovalGates {...props} />);
    expect(screen.getByRole("button", { name: /approve/i })).toBeDisabled();
  });

  it("approve button is disabled when plan has blocking_questions", () => {
    const props = defaultProps({
      plan: makePlan("low", ["What registry to use?"]),
    });
    render(<ApprovalGates {...props} />);
    expect(screen.getByRole("button", { name: /approve/i })).toBeDisabled();
  });

  it("approve button is enabled when validation passed and no blocking questions", () => {
    const props = defaultProps({ validation: passedValidation, target: stagingTarget });
    render(<ApprovalGates {...props} />);
    expect(screen.getByRole("button", { name: /approve/i })).not.toBeDisabled();
  });

  it("approve enabled when validation is warning with no blocking errors", () => {
    const props = defaultProps({ validation: warningValidation, target: stagingTarget });
    render(<ApprovalGates {...props} />);
    expect(screen.getByRole("button", { name: /approve/i })).not.toBeDisabled();
  });

  it("approve disabled for production when rollback not acknowledged", () => {
    const props = defaultProps({ rollbackAck: false }); // production, no ack
    render(<ApprovalGates {...props} />);
    expect(screen.getByRole("button", { name: /approve/i })).toBeDisabled();
  });

  it("approve enabled for production once rollback acknowledged", () => {
    const props = defaultProps({ rollbackAck: true });
    render(<ApprovalGates {...props} />);
    expect(screen.getByRole("button", { name: /approve/i })).not.toBeDisabled();
  });

  it("shows rollback acknowledgment checkbox for production environment", () => {
    render(<ApprovalGates {...defaultProps()} />);
    expect(screen.getByLabelText(/acknowledge rollback disabled/i)).toBeInTheDocument();
  });

  it("does not show rollback acknowledgment for staging environment", () => {
    const props = defaultProps({ target: stagingTarget });
    render(<ApprovalGates {...props} />);
    expect(screen.queryByLabelText(/acknowledge rollback disabled/i)).not.toBeInTheDocument();
  });

  it("calls onApprove when approve button is clicked", async () => {
    const onApprove = vi.fn().mockResolvedValue(undefined);
    const props = defaultProps({ onApprove, target: stagingTarget });
    render(<ApprovalGates {...props} />);
    await userEvent.click(screen.getByRole("button", { name: /approve/i }));
    expect(onApprove).toHaveBeenCalledOnce();
  });
});

describe("ApprovalGates – execute button rules", () => {
  it("execute button is disabled when not approved", () => {
    const props = defaultProps({ approved: false });
    render(<ApprovalGates {...props} />);
    expect(screen.getByRole("button", { name: /execute/i })).toBeDisabled();
  });

  it("execute button is disabled when validation is null even if approved", () => {
    const props = defaultProps({ approved: true, validation: null });
    render(<ApprovalGates {...props} />);
    expect(screen.getByRole("button", { name: /execute/i })).toBeDisabled();
  });

  it("execute button is disabled when validation blocked", () => {
    const props = defaultProps({ approved: true, validation: blockedValidation });
    render(<ApprovalGates {...props} />);
    expect(screen.getByRole("button", { name: /execute/i })).toBeDisabled();
  });

  it("execute button is disabled when active run exists", () => {
    const props = defaultProps({ approved: true, hasActiveRun: true });
    render(<ApprovalGates {...props} />);
    expect(screen.getByRole("button", { name: /execute/i })).toBeDisabled();
  });

  it("execute button is enabled when approved + validation passed + no active run", () => {
    const props = defaultProps({ approved: true, hasActiveRun: false });
    render(<ApprovalGates {...props} />);
    expect(screen.getByRole("button", { name: /execute/i })).not.toBeDisabled();
  });

  it("execute calls onRunStarted with run_id returned by onExecute", async () => {
    const onRunStarted = vi.fn();
    const onExecute = vi.fn().mockResolvedValue({ run_id: "run-abc-123", status: "started" });
    const props = defaultProps({ approved: true, onExecute, onRunStarted });
    render(<ApprovalGates {...props} />);
    await userEvent.click(screen.getByRole("button", { name: /execute/i }));
    await waitFor(() => expect(onRunStarted).toHaveBeenCalledWith("run-abc-123"));
  });

  it("execute does not call onRunStarted if run_id is absent", async () => {
    const onRunStarted = vi.fn();
    const onExecute = vi.fn().mockResolvedValue({ run_id: "", status: "rejected" });
    const props = defaultProps({ approved: true, onExecute, onRunStarted });
    render(<ApprovalGates {...props} />);
    await userEvent.click(screen.getByRole("button", { name: /execute/i }));
    await waitFor(() => expect(onExecute).toHaveBeenCalled());
    expect(onRunStarted).not.toHaveBeenCalled();
  });

  it("shows active run warning when hasActiveRun is true", () => {
    const props = defaultProps({ approved: true, hasActiveRun: true });
    render(<ApprovalGates {...props} />);
    expect(screen.getByLabelText("active run warning")).toBeInTheDocument();
  });

  it("shows execute blocked reasons when approved but validation fails", () => {
    const props = defaultProps({ approved: true, validation: blockedValidation });
    render(<ApprovalGates {...props} />);
    const blockedReasons = screen.getByLabelText("execute blocked reasons");
    expect(blockedReasons).toBeInTheDocument();
    expect(blockedReasons.textContent).toMatch(/validation must pass/i);
  });
});
