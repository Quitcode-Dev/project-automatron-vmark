"use client";

import Link from "next/link";
import { BookOpen, Compass, ArrowRight, Check, Circle } from "lucide-react";
import { TRACKS, lessonsForTrack, orderedLessons } from "@/content/learn/lessons";
import { useOnboardingStore } from "@/stores/onboardingStore";

export default function LearnIndex() {
  const lessonsDone = useOnboardingStore((s) => s.lessonsDone);
  const all = orderedLessons();
  const doneCount = all.filter((l) => lessonsDone[l.slug]).length;
  const pct = all.length ? Math.round((doneCount / all.length) * 100) : 0;

  return (
    <div className="pb-10">
      {/* Hero */}
      <div className="mb-6 rounded-xl border border-primary/20 bg-primary/5 p-6">
        <div className="flex items-start gap-3">
          <div className="mt-0.5 flex h-9 w-9 items-center justify-center rounded-lg bg-primary/10 text-primary">
            <Compass className="h-5 w-5" />
          </div>
          <div className="min-w-0">
            <h1 className="text-2xl font-bold">Learn Automatron</h1>
            <p className="mt-1 max-w-2xl text-sm text-muted-foreground">
              You don&apos;t need to write code. Automatron plans and builds; your job is
              to <strong className="text-foreground">describe, approve, and review</strong>.
              These lessons teach you how — starting with your role and just enough
              Git/GitHub to review with confidence.
            </p>
            <Link
              href="/learn/your-role"
              className="mt-4 inline-flex items-center gap-1.5 rounded-lg bg-primary px-4 py-2 text-sm font-medium text-primary-foreground transition-colors hover:bg-primary/90"
            >
              Start here <ArrowRight className="h-4 w-4" />
            </Link>
          </div>
        </div>
      </div>

      {/* Overall progress */}
      <div className="mb-6 flex items-center gap-3 text-sm">
        <BookOpen className="h-4 w-4 text-muted-foreground" />
        <span className="text-muted-foreground">
          {doneCount} of {all.length} lessons done
        </span>
        <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-muted">
          <div className="h-full rounded-full bg-primary transition-all" style={{ width: `${pct}%` }} />
        </div>
      </div>

      {/* Track cards */}
      <div className="space-y-4">
        {TRACKS.map((track) => {
          const lessons = lessonsForTrack(track.id);
          const done = lessons.filter((l) => lessonsDone[l.slug]).length;
          return (
            <section key={track.id} className="rounded-xl border border-border bg-card p-5">
              <div className="flex items-start justify-between gap-3">
                <div>
                  <h2 className="text-lg font-semibold">{track.label}</h2>
                  <p className="mt-0.5 text-sm text-muted-foreground">{track.blurb}</p>
                </div>
                {lessons.length > 0 && (
                  <span className="shrink-0 rounded-full border border-border px-2 py-0.5 text-xs text-muted-foreground">
                    {done}/{lessons.length}
                  </span>
                )}
              </div>
              {lessons.length === 0 ? (
                <p className="mt-3 text-sm text-muted-foreground/70">Coming soon.</p>
              ) : (
                <ul className="mt-3 divide-y divide-border/60">
                  {lessons.map((l) => {
                    const isDone = Boolean(lessonsDone[l.slug]);
                    return (
                      <li key={l.slug}>
                        <Link
                          href={`/learn/${l.slug}`}
                          className="flex items-start gap-2.5 py-2.5 transition-colors hover:text-primary"
                        >
                          {isDone ? (
                            <Check className="mt-0.5 h-4 w-4 shrink-0 text-green-400" />
                          ) : (
                            <Circle className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground/30" />
                          )}
                          <span className="min-w-0">
                            <span className="block text-sm font-medium">{l.title}</span>
                            <span className="block text-xs text-muted-foreground">{l.summary}</span>
                          </span>
                        </Link>
                      </li>
                    );
                  })}
                </ul>
              )}
            </section>
          );
        })}
      </div>
    </div>
  );
}
