"use client";

import { useState } from "react";
import { HelpCircle } from "lucide-react";
import { cn } from "@/lib/utils";

/**
 * Lightweight inline help popover — a small "?" that reveals a note on
 * hover/focus/click. No positioning library; a fixed-width panel anchored to the
 * icon. Use for the knowledge-heavy controls (PAT scopes, model choice, etc.).
 */
export function InfoTip({
  children,
  className,
  side = "top",
}: {
  children: React.ReactNode;
  className?: string;
  side?: "top" | "bottom";
}) {
  const [open, setOpen] = useState(false);
  return (
    <span className={cn("relative inline-flex align-middle", className)}>
      <button
        type="button"
        aria-label="More information"
        onClick={() => setOpen((v) => !v)}
        onMouseEnter={() => setOpen(true)}
        onMouseLeave={() => setOpen(false)}
        onFocus={() => setOpen(true)}
        onBlur={() => setOpen(false)}
        className="text-muted-foreground/60 transition-colors hover:text-foreground"
      >
        <HelpCircle className="h-3.5 w-3.5" />
      </button>
      {open && (
        <span
          role="tooltip"
          className={cn(
            "absolute left-1/2 z-50 w-64 -translate-x-1/2 rounded-lg border border-border bg-card p-2.5 text-xs font-normal normal-case leading-relaxed text-muted-foreground shadow-lg",
            side === "top" ? "bottom-full mb-1.5" : "top-full mt-1.5"
          )}
        >
          {children}
        </span>
      )}
    </span>
  );
}
