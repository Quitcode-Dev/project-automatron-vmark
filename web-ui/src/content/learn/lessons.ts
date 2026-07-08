/**
 * In-app course content. Lessons are plain Markdown strings rendered with
 * react-markdown (already a dependency) — no MDX build pipeline, no GFM tables
 * (so avoid pipe tables here; use lists/headings instead).
 *
 * Tracks: user (getting started) · operator (self-hosting) · developer ·
 * reference (glossary). The user track ships first; operator/developer/glossary
 * are filled in a later phase.
 */

import { foundationsLessons } from "./foundations";
import { gettingStartedLessons } from "./gettingStarted";

export type TrackId = "foundations" | "user" | "operator" | "developer" | "reference";
export type Audience = "reviewer" | "operator" | "developer";

export interface Track {
  id: TrackId;
  label: string;
  blurb: string;
}

export interface Lesson {
  slug: string;
  track: TrackId;
  title: string;
  summary: string;
  /** Estimated read time in minutes. */
  minutes?: number;
  /** Who this lesson is written for — drives the audience badge. */
  audience?: Audience;
  /** Slugs the reader should ideally do first. */
  prerequisites?: string[];
  body: string;
}

// Order matters — it defines track order in the nav and the prev/next chain.
export const TRACKS: Track[] = [
  { id: "foundations", label: "Foundations", blurb: "Your role, and the Git/GitHub you need to review AI work — no coding required." },
  { id: "user", label: "Getting Started", blurb: "Direct Automatron through a project end-to-end: plan, build, review, preview, ship." },
  { id: "operator", label: "Operator & Setup", blurb: "Self-host and configure Automatron (env, GitHub, Copilot, deploy)." },
  { id: "developer", label: "Developer", blurb: "Work in the Automatron codebase itself." },
  { id: "reference", label: "Reference", blurb: "Glossary of Automatron concepts." },
];

export const LESSONS: Lesson[] = [
  ...foundationsLessons,
  ...gettingStartedLessons,

  // ── Operator / Setup ────────────────────────────────────────────────────────
  {
    slug: "operator-environment",
    track: "operator",
    audience: "operator",
    title: "Environment configuration",
    summary: "The .env by area, and the keys that matter.",
    body: `
Automatron reads configuration from a \`.env\` file (pydantic-settings). It uses a
**strict** schema — an unknown key makes the backend refuse to start — so keep the
backend's env limited to the keys it actually defines.

## Core keys

- **LLM providers:** \`ANTHROPIC_API_KEY\`, \`OPENAI_API_KEY\`, \`GOOGLE_API_KEY\`.
- **Per-role models:** \`ARCHITECT_MODEL\`, \`BUILDER_MODEL\`, \`REVIEWER_MODEL\`.
  Note the backend defaults are Anthropic (Opus for architect, Sonnet for
  builder/reviewer), but the shipped \`.env.example\` sets \`gpt-5.3-codex\` — pick
  deliberately.
- **GitHub:** \`GITHUB_TOKEN\`, \`GITHUB_OWNER\`, \`GITHUB_OWNER_TYPE\` (\`user\`/\`org\`),
  \`GITHUB_WEBHOOK_SECRET\`, \`AUTOMATRON_PUBLIC_URL\`.
- **Auth:** \`AUTH_SECRET\`, \`GOOGLE_CLIENT_ID\`, \`GOOGLE_CLIENT_SECRET\`,
  \`AUTOMATRON_ALLOWED_EMAILS\`, and the dev-only \`AUTOMATRON_DEV_NO_AUTH\`.
- **Previews:** \`PREVIEW_BASE_DOMAIN\` (see [Production & troubleshooting](/learn/operator-production)).
- **Database:** \`SQLITE_DB_PATH\`.

See also [GitHub setup](/learn/operator-github) and [Copilot vs the builder](/learn/operator-copilot).
`,
  },
  {
    slug: "operator-github",
    track: "operator",
    audience: "operator",
    title: "GitHub setup",
    summary: "PAT scopes, owner, repo visibility, and webhooks.",
    body: `
## The token

\`GITHUB_TOKEN\` must be a PAT with write access to the target repos. The scopes
Automatron actually uses:

- \`repo\` — read/write repos, issues, PRs (and merge).
- \`workflow\` — manage the CI/Deploy GitHub Actions workflows.
- \`admin:repo_hook\` — auto-register the \`pull_request\` webhook when
  \`AUTOMATRON_PUBLIC_URL\` is set.

## Owner & visibility

\`GITHUB_OWNER\` + \`GITHUB_OWNER_TYPE\` decide where repos/issues are created (a user
or an org). \`GITHUB_REPO_VISIBILITY\` controls the visibility of repos Automatron
provisions.

## Webhooks

Set \`GITHUB_WEBHOOK_SECRET\` and \`AUTOMATRON_PUBLIC_URL\`. Automatron registers a
\`pull_request\` webhook and verifies each delivery's HMAC signature. When a Copilot
PR opens, the webhook triggers the AI review automatically; when it merges, it
syncs the issue and runs a build check. If reviews aren't firing, check the secret
and that the public URL is reachable.
`,
  },
  {
    slug: "operator-copilot",
    track: "operator",
    audience: "operator",
    title: "Copilot vs the Agent SDK builder",
    summary: "Plan-gating for the Copilot coding agent, and the built-in alternative.",
    body: `
Each issue can be built two ways.

## GitHub Copilot coding agent

Delegated by assigning the \`copilot-swe-agent[bot]\` to an issue. It is **plan-gated**
and not reliably triggerable by public API:

- Org repos need Copilot **Enterprise**; personal repos need Copilot **Pro+**.
- Copilot **Business does not include** the coding agent.
- Limits apply (repo size, session timeout, files per PR, concurrency).

If assignment fails, Automatron falls back to creating the issue unassigned.

## The built-in Agent SDK builder

The **Implement** button uses Automatron's own Anthropic Agent SDK tool-loop
(\`BUILDER_MODEL\`, your \`ANTHROPIC_API_KEY\`). No Copilot plan required. It pushes to
\`agent-sdk/fix-<n>\` and opens a PR — the same downstream review/preview/merge flow.

Guidance: if you don't have Copilot Enterprise/Pro+, use **Implement**.
`,
  },
  {
    slug: "operator-production",
    track: "operator",
    audience: "operator",
    title: "Production & troubleshooting",
    summary: "Traefik, previews over HTTPS, deploy secrets, and where to look when stuck.",
    body: `
## Serving & deploy

Production runs behind **Traefik** (TLS via Let's Encrypt) with services on an
external \`proxy\` network. Deploys run from GitHub Actions over SSH — set the
\`AUTOMATRON_DEPLOY_KEY\` secret and the host's deploy key.

## Previews over HTTPS

Previews must be exposed as HTTPS subdomains, not \`http://host:port\` (unreachable
behind Traefik and blocked as mixed content). Set \`PREVIEW_BASE_DOMAIN\` and add
**wildcard DNS** (\`*.<domain>\` → the server) so Traefik can issue a cert per
preview host. Preview URLs become \`https://preview-<id>.<PREVIEW_BASE_DOMAIN>\`.

## When "nothing happens"

Background work swallows exceptions. Inspect the SQLite tables directly:

- \`activity_logs\` — human-readable step log per project.
- \`trace_events\` — structured actor/event trace.

Other quick checks: webhook not firing → secret + public URL; Copilot 422 → plan
gating; preview blank → the Activity tab shows the docker build/health failure.
`,
  },

  // ── Developer ───────────────────────────────────────────────────────────────
  {
    slug: "developer-codebase",
    track: "developer",
    audience: "developer",
    title: "Codebase map",
    summary: "How the orchestrator and web-ui are laid out.",
    body: `
Two apps:

- **\`orchestrator/\`** — FastAPI + Socket.IO backend (Python 3.12). Entry point
  \`orchestrator.main:app\` (a Socket.IO ASGI app wrapping FastAPI), port 8000.
  - \`orchestrator.py\` — \`GitHubOrchestrator\` (analyze / plan / apply / review).
  - \`github/issues.py\` — \`GitHubClient\` REST wrapper.
  - \`api/\` — \`routes.py\`, \`webhook_github.py\`, \`socket_server.py\`, \`websocket.py\`.
  - \`llm/\`, \`builder/agent_sdk.py\`, \`plan_parser/\`, \`docker_deployment_ai/\`,
    \`preview.py\`, \`execution_contract.py\`.
- **\`web-ui/\`** — Next.js 15 (App Router), React 19, Zustand, Socket.IO client,
  Tailwind + shadcn/ui. Uses **npm**.

\`CLAUDE.md\` at the repo root has the working notes and gotchas.

Next: [Running locally](/learn/developer-running).
`,
  },
  {
    slug: "developer-running",
    track: "developer",
    audience: "developer",
    title: "Running locally & tests",
    summary: "Boot both apps in dev, plus test/lint commands.",
    body: `
## Backend (port 8000)

\`\`\`
cd orchestrator
pip install -e ".[dev]"
uvicorn orchestrator.main:app --reload --port 8000
\`\`\`

Give the backend a **backend-only** \`.env\` (its strict schema rejects web-ui keys
like \`WS_URL\`). Set \`AUTOMATRON_DEV_NO_AUTH=true\` to bypass backend auth in dev.

## Web UI (port 3000)

\`\`\`
cd web-ui
npm install
npm run dev
\`\`\`

## Tests & lint

- Backend: \`python -m pytest tests/ -v\`; \`ruff check orchestrator/\`; \`mypy\`.
- Web UI: \`npm test\` (vitest); \`npm run lint\`; \`npx tsc --noEmit\`.
`,
  },

  // ── Reference ───────────────────────────────────────────────────────────────
  {
    slug: "glossary",
    track: "reference",
    title: "Glossary",
    summary: "Automatron and Git/GitHub terms in one place.",
    body: `
## Git & GitHub

- **Repository (repo)** — your project: all its files plus the full change history.
- **Commit** — one saved change, with a short message.
- **Branch** — a safe parallel copy of the project. The AI works on a branch so
  unfinished work can't affect your live version (\`main\`).
- **\`main\`** — your live, default branch. Merged changes land here. Some projects
  also use a **\`develop\`** staging branch.
- **Pull request (PR)** — a proposal to add a branch's work, with a reviewable
  diff. Where you spend most of your review time.
- **Diff / Files changed** — the exact lines a change adds (green) or removes (red).
- **Merge** — accepting a pull request; the change becomes part of your project.
- **Gitflow** — the branch → PR → review → merge workflow Automatron follows.

## Automatron

- **Epic / Story / Task** — the plan hierarchy the Architect produces; each Task
  becomes one GitHub Issue.
- **Approval gates** — the two human checkpoints: approve the plan, and approve/
  trigger the deploy.
- **Architect** — the planning LLM role (\`ARCHITECT_MODEL\`).
- **Builder / Agent SDK builder** — the built-in Anthropic tool-loop that
  implements an issue and opens a PR (\`BUILDER_MODEL\`). Pushes to \`agent-sdk/fix-<n>\`.
- **Copilot coding agent** — GitHub's autonomous PR-opening agent
  (\`copilot-swe-agent[bot]\`); an alternative to the built-in builder.
- **Reviewer** — the LLM role that reviews PR diffs (\`REVIEWER_MODEL\`).
- **Preview** — the app built and run in a container so you can see a change
  before merging/shipping.
- **Deployment target** — a registered server (host, SSH, domain) the Deploy tab
  ships to.
- **Inventory** — the SSH introspection of a target before planning a deploy.
- **Docker-AI / Gordon** — the deployment-intelligence backend chain that proposes
  and executes deploys.
- **Execution contract** — the machine-readable coordination doc
  (\`execution_contract.json\`) shared across Architect/Builder/Reviewer.
`,
  },
];

export function getLesson(slug: string): Lesson | undefined {
  return LESSONS.find((l) => l.slug === slug);
}

export function lessonsForTrack(track: TrackId): Lesson[] {
  return LESSONS.filter((l) => l.track === track);
}

/** All lessons in reading order (track order, then within-track order). */
export function orderedLessons(): Lesson[] {
  return TRACKS.flatMap((t) => lessonsForTrack(t.id));
}

/** Previous / next lesson in the reading chain, for reader navigation. */
export function adjacentLessons(slug: string): { prev?: Lesson; next?: Lesson } {
  const all = orderedLessons();
  const i = all.findIndex((l) => l.slug === slug);
  if (i === -1) return {};
  return { prev: all[i - 1], next: all[i + 1] };
}

export function trackById(id: TrackId): Track | undefined {
  return TRACKS.find((t) => t.id === id);
}
