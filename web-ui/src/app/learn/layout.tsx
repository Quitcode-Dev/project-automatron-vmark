"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { AppLayout } from "@/components/layout";
import { cn } from "@/lib/utils";
import { TRACKS, lessonsForTrack } from "@/content/learn/lessons";

export default function LearnLayout({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();

  return (
    <AppLayout>
      <div className="mx-auto flex max-w-5xl gap-8">
        {/* Lesson nav */}
        <nav className="hidden w-56 shrink-0 md:block">
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
            return (
              <div key={track.id} className="mb-5">
                <p className="mb-1.5 text-xs font-semibold uppercase tracking-wide text-muted-foreground/70">
                  {track.label}
                </p>
                <ul className="space-y-0.5">
                  {lessons.map((l) => {
                    const href = `/learn/${l.slug}`;
                    const active = pathname === href;
                    return (
                      <li key={l.slug}>
                        <Link
                          href={href}
                          className={cn(
                            "block rounded-md px-2 py-1.5 text-sm transition-colors",
                            active
                              ? "bg-primary/10 font-medium text-primary"
                              : "text-muted-foreground hover:bg-muted hover:text-foreground"
                          )}
                        >
                          {l.title}
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
