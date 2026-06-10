import React from "react";
import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { RunView } from "../RunView";
import type { DeploymentRun } from "@/lib/deploymentApi";
import type { RunStep } from "../RunView";

// ---- Fixtures ----

function makeRun(overrides: Partial<DeploymentRun> = {}): DeploymentRun {
  return {
    id: "run-1",
    plan_id: "plan-1",
    project_id: "proj-1",
    target_id: "tgt-1",
    status: "completed",
    started_by: "user@test.com",
    started_at: "2024-01-01T10:00:00Z",
    finished_at: "2024-01-01T10:02:30Z",
    health_status: "healthy",
    rollback_available: 0,
    ...overrides,
  };
}

const sampleSteps: RunStep[] = [
  {
    id: "step-1", run_id: "run-1", step_index: 0,
    action_type: "CREATE_DIRECTORY", status: "completed",
    stdout_excerpt: "mkdir -p /opt/myapp: success", stderr_excerpt: null, error_message: null,
  },
  {
    id: "step-2", run_id: "run-1", step_index: 1,
    action_type: "DOCKER_COMPOSE_UP", status: "completed",
    stdout_excerpt: "Container myapp_web_1 started", stderr_excerpt: null, error_message: null,
  },
];

const stepsWithSecrets: RunStep[] = [
  {
    id: "step-s", run_id: "run-1", step_index: 0,
    action_type: "WRITE_ENV_FILE", status: "completed",
    stdout_excerpt: "DATABASE_PASSWORD=my-secret-pass\nAPP_KEY=abc123",
    stderr_excerpt: null, error_message: null,
  },
];

// ---- Tests ----

describe("RunView – run status display", () => {
  it("renders run status", () => {
    render(<RunView run={makeRun()} steps={[]} />);
    expect(screen.getByLabelText("run status: completed")).toBeInTheDocument();
  });

  it("renders health status", () => {
    render(<RunView run={makeRun()} steps={[]} />);
    expect(screen.getByLabelText("health status: healthy")).toBeInTheDocument();
  });

  it("renders started_at timestamp", () => {
    render(<RunView run={makeRun()} steps={[]} />);
    expect(screen.getByText("2024-01-01T10:00:00Z")).toBeInTheDocument();
  });

  it("renders finished_at timestamp when present", () => {
    render(<RunView run={makeRun()} steps={[]} />);
    expect(screen.getByText("2024-01-01T10:02:30Z")).toBeInTheDocument();
  });
});

describe("RunView – step display", () => {
  it("renders step list with action types", () => {
    render(<RunView run={makeRun()} steps={sampleSteps} />);
    expect(screen.getByLabelText("deployment steps")).toBeInTheDocument();
    expect(screen.getByText("CREATE_DIRECTORY")).toBeInTheDocument();
    expect(screen.getByText("DOCKER_COMPOSE_UP")).toBeInTheDocument();
  });

  it("renders step statuses", () => {
    render(<RunView run={makeRun()} steps={sampleSteps} />);
    const completedElements = screen.getAllByText("completed");
    expect(completedElements.length).toBeGreaterThanOrEqual(2);
  });

  it("shows step stdout when logs button clicked", () => {
    render(<RunView run={makeRun()} steps={sampleSteps} />);
    const logsButtons = screen.getAllByText("logs");
    fireEvent.click(logsButtons[0]);
    expect(screen.getByLabelText("stdout")).toBeInTheDocument();
    expect(screen.getByText(/mkdir.*success/i)).toBeInTheDocument();
  });

  it("redacts secrets in step stdout", () => {
    render(<RunView run={makeRun()} steps={stepsWithSecrets} />);
    fireEvent.click(screen.getByText("logs"));
    const stdout = screen.getByLabelText("stdout");
    expect(stdout.textContent).toContain("[REDACTED]");
    expect(stdout.textContent).not.toContain("my-secret-pass");
  });

  it("renders no step list when steps is empty", () => {
    render(<RunView run={makeRun()} steps={[]} />);
    expect(screen.queryByLabelText("deployment steps")).not.toBeInTheDocument();
  });
});

describe("RunView – rollback limitation", () => {
  it("shows rollback unavailable message when rollback_available is 0", () => {
    render(<RunView run={makeRun({ rollback_available: 0 })} steps={[]} />);
    const note = screen.getByLabelText("rollback unavailable");
    expect(note).toBeInTheDocument();
    expect(note.textContent).toMatch(/rollback not available/i);
    expect(note.textContent).toMatch(/MVP/i);
  });

  it("shows rollback button when rollback_available is 1", () => {
    render(<RunView run={makeRun({ rollback_available: 1 })} steps={[]} />);
    expect(screen.getByRole("button", { name: /rollback/i })).toBeInTheDocument();
  });

  it("calls onRollback when rollback button clicked", () => {
    const onRollback = vi.fn();
    render(<RunView run={makeRun({ rollback_available: 1 })} steps={[]} onRollback={onRollback} />);
    fireEvent.click(screen.getByRole("button", { name: /rollback/i }));
    expect(onRollback).toHaveBeenCalledOnce();
  });

  it("rollback button is disabled when busyRollback is true", () => {
    render(
      <RunView run={makeRun({ rollback_available: 1 })} steps={[]} busyRollback={true} />
    );
    expect(screen.getByRole("button", { name: /rollback/i })).toBeDisabled();
  });

  it("rollback unavailable message visible for failed run with rollback_available=0", () => {
    render(<RunView run={makeRun({ status: "failed", rollback_available: 0 })} steps={[]} />);
    expect(screen.getByLabelText("rollback unavailable")).toBeInTheDocument();
  });

  it("rollback unavailable message mentions manual recovery option", () => {
    render(<RunView run={makeRun({ rollback_available: 0 })} steps={[]} />);
    const note = screen.getByLabelText("rollback unavailable");
    expect(note.textContent).toMatch(/manually|manual/i);
  });
});
