"use client";

import { useCallback, useEffect, useState } from "react";
import * as api from "@/lib/api";
import type { ProjectMember, ViewerRole } from "@/lib/types";
import { Clock, Loader2, Mail, Trash2, X } from "lucide-react";

interface ShareDialogProps {
  open: boolean;
  onClose: () => void;
  projectId: string;
  projectName: string;
  /** Only an owner (or admin) can add or remove people; collaborators see the list read-only. */
  viewerRole: ViewerRole | null;
}

/** The API surfaces validation failures as `API 422: {"detail":"..."}` — pull the
 *  human sentence out so the dialog can show it instead of the status line. */
function readableError(err: unknown): string {
  const message = err instanceof Error ? err.message : String(err);
  const match = message.match(/\{"detail":\s*"([^"]+)"\}/);
  if (match) return match[1];
  if (message.startsWith("API 403")) return "Only the project owner can change sharing.";
  return message || "Something went wrong.";
}

export function ShareDialog({
  open,
  onClose,
  projectId,
  projectName,
  viewerRole,
}: ShareDialogProps) {
  const [members, setMembers] = useState<ProjectMember[]>([]);
  const [email, setEmail] = useState("");
  const [isLoading, setIsLoading] = useState(false);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const canManage = viewerRole === "owner" || viewerRole === "admin";

  const load = useCallback(async () => {
    setIsLoading(true);
    try {
      setMembers(await api.getProjectMembers(projectId));
      setError(null);
    } catch (err) {
      setError(readableError(err));
    } finally {
      setIsLoading(false);
    }
  }, [projectId]);

  useEffect(() => {
    if (open) void load();
  }, [open, load]);

  if (!open) return null;

  const handleAdd = async (e: React.FormEvent) => {
    e.preventDefault();
    const trimmed = email.trim();
    if (!trimmed || isSubmitting) return;
    setIsSubmitting(true);
    try {
      const member = await api.addProjectMember(projectId, trimmed);
      setMembers((cur) => [...cur.filter((m) => m.user_id !== member.user_id), member]);
      setEmail("");
      setError(null);
    } catch (err) {
      setError(readableError(err));
    } finally {
      setIsSubmitting(false);
    }
  };

  const handleRemove = async (userId: string) => {
    try {
      await api.removeProjectMember(projectId, userId);
      setMembers((cur) => cur.filter((m) => m.user_id !== userId));
      setError(null);
    } catch (err) {
      setError(readableError(err));
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center">
      <div className="absolute inset-0 bg-black/50 backdrop-blur-sm" onClick={onClose} />

      <div className="relative w-full max-w-lg rounded-xl border border-border bg-card p-6 shadow-2xl">
        <div className="flex items-start justify-between gap-4">
          <div>
            <h2 className="text-lg font-semibold">Share project</h2>
            <p className="mt-1 text-sm text-muted-foreground">
              {canManage
                ? `Give someone access to ${projectName}. They can drive it, but only you can delete it or change sharing.`
                : `People with access to ${projectName}.`}
            </p>
          </div>
          <button onClick={onClose} className="text-muted-foreground hover:text-foreground">
            <X className="h-5 w-5" />
          </button>
        </div>

        {canManage && (
          <form onSubmit={handleAdd} className="mt-4 flex gap-2">
            <div className="relative flex-1">
              <Mail className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
              <input
                type="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                placeholder="teammate@company.com"
                className="w-full rounded-lg border border-border bg-background py-2 pl-9 pr-3 text-sm outline-none focus:border-primary"
              />
            </div>
            <button
              type="submit"
              disabled={!email.trim() || isSubmitting}
              className="flex items-center gap-2 rounded-lg bg-primary px-4 py-2 text-sm font-medium text-primary-foreground disabled:opacity-50"
            >
              {isSubmitting && <Loader2 className="h-4 w-4 animate-spin" />}
              Share
            </button>
          </form>
        )}

        {error && (
          <p className="mt-3 rounded-lg border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive">
            {error}
          </p>
        )}

        <div className="mt-5 space-y-1">
          <p className="text-xs uppercase tracking-[0.18em] text-muted-foreground">
            People with access
          </p>
          {isLoading ? (
            <p className="py-4 text-sm text-muted-foreground">Loading…</p>
          ) : (
            <ul className="divide-y divide-border">
              {members.map((member) => (
                <li key={member.user_id} className="flex items-center justify-between gap-3 py-2.5">
                  <div className="min-w-0">
                    <p className="truncate text-sm">
                      {member.name || member.email}
                      {member.name && (
                        <span className="ml-2 text-xs text-muted-foreground">{member.email}</span>
                      )}
                    </p>
                    <p className="flex items-center gap-1.5 text-xs text-muted-foreground">
                      {member.role === "owner" ? "Owner" : "Collaborator"}
                      {member.pending && (
                        <span className="inline-flex items-center gap-1">
                          <Clock className="h-3 w-3" />
                          hasn&apos;t signed in yet
                        </span>
                      )}
                    </p>
                  </div>
                  {canManage && member.role !== "owner" && (
                    <button
                      onClick={() => void handleRemove(member.user_id)}
                      title="Revoke access"
                      className="rounded p-1.5 text-muted-foreground transition-colors hover:bg-destructive/10 hover:text-destructive"
                    >
                      <Trash2 className="h-4 w-4" />
                    </button>
                  )}
                </li>
              ))}
            </ul>
          )}
        </div>
      </div>
    </div>
  );
}
