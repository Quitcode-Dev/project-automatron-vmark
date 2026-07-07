"use client";

import { StatusBadge } from "@/components/ui";
import { UserMenu } from "@/components/layout/UserMenu";
import { cn } from "@/lib/utils";
import { useProjectStore } from "@/stores/projectStore";
import { useOnboardingStore } from "@/stores/onboardingStore";
import { AlertTriangle, Wifi, WifiOff, HelpCircle } from "lucide-react";

export function Header() {
  const { isConnected, currentProject, humanRequired, humanReason, error } =
    useProjectStore();
  const requestReplay = useOnboardingStore((s) => s.requestReplay);

  return (
    <header className="flex h-16 items-center justify-between border-b border-border bg-card px-6">
      <div className="flex items-center gap-3">
        {currentProject ? (
          <>
            <h1 className="text-lg font-semibold">{currentProject.name}</h1>
            <StatusBadge status={currentProject.status} />
          </>
        ) : (
          <h1 className="text-lg font-semibold">Dashboard</h1>
        )}
      </div>

      <div className="flex items-center gap-4">
        {humanRequired && (
          <div className="flex items-center gap-2 rounded-lg bg-amber-500/10 px-3 py-1.5 text-sm text-amber-500">
            <AlertTriangle className="h-4 w-4" />
            <span className="max-w-sm truncate">
              {humanReason || "Action required"}
            </span>
          </div>
        )}

        {error && (
          <div className="max-w-xs truncate rounded-lg bg-destructive/10 px-3 py-1.5 text-sm text-destructive">
            {error}
          </div>
        )}

        <div
          className={cn(
            "flex items-center gap-1.5 text-xs",
            isConnected ? "text-green-500" : "text-muted-foreground"
          )}
        >
          {isConnected ? (
            <Wifi className="h-3.5 w-3.5" />
          ) : (
            <WifiOff className="h-3.5 w-3.5" />
          )}
          <span>{isConnected ? "Connected" : "Disconnected"}</span>
        </div>

        <button
          data-tour="help"
          onClick={() => requestReplay()}
          title="Take the walkthrough"
          className="flex items-center gap-1.5 rounded-lg px-2 py-1.5 text-xs text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
        >
          <HelpCircle className="h-4 w-4" />
          <span className="hidden sm:inline">Tour</span>
        </button>

        <UserMenu />
      </div>
    </header>
  );
}
