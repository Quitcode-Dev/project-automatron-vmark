"use client";

import { useEffect, useState } from "react";
import { LogOut, User, PlayCircle } from "lucide-react";
import { useOnboardingStore } from "@/stores/onboardingStore";

interface SessionUser {
  email?: string | null;
  name?: string | null;
}

export function UserMenu() {
  const [user, setUser] = useState<SessionUser | null>(null);
  const [open, setOpen] = useState(false);
  const requestReplay = useOnboardingStore((s) => s.requestReplay);

  useEffect(() => {
    let cancelled = false;
    fetch("/api/auth/session", { credentials: "include" })
      .then((r) => r.json())
      .then((s) => {
        if (!cancelled) setUser(s?.user ?? null);
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, []);

  if (!user?.email) return null;

  return (
    <div className="relative" data-tour="user-menu">
      <button
        onClick={() => setOpen((v) => !v)}
        className="flex items-center gap-2 rounded-lg px-2 py-1.5 text-sm text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
      >
        <User className="h-4 w-4" />
        <span className="max-w-[180px] truncate">{user.email}</span>
      </button>
      {open && (
        <div className="absolute right-0 top-full z-50 mt-1 w-48 rounded-lg border border-border bg-card p-1 shadow-lg">
          <button
            onClick={() => {
              setOpen(false);
              requestReplay();
            }}
            className="flex w-full items-center gap-2 rounded-md px-3 py-2 text-sm text-muted-foreground hover:bg-muted hover:text-foreground"
          >
            <PlayCircle className="h-4 w-4" />
            Replay walkthrough
          </button>
          <a
            href="/api/auth/signout"
            className="flex w-full items-center gap-2 rounded-md px-3 py-2 text-sm text-muted-foreground hover:bg-muted hover:text-foreground"
          >
            <LogOut className="h-4 w-4" />
            Sign out
          </a>
        </div>
      )}
    </div>
  );
}
