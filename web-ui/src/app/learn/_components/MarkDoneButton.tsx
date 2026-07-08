"use client";

import { Check, Circle } from "lucide-react";
import { useOnboardingStore } from "@/stores/onboardingStore";
import { cn } from "@/lib/utils";

export function MarkDoneButton({ slug }: { slug: string }) {
  const done = useOnboardingStore((s) => Boolean(s.lessonsDone[slug]));
  const setLessonDone = useOnboardingStore((s) => s.setLessonDone);
  return (
    <button
      onClick={() => setLessonDone(slug, !done)}
      className={cn(
        "inline-flex items-center gap-1.5 rounded-lg border px-3 py-1.5 text-sm font-medium transition-colors",
        done
          ? "border-green-500/40 bg-green-500/10 text-green-400"
          : "border-border text-muted-foreground hover:bg-muted hover:text-foreground"
      )}
    >
      {done ? <Check className="h-4 w-4" /> : <Circle className="h-4 w-4" />}
      {done ? "Completed" : "Mark as done"}
    </button>
  );
}
