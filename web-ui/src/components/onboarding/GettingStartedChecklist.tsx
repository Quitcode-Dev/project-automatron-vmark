"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { CheckCircle2, Circle, X, PlayCircle } from "lucide-react";
import { useProjectStore } from "@/stores/projectStore";
import { useOnboardingStore } from "@/stores/onboardingStore";

/**
 * Dashboard "Getting started" checklist. Item completion is derived from real
 * project state where possible; dismissal is persisted. Hides itself once every
 * item is done (or the user dismisses it).
 */
export function GettingStartedChecklist() {
  const projects = useProjectStore((s) => s.projects);
  const hasSeenTour = useOnboardingStore((s) => s.hasSeenTour);
  const dismissed = useOnboardingStore((s) => s.checklistDismissed);
  const dismissChecklist = useOnboardingStore((s) => s.dismissChecklist);
  const requestReplay = useOnboardingStore((s) => s.requestReplay);
  const [email, setEmail] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    fetch("/api/auth/session", { credentials: "include" })
      .then((r) => r.json())
      .then((s) => !cancelled && setEmail(s?.user?.email ?? "_local"))
      .catch(() => !cancelled && setEmail("_local"));
    return () => {
      cancelled = true;
    };
  }, []);

  if (dismissed) return null;

  const items = [
    {
      label: "Take the 60-second walkthrough",
      done: hasSeenTour(email),
      action: (
        <button
          onClick={() => requestReplay()}
          className="inline-flex items-center gap-1 text-xs font-medium text-primary hover:underline"
        >
          <PlayCircle className="h-3.5 w-3.5" /> Start
        </button>
      ),
    },
    { label: "Create your first project", done: projects.length > 0 },
    {
      label: "Approve a plan",
      done: projects.some((p) => p.plan_approved),
      href: "/learn/plan-and-approval",
    },
    {
      label: "Preview a build",
      done: projects.some((p) => Boolean(p.preview_url)),
      href: "/learn/preview",
    },
    {
      label: "Deploy a project",
      done: projects.some(
        (p) => Boolean(p.deploy_run_url) || p.deploy_status === "deployed" || p.project_stage === "deployed"
      ),
      href: "/learn/deploying",
    },
  ];

  const doneCount = items.filter((i) => i.done).length;
  if (doneCount === items.length) return null;

  return (
    <div className="mb-6 rounded-xl border border-border bg-card p-5" data-tour="checklist">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-sm font-semibold">Getting started</h2>
          <p className="mt-0.5 text-xs text-muted-foreground">
            {doneCount} of {items.length} done
          </p>
        </div>
        <button
          onClick={dismissChecklist}
          aria-label="Dismiss checklist"
          className="text-muted-foreground/60 transition-colors hover:text-foreground"
        >
          <X className="h-4 w-4" />
        </button>
      </div>
      <ul className="mt-3 space-y-1.5">
        {items.map((item) => (
          <li key={item.label} className="flex items-center gap-2.5 text-sm">
            {item.done ? (
              <CheckCircle2 className="h-4 w-4 shrink-0 text-green-400" />
            ) : (
              <Circle className="h-4 w-4 shrink-0 text-muted-foreground/40" />
            )}
            <span className={item.done ? "text-muted-foreground line-through" : "text-foreground/90"}>
              {item.label}
            </span>
            {!item.done && item.action}
            {!item.done && item.href && (
              <Link href={item.href} className="text-xs font-medium text-primary hover:underline">
                Learn
              </Link>
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}
