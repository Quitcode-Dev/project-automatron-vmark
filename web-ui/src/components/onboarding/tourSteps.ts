import type { DriveStep } from "driver.js";

/**
 * Tour step-sets. driver.js drives one screen at a time, so we keep a set per
 * context and TourProvider picks by route. Steps whose `element` selector isn't
 * on the page are filtered out at runtime, so a set degrades gracefully.
 *
 * Anchors are `data-tour="…"` attributes added to the real UI elements.
 */

export const dashboardTourSteps: DriveStep[] = [
  {
    popover: {
      title: "Welcome to Automatron 👋",
      description:
        "Automatron turns a GitHub repo and a product idea into planned issues, delegates the coding to an AI agent, reviews the PRs, and ships it. Here's the 60-second tour.",
    },
  },
  {
    element: '[data-tour="new-project"]',
    popover: {
      title: "1 · Start a project",
      description:
        "Connect a GitHub repository here. The Architect reads it and drafts a plan of Epics → Stories → Tasks for you to approve.",
      side: "bottom",
      align: "end",
    },
  },
  {
    element: '[data-tour="sidebar-nav"]',
    popover: {
      title: "Your projects & the guide",
      description:
        "Projects live here. Open one to watch it move through Plan → Build → Preview → Deploy. New to the concepts? The Learn section has short lessons.",
      side: "right",
    },
  },
  {
    element: '[data-tour="user-menu"]',
    popover: {
      title: "Replay anytime",
      description:
        "You can replay this walkthrough from this menu whenever you like.",
      side: "bottom",
      align: "end",
    },
  },
];

export const projectTourSteps: DriveStep[] = [
  {
    element: '[data-tour="stage-tracker"]',
    popover: {
      title: "You are here",
      description:
        "This tracks the project through Intake → Plan → Build → Preview → Deploy. It advances as the Architect and builder work.",
      side: "bottom",
    },
  },
  {
    element: '[data-tour="action-bar"]',
    popover: {
      title: "Your next action",
      description:
        "Context-aware buttons appear here — Start Build, Approve Plan, and so on — depending on the current stage.",
      side: "bottom",
      align: "end",
    },
  },
  {
    element: '[data-tour="tabs"]',
    popover: {
      title: "Work areas",
      description:
        "Chat with the Architect, edit the plan, work the issues (assign Copilot or the built-in builder), preview the app, watch activity, and deploy.",
      side: "top",
    },
  },
];
