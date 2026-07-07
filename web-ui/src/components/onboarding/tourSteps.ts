import type { DriveStep } from "driver.js";

/**
 * Tour step-sets. driver.js drives one screen at a time, so we keep a set per
 * context and TourProvider picks by route. Steps whose `element` selector isn't
 * on the page are filtered out at runtime, so a set degrades gracefully.
 *
 * `onShow` is our extension (run by TourProvider on highlight) — used to switch
 * the project tabs so each step's description matches what's on screen. Steps
 * that drive tabs anchor to the always-present tab buttons, so they survive the
 * "is the element present?" filter even when their content isn't mounted yet.
 *
 * Anchors are `data-tour="…"` / `data-tab="…"` attributes on the real elements.
 */
export interface TourStep extends DriveStep {
  onShow?: () => void;
}

// Click a project tab button to reveal its panel (safe no-op if absent).
const showTab = (tab: string) => () => {
  (document.querySelector(`[data-tab="${tab}"]`) as HTMLElement | null)?.click();
};

export const dashboardTourSteps: TourStep[] = [
  {
    popover: {
      title: "Welcome to Automatron 👋",
      description:
        "Automatron turns a GitHub repo and a product idea into planned issues, delegates the coding to an AI agent, reviews the PRs, previews the app, and deploys it. This tour shows the whole flow — about a minute.",
    },
  },
  {
    element: '[data-tour="new-project"]',
    popover: {
      title: "1 · Start a project",
      description:
        "Connect a GitHub repository here. The dialog also takes optional Figma designs and Supabase keys, and lets you choose the Architect/Reviewer models. On submit, the Architect reads the repo and drafts a plan.",
      side: "bottom",
      align: "end",
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
        "Open a project from here to work it through Plan → Build → Preview → Deploy. New to the concepts? The Learn section has short lessons on every step.",
      side: "right",
    },
  },
  {
    element: '[data-tour="user-menu"]',
    popover: {
      title: "Replay anytime",
      description: "You can replay this walkthrough from this menu whenever you like.",
      side: "bottom",
      align: "end",
    },
  },
];

export const projectTourSteps: TourStep[] = [
  {
    element: '[data-tour="stage-tracker"]',
    popover: {
      title: "The pipeline — you are here",
      description:
        "Every project moves left-to-right: Intake → Plan → Build → Preview → Deploy. This tracker shows the current stage and advances as the Architect and builder work.",
      side: "bottom",
    },
  },
  {
    element: '[data-tour="action-bar"]',
    popover: {
      title: "Your next action",
      description:
        "Context-aware buttons live here — Start Build, Stop, Approve Plan — plus quick links to the repo and the live preview. Which buttons show depends on the current stage.",
      side: "bottom",
      align: "end",
    },
  },
  {
    element: '[data-tour="llm-config"]',
    popover: {
      title: "Models per role",
      description:
        "Pick the LLM for each role — Architect (planning), Builder (writes the code), Reviewer (reviews PRs). Bigger models plan better; smaller ones are cheaper. Save to apply.",
      side: "top",
    },
  },
  {
    element: '[data-tab="chat"]',
    onShow: showTab("chat"),
    popover: {
      title: "Architect chat",
      description:
        "Talk to the Architect. Before planning it shapes the plan; after planning, describe a change or report a bug in plain language and it drafts new GitHub issues for you.",
      side: "top",
    },
  },
  {
    element: '[data-tab="plan"]',
    onShow: showTab("plan"),
    popover: {
      title: "The plan (PLAN.md)",
      description:
        "The Architect's plan as Epics → Stories → Tasks. Review and edit it here, then use Approve Plan (top bar) — the first approval gate — to turn each task into a GitHub issue.",
      side: "top",
    },
  },
  {
    element: '[data-tab="issues"]',
    onShow: showTab("issues"),
    popover: {
      title: "The build loop",
      description:
        "Work each issue: Assign Copilot or Implement (built-in builder) → Request AI Review → Preview the branch → Approve & Merge. The header also has Audit Code, Build Check, Sync, and New Issue.",
      side: "top",
    },
  },
  {
    element: '[data-tab="preview"]',
    onShow: showTab("preview"),
    popover: {
      title: "Live preview",
      description:
        "Automatron builds and runs the app so you can click through a change before shipping — either inline here or opened in a new tab over HTTPS.",
      side: "top",
    },
  },
  {
    element: '[data-tab="deploy"]',
    onShow: showTab("deploy"),
    popover: {
      title: "Deploy — the final stage",
      description:
        "Register a target server, run inventory, let the Docker-AI agent draft a plan, validate it, approve, and execute — with a step-by-step run view and rollback.",
      side: "top",
    },
  },
  {
    element: '[data-tab="activity"]',
    onShow: showTab("activity"),
    popover: {
      title: "Activity log",
      description:
        "A live, step-by-step log of everything the orchestrator does. When something seems stuck, this is the first place to look. That's the full flow — happy building!",
      side: "top",
    },
  },
];
