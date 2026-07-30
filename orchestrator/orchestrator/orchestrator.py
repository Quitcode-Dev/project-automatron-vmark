"""GitHub-native orchestrator — replaces LangGraph-based graph execution."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from orchestrator.api.websocket import (
    emit_architect_chunk,
    emit_architect_message,
    emit_builder_log,
    emit_error,
    emit_human_required,
    emit_issues_updated,
    emit_pr_review_ready,
    emit_status_update,
)
from orchestrator.config import settings
from orchestrator.github.issues import GitHubClient
from orchestrator.llm.configuration import normalize_llm_config
from orchestrator.llm.provider import call_llm, call_llm_streaming
from orchestrator.models.project import (
    create_github_issue,
    get_project,
    list_github_issues,
    record_approval,
    save_activity_log,
    update_github_issue_pr,
    update_github_issue_status,
    update_project,
    update_project_preview,
)
from orchestrator.observability import trace_event

logger = logging.getLogger(__name__)

_PROMPT_DIR = Path(__file__).parent.parent / "prompts"


def _load_prompt(name: str) -> str:
    return (_PROMPT_DIR / name).read_text(encoding="utf-8")


def _parse_tagged_block(response: str, tag: str) -> str | None:
    marker = f"```{tag}"
    if marker not in response:
        return None
    try:
        start = response.index(marker) + len(marker)
        end = response.index("```", start)
        return response[start:end].strip() or None
    except ValueError:
        return None


def _parse_repo_url(repo_url: str) -> tuple[str, str] | None:
    """Extract (owner, repo) from a GitHub URL or owner/repo string."""
    # https://github.com/owner/repo  or  github.com/owner/repo
    match = re.search(r"github\.com/([^/]+)/([^/?\s#]+)", repo_url)
    if match:
        return match.group(1), match.group(2).rstrip(".git")
    # owner/repo shorthand
    parts = repo_url.strip().split("/")
    if len(parts) == 2 and parts[0] and parts[1]:
        return parts[0], parts[1].rstrip(".git")
    return None


def _read_source_files(repo_dir: "Path", max_chars: int = 40000) -> str:
    """Walk repo_dir and return key source files as a formatted string."""
    import os as _os

    SKIP_DIRS = {
        "node_modules", ".git", ".next", "dist", "build", ".venv",
        "__pycache__", ".cache", "coverage", ".turbo", "out", ".mypy_cache",
    }
    INCLUDE_EXTS = {".ts", ".tsx", ".js", ".jsx", ".py", ".css", ".md"}
    SKIP_FILES = {"package-lock.json", "yarn.lock", "pnpm-lock.yaml"}

    parts: list[str] = []
    total = 0

    for dirpath, dirnames, filenames in _os.walk(repo_dir):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for filename in sorted(filenames):
            if filename in SKIP_FILES:
                continue
            path = Path(dirpath) / filename
            if path.suffix not in INCLUDE_EXTS:
                continue
            rel = path.relative_to(repo_dir)
            try:
                content = path.read_text(encoding="utf-8", errors="replace")
                if len(content) > 5000:
                    content = content[:5000] + "\n... [truncated]"
                entry = f"### {rel}\n```\n{content}\n```\n\n"
                parts.append(entry)
                total += len(entry)
                if total >= max_chars:
                    return "\n".join(parts)
            except Exception:
                pass

    return "\n".join(parts)


async def _fetch_figma_context(urls: list[str], token: str) -> str:
    """Fetch frame/component names and text from Figma files and return as text summary."""
    import httpx

    parts: list[str] = []
    async with httpx.AsyncClient(timeout=15) as client:
        for url in urls:
            file_key_m = re.search(r"figma\.com/(?:design|file)/([^/?#]+)", url)
            if not file_key_m:
                parts.append(f"[Skipped — not a valid Figma URL: {url}]")
                continue
            file_key = file_key_m.group(1)
            node_id_m = re.search(r"node-id=([^&]+)", url)

            try:
                if node_id_m:
                    node_id = node_id_m.group(1).replace("-", ":")
                    resp = await client.get(
                        f"https://api.figma.com/v1/files/{file_key}/nodes",
                        params={"ids": node_id},
                        headers={"X-Figma-Token": token},
                    )
                else:
                    resp = await client.get(
                        f"https://api.figma.com/v1/files/{file_key}",
                        headers={"X-Figma-Token": token},
                    )
            except Exception as exc:
                parts.append(f"[Figma fetch error for {url}: {exc}]")
                continue

            if resp.status_code != 200:
                parts.append(f"[Figma {url}: HTTP {resp.status_code}]")
                continue

            summary = _summarise_figma_node(resp.json())
            parts.append(f"**{url}**\n{summary}")

    return "\n\n".join(parts)


@dataclass
class SupabaseSchema:
    """Structured result of introspecting a live Supabase project.

    Used to feed three downstream consumers from one introspection call:
      - markdown    → architect + builder prompt injection
      - typescript  → scaffolded src/lib/supabase/database.types.ts
      - tables/table_kinds → programmatic schema-violation check in review_pr

    `ok=False` means the introspection itself failed (network / 4xx / 5xx /
    empty definitions). Callers MUST check `ok` before injecting `markdown`
    into a prompt — silently passing an error string into the architect was
    the original bug that let it invent a fictional schema.
    """
    ok: bool
    error: str | None = None
    markdown: str = ""
    typescript: str = ""
    tables: dict[str, list[str]] = field(default_factory=dict)
    table_kinds: dict[str, str] = field(default_factory=dict)  # name → "table" | "view"
    # Insert/Update column sets per table (excludes readOnly columns).
    writable_cols: dict[str, list[str]] = field(default_factory=dict)
    # Nullable column set per table (subset of Row cols not in `required`).
    nullable_cols: dict[str, set[str]] = field(default_factory=dict)


_TS_PRIMITIVE = {
    "string": "string",
    "integer": "number",
    "number": "number",
    "boolean": "boolean",
    "array": "unknown[]",
    "object": "Record<string, unknown>",
}


def _ts_type_for_column(col: dict[str, Any]) -> str:
    """Map a PostgREST OpenAPI column descriptor to a TypeScript type literal."""
    fmt = (col.get("format") or "").lower()
    typ = (col.get("type") or "").lower()
    if fmt in ("text", "character varying", "uuid", "date", "timestamp", "timestamptz",
               "timestamp with time zone", "timestamp without time zone", "time", "bytea"):
        return "string"
    if fmt in ("smallint", "integer", "bigint", "numeric", "real",
               "double precision", "int2", "int4", "int8", "float4", "float8"):
        return "number"
    if fmt == "boolean":
        return "boolean"
    if fmt == "json" or fmt == "jsonb":
        return "Json"
    return _TS_PRIMITIVE.get(typ, "unknown")


def _build_typescript_types(
    definitions: dict[str, Any],
    table_kinds: dict[str, str],
) -> str:
    """Generate src/lib/supabase/database.types.ts content matching Supabase's
    `gen types typescript` output shape (Tables/Views/Row/Insert/Update).

    Views and definitions of uncertain kind get `Record<string, unknown>` rather
    than risk wrong row shapes. readOnly columns appear in Row but are omitted
    from Insert/Update — emitting them there would break runtime inserts on
    generated columns.
    """
    out: list[str] = []
    out.append("// Auto-generated from the live Supabase project's PostgREST OpenAPI.")
    out.append("// This file is the source of truth for table/column names — do not edit by hand.")
    out.append("// Regenerated on every project scaffold; runtime introspection refreshes it.")
    out.append("")
    out.append("export type Json =")
    out.append("  | string")
    out.append("  | number")
    out.append("  | boolean")
    out.append("  | null")
    out.append("  | { [key: string]: Json | undefined }")
    out.append("  | Json[];")
    out.append("")
    out.append("export type Database = {")
    out.append("  public: {")
    out.append("    Tables: {")

    table_names = [n for n, k in sorted(table_kinds.items()) if k == "table"]
    view_names = [n for n, k in sorted(table_kinds.items()) if k == "view"]

    def _emit_row(name: str, props: dict[str, Any], required: set[str]) -> list[str]:
        block: list[str] = []
        for col_name in sorted(props.keys()):
            col = props[col_name]
            ts = _ts_type_for_column(col)
            nullable = col_name not in required
            block.append(f"          {col_name}: {ts}{' | null' if nullable else ''};")
        return block

    for name in table_names:
        table = definitions.get(name, {})
        props = table.get("properties", {}) or {}
        required = set(table.get("required", []) or [])
        out.append(f"      {name}: {{")
        out.append("        Row: {")
        out.extend(_emit_row(name, props, required))
        out.append("        };")
        # Insert: required cols are mandatory, readOnly cols are excluded
        out.append("        Insert: {")
        for col_name in sorted(props.keys()):
            col = props[col_name]
            if col.get("readOnly") is True:
                continue
            ts = _ts_type_for_column(col)
            optional = col_name not in required or col.get("default") is not None
            nullable = col_name not in required
            out.append(
                f"          {col_name}{'?' if optional else ''}: {ts}{' | null' if nullable else ''};"
            )
        out.append("        };")
        out.append("        Update: {")
        for col_name in sorted(props.keys()):
            col = props[col_name]
            if col.get("readOnly") is True:
                continue
            ts = _ts_type_for_column(col)
            nullable = col_name not in required
            out.append(
                f"          {col_name}?: {ts}{' | null' if nullable else ''};"
            )
        out.append("        };")
        out.append("      };")

    out.append("    };")
    out.append("    Views: {")
    for name in view_names:
        # Views: emit Row only, with best-effort columns. Safer than guessing.
        table = definitions.get(name, {})
        props = table.get("properties", {}) or {}
        required = set(table.get("required", []) or [])
        out.append(f"      {name}: {{")
        out.append("        Row: {")
        out.extend(_emit_row(name, props, required))
        out.append("        };")
        out.append("      };")
    out.append("    };")
    out.append("    Functions: Record<string, never>;")
    out.append("    Enums: Record<string, string>;")
    out.append("    CompositeTypes: Record<string, never>;")
    out.append("  };")
    out.append("};")
    return "\n".join(out) + "\n"


def _infer_table_kind(name: str, table: dict[str, Any]) -> str:
    """Best-effort table-vs-view classification from PostgREST OpenAPI hints.

    PostgREST doesn't explicitly tag views in `definitions`, but views typically
    have no primary-key-shaped `required` field. Conservative heuristic: any
    definition with an `id` column in `required` is a table; everything else is
    a view. Wrong classifications are recoverable (view treated as table is
    still readable; we just lose Insert/Update typing).
    """
    required = set(table.get("required", []) or [])
    if "id" in required:
        return "table"
    props = table.get("properties", {}) or {}
    # Composite-PK tables often have multiple required *_id columns
    if any(c.endswith("_id") for c in required) and len(required) >= 2:
        return "table"
    if "id" in props:
        return "table"  # Mostly-nullable table is still a table
    return "view"


async def _introspect_supabase_schema(supabase_url: str, service_role_key: str) -> SupabaseSchema:
    """Query PostgREST's OpenAPI endpoint and return a SupabaseSchema.

    Failure modes return `ok=False` with a populated `error` — the caller MUST
    check `ok` and refuse to inject the schema into LLM prompts when not ok.
    Silently passing an error string into the architect was the original bug
    that let it invent a fictional schema in the in-flight project.
    """
    import httpx

    base = supabase_url.rstrip("/")
    headers = {"apikey": service_role_key, "Authorization": f"Bearer {service_role_key}"}

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(f"{base}/rest/v1/", headers=headers)
    except Exception as exc:
        return SupabaseSchema(ok=False, error=f"Network/connection error: {exc}")

    if resp.status_code != 200:
        return SupabaseSchema(
            ok=False,
            error=f"HTTP {resp.status_code} from {base}/rest/v1/ — {resp.text[:200]}",
        )

    try:
        spec = resp.json()
    except ValueError:
        return SupabaseSchema(ok=False, error="Invalid OpenAPI JSON response")

    definitions = spec.get("definitions", {}) or {}
    if not definitions:
        return SupabaseSchema(ok=False, error="No tables exposed via PostgREST (RLS or grants?)")

    # Build all the result fields in one pass.
    tables: dict[str, list[str]] = {}
    table_kinds: dict[str, str] = {}
    writable_cols: dict[str, list[str]] = {}
    nullable_cols: dict[str, set[str]] = {}

    md_lines: list[str] = []
    md_lines.append(
        "**This schema is the source of truth.** "
        "If you find migration files in the repo with different column names, trust this schema, "
        "NOT the migrations."
    )
    md_lines.append("")

    # Group by kind for markdown grouping; tables first, then views.
    for name, defn in definitions.items():
        table_kinds[name] = _infer_table_kind(name, defn)

    for table_name in sorted(definitions.keys()):
        table = definitions[table_name]
        props = table.get("properties", {}) or {}
        required = set(table.get("required", []) or [])
        col_names = sorted(props.keys())
        tables[table_name] = col_names
        writable_cols[table_name] = [c for c in col_names if not props[c].get("readOnly")]
        nullable_cols[table_name] = {c for c in col_names if c not in required}

        kind = table_kinds[table_name]
        kind_tag = "" if kind == "table" else " (view — read-only)"
        cols_md = []
        for col_name in col_names:
            col = props[col_name]
            t = col.get("format") or col.get("type") or "?"
            nullable = " NULL" if col_name not in required else ""
            ro = " READONLY" if col.get("readOnly") else ""
            cols_md.append(f"  {col_name}: {t}{nullable}{ro}")
        md_lines.append(f"### `{table_name}`{kind_tag}\n" + "\n".join(cols_md))

    # Inferred relationships (heuristic only — PostgREST doesn't expose FKs).
    rel_lines: list[str] = []
    all_names = set(tables.keys())
    for tname, cols in sorted(tables.items()):
        for col in cols:
            if col.endswith("_id") and col != "id":
                stem = col[:-3]
                if stem in all_names:
                    rel_lines.append(f"- `{tname}.{col}` → likely `{stem}.id`")
                elif (stem + "s") in all_names:
                    rel_lines.append(f"- `{tname}.{col}` → likely `{stem}s.id`")
    if rel_lines:
        md_lines.append("")
        md_lines.append("### Likely relationships (inferred from `_id` suffixes — not authoritative)")
        md_lines.extend(rel_lines)

    typescript = _build_typescript_types(definitions, table_kinds)

    return SupabaseSchema(
        ok=True,
        error=None,
        markdown="\n\n".join(md_lines),
        typescript=typescript,
        tables=tables,
        table_kinds=table_kinds,
        writable_cols=writable_cols,
        nullable_cols=nullable_cols,
    )


def _summarise_figma_node(data: dict[str, Any]) -> str:
    """Walk Figma JSON and extract frame/component names and visible text (max 100 lines)."""
    lines: list[str] = []

    def walk(node: dict[str, Any], depth: int = 0) -> None:
        if len(lines) >= 100 or depth > 5:
            return
        node_type = node.get("type", "")
        name = node.get("name", "")
        chars = node.get("characters", "")
        indent = "  " * depth
        if node_type in ("FRAME", "COMPONENT", "COMPONENT_SET", "SECTION", "GROUP", "PAGE"):
            lines.append(f"{indent}- [{node_type}] {name}")
        elif chars:
            lines.append(f'{indent}  "{chars[:80]}"')
        for child in node.get("children", []):
            walk(child, depth + 1)

    doc = data.get("document") or {}
    nodes = data.get("nodes") or {}
    if doc:
        walk(doc)
    for node_data in nodes.values():
        walk(node_data.get("document", node_data))

    return "\n".join(lines)


async def _build_stack_summary(gh: Any, owner: str, repo: str) -> str:
    """Read package.json + tailwind config and produce a short stack summary for issue bodies."""
    import json as _json
    lines: list[str] = []
    try:
        raw = await gh.read_file(owner, repo, "package.json") or ""
        if raw:
            pkg = _json.loads(raw)
            deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
            interesting = [
                "next", "react", "vue", "nuxt", "svelte", "astro",
                "tailwindcss", "@shadcn/ui", "radix-ui", "@radix-ui/react-dialog",
                "prisma", "drizzle-orm", "supabase", "@supabase/supabase-js",
                "zustand", "jotai", "react-query", "@tanstack/react-query",
                "framer-motion", "lucide-react", "zod", "next-intl",
            ]
            found = [k for k in interesting if any(k in d for d in deps)]
            if found:
                lines.append(f"- **Libraries:** {', '.join(found)}")
            if "typescript" in deps or pkg.get("devDependencies", {}).get("typescript"):
                lines.append("- **Language:** TypeScript")
    except Exception:
        pass

    try:
        tw = (
            await gh.read_file(owner, repo, "tailwind.config.ts")
            or await gh.read_file(owner, repo, "tailwind.config.js")
            or ""
        )
        if tw and "colors" in tw:
            lines.append("- **Tailwind:** custom theme with project color tokens (see tailwind.config)")
        elif tw:
            lines.append("- **Styling:** Tailwind CSS")
    except Exception:
        pass

    try:
        comp = await gh.read_file(owner, repo, "components.json") or ""
        if comp:
            lines.append("- **UI components:** shadcn/ui (components in `components/ui/`)")
    except Exception:
        pass

    return "\n".join(lines) if lines else ""


def _render_issue_body(
    task: dict[str, Any],
    epic: str,
    story: str,
    stack_summary: str = "",
    skill_context: str = "",
) -> str:
    lines: list[str] = []
    lines.append(f"**Epic:** {epic}  ")
    lines.append(f"**Story:** {story}")
    lines.append("")

    if task.get("description"):
        lines.append("## Overview")
        lines.append(task["description"])
        lines.append("")

    if task.get("file") or task.get("component"):
        lines.append("## Scope")
        if task.get("file"):
            lines.append(f"- **Primary file:** `{task['file']}`")
        if task.get("component"):
            lines.append(f"- **Component/function:** `{task['component']}`")
        lines.append("")

    notes = task.get("implementation_notes") or []
    if notes:
        lines.append("## Implementation notes")
        for note in notes:
            lines.append(f"- {note}")
        lines.append("")

    criteria = task.get("acceptance_criteria") or []
    if criteria:
        lines.append("## Acceptance criteria")
        for c in criteria:
            lines.append(f"- [ ] {c}")
        lines.append("")

    if task.get("validation"):
        lines.append("## Validation")
        lines.append(f"```\n{task['validation']}\n```")
        lines.append("")

    if stack_summary:
        lines.append("## Tech stack context")
        lines.append(stack_summary)
        lines.append("")

    if skill_context:
        lines.append(skill_context)

    return "\n".join(lines)


class GitHubOrchestrator:
    """Controls the full project lifecycle: planning → issue creation → PR review."""

    def __init__(self, project_id: str) -> None:
        self.project_id = project_id
        self.gh = GitHubClient()
        self._trace_ctx: dict[str, Any] = {
            "project_id": project_id,
            "actor": "orchestrator",
            "stage": "orchestration",
        }
        self._log_seq = 0
        # Lazily populated by analyze_and_plan or get_supabase_schema(); shared
        # with the builder + reviewer to avoid 40+ PostgREST calls per project.
        self._cached_schema: SupabaseSchema | None = None
        self._schema_cached_at: float = 0.0

    # ── Internal helpers ─────────────────────────────────────────────────────

    async def _project(self) -> dict[str, Any]:
        p = await get_project(self.project_id)
        if not p:
            raise RuntimeError(f"Project {self.project_id} not found")
        return p

    async def _llm_config(self) -> dict[str, Any]:
        p = await self._project()
        return normalize_llm_config(p.get("llm_config_json") or p.get("llm_config") or {})

    async def get_supabase_schema(self, ttl_seconds: int = 60) -> SupabaseSchema | None:
        """Return the project's live Supabase schema, cached for `ttl_seconds`.

        Returns None when the project has no Supabase creds. Returns a
        SupabaseSchema with ok=False (and an error string) when introspection
        was attempted but failed — callers should NOT inject the markdown into
        prompts in that case.
        """
        now = asyncio.get_event_loop().time()
        if self._cached_schema is not None and self._cached_schema.ok and (now - self._schema_cached_at) < ttl_seconds:
            return self._cached_schema
        p = await self._project()
        url = (p.get("supabase_url") or "").strip()
        key = (p.get("supabase_service_role_key") or "").strip()
        if not url or not key:
            return None
        result = await _introspect_supabase_schema(url, key)
        if result.ok:
            self._cached_schema = result
            self._schema_cached_at = now
        return result

    def _set_trace(self, stage: str, actor: str = "orchestrator") -> None:
        self._trace_ctx["stage"] = stage
        self._trace_ctx["actor"] = actor

    async def _log(self, task_text: str, output: str = "", status: str = "INFO") -> None:
        self._log_seq += 1
        await emit_builder_log(
            self.project_id,
            task_index=self._log_seq,
            task_text=task_text,
            output=output,
            status=status,
        )
        await save_activity_log(
            self.project_id,
            seq=self._log_seq,
            task_text=task_text,
            output=output,
            status=status,
        )

    # ── Stage 1: Analyze repo and produce plan ────────────────────────────────

    async def analyze_and_plan(self) -> None:
        """Read the repo, call architect LLM, stream the plan, wait for approval."""
        project = await self._project()

        # Parse owner/repo from stored repo_url or github_repo_owner/name
        owner = project.get("github_repo_owner")
        repo = project.get("github_repo_name")
        if not owner or not repo:
            repo_url = project.get("repo_url") or project.get("intake_text", "")
            parsed = _parse_repo_url(repo_url)
            if not parsed:
                raise RuntimeError("Cannot parse GitHub owner/repo from project")
            owner, repo = parsed
            # If no owner in URL (just a bare repo name), default to the configured org
            if owner == repo or not owner:
                org = settings.github_default_org or settings.github_owner
                owner = org
            await update_project(
                self.project_id,
                github_repo_owner=owner,
                github_repo_name=repo,
            )

        # Auto-register webhook so PR events are delivered without manual setup
        webhook_result = await self.gh.register_webhook(owner, repo)
        if webhook_result == "registered":
            await self._log("Webhook registered", f"PR events → {settings.automatron_public_url}/api/webhooks/github", "SUCCESS")
        elif webhook_result == "already_exists":
            await self._log("Webhook already registered", f"{owner}/{repo}", "INFO")
        elif webhook_result.startswith("error:"):
            await self._log("Webhook registration failed", webhook_result, "AMBIGUITY")
        # "skipped" means AUTOMATRON_PUBLIC_URL not set — silent, user hasn't configured it

        self._set_trace("planning", "architect")
        await trace_event(
            self.project_id, "orchestrator", "architect.run.started",
            {"owner": owner, "repo": repo},
        )

        await self._log("Reading repository files", f"{owner}/{repo}")

        # Read repo context
        readme = await self.gh.read_file(owner, repo, "README.md") or ""

        # Pre-architect Supabase schema introspection. Done BEFORE scaffolding
        # so the generated `src/lib/supabase/database.types.ts` can land in the
        # initial commit — that's the compile-time safety net. The architect's
        # call site below reuses self._cached_schema if it's already populated.
        early_schema: SupabaseSchema | None = None
        early_supabase_url = (project.get("supabase_url") or "").strip()
        early_supabase_key = (project.get("supabase_service_role_key") or "").strip()
        if early_supabase_url and early_supabase_key:
            await self._log("Pre-scaffold Supabase schema introspection", early_supabase_url, "RUNNING")
            early_schema = await _introspect_supabase_schema(early_supabase_url, early_supabase_key)
            if early_schema.ok:
                self._cached_schema = early_schema
                self._schema_cached_at = asyncio.get_event_loop().time()
                await self._log(
                    "Schema ready for scaffold",
                    f"{len(early_schema.tables)} tables → real database.types.ts will be pushed",
                    "SUCCESS",
                )
            else:
                # Don't abort yet — the project may not actually use Supabase
                # (we'll re-check after _build_stack_summary). But warn loudly.
                await self._log(
                    "Pre-scaffold schema introspection FAILED",
                    f"{early_schema.error} — falling back to stub database.types.ts. "
                    f"If this project actually uses Supabase, the architect run will abort.",
                    "AMBIGUITY",
                )

        # Pre-architect scaffolding step. If main has no package.json AND the
        # intake / README signals Next.js, push a vendored Next.js + Tailwind +
        # shadcn skeleton to main as a single batch of commits BEFORE the
        # architect plans any issues. Eliminates the recurring failure mode where
        # the architect generates an "Initialize project" task that the builder
        # can't actually fulfil (only file edits) — see the root-cause
        # plan in /Users/qc3/.claude/plans/atomic-fluttering-duckling.md.
        from orchestrator.scaffolding import maybe_scaffold_repo
        scaffolded = await maybe_scaffold_repo(
            self.gh, owner, repo,
            intake_text=project.get("intake_text") or "",
            readme=readme,
            log_fn=self._log,
            database_types_ts=(early_schema.typescript if early_schema and early_schema.ok else None),
        )
        if scaffolded:
            # Re-read README so the architect sees any scaffold-added README
            # content (currently a no-op since the fixture doesn't touch README,
            # but keeps the contract clean for future framework adapters).
            readme = await self.gh.read_file(owner, repo, "README.md") or readme

        # Auto-discover docs — scan root + docs/ folder, read all .md files up to 3 levels deep
        docs_content = ""
        docs_files_read: list[str] = []
        root_entries = await self.gh.list_directory(owner, repo, "")
        docs_entries = await self.gh.list_directory(owner, repo, "docs")
        # Root-level .md files (excluding README, already captured above)
        for entry in root_entries:
            if entry.get("type") == "file" and entry["name"].endswith(".md") and entry["name"] != "README.md":
                text = await self.gh.read_file(owner, repo, entry["path"]) or ""
                if text:
                    docs_content += f"\n\n---\n## {entry['name']}\n\n{text[:8000]}"
                    docs_files_read.append(entry["path"])
        for entry in docs_entries:
            if entry.get("type") == "file" and entry["name"].endswith(".md"):
                text = await self.gh.read_file(owner, repo, entry["path"]) or ""
                if text:
                    docs_content += f"\n\n---\n## {entry['name']}\n\n{text[:8000]}"
                    docs_files_read.append(entry["path"])
            elif entry.get("type") == "dir":
                sub_entries = await self.gh.list_directory(owner, repo, entry["path"])
                for sub in sub_entries:
                    if sub.get("type") == "file" and sub["name"].endswith(".md"):
                        text = await self.gh.read_file(owner, repo, sub["path"]) or ""
                        if text:
                            docs_content += f"\n\n---\n## {sub['path']}\n\n{text[:4000]}"
                            docs_files_read.append(sub["path"])
                    elif sub.get("type") == "dir":
                        # One more level (e.g. docs/user-stories/E-001/)
                        deep_entries = await self.gh.list_directory(owner, repo, sub["path"])
                        for deep in deep_entries:
                            if deep.get("type") == "file" and deep["name"].endswith(".md"):
                                text = await self.gh.read_file(owner, repo, deep["path"]) or ""
                                if text:
                                    docs_content += f"\n\n---\n## {deep['path']}\n\n{text[:2000]}"
                                    docs_files_read.append(deep["path"])

        extra_context = docs_content

        # Read stack files so the LLM knows exact libraries, components, and patterns
        pkg_json = await self.gh.read_file(owner, repo, "package.json") or ""
        tw_config = (
            await self.gh.read_file(owner, repo, "tailwind.config.ts")
            or await self.gh.read_file(owner, repo, "tailwind.config.js")
            or ""
        )
        components_json = await self.gh.read_file(owner, repo, "components.json") or ""  # shadcn config

        stack_context = ""
        if pkg_json:
            stack_context += f"\n\n## package.json\n```json\n{pkg_json[:3000]}\n```"
        if tw_config:
            stack_context += f"\n\n## tailwind.config\n```js\n{tw_config[:2000]}\n```"
        if components_json:
            stack_context += f"\n\n## components.json (shadcn/ui config)\n```json\n{components_json[:1000]}\n```"

        context_parts = []
        if readme:
            context_parts.append("README.md")
        context_parts.extend(docs_files_read)
        if pkg_json:
            context_parts.append("package.json")
        if tw_config:
            context_parts.append("tailwind.config")
        if components_json:
            context_parts.append("components.json")
        await self._log(
            "Repository context loaded",
            ", ".join(context_parts) if context_parts else "No README found",
            "SUCCESS" if context_parts else "AMBIGUITY",
        )

        # Fetch optional Figma design context (from URLs)
        figma_context = ""
        figma_urls = project.get("figma_urls") or []
        if figma_urls and settings.figma_access_token:
            await self._log("Fetching Figma design context", f"{len(figma_urls)} URL(s)", "RUNNING")
            figma_context = await _fetch_figma_context(figma_urls, settings.figma_access_token)
            await self._log("Figma context loaded", f"{len(figma_context)} chars", "INFO")
        elif figma_urls and not settings.figma_access_token:
            await self._log("Figma URLs present but FIGMA_ACCESS_TOKEN not set", "", "AMBIGUITY")

        # Append Figma file context if a .fig was uploaded
        figma_file_context = (project.get("figma_file_context") or "").strip()
        if figma_file_context:
            figma_context = (figma_context + "\n\n" + figma_file_context).strip()
            await self._log("Figma file context included", f"{len(figma_file_context)} chars", "INFO")

        # Reuse the schema we already introspected pre-scaffold (or run it now if
        # the pre-scaffold step was skipped because creds were absent and only
        # the scaffold added them). Abort loudly when Supabase deps are in
        # package.json but the schema is unreadable — silently letting the
        # architect proceed with no schema is what caused the in-flight project
        # to invent a 3-migration fictional data layer.
        supabase_schema: SupabaseSchema | None = self._cached_schema
        supabase_url = (project.get("supabase_url") or "").strip()
        supabase_key = (project.get("supabase_service_role_key") or "").strip()
        supabase_in_pkg = "@supabase/supabase-js" in pkg_json or "@supabase/ssr" in pkg_json
        if supabase_schema is None and supabase_url and supabase_key:
            await self._log("Introspecting Supabase schema", supabase_url, "RUNNING")
            supabase_schema = await _introspect_supabase_schema(supabase_url, supabase_key)
            if supabase_schema.ok:
                self._cached_schema = supabase_schema
                self._schema_cached_at = asyncio.get_event_loop().time()
                await self._log(
                    "Supabase schema loaded",
                    f"{len(supabase_schema.tables)} tables, {len(supabase_schema.markdown)} chars",
                    "SUCCESS",
                )

        if supabase_in_pkg and (supabase_schema is None or not supabase_schema.ok):
            err = (
                supabase_schema.error if supabase_schema and supabase_schema.error
                else "Supabase credentials not set on the project"
            )
            await self._log(
                "Supabase schema unavailable — planning ABORTED",
                f"{err}\n\nThe project's package.json includes @supabase/supabase-js but the schema "
                f"could not be read. Refusing to let the architect invent a fictional schema. "
                f"Fix the supabase_url + supabase_service_role_key on the project (the URL should be "
                f"`https://<ref>.supabase.co`, not the database URL), then re-run planning.",
                "ERROR",
            )
            await emit_error(
                self.project_id,
                f"Schema introspection failed and Supabase is a project dependency. "
                f"Planning aborted to prevent generating fictional schema. Detail: {err}",
            )
            return

        if supabase_schema and not supabase_schema.ok:
            # Creds set, introspection failed, but no Supabase deps → soft warn,
            # don't inject error string into the prompt.
            supabase_schema = None

        # Stack-specific best-practice skills from skills.sh / GitHub
        from orchestrator.skills import detect_skills, build_skill_context
        skill_ids = detect_skills(pkg_json, tw_config, components_json)
        skill_context = await build_skill_context(skill_ids, "Best-practice guidelines for this stack")
        if skill_ids:
            await self._log("Skills loaded", ", ".join(skill_ids), "INFO")

        system_prompt = _load_prompt("architect_github_v1.txt")
        user_msg = (
            f"Repository: {owner}/{repo}\n\n"
            f"## README\n\n{readme}{extra_context}\n\n"
        )
        if stack_context:
            user_msg += f"## Tech Stack Files{stack_context}\n\n"
        if skill_context:
            user_msg += f"{skill_context}\n\n"
        if supabase_schema and supabase_schema.ok:
            user_msg += f"## Live Supabase Schema\n\n{supabase_schema.markdown}\n\n"
        if figma_context:
            user_msg += f"## Figma Design Context\n\n{figma_context}\n\n"
        user_msg += "Produce the architecture document, stories document, and issue plan now."

        llm_cfg = await self._llm_config()
        model = llm_cfg["architect"]["model"]

        await self._log("Architect LLM generating plan", f"Model: {model}", "RUNNING")

        # Stream tokens to UI
        full_response = ""
        async for chunk in call_llm_streaming(
            [SystemMessage(content=system_prompt), HumanMessage(content=user_msg)],
            model=model,
            max_tokens=32768,
            trace_context={**self._trace_ctx, "actor": "architect", "prompt_name": "architect_github_v1"},
        ):
            full_response += chunk
            await emit_architect_chunk(self.project_id, chunk)

        # Parse blocks
        architecture_md = _parse_tagged_block(full_response, "markdown:architecture") or ""
        stories_md = _parse_tagged_block(full_response, "markdown:stories") or ""
        issue_plan_raw = _parse_tagged_block(full_response, "json:issue_plan") or "{}"

        try:
            issue_plan = json.loads(issue_plan_raw)
        except json.JSONDecodeError:
            logger.warning(
                "architect: json:issue_plan block not parseable — response length=%d, block_raw=%r",
                len(full_response), issue_plan_raw[:200],
            )
            issue_plan = {}

        # Build a human-readable plan_md from the issue plan
        plan_md = _build_plan_md(issue_plan, architecture_md, stories_md)

        epics = len(issue_plan.get("epics", []))
        tasks = _count_tasks(issue_plan)
        await self._log(
            "Plan generated — awaiting your approval",
            f"{epics} epics · {tasks} tasks",
            "SUCCESS",
        )

        await update_project(
            self.project_id,
            plan_md=plan_md,
            issue_plan_json=json.dumps(issue_plan),
            project_stage="awaiting_plan_approval",
            status="planning",
        )

        await emit_architect_message(self.project_id, plan_md, streaming=False)
        await emit_status_update(
            self.project_id,
            status="planning",
            stage="awaiting_plan_approval",
            progress={"completed": 0, "total": _count_tasks(issue_plan)},
        )
        await emit_human_required(
            self.project_id,
            "Review the plan and approve to create GitHub Issues.",
            stage="awaiting_plan_approval",
        )

        await trace_event(
            self.project_id, "architect", "architect.run.completed",
            {"epics": len(issue_plan.get("epics", []))},
        )

    # ── Stage 2: Push docs + create milestones + issues ───────────────────────

    async def apply_plan(self) -> None:
        """Push docs to repo, create GitHub Milestones and Issues."""
        project = await self._project()
        owner = project["github_repo_owner"]
        repo = project["github_repo_name"]
        issue_plan_raw = project.get("issue_plan_json") or project.get("execution_contract_json") or "{}"
        issue_plan = json.loads(issue_plan_raw) if isinstance(issue_plan_raw, str) else issue_plan_raw

        # Build stack summary + best-practice skill context to embed in every issue body
        stack_summary = await _build_stack_summary(self.gh, owner, repo)
        from orchestrator.skills import detect_skills, build_skill_context
        _pkg = await self.gh.read_file(owner, repo, "package.json") or ""
        _tw = (
            await self.gh.read_file(owner, repo, "tailwind.config.ts")
            or await self.gh.read_file(owner, repo, "tailwind.config.js")
            or ""
        )
        _comp = await self.gh.read_file(owner, repo, "components.json") or ""
        skill_ids = detect_skills(_pkg, _tw, _comp)
        skill_context = await build_skill_context(skill_ids, "Best-practice context (apply these patterns)")

        if not issue_plan.get("epics"):
            raise RuntimeError("No issue plan found — re-run planning first")

        self._set_trace("apply_plan", "orchestrator")
        await trace_event(self.project_id, "orchestrator", "apply_plan.started", {})

        await update_project(self.project_id, project_stage="building", status="building")
        await self._log("Plan approved — starting issue creation", f"{owner}/{repo}")

        # Push architecture + stories docs
        architecture_md = _parse_tagged_block(project.get("plan_md", ""), "markdown:architecture") or ""
        stories_md = _parse_tagged_block(project.get("plan_md", ""), "markdown:stories") or ""

        # Re-read from plan_md if parsing failed (plan_md is the rendered summary)
        # Fall back: read directly from DB columns if available
        if not architecture_md:
            architecture_md = project.get("architecture_md", "")
        if not stories_md:
            stories_md = project.get("stories_md", "")

        default_branch = project.get("default_branch") or "main"

        docs_pushed = []
        for path, content, label in [
            ("docs/ARCHITECTURE.md", architecture_md, "architecture"),
            ("docs/STORIES.md", stories_md, "stories"),
        ]:
            if content:
                try:
                    await self.gh.push_file(
                        owner, repo, path, content,
                        f"docs: add {label} by Automatron",
                        branch=default_branch,
                    )
                    docs_pushed.append(path)
                except Exception as exc:
                    logger.warning("Could not push %s: %s", path, exc)

        if docs_pushed:
            await self._log("Docs pushed to repository", ", ".join(docs_pushed), "SUCCESS")

        # Create milestones and issues
        total = _count_tasks(issue_plan)
        created = 0
        skipped = 0
        # Idempotency: snapshot the issues already created for this project so a re-run
        # (repeat approval, or a retry after a partial failure) creates only the MISSING
        # issues instead of duplicating the whole plan. Keyed on the local DB (complete +
        # project-scoped), matching the (epic, story, title) tuple we persist per task.
        _seen_keys = {
            (i.get("epic"), i.get("story"), i.get("title"))
            for i in await list_github_issues(self.project_id)
        }
        await self._log("Creating GitHub milestones and issues", f"{total} tasks planned")

        for epic in issue_plan.get("epics", []):
            epic_title = epic.get("title", "Untitled Epic")
            milestone_number: int | None = None
            try:
                milestone_number = await self.gh.create_milestone(
                    owner, repo, epic_title, epic.get("description", "")
                )
                await self._log(f"Milestone: {epic_title}", f"#{milestone_number}", "INFO")
            except Exception as exc:
                logger.warning("Could not create milestone '%s': %s", epic_title, exc)
                await self._log(f"Milestone skipped: {epic_title}", str(exc), "AMBIGUITY")

            # Ensure story label exists (GitHub caps label names at 50 chars)
            for story in epic.get("stories", []):
                story_title = story.get("title", "")
                if story_title:
                    label_name = story_title[:50]
                    try:
                        await self.gh.ensure_label(owner, repo, label_name, "bfd4f2")
                    except Exception as exc:
                        logger.warning("Could not ensure label '%s': %s", label_name, exc)

                for task in story.get("tasks", []):
                    task_title = task.get("title", "Untitled Task")
                    dedup_key = (epic_title, story_title or None, task_title)
                    if dedup_key in _seen_keys:
                        skipped += 1
                        await self._log(f"Issue exists, skipping: {task_title}", epic_title, "INFO")
                        continue
                    # Skill content stays in the architect prompt (and reviewer), NOT in
                    # the issue body — the builder gets the spec the architect produced, and
                    # the raw skill prose would only inflate input tokens at implementation time.
                    body = _render_issue_body(task, epic_title, story_title, stack_summary)
                    # GitHub label names are capped at 50 chars
                    label_name = story_title[:50] if story_title else ""
                    labels = [label_name] if label_name else []

                    try:
                        try:
                            gh_issue = await self.gh.create_issue(
                                owner, repo,
                                title=task_title,
                                body=body,
                                milestone_number=milestone_number,
                                labels=labels,
                                assignees=["copilot"],
                            )
                        except Exception:
                            try:
                                # Copilot agent not enabled on this repo — create without assignee
                                gh_issue = await self.gh.create_issue(
                                    owner, repo,
                                    title=task_title,
                                    body=body,
                                    milestone_number=milestone_number,
                                    labels=labels,
                                )
                            except Exception:
                                # Milestone or label invalid — bare create
                                gh_issue = await self.gh.create_issue(
                                    owner, repo,
                                    title=task_title,
                                    body=body,
                                )
                        issue_number = gh_issue["number"]
                        cw_url = gh_issue["html_url"]
                        await create_github_issue(
                            str(uuid.uuid4()),
                            self.project_id,
                            issue_number,
                            task_title,
                            epic=epic_title,
                            story=story_title or None,
                            copilot_workspace_url=cw_url,
                        )
                        created += 1
                        _seen_keys.add(dedup_key)
                        await self._log(
                            f"Issue #{issue_number}: {task_title}",
                            epic_title,
                            "SUCCESS",
                        )
                    except Exception as exc:
                        logger.error("Failed to create issue '%s': %s", task_title, exc)
                        await self._log(f"Failed: {task_title}", str(exc), "BLOCKER")

        await self._log(
            f"{created}/{total} issues created on GitHub"
            + (f" ({skipped} already existed)" if skipped else ""),
            "Assign to Copilot to start building" if (created or skipped) else "Check errors above",
            "SUCCESS" if created + skipped == total else "AMBIGUITY",
        )

        issues = await list_github_issues(self.project_id)
        await emit_issues_updated(self.project_id, issues)
        await emit_status_update(
            self.project_id,
            status="building",
            stage="building",
            progress={"completed": 0, "total": total},
        )
        await trace_event(
            self.project_id, "orchestrator", "apply_plan.completed",
            {"issues_created": created, "total_tasks": total},
        )

    # ── Stage 3: Sync issue/PR status from GitHub ─────────────────────────────

    async def sync_issues(self) -> None:
        """Poll GitHub for issue/PR state and update the local DB; import new issues."""
        project = await self._project()
        owner = project.get("github_repo_owner")
        repo = project.get("github_repo_name")
        if not owner or not repo:
            return

        # Fetch all issues from GitHub (open + closed)
        gh_issues = await self.gh.list_issues(owner, repo, state="all")
        gh_map = {i["number"]: i for i in gh_issues}

        local_issues = await list_github_issues(self.project_id)
        issue_map = {i["issue_number"]: i for i in local_issues}

        # Import any GitHub issue not yet tracked in Automatron
        for number, gh in gh_map.items():
            if number not in issue_map:
                # Derive epic from first label, fall back to "General"
                labels = [lbl["name"] for lbl in gh.get("labels") or []]
                epic = labels[0] if labels else "General"
                await create_github_issue(
                    str(uuid.uuid4()),
                    self.project_id,
                    number,
                    gh["title"],
                    epic=epic,
                    copilot_workspace_url=gh["html_url"],
                )
                issue_map[number] = {
                    "issue_number": number,
                    "title": gh["title"],
                    "status": "open" if gh["state"] == "open" else "closed",
                    "pr_number": None,
                }

        # Update status of all tracked issues
        for number, local in issue_map.items():
            gh = gh_map.get(number)
            if not gh:
                continue

            new_status = local["status"]

            if gh["state"] == "closed":
                new_status = "merged" if local.get("pr_number") else "closed"
            elif local["status"] == "open":
                # Check for a linked PR if we don't already know about one
                if not local.get("pr_number"):
                    pr = await self.gh.find_pr_for_issue(owner, repo, number)
                    if pr:
                        await update_github_issue_pr(
                            self.project_id, number,
                            pr["number"], pr["html_url"],
                            status="pr_open",
                        )
                        new_status = "pr_open"
                        continue

            if new_status != local["status"]:
                await update_github_issue_status(self.project_id, number, new_status)

        issues = await list_github_issues(self.project_id)
        done = sum(1 for i in issues if i["status"] in ("merged", "closed"))
        await emit_issues_updated(self.project_id, issues)
        await emit_status_update(
            self.project_id,
            status="building",
            stage="building",
            progress={"completed": done, "total": len(issues)},
        )

        # If all issues are done and we don't have a preview URL yet, auto-detect from GitHub Deployments
        project = await self._project()
        if done == len(issues) and done > 0 and not project.get("preview_url"):
            await self._detect_and_set_preview_url(owner, repo)

    async def _detect_and_set_preview_url(self, owner: str, repo: str) -> None:
        """Clone the repo locally and run it in Docker for a live preview."""
        from orchestrator.preview import run_preview_locally
        await self._log("Starting local preview", f"Cloning and building {owner}/{repo}", "RUNNING")
        preview_url = await run_preview_locally(str(self.project_id), owner, repo)
        if preview_url:
            await update_project_preview(self.project_id, preview_url, "ready")
            await self._log("Preview ready", preview_url, "SUCCESS")
            await emit_status_update(
                self.project_id,
                status="building",
                stage="building",
                progress={},
                preview_url=preview_url,
            )
        else:
            await self._log("Preview failed", "Could not build or run the project locally", "ERROR")

    # ── Stage 4: AI review a PR ───────────────────────────────────────────────

    async def _collect_imported_signatures(
        self,
        owner: str,
        repo: str,
        diff: str,
        diff_file_paths: list[str],
    ) -> str:
        """For every relative/alias import that appears in the diff, fetch the imported file
        and extract its top-level export signatures. Lets the reviewer catch wrong call sites
        and missing exports without trusting the LLM to remember signatures across files.
        """
        # Match: import { x, y as z } from "@/lib/auth"  OR  from "./utils"
        import_re = re.compile(
            r'^\+?\s*import\s+(?:\*\s+as\s+\w+|\w+(?:\s*,\s*\{[^}]+\})?|\{[^}]+\})?\s*'
            r'from\s+["\']([^"\']+)["\']',
            re.MULTILINE,
        )
        sources: set[str] = set()
        for m in import_re.finditer(diff):
            src = m.group(1)
            # Skip bare npm packages — we only want first-party files
            if src.startswith(("@/", "./", "../")) or src.startswith("~/"):
                sources.add(src)

        if not sources:
            return ""

        # Resolve each import to a candidate file path
        def _resolve(src: str, from_file: str | None) -> list[str]:
            if src.startswith("@/"):
                base = "src/" + src[2:]
            elif src.startswith("~/"):
                base = "src/" + src[2:]
            elif src.startswith(("./", "../")) and from_file:
                # posixpath.normpath handles ./ and ../ without touching the
                # filesystem. PurePosixPath has no .resolve() (that's only on
                # the concrete Path class) — using it raises AttributeError.
                import posixpath
                from_dir = posixpath.dirname(from_file)
                base = posixpath.normpath(posixpath.join(from_dir, src)).lstrip("/")
                # Block paths that escape the repo root (e.g. ../../etc/passwd)
                if base.startswith("../") or base == "..":
                    return []
            else:
                return []
            # Try common extensions / index files
            return [
                base, f"{base}.ts", f"{base}.tsx", f"{base}.js", f"{base}.jsx",
                f"{base}/index.ts", f"{base}/index.tsx", f"{base}/index.js",
            ]

        # Map each source to the first file in the diff that imported it (for relative resolution)
        # Cheap heuristic: try resolving from each touched file until one resolves
        sigs_by_file: dict[str, str] = {}
        for src in sources:
            candidates: list[str] = []
            for diff_fp in diff_file_paths or [None]:
                candidates.extend(_resolve(src, diff_fp))
            # Dedupe, keep order
            seen: set[str] = set()
            uniq = [c for c in candidates if not (c in seen or seen.add(c))]
            for cand in uniq[:7]:
                if cand in sigs_by_file:
                    break
                try:
                    content = await self.gh.read_file(owner, repo, cand)
                except Exception:
                    content = None
                if content is None:
                    continue
                # Extract top-level export signatures (lightweight regex)
                sig_lines = re.findall(
                    r'^export\s+(?:async\s+)?(?:function|class|const|let|var|interface|type|enum)\s+\w+[^{=;\n]*',
                    content,
                    re.MULTILINE,
                )
                # ALSO extract cva variant keys when the file uses class-variance-authority.
                # Without this, a Badge / Button component that defines a `success` variant
                # is invisible to the reviewer — the regex above only catches the wrapping
                # interface, not the variant strings inside cva(). The reviewer then falls
                # back to "standard shadcn has 4 variants" priors and flags valid usage as
                # a build error. (Real false-positive observed on PR #111, issue #69.)
                variant_lines: list[str] = []
                cva_match = re.search(
                    r'cva\s*\([^,]+,\s*\{\s*variants\s*:\s*\{\s*variant\s*:\s*\{([^}]+)\}',
                    content,
                    re.DOTALL,
                )
                if cva_match:
                    keys = re.findall(r'(\w+)\s*:', cva_match.group(1))
                    if keys:
                        variant_lines.append(
                            f"// cva variant keys available: {', '.join(keys)}"
                        )
                combined_lines = [*(s.strip() for s in sig_lines[:15]), *variant_lines]
                if combined_lines:
                    sigs_by_file[cand] = "\n".join(combined_lines)
                break

        if not sigs_by_file:
            return ""
        sections = [f"### `{path}`\n```ts\n{sigs}\n```" for path, sigs in sigs_by_file.items()]
        return (
            "\n\n---\n\n## Imported file signatures (for cross-file consistency check)\n\n"
            + "\n\n".join(sections)
        )

    @staticmethod
    def _collect_supabase_references(diff: str) -> list[tuple[str, str, list[str]]]:
        """Extract Supabase data references from a unified diff for schema validation.

        Returns a list of (kind, table, columns) tuples where kind is one of
        "select" (`.from("t").select("a,b")`) or "insert" (raw SQL insert).
        Columns is the parsed column list — `["*"]` for `select("*")`, empty
        list when the column projection isn't parseable.

        Only inspects ADDED lines (`+` prefixed in the diff) — drift introduced
        in this PR. Existing fictional references on the base branch are NOT
        flagged here (they were the architect's job to prevent).
        """
        refs: list[tuple[str, str, list[str]]] = []
        added_lines = [
            line[1:] for line in diff.splitlines()
            if line.startswith("+") and not line.startswith("+++")
        ]
        added_text = "\n".join(added_lines)

        # Pattern A: .from("table").select("col1, col2, ...") — JS client style
        # Handles single + double quotes; chained calls across one or two lines.
        select_re = re.compile(
            r'''\.from\(\s*["']([^"']+)["']\s*\)         # .from("table")
                (?:\s*\.\w+\([^)]*\))*?                    # zero+ chained method calls
                \s*\.select\(\s*["']([^"']*)["']            # .select("col1,col2,...")
            ''',
            re.VERBOSE | re.DOTALL,
        )
        for m in select_re.finditer(added_text):
            table = m.group(1)
            cols_raw = m.group(2).strip()
            # PostgREST select syntax: "col1,col2,rel(col3,col4),*"
            # We only check top-level columns; nested rel() projections are
            # FK joins and validated separately by their referenced table.
            cols: list[str] = []
            depth = 0
            current = []
            for ch in cols_raw:
                if ch == "(":
                    depth += 1
                elif ch == ")":
                    depth -= 1
                elif ch == "," and depth == 0:
                    if current:
                        cols.append("".join(current).strip())
                        current = []
                    continue
                current.append(ch)
            if current:
                cols.append("".join(current).strip())
            # Strip aliases / renames: "name:full_name" → "full_name"
            cleaned = []
            for c in cols:
                if ":" in c:
                    c = c.split(":")[-1].strip()
                # Strip JSON / order operators like name->first
                c = c.split("->")[0].split("(")[0].strip()
                if c and c != "*":
                    cleaned.append(c)
            refs.append(("select", table, cleaned))

        # Pattern B: .from("t").insert({...}) / .update({...}) — keys are columns
        write_re = re.compile(
            r'''\.from\(\s*["']([^"']+)["']\s*\)
                (?:\s*\.\w+\([^)]*\))*?
                \s*\.(insert|update|upsert)\(\s*(\{[^}]*\})
            ''',
            re.VERBOSE | re.DOTALL,
        )
        for m in write_re.finditer(added_text):
            table = m.group(1)
            obj_src = m.group(3)
            # Pull bareword/quoted property keys: { foo: 1, "bar": 2 }
            keys = re.findall(r'(?:^|[,{])\s*["\']?(\w+)["\']?\s*:', obj_src)
            refs.append(("write", table, [k for k in keys if k]))

        # Pattern C: raw SQL INSERT INTO table (a, b) VALUES — for migration / raw paths
        sql_re = re.compile(
            r'INSERT\s+INTO\s+["`]?(\w+)["`]?\s*\(\s*([^)]+)\)',
            re.IGNORECASE,
        )
        for m in sql_re.finditer(added_text):
            table = m.group(1)
            cols = [c.strip().strip('"`') for c in m.group(2).split(",") if c.strip()]
            refs.append(("write", table, cols))

        return refs

    @staticmethod
    def _check_schema_violations(
        refs: list[tuple[str, str, list[str]]],
        schema: SupabaseSchema,
    ) -> list[str]:
        """Compare extracted refs against the schema; return human-readable violation lines.

        Returns the list of violations to inject into the reviewer prompt as
        CONFIRMED issues. Each line names the kind, table, and column.
        Unknown tables produce one entry; known tables with unknown columns
        produce one entry per missing column.
        """
        violations: list[str] = []
        seen: set[str] = set()  # dedupe — diffs often repeat the same query
        for kind, table, cols in refs:
            if table not in schema.tables:
                key = f"table:{table}"
                if key in seen:
                    continue
                seen.add(key)
                violations.append(
                    f"- Unknown table `{table}` referenced in a `{kind}` query. "
                    f"Not in the live schema. (Available tables: "
                    f"{', '.join(sorted(schema.tables.keys())[:10])}"
                    f"{'...' if len(schema.tables) > 10 else ''})"
                )
                continue
            known = set(schema.tables[table])
            # For writes, validate against writable cols (excludes readOnly).
            writable = set(schema.writable_cols.get(table, schema.tables[table]))
            target_cols = writable if kind == "write" else known
            for col in cols:
                if col not in target_cols:
                    key = f"col:{table}.{col}"
                    if key in seen:
                        continue
                    seen.add(key)
                    if col in known and kind == "write":
                        violations.append(
                            f"- `{table}.{col}` is read-only in the live schema; "
                            f"cannot be used in `{kind}`."
                        )
                    else:
                        violations.append(
                            f"- Unknown column `{table}.{col}` in a `{kind}` query. "
                            f"Did you mean one of: "
                            f"{', '.join(sorted(known)[:8])}"
                            f"{'...' if len(known) > 8 else ''}?"
                        )
        return violations

    async def review_pr(self, issue_number: int, pr_number: int) -> None:
        """Fetch the PR diff, call the reviewer LLM, post a comment, store the result."""
        project = await self._project()
        owner = project["github_repo_owner"]
        repo = project["github_repo_name"]

        self._set_trace("pr_review", "reviewer")
        await self._log(f"Reviewing PR #{pr_number}", f"Issue #{issue_number} · {owner}/{repo}")

        # Short-circuit if we already reviewed this exact PR head SHA. Every PR
        # `opened`/`reopened` webhook fires a fresh review, but a chain of
        # auto-pushes to the same PR (re-impl loop, retry path, etc.) can fire
        # the webhook 3-5 times in a row with NO code change between firings.
        # Re-running the reviewer LLM each time burns expensive tokens for
        # identical output. Cache by head SHA.
        head_sha = await self.gh.get_pr_head_sha(owner, repo, pr_number)
        if head_sha:
            from orchestrator.models.project import _get_github_issue
            existing_local = await _get_github_issue(self.project_id, issue_number)
            existing_review = (existing_local or {}).get("pr_review") or {}
            if existing_review.get("head_sha") == head_sha:
                await self._log(
                    f"Reviewer: PR #{pr_number} head unchanged — skipping",
                    f"head_sha {head_sha[:8]} matches the last review on record",
                    "INFO",
                )
                return

        # Get diff and task spec (issue body)
        diff = await self.gh.get_pr_diff(owner, repo, pr_number)
        issue = await self.gh.get_issue(owner, repo, issue_number)
        issue_body = issue.get("body", "")

        llm_cfg = await self._llm_config()
        model = llm_cfg["reviewer"]["model"]
        await self._log(
            f"Running reviewer LLM on PR #{pr_number}",
            f"Model: {model} · diff {len(diff)} chars",
            "RUNNING",
        )

        system_prompt = (
            "You are a code reviewer. You will receive a GitHub issue (the task specification), "
            "the PR diff that implements it, and the signatures of files the diff imports from.\n\n"
            "Review the diff against the acceptance criteria and implementation notes. "
            "Additionally: confirm every call site in the diff matches the signature of the function "
            "it calls (argument count and types), and confirm every imported symbol actually exists in "
            "the referenced module.\n\n"
            "## Next.js route group equivalence\n"
            "When evaluating Next.js App Router file paths, treat `app/(group)/X/page.tsx` and "
            "`app/X/page.tsx` as **equivalent routes** — they both resolve to `/X`. If the spec "
            "specified one form and the implementation used the other, note it under \"What was done "
            "well\" (the layout from `(group)/layout.tsx` will only wrap the in-group form), do NOT "
            "flag it under Issues. The same equivalence applies to sibling files like `actions.ts`, "
            "`loading.tsx`, `error.tsx` that live next to the page. Exception: if BOTH copies exist "
            "on disk simultaneously, that's a duplicate-route build error — flag THAT.\n\n"
            "## What counts as an Issue\n"
            "The Issues section is ONLY for things you have CONFIRMED are broken — real bugs, "
            "signature mismatches, missing required files, failed acceptance criteria, type errors "
            "you can verify from the imported signatures.\n"
            "Do NOT put under Issues:\n"
            "- **Verifications that passed.** If an import is valid, a signature matches, a "
            "criterion is met — put it under \"What was done well\" or omit it. A bullet that ends "
            "in ✓ or contains \"this is correct / valid / matches / satisfies\" does NOT belong in Issues.\n"
            "- **Speculative concerns** (\"may complain\", \"could fail\", \"potentially\", "
            "\"depending on version\", \"if X is not typed as Y…\"). Either verify the concern using "
            "the imported signatures and stack files OR omit it. Unverified hedged concerns are noise.\n"
            "- **Component variant claims you can't verify.** When the diff uses something like "
            "`variant=\"success\"` on a UI component (Badge, Button, Alert, etc.), DO NOT flag it as "
            "an invalid variant unless the imported file's signatures explicitly contradict it. "
            "Look for a `// cva variant keys available: ...` comment in the imported signatures — "
            "that's the canonical list. If `success` is listed, the usage is valid. If no variant "
            "list is shown (e.g. the component doesn't use cva, or the variants couldn't be parsed), "
            "DO NOT assert what the variants are from prior knowledge of shadcn/MUI/etc. — omit the "
            "concern. Standard-library variant priors are NOT a verification source.\n"
            "- **Additive code that doesn't break anything** (extra test cases, extra exports the "
            "spec didn't ask for). Note them under \"What was done well\" if worth mentioning.\n"
            "- **Stylistic preferences** unless they violate stated implementation_notes.\n\n"
            "## Pass/fail rules — strict\n"
            "- Output **PASSED** ONLY if every acceptance criterion is met AND there are no correctness "
            "issues to list under the Issues section.\n"
            "- If ANY acceptance criterion is partially met, missed, or skipped — output **ISSUES FOUND**.\n"
            "- If ANY call site, import, or signature is wrong — output **ISSUES FOUND**.\n"
            "- You cannot output PASSED while also listing concrete issues. The two are mutually exclusive.\n\n"
            "Respond with:\n"
            "1. **PASSED** or **ISSUES FOUND** on the first line\n"
            "2. A brief summary (2-3 sentences)\n"
            "3. Bullet list of specific issues (if any), referencing file paths and line numbers\n"
            "4. Bullet list of what was done well\n\n"
            "Be concise. Focus on correctness and completeness vs the spec, not style."
        )

        # Extract paths the diff touches so relative imports resolve correctly
        diff_file_paths = re.findall(r'^\+\+\+ b/(.+)$', diff, re.MULTILINE)
        imported_sigs = await self._collect_imported_signatures(owner, repo, diff, diff_file_paths)

        # Programmatic Supabase schema check — catches what the architect/builder
        # might have missed (e.g. fictional `profiles.is_super_admin`). When
        # confirmed violations exist, prepend them to the user message so the
        # reviewer's "ISSUES FOUND when concrete issues exist" rule fires
        # deterministically — no LLM judgment, no false positives.
        schema_violations_block = ""
        schema_for_review = await self.get_supabase_schema()
        if schema_for_review and schema_for_review.ok:
            refs = self._collect_supabase_references(diff)
            violations = self._check_schema_violations(refs, schema_for_review)
            if violations:
                schema_violations_block = (
                    "\n\n---\n\n## CONFIRMED schema violations (do NOT contradict)\n\n"
                    "The following references in this PR do not match the live Supabase schema. "
                    "These are mechanical, verified violations — they MUST appear under your "
                    "Issues section verbatim. Output ISSUES FOUND.\n\n"
                    + "\n".join(violations)
                )
                await self._log(
                    f"Reviewer: {len(violations)} schema violation(s) on PR #{pr_number}",
                    "\n".join(violations),
                    "BLOCKER",
                )

        # Stack-specific best-practice skills — give the reviewer the same context the
        # architect used so it can flag violations of those guidelines.
        from orchestrator.skills import detect_skills, build_skill_context
        pkg_json = await self.gh.read_file(owner, repo, "package.json") or ""
        tw_config = (
            await self.gh.read_file(owner, repo, "tailwind.config.ts")
            or await self.gh.read_file(owner, repo, "tailwind.config.js")
            or ""
        )
        components_json = await self.gh.read_file(owner, repo, "components.json") or ""
        skill_ids = detect_skills(pkg_json, tw_config, components_json)
        skill_context = await build_skill_context(skill_ids, "Stack best-practice guidelines (flag any diff that violates these)")

        user_msg = (
            f"## Issue #{issue_number}: {issue.get('title', '')}\n\n"
            f"{issue_body}\n\n"
            f"---\n\n## PR Diff\n\n```diff\n{diff[:80000]}\n```"
            f"{imported_sigs}"
            f"{schema_violations_block}"
        )
        if skill_context:
            user_msg += f"\n\n---\n\n{skill_context}"

        try:
            # Hard timeout so a hung LLM provider can't lock the issue in
            # "reviewing" forever. 3 minutes is generous for a 2k-output review.
            review_text = await asyncio.wait_for(
                call_llm(
                    [SystemMessage(content=system_prompt), HumanMessage(content=user_msg)],
                    model=model,
                    max_tokens=2048,
                    trace_context={**self._trace_ctx, "actor": "reviewer"},
                ),
                timeout=180,
            )
        except asyncio.TimeoutError:
            await self._log(f"PR #{pr_number} review timed out", "LLM call exceeded 180s", "ERROR")
            await emit_error(self.project_id, f"PR review timed out after 3 min for PR #{pr_number}")
            return
        except Exception as exc:
            await emit_error(self.project_id, f"PR review failed: {exc}")
            return

        passed = review_text.strip().upper().startswith("PASSED")

        # Safety net works both directions:
        # (a) Model wrote PASSED but listed real issues → override to ISSUES FOUND.
        # (b) Model wrote ISSUES FOUND but every bullet is a ✓-confirmed verification
        #     or a hedged speculative concern → override to PASSED.
        # Compute the substantive-issue count once and decide.
        #
        # The outer regex REQUIRES a real markdown heading prefix (`## ` or `**`).
        # Without that requirement, the regex falsely anchors on the verdict line
        # `ISSUES FOUND` (case-insensitive `Issues found`) and captures the summary
        # paragraph, finds zero bullets, and the (b) branch wrongly flips to PASSED.
        # `Issues found` is also dropped from the alternatives — it's the verdict, not a heading.
        issues_section = re.search(
            r'(?:^|\n)(?:##\s+|\*\*\s*)(?:Issues|Problems|Concerns)\b\*?\*?[:\s]*\n((?:.|\n)*?)(?=\n\s*(?:##|\*\*(?:Strengths|What was done well|Praise))|$)',
            review_text,
            re.IGNORECASE,
        )
        bullets: list[str] = []
        substantive: list[str] = []
        if issues_section:
            bullets = re.findall(r'^\s*[-*]\s+(.+)$', issues_section.group(1), re.MULTILINE)
            # Patterns that mark a bullet as a ✓-confirmed verification, not an issue
            _confirm_pat = re.compile(
                r'(?:[✓✔☑]|\bthis is correct\b|\bis valid\b|\bmatches the spec\b|'
                r'\bsatisfies the criterion\b|\bis correctly\b|\bcorrectly handles\b|'
                r'\bcorrectly implements\b|\bare correctly defined\b|\bmatch the spec\b|'
                r'\bis present\b|\bis fine\b|\bok here\b|\bnot a missing criterion\b)',
                re.IGNORECASE,
            )
            # Hedged speculation patterns — concerns the model didn't verify
            _speculative_pat = re.compile(
                r'\b(?:may complain|could fail|potentially|depending on (?:the )?(?:next\.?js )?version|'
                r'if .+ is not typed|might cause|may produce|may fail|likely to|could be a)\b',
                re.IGNORECASE,
            )
            for b in bullets:
                bs = b.strip()
                if len(bs) <= 25:
                    continue
                if re.match(r'^\s*(none|n/a|no issues|nothing|—)\s*$', bs, re.IGNORECASE):
                    continue
                if _confirm_pat.search(bs):
                    continue
                if _speculative_pat.search(bs):
                    continue
                substantive.append(bs)

        if passed and substantive:
            logger.warning(
                "PR review: model wrote PASSED but listed %d substantive issue(s) — overriding to ISSUES FOUND",
                len(substantive),
            )
            passed = False
        elif not passed and issues_section is not None and bullets and not substantive:
            # Guard: only flip ISSUES FOUND → PASSED when we actually parsed a real
            # Issues section with bullets that ALL got filtered. Empty section or
            # missing section → trust the model's verdict.
            logger.warning(
                "PR review: model wrote ISSUES FOUND but all %d bullet(s) were ✓-confirmed or "
                "speculative — overriding to PASSED",
                len(bullets),
            )
            passed = True

        # Mechanical schema check always wins. If we injected CONFIRMED schema
        # violations into the user message and the model still returned PASSED,
        # it ignored its instructions — force-fail. The violations are verifiable
        # by Python; no LLM judgment to debate.
        if schema_violations_block and passed:
            logger.warning(
                "PR review: model returned PASSED despite confirmed schema violations — overriding to FAIL"
            )
            passed = False
            review_text = (
                "ISSUES FOUND\n\n"
                "Confirmed schema violations (mechanical check):\n\n"
                + schema_violations_block.split("\n\n", 2)[-1]
                + "\n\n---\n\n"
                + "## Original LLM review (informational)\n\n"
                + review_text
            )

        await self._log(
            f"PR #{pr_number} review: {'PASSED' if passed else 'ISSUES FOUND'}",
            review_text[:200].replace("\n", " "),
            "SUCCESS" if passed else "BLOCKER",
        )
        review_data = {
            "passed": passed,
            "summary": review_text,
            "pr_number": pr_number,
            "issue_number": issue_number,
            "head_sha": head_sha,  # for cache-skip on next webhook firing
        }

        # Preserve the real PR url (set when the PR opened). `issue` here is the
        # GitHub *issue* object, so issue.html_url points at the issue, not the
        # PR — using it would break the "PR #n" link and "Approve & Merge".
        from orchestrator.models.project import _get_github_issue
        _local_for_url = await _get_github_issue(self.project_id, issue_number)
        pr_url = (_local_for_url or {}).get("pr_url") or issue.get("html_url", "")

        await update_github_issue_pr(
            self.project_id, issue_number, pr_number,
            pr_url,
            status="pr_reviewed",
            pr_review=review_data,
        )

        # Post review as a comment on the PR
        review_comment = (
            f"## Automatron AI Review\n\n{review_text}\n\n"
            f"*Reviewed by Automatron (model: {model})*"
        )
        try:
            await self.gh.post_pr_comment(owner, repo, pr_number, review_comment)
        except Exception as exc:
            logger.warning("Could not post PR comment: %s", exc)

        await emit_pr_review_ready(
            self.project_id, issue_number, pr_number, passed, review_text
        )
        await trace_event(
            self.project_id, "reviewer", "pr.review.completed",
            {"issue_number": issue_number, "pr_number": pr_number, "passed": passed},
        )

    # ── Post-build feedback loop ──────────────────────────────────────────────

    async def process_feedback_message(self, message: str) -> None:
        """Classify a user chat message into issues / epic / clarify and act on it.

        Called when a chat message arrives after the project has passed initial planning.
        The architect LLM decides whether the message describes concrete change(s), needs
        clarification, or is a major feature requiring an epic + sub-tasks.
        """
        from orchestrator.models.project import create_github_issue, list_github_issues

        project = await self._project()
        owner = project.get("github_repo_owner") or ""
        repo = project.get("github_repo_name") or ""
        if not owner or not repo:
            await emit_architect_message(
                self.project_id,
                "I can't act on feedback yet — no GitHub repo is wired up for this project.",
            )
            return

        self._set_trace("feedback", "architect")
        await self._log("Classifying feedback message", message[:120], "RUNNING")

        # Build context for the classifier
        plan_excerpt = (project.get("plan_md") or "")[:4000] or "(no plan yet)"
        existing_issues = await list_github_issues(self.project_id)
        recent_issues = "\n".join(
            f"- #{i.get('issue_number')} [{i.get('status', '?')}] {i.get('title', '')}"
            for i in existing_issues[-10:]
        ) or "(no issues yet)"
        stack_summary = await _build_stack_summary(self.gh, owner, repo) or "(stack unknown)"

        # Best-practice skills based on the project's stack
        from orchestrator.skills import detect_skills, build_skill_context
        _pkg = await self.gh.read_file(owner, repo, "package.json") or ""
        _tw = (
            await self.gh.read_file(owner, repo, "tailwind.config.ts")
            or await self.gh.read_file(owner, repo, "tailwind.config.js")
            or ""
        )
        _comp = await self.gh.read_file(owner, repo, "components.json") or ""
        skill_ids = detect_skills(_pkg, _tw, _comp)
        skill_context_for_body = await build_skill_context(skill_ids, "Best-practice context (apply these patterns)")

        # Live schema (only if user provided Supabase creds at project creation).
        # On failure, surface the error in the context block rather than silently
        # injecting a corrupt schema — the feedback classifier won't propose a
        # fictional column if it sees a clear "schema unavailable" note.
        schema_context = "(not available — pass supabase creds at project creation to enable)"
        supabase_url = (project.get("supabase_url") or "").strip()
        supabase_key = (project.get("supabase_service_role_key") or "").strip()
        if supabase_url and supabase_key:
            schema_result = await _introspect_supabase_schema(supabase_url, supabase_key)
            if schema_result.ok:
                schema_context = schema_result.markdown
            else:
                schema_context = (
                    f"(schema introspection failed: {schema_result.error}. "
                    f"Do NOT propose tasks that depend on inferring the schema. "
                    f"If the feedback requires DB work, mark `kind: \"existing_issue\"` and "
                    f"add a note that the schema must be verified before implementation.)"
                )

        prompt_tpl = _load_prompt("feedback_classifier_v1.txt")
        user_msg = prompt_tpl.format(
            message=message,
            plan_excerpt=plan_excerpt,
            recent_issues=recent_issues,
            stack_context=stack_summary,
            schema_context=schema_context,
        )

        llm_cfg = await self._llm_config()
        model = llm_cfg["architect"]["model"]

        try:
            raw = await call_llm(
                [
                    SystemMessage(content="You output only valid JSON, no markdown fences, no preamble."),
                    HumanMessage(content=user_msg),
                ],
                model=model,
                max_tokens=16000,
                trace_context={**self._trace_ctx, "actor": "architect", "prompt_name": "feedback_classifier_v1"},
            )
        except Exception as exc:
            logger.exception("feedback: LLM call failed")
            await emit_architect_message(
                self.project_id,
                f"Sorry — I couldn't process that feedback ({exc}). Try rephrasing or check the orchestrator logs.",
            )
            return

        # Strip ```json / ``` fences defensively — the model ignores the "no fences"
        # instruction often enough that this is worth doing always.
        cleaned = raw.strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
            cleaned = re.sub(r"\s*```\s*$", "", cleaned)

        try:
            spec = json.loads(cleaned)
        except json.JSONDecodeError:
            await self._log("feedback: invalid JSON from LLM", cleaned[-800:], "ERROR")
            await emit_architect_message(
                self.project_id,
                "I had trouble parsing my own response. Please rephrase the request.",
            )
            return

        kind = spec.get("kind", "clarify")
        chat_response = spec.get("chat_response") or "(no response)"
        issues = spec.get("issues") or []

        if kind == "clarify" or not issues:
            await emit_architect_message(self.project_id, chat_response)
            await self._log("feedback: clarification requested", chat_response[:200], "INFO")
            return

        # Create issues on GitHub + persist locally
        created_numbers: list[int] = []
        skipped_dupes: list[str] = []
        # Dedup: never open an issue whose title already matches an OPEN issue for this
        # project (a re-sent request, or two near-identical messages, must not spawn
        # twins). The per-project lock in the socket dispatcher serializes concurrent
        # feedback runs, so this snapshot reflects issues created by a prior run.
        open_titles = {
            (i.get("title") or "").strip().lower()
            for i in existing_issues
            if (i.get("status") or "open") not in ("closed", "merged")
        }
        milestone_id: int | None = None
        if kind == "epic" and len(issues) > 1:
            epic_title = issues[0].get("title", "New epic")
            try:
                ms = await self.gh.create_milestone(owner, repo, title=epic_title)
                milestone_id = ms.get("number")
            except Exception as exc:
                logger.warning("feedback: milestone creation failed: %s", exc)

        for issue_spec in issues:
            title = issue_spec.get("title", message[:80])
            if title.strip().lower() in open_titles:
                skipped_dupes.append(title)
                await self._log("feedback: issue already open, skipping", title, "INFO")
                continue
            open_titles.add(title.strip().lower())  # block intra-batch twins too
            epic = issue_spec.get("epic") or "Feedback"
            body = _render_issue_body(issue_spec, epic, "", stack_summary)
            labels = issue_spec.get("labels") or ["enhancement"]
            try:
                gh_issue = await self.gh.create_issue(
                    owner, repo,
                    title=title, body=body,
                    labels=labels,
                    milestone_number=milestone_id,
                )
            except Exception as exc:
                logger.exception("feedback: failed to create GitHub issue '%s'", title)
                await emit_architect_message(
                    self.project_id,
                    f"Failed to create issue '{title}': {exc}",
                )
                continue

            issue_number = gh_issue["number"]
            html_url = gh_issue["html_url"]
            await create_github_issue(
                str(uuid.uuid4()),
                self.project_id,
                issue_number,
                title,
                epic=epic,
                copilot_workspace_url=html_url,
            )
            created_numbers.append(issue_number)

        if skipped_dupes:
            chat_response += (
                "\n\n_(Skipped "
                + ", ".join(f"“{t}”" for t in skipped_dupes)
                + " — an open issue with that title already exists.)_"
            )

        updated_issues = await list_github_issues(self.project_id)
        await emit_issues_updated(self.project_id, updated_issues)
        await emit_architect_message(self.project_id, chat_response)
        await self._log(
            f"Feedback processed: {kind}",
            f"Created issues: {created_numbers}",
            "SUCCESS" if created_numbers else "INFO",
        )
        await trace_event(
            self.project_id, "architect", "feedback.processed",
            {"kind": kind, "issues_created": created_numbers},
        )

    # ── Stage 5: Audit codebase and create fix issues ────────────────────────

    async def audit_codebase(self) -> None:
        """Reviewer scans the current code, architect generates new fix issues."""
        import os

        project = await self._project()
        owner = project["github_repo_owner"]
        repo = project["github_repo_name"]

        self._set_trace("audit", "reviewer")
        await self._log("Starting code audit", f"{owner}/{repo}", "RUNNING")

        # Read source files from local workspace clone if available
        workspace_dir = settings.workspace_base_dir / str(self.project_id) / "repo"
        if workspace_dir.exists():
            code_context = _read_source_files(workspace_dir)
            await self._log("Source files loaded", f"{len(code_context)} chars from workspace", "INFO")
        else:
            readme = await self.gh.read_file(owner, repo, "README.md") or ""
            code_context = f"### README.md\n{readme}"
            await self._log("Source files loaded", "from GitHub README (no local clone)", "INFO")

        # Build context from existing issues and reviews
        existing_issues = await list_github_issues(self.project_id)
        plan_summary = "\n".join(
            f"- #{i['issue_number']}: {i['title']} [{i['status']}]"
            for i in existing_issues
        )
        review_notes = []
        for i in existing_issues:
            review = i.get("pr_review_json") or {}
            if isinstance(review, dict) and not review.get("passed") and review.get("summary"):
                review_notes.append(
                    f"Issue #{i['issue_number']} ({i['title']}): {review['summary'][:400]}"
                )

        llm_cfg = await self._llm_config()
        reviewer_model = llm_cfg["reviewer"]["model"]
        architect_model = llm_cfg["architect"]["model"]

        # ── Reviewer: identify what's missing / broken ───────────────────────
        await self._log("Reviewer scanning codebase", f"Model: {reviewer_model}", "RUNNING")

        reviewer_system = (
            "You are an expert code reviewer. You will receive the original task plan and "
            "the current state of the codebase.\n\n"
            "Identify:\n"
            "1. Features from the plan that were not implemented\n"
            "2. Code that is incomplete, broken, or is just the default scaffold\n"
            "3. Critical bugs or architectural problems\n\n"
            "Be specific: reference file paths and exactly what is missing or wrong.\n"
            "Output a numbered list of findings. Be concise.\n\n"
            "IMPORTANT — Fix strategy:\n"
            "If the project already has substantial working code, prioritise fixes that require "
            "the MINIMUM change to the existing codebase. Do not propose rewriting or replacing "
            "code that already works. Prefer patching, adding missing pieces, or swapping a single "
            "dependency over full rewrites. Only recommend a rewrite if the existing code is "
            "fundamentally incompatible and cannot be patched."
        )
        reviewer_user = (
            f"## Original Task Plan\n\n{plan_summary}\n\n"
            + (f"## Failed PR Reviews\n\n" + "\n\n".join(review_notes) + "\n\n" if review_notes else "")
            + f"## Current Codebase\n\n{code_context[:30000]}"
        )

        try:
            findings = await call_llm(
                [SystemMessage(content=reviewer_system), HumanMessage(content=reviewer_user)],
                model=reviewer_model,
                max_tokens=4096,
                trace_context={**self._trace_ctx, "actor": "reviewer"},
            )
        except Exception as exc:
            await emit_error(self.project_id, f"Audit review failed: {exc}")
            return

        await self._log("Reviewer findings", findings[:300].replace("\n", " "), "BLOCKER")

        # ── Architect: generate fix issues ────────────────────────────────────
        self._set_trace("audit", "architect")
        await self._log("Architect generating fix issues", f"Model: {architect_model}", "RUNNING")

        arch_system = _load_prompt("architect_github_v1.txt")
        arch_user = (
            f"## Project: {project.get('name', repo)}\n\n"
            f"The codebase has been reviewed. The following problems were found:\n\n"
            f"{findings}\n\n"
            "Generate a json:issue_plan with specific tasks to fix each problem. "
            "Focus only on the issues found above. Each task must fix one specific problem.\n\n"
            "IMPORTANT — Minimum-change principle: if the project already has substantial working "
            "code, each task must patch or extend the existing code rather than replace it. "
            "Do not generate tasks that rewrite working features from scratch. "
            "A task should change only what is necessary to fix the reported problem."
        )

        full_response = ""
        async for chunk in call_llm_streaming(
            [SystemMessage(content=arch_system), HumanMessage(content=arch_user)],
            model=architect_model,
            max_tokens=16384,
            trace_context={**self._trace_ctx, "actor": "architect"},
        ):
            full_response += chunk
            await emit_architect_chunk(self.project_id, chunk)

        await emit_architect_message(self.project_id, full_response)

        issue_plan_raw = _parse_tagged_block(full_response, "json:issue_plan")
        if not issue_plan_raw:
            await emit_error(self.project_id, "Architect did not return a valid issue plan")
            return

        try:
            issue_plan = json.loads(issue_plan_raw)
        except json.JSONDecodeError as exc:
            await emit_error(self.project_id, f"Issue plan JSON parse error: {exc}")
            return

        # ── Create issues on GitHub ───────────────────────────────────────────
        total = _count_tasks(issue_plan)
        await self._log("Creating fix issues on GitHub", f"{total} tasks planned", "RUNNING")

        created = 0
        for epic in issue_plan.get("epics", []):
            epic_title = epic.get("title", "Untitled Epic")
            milestone_number: int | None = None
            try:
                milestone_number = await self.gh.create_milestone(owner, repo, epic_title)
            except Exception:
                pass

            # Cache stack + skill context once per audit run (was re-fetched per task)
            audit_stack = await _build_stack_summary(self.gh, owner, repo)
            from orchestrator.skills import detect_skills, build_skill_context
            _pkg = await self.gh.read_file(owner, repo, "package.json") or ""
            _tw = (
                await self.gh.read_file(owner, repo, "tailwind.config.ts")
                or await self.gh.read_file(owner, repo, "tailwind.config.js")
                or ""
            )
            _comp = await self.gh.read_file(owner, repo, "components.json") or ""
            audit_skill_ids = detect_skills(_pkg, _tw, _comp)
            audit_skill_context = await build_skill_context(audit_skill_ids, "Best-practice context (apply these patterns)")

            for story in epic.get("stories", []):
                story_title = story.get("title", "")
                for task in story.get("tasks", []):
                    task_title = task.get("title", "Untitled Task")
                    body = _render_issue_body(task, epic_title, story_title, audit_stack)
                    try:
                        gh_issue = await self.gh.create_issue(
                            owner, repo,
                            title=task_title,
                            body=body,
                            milestone_number=milestone_number,
                        )
                        issue_num = gh_issue["number"]
                        await create_github_issue(
                            str(uuid.uuid4()),
                            str(self.project_id),
                            issue_num,
                            task_title,
                            epic=epic_title,
                            story=story_title or None,
                            copilot_workspace_url=gh_issue["html_url"],
                        )
                        created += 1
                        await self._log(f"Issue #{issue_num}: {task_title}", "", "SUCCESS")
                    except Exception as exc:
                        logger.warning("Failed to create issue '%s': %s", task_title, exc)

        issues = await list_github_issues(self.project_id)
        await emit_issues_updated(self.project_id, issues)
        await self._log(
            f"Audit complete — {created}/{total} fix issues created",
            "Use 'Assign Copilot' to start fixing",
            "SUCCESS",
        )


# ── Cancellable orchestration task registry ───────────────────────────────────
# FastAPI BackgroundTasks give no handle to cancel, so long-running orchestration
# (planning/building) is launched through here instead — letting POST /stop
# actually interrupt the work rather than just flipping a DB label. A project
# runs at most one tracked task at a time.
_running_tasks: dict[str, asyncio.Task] = {}


def run_tracked(project_id: str, coro: Any) -> None:
    """Launch an orchestration coroutine as a cancellable, tracked asyncio task."""
    existing = _running_tasks.get(project_id)
    if existing and not existing.done():
        existing.cancel()

    async def _wrapper() -> None:
        try:
            await coro
        except asyncio.CancelledError:
            logger.info("Orchestration for %s cancelled (stopped by user)", project_id)
        except Exception:  # already logged + surfaced by the runner's own try/except
            pass
        finally:
            if _running_tasks.get(project_id) is task:
                _running_tasks.pop(project_id, None)

    task = asyncio.create_task(_wrapper())
    _running_tasks[project_id] = task


def cancel_project_task(project_id: str) -> bool:
    """Cancel a project's running orchestration task. Returns True if one was live."""
    task = _running_tasks.get(project_id)
    if task and not task.done():
        task.cancel()
        return True
    return False


def is_running(project_id: str) -> bool:
    """True if a tracked orchestration task for this project is currently in-flight."""
    task = _running_tasks.get(project_id)
    return task is not None and not task.done()


# ── Module-level runner functions (called by API routes) ──────────────────────

async def start_project(project_id: str) -> None:
    """Entry point: read repo and generate the plan."""
    try:
        orch = GitHubOrchestrator(project_id)
        await orch.analyze_and_plan()
    except Exception as exc:
        logger.error("start_project failed for %s: %s", project_id, exc)
        await update_project(project_id, status="error", project_stage="error")
        await emit_status_update(project_id, status="error", stage="error", progress={})
        await emit_error(project_id, f"{type(exc).__name__}: {exc}")
        raise


async def resume_project(project_id: str, approval_type: str, approved: bool = True) -> None:
    """Resume after a human approval gate."""
    await record_approval(project_id, approval_type, approved)
    if approval_type == "plan" and approved:
        try:
            orch = GitHubOrchestrator(project_id)
            await orch.apply_plan()
        except Exception as exc:
            logger.error("resume_project (plan) failed for %s: %s", project_id, exc)
            await update_project(project_id, status="error", project_stage="error")
            await emit_status_update(project_id, status="error", stage="error", progress={})
            await emit_error(project_id, f"{type(exc).__name__}: {exc}")
            raise


async def sync_issues(project_id: str) -> None:
    orch = GitHubOrchestrator(project_id)
    await orch.sync_issues()


async def audit_project(project_id: str) -> None:
    orch = GitHubOrchestrator(project_id)
    await orch.audit_codebase()


async def assign_to_copilot(project_id: str) -> dict:
    """Assign all open issues in the project to the copilot agent."""
    orch = GitHubOrchestrator(project_id)
    project = await orch._project()
    owner = project.get("github_repo_owner")
    repo = project.get("github_repo_name")
    if not owner or not repo:
        raise RuntimeError("Project has no GitHub repo configured")

    issues = await list_github_issues(project_id)
    open_issues = [i for i in issues if i["status"] == "open"]

    await orch._log(
        f"Triggering Copilot agent on {len(open_issues)} open issues",
        f"{owner}/{repo}",
    )

    assigned, failed = 0, 0
    for issue in open_issues:
        try:
            await orch.gh.trigger_copilot_agent(owner, repo, issue["issue_number"])
            assigned += 1
            await orch._log(
                f"Copilot triggered on #{issue['issue_number']}",
                issue["title"],
                "SUCCESS",
            )
        except Exception as exc:
            logger.warning("Could not trigger copilot on issue #%s: %s", issue["issue_number"], exc)
            failed += 1
            await orch._log(
                f"Failed to trigger #{issue['issue_number']}",
                str(exc),
                "BLOCKER",
            )

    await orch._log(
        f"Copilot assignment complete: {assigned} triggered, {failed} failed",
        "",
        "SUCCESS" if failed == 0 else "AMBIGUITY",
    )
    logger.info("assign_to_copilot: %d assigned, %d failed (project=%s)", assigned, failed, project_id)
    return {"assigned": assigned, "failed": failed}


async def assign_to_copilot_issue(project_id: str, issue_number: int) -> dict:
    """Assign a single issue to the Copilot coding agent."""
    orch = GitHubOrchestrator(project_id)
    project = await orch._project()
    owner = project.get("github_repo_owner")
    repo = project.get("github_repo_name")
    if not owner or not repo:
        raise RuntimeError("Project has no GitHub repo configured")

    await orch.gh.trigger_copilot_agent(owner, repo, issue_number)
    await orch._log(f"Copilot assigned to #{issue_number}", f"{owner}/{repo}", "SUCCESS")
    return {"assigned": 1, "issue_number": issue_number}


async def implement_issue_with_builder(
    project_id: str, issue_number: int, extra_context: str | None = None,
) -> None:
    """Clone the repo, run the Agent SDK builder on the issue, push a branch, open a PR.

    `extra_context`, when provided, is prepended to the issue body before the
    builder sees it. Used by the "Retry with context" button on ERROR-level
    activity rows so a build error / type error / runtime crash gets fed back as
    a fix directive.
    """
    from orchestrator.builder.agent_sdk import implement_issue_via_agent_sdk
    from orchestrator.models.project import update_github_issue_pr

    orch = GitHubOrchestrator(project_id)
    project = await orch._project()
    owner = project.get("github_repo_owner") or ""
    repo = project.get("github_repo_name") or ""
    if not owner or not repo:
        await orch._log(f"Builder #{issue_number}: no repo configured", "", "ERROR")
        return

    # Read issue from GitHub
    issue_data = await orch.gh.get_issue(owner, repo, issue_number)
    if not issue_data:
        await orch._log(f"Builder #{issue_number}: issue not found", "", "ERROR")
        return

    issue_title = issue_data.get("title", f"Issue #{issue_number}")
    issue_body = issue_data.get("body", "")
    default_branch = project.get("default_branch") or "main"

    # Append previous review feedback so the builder knows what to fix
    from orchestrator.models.project import _get_github_issue
    local_issue = await _get_github_issue(project_id, issue_number)
    existing_pr_number = local_issue.get("pr_number") if local_issue else None
    review = (local_issue.get("pr_review") or {}) if local_issue else {}
    if review.get("summary"):
        issue_body += (
            f"\n\n---\n## Previous AI Review Feedback\n\n"
            f"{'PASSED' if review.get('passed') else 'FAILED'}\n\n"
            f"{review['summary']}\n\n"
            f"Address all issues listed above in your implementation."
        )

    if extra_context:
        # Prepend, not append. When extra_context lived at the bottom of a
        # multi-KB spec, the LLM treated it as a footnote and kept executing
        # the top-level spec — producing zero diff (everything already done)
        # and falling back to .gitignore housekeeping. Hoisted to the top
        # and framed as the PRIMARY directive, it dominates the builder's attention.
        issue_body = (
            f"# PRIMARY DIRECTIVE — Fix this error\n\n"
            f"This issue was previously implemented. A subsequent build or runtime "
            f"error was reported on the branch. Your PRIMARY task is to fix the error "
            f"below. The original spec is preserved further down as **reference only**: "
            f"do not re-implement files that already work — only patch what the error "
            f"says is broken. The error often names the exact file and line.\n\n"
            f"## Error to fix\n\n"
            f"{extra_context}\n\n"
            f"---\n\n"
            f"## Original issue spec (reference only — do NOT re-implement)\n\n"
            f"{issue_body}"
        )

    llm_cfg = await orch._llm_config()
    builder_cfg = llm_cfg.get("builder", {}) or {}
    model = builder_cfg.get("model", "anthropic/claude-sonnet-4-6")
    # Remove internal claude/ prefix if present; keep anthropic/ for LiteLLM routing
    model = model.replace("claude/", "")
    # Validate model — fall back if it looks like a hallucinated or unrecognized name
    _KNOWN_PREFIXES = ("anthropic/", "claude-", "gpt-4", "gpt-4o", "openai/", "gemini", "deepseek")
    _is_gpt = model.startswith(("gpt-", "openai/"))
    if not any(model.startswith(p) for p in _KNOWN_PREFIXES) or (_is_gpt and not settings.openai_api_key):
        logger.warning("Builder: unrecognized or unconfigured model %r — falling back to claude-sonnet-4-6", model)
        model = "anthropic/claude-sonnet-4-6"

    is_reimplementation = bool(existing_pr_number)
    action = "re-implementing" if is_reimplementation else "starting"
    await orch._log(f"Agent SDK {action} on #{issue_number}", issue_title, "RUNNING")

    # Set "implementing" status immediately so the UI shows "Working…" while the builder runs
    from orchestrator.models.project import update_github_issue_status, list_github_issues as _list_issues
    await update_github_issue_status(project_id, issue_number, "implementing")
    await emit_issues_updated(project_id, await _list_issues(project_id))

    # Fetch the live Supabase schema (cached on the orchestrator instance) so
    # the builder sees authoritative table/column names. Without this the
    # builder writes against fictional schemas — the original bug.
    schema_for_builder = await orch.get_supabase_schema()
    schema_md_for_builder: str | None = (
        schema_for_builder.markdown if schema_for_builder and schema_for_builder.ok else None
    )

    # Agent SDK takes the raw model name (no LiteLLM "anthropic/" prefix).
    sdk_model = model.removeprefix("anthropic/")
    branch, failure_reason, usage = await implement_issue_via_agent_sdk(
        project_id=project_id,
        owner=owner,
        repo=repo,
        issue_number=issue_number,
        issue_title=issue_title,
        issue_body=issue_body,
        default_branch=default_branch,
        model=sdk_model,
        is_reimplementation=is_reimplementation,
        extra_context=None,  # already prepended above
        supabase_schema_markdown=schema_md_for_builder,
    )
    # Surface cost in the activity log.
    await orch._log(
        f"Agent SDK done for #{issue_number}",
        (
            f"iterations={usage.iterations} • tool_calls={usage.tool_calls} • "
            f"in={usage.input_tokens} • out={usage.output_tokens} • "
            f"cache_r={usage.cache_read_input_tokens} • cache_w={usage.cache_creation_input_tokens} • "
            f"~${usage.estimated_cost_usd(model):.4f}"
        ),
        "INFO" if branch else "AMBIGUITY",
    )

    if not branch:
        detail = failure_reason or "unknown error"
        await orch._log(f"Builder #{issue_number}: implementation failed", detail, "ERROR")
        await emit_error(project_id, f"Builder failed on #{issue_number}: {detail[:200]}")
        # Revert status back to open so user can retry
        revert_status = "pr_reviewed" if is_reimplementation else "open"
        await update_github_issue_status(project_id, issue_number, revert_status)
        await emit_issues_updated(project_id, await _list_issues(project_id))
        return

    from orchestrator.models.project import update_github_issue_pr, list_github_issues

    # If a PR already exists on this branch, just reset the review status
    if existing_pr_number:
        existing_pr = await orch.gh.find_pr_for_issue(owner, repo, issue_number)
        pr_url = existing_pr["html_url"] if existing_pr else (local_issue or {}).get("pr_url") or ""
        await orch._log(f"Builder #{issue_number}: pushed update to existing PR #{existing_pr_number}", f"PR #{existing_pr_number}", "SUCCESS")
        await update_github_issue_pr(project_id, issue_number, existing_pr_number, pr_url, status="pr_open", pr_review=None)
        updated_issues = await list_github_issues(project_id)
        await emit_issues_updated(project_id, updated_issues)
        return

    # Open a new PR
    pr_title = f"fix: implement #{issue_number} {issue_title}"
    pr_body = f"Closes #{issue_number}\n\nImplemented by the Automatron Agent SDK builder ({model})."
    try:
        pr = await orch.gh.create_pull_request(
            owner, repo,
            title=pr_title,
            body=pr_body,
            head=branch,
            base=default_branch,
        )
        pr_number = pr["number"]
        pr_url = pr["html_url"]
        await orch._log(f"Builder #{issue_number}: PR opened", f"PR #{pr_number}", "SUCCESS")

        await update_github_issue_pr(project_id, issue_number, pr_number, pr_url, status="pr_open")
        updated_issues = await list_github_issues(project_id)
        await emit_issues_updated(project_id, updated_issues)
    except Exception as exc:
        await orch._log(f"Builder #{issue_number}: PR creation failed", str(exc), "ERROR")
        await emit_error(project_id, f"Builder: PR failed for #{issue_number}: {exc}")


async def create_issue_from_prompt(project_id: str, prompt: str) -> None:
    """Use the architect LLM to turn a free-text prompt into a structured GitHub issue."""
    from orchestrator.models.project import create_github_issue, list_github_issues

    orch = GitHubOrchestrator(project_id)
    project = await orch._project()
    owner = project.get("github_repo_owner") or ""
    repo = project.get("github_repo_name") or ""
    if not owner or not repo:
        await orch._log("create_issue: no repo configured", "", "ERROR")
        return

    await orch._log("Creating issue from prompt", prompt[:120], "RUNNING")

    llm_cfg = await orch._llm_config()
    arch_model = llm_cfg["architect"]["model"]

    stack_summary = await _build_stack_summary(orch.gh, owner, repo)
    from orchestrator.skills import detect_skills, build_skill_context
    _pkg = await orch.gh.read_file(owner, repo, "package.json") or ""
    _tw = (
        await orch.gh.read_file(owner, repo, "tailwind.config.ts")
        or await orch.gh.read_file(owner, repo, "tailwind.config.js")
        or ""
    )
    _comp = await orch.gh.read_file(owner, repo, "components.json") or ""
    cip_skill_ids = detect_skills(_pkg, _tw, _comp)
    cip_skill_context = await build_skill_context(cip_skill_ids, "Best-practice context (apply these patterns)")

    prompt_tpl = _load_prompt("issue_from_prompt_v1.txt")
    user_msg = prompt_tpl.format(prompt=prompt, stack_context=stack_summary or "(not available)")

    raw = await call_llm(
        [
            SystemMessage(content="You are a senior software engineer. Output only valid JSON, no markdown fences."),
            HumanMessage(content=user_msg),
        ],
        model=arch_model,
        max_tokens=2000,
        trace_context={**orch._trace_ctx, "actor": "architect"},
    )

    try:
        spec = json.loads(raw.strip())
    except Exception:
        await orch._log("create_issue: LLM returned invalid JSON", raw[:300], "ERROR")
        return

    title = spec.get("title", prompt[:80])
    epic = spec.get("epic") or "General"
    body = _render_issue_body(spec, epic, "", stack_summary)

    gh_issue = await orch.gh.create_issue(owner, repo, title=title, body=body)
    issue_number = gh_issue["number"]
    html_url = gh_issue["html_url"]

    await create_github_issue(
        str(uuid.uuid4()),
        project_id,
        issue_number,
        title,
        epic=epic,
        copilot_workspace_url=html_url,
    )

    updated_issues = await list_github_issues(project_id)
    await emit_issues_updated(project_id, updated_issues)
    await orch._log(f"Issue #{issue_number} created", title, "SUCCESS")


async def review_pr(project_id: str, issue_number: int, pr_number: int) -> None:
    orch = GitHubOrchestrator(project_id)
    await orch.review_pr(issue_number, pr_number)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _count_tasks(issue_plan: dict) -> int:
    total = 0
    for epic in issue_plan.get("epics", []):
        for story in epic.get("stories", []):
            total += len(story.get("tasks", []))
    return total


def _build_plan_md(issue_plan: dict, architecture_md: str, stories_md: str) -> str:
    lines = ["# Project Plan\n"]

    if architecture_md:
        lines.append("## Architecture\n")
        lines.append(architecture_md)
        lines.append("")

    lines.append("## Tasks\n")
    for epic in issue_plan.get("epics", []):
        lines.append(f"### {epic.get('title', 'Epic')}\n")
        for story in epic.get("stories", []):
            lines.append(f"**{story.get('title', 'Story')}**\n")
            for task in story.get("tasks", []):
                lines.append(f"- [ ] {task.get('title', 'Task')}")
        lines.append("")

    return "\n".join(lines)
