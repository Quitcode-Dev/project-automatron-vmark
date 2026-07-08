"use client";

import { useParams } from "next/navigation";
import Link from "next/link";
import { ArrowLeft, ArrowRight, Clock } from "lucide-react";
import { getLesson, adjacentLessons, type Audience } from "@/content/learn/lessons";
import { MarkdownRenderer } from "../_components/MarkdownRenderer";
import { MarkDoneButton } from "../_components/MarkDoneButton";

const AUDIENCE: Record<Audience, { label: string; cls: string }> = {
  reviewer: { label: "For reviewers", cls: "border-primary/30 bg-primary/10 text-primary" },
  operator: { label: "For operators", cls: "border-amber-500/30 bg-amber-500/10 text-amber-400" },
  developer: { label: "For developers", cls: "border-blue-500/30 bg-blue-500/10 text-blue-400" },
};

export default function LessonPage() {
  const params = useParams();
  const slug = (params?.slug as string) ?? "";
  const lesson = getLesson(slug);

  if (!lesson) {
    return (
      <div className="text-sm text-muted-foreground">
        Lesson not found.{" "}
        <Link href="/learn" className="text-primary hover:underline">
          Back to Learn
        </Link>
        .
      </div>
    );
  }

  const { prev, next } = adjacentLessons(slug);
  const prereqs = (lesson.prerequisites ?? [])
    .map((s) => getLesson(s))
    .filter((l): l is NonNullable<typeof l> => Boolean(l));
  const badge = lesson.audience ? AUDIENCE[lesson.audience] : null;

  return (
    <article className="pb-10">
      <Link
        href="/learn"
        className="mb-4 inline-flex items-center gap-1.5 text-sm text-muted-foreground hover:text-foreground"
      >
        <ArrowLeft className="h-4 w-4" />
        Learn
      </Link>

      {/* Header */}
      <div className="mb-5 border-b border-border pb-5">
        <div className="mb-2 flex flex-wrap items-center gap-2">
          {badge && (
            <span className={`rounded-full border px-2 py-0.5 text-xs font-medium ${badge.cls}`}>
              {badge.label}
            </span>
          )}
          {lesson.minutes ? (
            <span className="inline-flex items-center gap-1 text-xs text-muted-foreground">
              <Clock className="h-3.5 w-3.5" /> {lesson.minutes} min read
            </span>
          ) : null}
        </div>
        <h1 className="text-2xl font-bold">{lesson.title}</h1>
        <p className="mt-1 text-sm text-muted-foreground">{lesson.summary}</p>
        {prereqs.length > 0 && (
          <p className="mt-3 text-xs text-muted-foreground">
            First, read:{" "}
            {prereqs.map((p, i) => (
              <span key={p.slug}>
                {i > 0 && ", "}
                <Link href={`/learn/${p.slug}`} className="text-primary hover:underline">
                  {p.title}
                </Link>
              </span>
            ))}
          </p>
        )}
      </div>

      {/* Body */}
      <MarkdownRenderer body={lesson.body} />

      {/* Footer: mark done + prev/next */}
      <div className="mt-8 border-t border-border pt-5">
        <MarkDoneButton slug={slug} />
        <div className="mt-4 flex items-stretch justify-between gap-3">
          {prev ? (
            <Link
              href={`/learn/${prev.slug}`}
              className="group flex max-w-[48%] flex-col rounded-lg border border-border px-3 py-2 transition-colors hover:bg-muted"
            >
              <span className="inline-flex items-center gap-1 text-xs text-muted-foreground">
                <ArrowLeft className="h-3 w-3" /> Previous
              </span>
              <span className="truncate text-sm font-medium group-hover:text-primary">{prev.title}</span>
            </Link>
          ) : (
            <span />
          )}
          {next ? (
            <Link
              href={`/learn/${next.slug}`}
              className="group flex max-w-[48%] flex-col items-end rounded-lg border border-border px-3 py-2 text-right transition-colors hover:bg-muted"
            >
              <span className="inline-flex items-center gap-1 text-xs text-muted-foreground">
                Next <ArrowRight className="h-3 w-3" />
              </span>
              <span className="truncate text-sm font-medium group-hover:text-primary">{next.title}</span>
            </Link>
          ) : (
            <span />
          )}
        </div>
      </div>
    </article>
  );
}
