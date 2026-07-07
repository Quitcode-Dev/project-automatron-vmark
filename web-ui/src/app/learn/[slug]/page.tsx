"use client";

import { useParams } from "next/navigation";
import Link from "next/link";
import ReactMarkdown from "react-markdown";
import { ArrowLeft } from "lucide-react";
import { getLesson } from "@/content/learn/lessons";

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

  return (
    <article>
      <Link
        href="/learn"
        className="mb-4 inline-flex items-center gap-1.5 text-sm text-muted-foreground hover:text-foreground"
      >
        <ArrowLeft className="h-4 w-4" />
        Learn
      </Link>
      <h1 className="text-2xl font-bold">{lesson.title}</h1>
      <div className="prose prose-invert mt-4 max-w-none prose-headings:font-semibold prose-a:text-primary prose-code:rounded prose-code:bg-muted prose-code:px-1 prose-code:py-0.5 prose-code:text-sm prose-code:before:content-none prose-code:after:content-none">
        <ReactMarkdown
          components={{
            a: ({ href, children }) =>
              href && href.startsWith("/") ? (
                <Link href={href}>{children}</Link>
              ) : (
                <a href={href} target="_blank" rel="noopener noreferrer">
                  {children}
                </a>
              ),
          }}
        >
          {lesson.body}
        </ReactMarkdown>
      </div>
    </article>
  );
}
