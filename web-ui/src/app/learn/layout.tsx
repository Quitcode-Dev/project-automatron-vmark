"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { Check } from "lucide-react";
import { AppLayout } from "@/components/layout";
import { cn } from "@/lib/utils";
import { TRACKS, lessonsForTrack } from "@/content/learn/lessons";
import { useOnboardingStore } from "@/stores/onboardingStore";

export default function LearnLayout({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const lessonsDone = useOnboardingStore((s) => s.lessonsDone);

  return (
    <AppLayout>
      <div className="mx-auto flex max-w-5xl gap-8">
        {/* Lesson nav */}
        <nav className="hidden w-60 shrink-0 md:block">
          <Link
            href="/learn"
            className={cn(
              "mb-4 block text-sm font-medium",
              pathname === "/learn" ? "text-primary" : "text-muted-foreground hover:text-foreground"
            )}
          >
            Overview
          </Link>
          {TRACKS.map((track) => {
            const lessons = lessonsForTrack(track.id);
            if (lessons.length === 0) return null;
            const done = lessons.filter((l) => lessonsDone[l.slug]).length;
            return (
              <div key={track.id} className="mb-5">
                <div className="mb-1.5 flex items-center justify-between">
                  <p className="text-xs font-semibold uppercase tracking-wide text-muted-foreground/70">
                    {track.label}
                  </p>
                  <span className="text-[10px] text-muted-foreground/60">
                    {done}/{lessons.length}
                  </span>
                </div>
                <ul className="space-y-0.5">
                  {lessons.map((l) => {
                    const href = `/learn/${l.slug}`;
                    const active = pathname === href;
                    const isDone = Boolean(lessonsDone[l.slug]);
                    return (
                      <li key={l.slug}>
                        <Link
                          href={href}
                          className={cn(
                            "flex items-center gap-1.5 rounded-md px-2 py-1.5 text-sm transition-colors",
                            active
                              ? "bg-primary/10 font-medium text-primary"
                              : "text-muted-foreground hover:bg-muted hover:text-foreground"
                          )}
                        >
                          <Check
                            className={cn(
                              "h-3.5 w-3.5 shrink-0",
                              isDone ? "text-green-400" : "text-transparent"
                            )}
                          />
                          <span className="min-w-0 truncate">{l.title}</span>
                        </Link>
                      </li>
                    );
                  })}
                </ul>
              </div>
            );
          })}
        </nav>

        <div className="min-w-0 flex-1">{children}</div>
      </div>
    </AppLayout>
  );
}
