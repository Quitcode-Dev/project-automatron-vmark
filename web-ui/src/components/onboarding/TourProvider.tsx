"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { usePathname } from "next/navigation";
import { driver, type Driver } from "driver.js";
import "driver.js/dist/driver.css";
import { useOnboardingStore } from "@/stores/onboardingStore";
import { dashboardTourSteps, projectTourSteps } from "./tourSteps";

/**
 * Mounts the interactive walkthrough. Auto-launches the dashboard tour the first
 * time a user lands on "/", and re-runs the tour for the current route when
 * "Replay walkthrough" bumps `replayNonce`. Completion is remembered per email.
 */
export function TourProvider() {
  const pathname = usePathname();
  const [email, setEmail] = useState<string | null>(null);
  const replayNonce = useOnboardingStore((s) => s.replayNonce);
  const hasSeenTour = useOnboardingStore((s) => s.hasSeenTour);
  const markTourSeen = useOnboardingStore((s) => s.markTourSeen);

  const driverRef = useRef<Driver | null>(null);
  const lastReplay = useRef(replayNonce);
  const autoLaunched = useRef(false);

  // Resolve the session email (keys the "seen" flag). Falls back to "_local".
  useEffect(() => {
    let cancelled = false;
    fetch("/api/auth/session", { credentials: "include" })
      .then((r) => r.json())
      .then((s) => {
        if (!cancelled) setEmail(s?.user?.email ?? "_local");
      })
      .catch(() => {
        if (!cancelled) setEmail("_local");
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const startTour = useCallback(() => {
    const isProject = pathname?.startsWith("/project/") ?? false;
    const steps = isProject ? projectTourSteps : dashboardTourSteps;
    // Drop steps whose anchor isn't rendered so we never point at nothing.
    const present = steps.filter(
      (s) => !s.element || document.querySelector(s.element as string)
    );
    if (present.length === 0) return;

    driverRef.current?.destroy();
    const d = driver({
      showProgress: true,
      allowClose: true,
      nextBtnText: "Next",
      prevBtnText: "Back",
      doneBtnText: "Done",
      // Strip our custom `onShow` before handing steps to driver.js.
      steps: present.map(({ onShow: _onShow, ...rest }) => rest),
      // Run a step's side-effect (e.g. switch tab) as it's highlighted so the
      // popover's description matches what's on screen.
      onHighlightStarted: (_el, _step, opts) => {
        const idx = opts?.state?.activeIndex ?? 0;
        present[idx]?.onShow?.();
      },
      onDestroyed: () => {
        markTourSeen(email);
      },
    });
    driverRef.current = d;
    d.drive();
  }, [pathname, email, markTourSeen]);

  // First-visit auto-launch on the dashboard.
  useEffect(() => {
    if (!email || autoLaunched.current) return;
    if (pathname !== "/") return;
    autoLaunched.current = true;
    if (hasSeenTour(email)) return;
    // Small delay so the dashboard (and its anchors) have rendered.
    const t = setTimeout(startTour, 700);
    return () => clearTimeout(t);
  }, [email, pathname, hasSeenTour, startTour]);

  // Replay trigger (from the user menu).
  useEffect(() => {
    if (replayNonce === lastReplay.current) return;
    lastReplay.current = replayNonce;
    startTour();
  }, [replayNonce, startTour]);

  // Clean up on unmount.
  useEffect(() => () => driverRef.current?.destroy(), []);

  return null;
}
