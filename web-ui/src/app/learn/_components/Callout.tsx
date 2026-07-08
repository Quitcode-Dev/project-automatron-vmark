import {
  Lightbulb,
  AlertTriangle,
  CheckCircle2,
  Compass,
  Info,
  ListChecks,
} from "lucide-react";
import { cn } from "@/lib/utils";

export type CalloutKind = "tip" | "warning" | "check" | "do" | "role" | "note";

const META: Record<
  CalloutKind,
  { icon: typeof Info; label: string; box: string; accent: string }
> = {
  tip: { icon: Lightbulb, label: "Tip", box: "border-blue-500/30 bg-blue-500/5", accent: "text-blue-400" },
  warning: { icon: AlertTriangle, label: "Watch out", box: "border-amber-500/30 bg-amber-500/5", accent: "text-amber-400" },
  check: { icon: CheckCircle2, label: "What to check", box: "border-green-500/30 bg-green-500/5", accent: "text-green-400" },
  do: { icon: ListChecks, label: "Do this", box: "border-violet-500/30 bg-violet-500/5", accent: "text-violet-400" },
  role: { icon: Compass, label: "Your role", box: "border-primary/30 bg-primary/5", accent: "text-primary" },
  note: { icon: Info, label: "Note", box: "border-border bg-muted/30", accent: "text-muted-foreground" },
};

export function Callout({
  kind = "note",
  title,
  children,
}: {
  kind?: CalloutKind;
  title?: string;
  children: React.ReactNode;
}) {
  const m = META[kind] ?? META.note;
  const Icon = m.icon;
  return (
    <div className={cn("not-prose my-4 rounded-lg border px-4 py-3", m.box)}>
      <div className="mb-1 flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wide">
        <Icon className={cn("h-3.5 w-3.5", m.accent)} />
        <span className={m.accent}>{title || m.label}</span>
      </div>
      <div className="space-y-1.5 text-sm leading-relaxed text-foreground/90 [&_a]:text-primary [&_a]:underline [&_code]:rounded [&_code]:bg-muted [&_code]:px-1 [&_code]:py-0.5 [&_code]:text-xs [&_ol]:list-decimal [&_ol]:pl-5 [&_strong]:text-foreground [&_ul]:list-disc [&_ul]:pl-5">
        {children}
      </div>
    </div>
  );
}
