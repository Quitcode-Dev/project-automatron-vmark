"use client";

import Link from "next/link";
import { BookOpen } from "lucide-react";
import { TRACKS, lessonsForTrack } from "@/content/learn/lessons";

export default function LearnIndex() {
  return (
    <div>
      <div className="mb-8 flex items-start gap-3">
        <div className="mt-0.5 flex h-9 w-9 items-center justify-center rounded-lg bg-primary/10 text-primary">
          <BookOpen className="h-5 w-5" />
        </div>
        <div>
          <h1 className="text-2xl font-bold">Learn Automatron</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            Short lessons on using, operating, and extending Automatron. New here? Start with{" "}
            <Link href="/learn/what-is-automatron" className="text-primary hover:underline">
              What Automatron does
            </Link>
            .
          </p>
        </div>
      </div>

      <div className="space-y-6">
        {TRACKS.map((track) => {
          const lessons = lessonsForTrack(track.id);
          return (
            <section key={track.id} className="rounded-xl border border-border bg-card p-5">
              <h2 className="text-lg font-semibold">{track.label}</h2>
              <p className="mt-0.5 text-sm text-muted-foreground">{track.blurb}</p>
              {lessons.length === 0 ? (
                <p className="mt-3 text-sm text-muted-foreground/70">Coming soon.</p>
              ) : (
                <ul className="mt-3 divide-y divide-border/60">
                  {lessons.map((l) => (
                    <li key={l.slug}>
                      <Link
                        href={`/learn/${l.slug}`}
                        className="flex flex-col gap-0.5 py-2.5 transition-colors hover:text-primary"
                      >
                        <span className="text-sm font-medium">{l.title}</span>
                        <span className="text-xs text-muted-foreground">{l.summary}</span>
                      </Link>
                    </li>
                  ))}
                </ul>
              )}
            </section>
          );
        })}
      </div>
    </div>
  );
}
