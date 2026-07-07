import type { DriveStep } from "driver.js";

/**
 * Tour step-sets. driver.js drives one screen at a time, so we keep a set per
 * context and TourProvider picks by route.
 *
 * Extensions handled by TourProvider:
 *  - `onShow`  — run when the step is highlighted (switch tab, ensure the modal
 *                is open) so the popover matches what's on screen.
 *  - `onNext`  — take over the Next button: run a side-effect (open the modal)
 *                then call the provided `next()` once the DOM is ready.
 *  - `keep`    — don't filter this step out if its anchor is absent at start
 *                (it appears later, e.g. a modal field revealed by `onNext`).
 *
 * Anchors are `data-tour="…"` / `data-tab="…"` attributes on the real elements.
 */
export interface TourStep extends DriveStep {
  onShow?: () => void;
  onNext?: (next: () => void) => void;
  keep?: boolean;
}

const q = (sel: string) => document.querySelector(sel) as HTMLElement | null;
const showTab = (tab: string) => () => q(`[data-tab="${tab}"]`)?.click();
// Open the New Project modal if it isn't already open (idempotent).
const openModal = () => {
  if (!q('[data-tour="np-repo"]')) q('[data-tour="new-project"]')?.click();
};

export const dashboardTourSteps: TourStep[] = [
  {
    popover: {
      title: "Welcome to Automatron 👋",
      description:
        "Automatron turns a GitHub repo and a product idea into planned issues, delegates the coding to an AI agent, reviews the PRs, previews the app, and deploys it. This tour walks the whole thing.",
    },
  },
  {
    element: '[data-tour="checklist"]',
    popover: {
      title: "Your progress",
      description:
        "This checklist tracks your first run end-to-end — take the tour, create a project, approve a plan, preview, and deploy. Items tick themselves as you go.",
      side: "bottom",
    },
  },
  {
    element: '[data-tour="sidebar-nav"]',
    popover: {
      title: "Projects & the guide",
      description:
        "Your projects live here. The Learn section has short lessons on every concept if you want to go deeper.",
      side: "right",
    },
  },
  {
    element: '[data-tour="new-project"]',
    popover: {
      title: "Create a project",
      description:
        "This opens the Connect Repository dialog. Let's walk through what goes in it — click Next.",
      side: "bottom",
      align: "end",
    },
    onNext: (next) => {
      openModal();
      setTimeout(next, 400);
    },
  },
  {
    element: '[data-tour="np-repo"]',
    keep: true,
    onShow: openModal,
    popover: {
      title: "1 · GitHub repository URL",
      description:
        "The repo Automatron will plan and open issues in. It needs a GitHub token with write access (repo, workflow, admin:repo_hook). Ideally the repo has a README/PRD describing what to build.",
    },
  },
  {
    element: '[data-tour="np-name"]',
    keep: true,
    onShow: openModal,
    popover: {
      title: "2 · Project name",
      description: "A friendly name — auto-filled from the repo, editable.",
    },
  },
  {
    element: '[data-tour="np-figma"]',
    keep: true,
    onShow: openModal,
    popover: {
      title: "3 · Figma designs (optional)",
      description:
        "Paste Figma URLs or upload a .fig file. The Architect uses them as design context so the plan and generated UI match your designs.",
    },
  },
  {
    element: '[data-tour="np-supabase"]',
    keep: true,
    onShow: openModal,
    popover: {
      title: "4 · Supabase (optional)",
      description:
        "Project URL + Service Role key (schema introspection) + Anon key (written into the app's .env). With these, generated code runs against your real database schema instead of guesses.",
    },
  },
  {
    element: '[data-tour="np-models"]',
    keep: true,
    onShow: openModal,
    popover: {
      title: "5 · Models",
      description:
        "Pick the LLM for the Architect (plans the work) and the Reviewer (reviews PRs). Bigger models plan better; smaller ones are cheaper.",
    },
  },
  {
    element: '[data-tour="np-submit"]',
    keep: true,
    onShow: openModal,
    popover: {
      title: "6 · Connect & Plan",
      description:
        "This creates the project, scaffolds the repo if needed, and the Architect drafts the plan. You'll land on the project page — the rest of the tour continues there (open a project and hit Tour).",
    },
  },
];

export const projectTourSteps: TourStep[] = [
  {
    element: '[data-tour="stage-tracker"]',
    popover: {
      title: "The pipeline — you are here",
      description:
        "After you create a project you land here. It moves left-to-right: Intake → Plan → Build → Preview → Deploy, advancing as the Architect and builder work.",
      side: "bottom",
    },
  },
  {
    element: '[data-tour="action-bar"]',
    popover: {
      title: "Start & control the run",
      description:
        "Click Start Build to kick off planning. Stop/Resume, Approve Plan, and quick links to the repo and live preview all appear here depending on the stage.",
      side: "bottom",
      align: "end",
    },
  },
  {
    element: '[data-tour="llm-config"]',
    popover: {
      title: "Models per role",
      description:
        "Change the Architect / Builder / Reviewer models any time and Save. Builder is the Anthropic Agent SDK.",
      side: "top",
    },
  },
  {
    element: '[data-tour="chat-input"]',
    keep: true,
    onShow: showTab("chat"),
    popover: {
      title: "Write issues from chat",
      description:
        "Talk to the Architect here. After planning, describe a change or report a bug in plain language and it drafts the matching GitHub issues for you — no manual issue writing.",
      side: "top",
    },
  },
  {
    element: '[data-tab="plan"]',
    onShow: showTab("plan"),
    popover: {
      title: "Review & approve the plan",
      description:
        "PLAN.md is the Architect's Epics → Stories → Tasks. Review or edit it here, then click Approve Plan (top bar) — the first gate — to turn each task into a GitHub issue.",
      side: "top",
    },
  },
  {
    element: '[data-tab="issues"]',
    onShow: showTab("issues"),
    popover: {
      title: "Assign an agent & open a PR",
      description:
        "Per issue: Assign Copilot (GitHub's coding agent) or Implement (built-in builder) — either one opens a pull request. Then Request AI Review, Preview the branch, and Approve & Merge.",
      side: "top",
    },
  },
  {
    element: '[data-tour="build-check"]',
    keep: true,
    onShow: showTab("issues"),
    popover: {
      title: "Check the build",
      description:
        "Build Check runs `npm run build` on the default branch in a container and flags failures — with a one-click 'Create GitHub Issue' for the error. Audit Code and Sync sit alongside it.",
      side: "bottom",
    },
  },
  {
    element: '[data-tour="new-issue"]',
    keep: true,
    onShow: showTab("issues"),
    popover: {
      title: "Add an issue by hand",
      description:
        "New Issue: type a short description and the Architect expands it into a full, stack-aware spec, then files it on GitHub.",
      side: "bottom",
    },
  },
  {
    element: '[data-tab="preview"]',
    onShow: showTab("preview"),
    popover: {
      title: "Preview the app",
      description:
        "Automatron builds and runs the app so you can click through it — inline here, or opened in a new tab over HTTPS. You can also preview a single PR's branch from its issue before merging.",
      side: "top",
    },
  },
  {
    element: '[data-tab="deploy"]',
    onShow: showTab("deploy"),
    popover: {
      title: "Deploy — the final stage",
      description:
        "Register a target server, Run Inventory (SSH introspection), let the Docker-AI agent draft a plan, Validate, Approve, and Execute — with a step-by-step run view and rollback.",
      side: "top",
    },
  },
  {
    element: '[data-tab="activity"]',
    onShow: showTab("activity"),
    popover: {
      title: "Activity log",
      description:
        "A live, step-by-step log of everything the orchestrator does. When something seems stuck, look here first. That's the full flow — happy building!",
      side: "top",
    },
  },
];
