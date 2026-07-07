"use client";

import { useState } from "react";
import Link from "next/link";
import { Lightbulb, ArrowRight, X } from "lucide-react";
import type { ProjectStage } from "@/lib/types";

/**
 * A dismissible "what's happening / do this next" banner keyed off the project
 * stage — turns the stage tracker into a teacher. Render it with `key={stage}`
 * so dismissing one stage's tip doesn't hide the next stage's.
 */
const COACH: Partial<Record<ProjectStage, { text: string; lessonSlug: string }>> = {
  intake: {
    text: "You're at the start. Click Start Build and the Architect will read your repo and draft a plan.",
    lessonSlug: "what-is-automatron",
  },
  planning: {
    text: "The Architect is writing PLAN.md — Epics → Stories → Tasks. This can take a minute.",
    lessonSlug: "plan-and-approval",
  },
  awaiting_plan_approval: {
    text: "Review the plan in the Plan tab, then Approve Plan to create the GitHub issues.",
    lessonSlug: "plan-and-approval",
  },
  building: {
    text: "Issues are live. On each one, Assign Copilot or Implement, then request an AI review and preview the branch before merging.",
    lessonSlug: "the-build-loop",
  },
  awaiting_preview_approval: {
    text: "Check the live preview, then approve to move toward deployment.",
    lessonSlug: "preview",
  },
  ready_for_deploy: {
    text: "Ready to ship. Open the Deploy tab: add a target, run inventory, create a plan, approve, and execute.",
    lessonSlug: "deploying",
  },
  deploying: {
    text: "Deploying — watch the run steps in the Deploy tab.",
    lessonSlug: "deploying",
  },
  error: {
    text: "Something failed. Open the Activity tab for the exact error, fix it, and retry.",
    lessonSlug: "iterating",
  },
};

export function StageCoach({ stage }: { stage: ProjectStage | undefined }) {
  const [dismissed, setDismissed] = useState(false);
  if (!stage || dismissed) return null;
  const coach = COACH[stage];
  if (!coach) return null;

  return (
    <div className="mb-4 flex items-start gap-3 rounded-lg border border-primary/20 bg-primary/5 px-4 py-3 text-sm">
      <Lightbulb className="mt-0.5 h-4 w-4 shrink-0 text-primary" />
      <div className="min-w-0 flex-1">
        <p className="text-foreground/90">{coach.text}</p>
        <Link
          href={`/learn/${coach.lessonSlug}`}
          className="mt-1 inline-flex items-center gap-1 text-xs font-medium text-primary hover:underline"
        >
          Learn more <ArrowRight className="h-3 w-3" />
        </Link>
      </div>
      <button
        onClick={() => setDismissed(true)}
        aria-label="Dismiss"
        className="text-muted-foreground/60 transition-colors hover:text-foreground"
      >
        <X className="h-4 w-4" />
      </button>
    </div>
  );
}
