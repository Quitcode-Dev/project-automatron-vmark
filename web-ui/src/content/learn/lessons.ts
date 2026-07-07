/**
 * In-app course content. Lessons are plain Markdown strings rendered with
 * react-markdown (already a dependency) — no MDX build pipeline, no GFM tables
 * (so avoid pipe tables here; use lists/headings instead).
 *
 * Tracks: user (getting started) · operator (self-hosting) · developer ·
 * reference (glossary). The user track ships first; operator/developer/glossary
 * are filled in a later phase.
 */

export type TrackId = "user" | "operator" | "developer" | "reference";

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
  body: string;
}

export const TRACKS: Track[] = [
  { id: "user", label: "Getting Started", blurb: "Use Automatron to plan, build, preview and ship a project." },
  { id: "operator", label: "Operator & Setup", blurb: "Self-host and configure Automatron (env, GitHub, Copilot, deploy)." },
  { id: "developer", label: "Developer", blurb: "Work in the Automatron codebase itself." },
  { id: "reference", label: "Reference", blurb: "Glossary of Automatron concepts." },
];

export const LESSONS: Lesson[] = [
  {
    slug: "what-is-automatron",
    track: "user",
    title: "What Automatron does",
    summary: "The 5-stage pipeline and how to think about it.",
    body: `
## The mental model

Automatron is **GitHub-native**. You point it at a repository and describe what
you want; it plans the work, opens GitHub Issues, delegates the coding to an AI
agent, reviews the resulting pull requests, previews the app, and deploys it.

Every project moves left-to-right through five stages — the tracker at the top
of a project shows where you are:

1. **Intake** — you connect a repo and describe the work.
2. **Plan** — the *Architect* LLM drafts a plan of Epics → Stories → Tasks. You approve it.
3. **Build** — each task becomes a GitHub Issue; a coding agent implements it and opens a PR, which Automatron AI-reviews.
4. **Preview** — the app runs in a live preview so you can see it before shipping.
5. **Deploy** — the Deployment tab ships it to your server.

## Two things that stay in your control

- **Two approval gates.** Nothing is built until you **Approve the plan**, and you decide when to deploy.
- **Who writes the code.** Each issue can go to the **GitHub Copilot coding agent** *or* Automatron's **built-in Agent SDK builder** (uses your Anthropic key). See [The build loop](/learn/the-build-loop).

Next: [Create your first project](/learn/create-your-first-project).
`,
  },
  {
    slug: "create-your-first-project",
    track: "user",
    title: "Create your first project",
    summary: "The Connect Repository dialog, models, and optional integrations.",
    body: `
## Connect a repository

Click **New Project** on the dashboard. In the *Connect Repository* dialog:

- **GitHub Repository URL** — the repo Automatron will plan and open issues in.
  It needs a GitHub token with write access (an operator configures this; see the
  Operator track for the exact PAT scopes).
- **Project Name** — auto-filled from the repo; edit if you like.
- **LLM Configuration** — pick the model for the **Architect** (planning) and
  **Reviewer** (PR review) roles. Bigger models plan better; smaller models are
  cheaper. The builder engine is fixed to the Agent SDK.

## Optional integrations

- **Figma** — attach design URLs or upload a \`.fig\` so the plan reflects your designs.
- **Supabase** — paste your project URL + keys so generated code (and previews)
  run against your real schema instead of a guessed one.

Click **Connect & Plan**. Automatron reads the repo and starts planning.

Next: [The plan & approval gate](/learn/plan-and-approval).
`,
  },
  {
    slug: "plan-and-approval",
    track: "user",
    title: "The plan & first approval gate",
    summary: "PLAN.md, the Epics→Stories→Tasks hierarchy, and approving.",
    body: `
## What the Architect produces

When a project starts, the Architect LLM reads the repo and writes a **PLAN.md**:
a hierarchy of **Epics → Stories → Tasks**. Open the **Plan** tab to read it; you
can edit it before approving.

- **Epic** — a large area of work (e.g. "Authentication").
- **Story** — a user-facing slice within an epic.
- **Task** — a single, concrete change that becomes one GitHub Issue.

## The first gate

Nothing is built until you click **Approve Plan** (top action bar). This is the
first of the two human-in-the-loop gates. On approval, Automatron provisions the
repo's Milestones and creates a GitHub Issue per task, and the project enters the
**Build** stage.

Tip: if the plan is off, edit PLAN.md (or use the Architect **chat**) before
approving — it's much cheaper to fix the plan than the code.

Next: [The build loop](/learn/the-build-loop).
`,
  },
  {
    slug: "the-build-loop",
    track: "user",
    title: "The build loop",
    summary: "Assign Copilot vs Implement, AI review, preview a branch, merge.",
    body: `
## Per-issue actions

Open the **Issues** tab. Each issue card has actions that change with its state:

- **Assign Copilot** — hands the issue to the GitHub Copilot coding agent. Requires
  a Copilot plan that includes the coding agent (see the Operator track).
- **Implement** — uses Automatron's built-in **Agent SDK builder** (your Anthropic
  key). No Copilot plan needed. It pushes to \`agent-sdk/fix-<n>\` and opens a PR.

Either way you end up with a **pull request**.

## Review, preview, merge

- **Request AI Review** — the reviewer LLM reads the PR diff and posts a structured
  review (Passed / Changes Needed) as a PR comment and on the card.
- **Preview branch** — spin up the PR's branch as a live preview *without* merging,
  so you can see the change.
- **Re-implement / Re-review** — if the review found problems, send it back.
- **Approve & Merge** — opens the PR on GitHub to merge. Merging fires a webhook
  that syncs the issue to *merged* and runs a build check.

Next: [Preview](/learn/preview).
`,
  },
  {
    slug: "preview",
    track: "user",
    title: "Preview",
    summary: "The live preview and the second approval gate.",
    body: `
## Seeing the app run

Automatron builds and runs your app in a container so you can click through it
before shipping. Use **Launch Preview** / **Restart Preview**, then **Open
Preview** to open it in a new tab (it's served over HTTPS in production), or view
it inline in the **Preview** tab.

If the preview won't start, check the **Activity** tab — it logs each step (clone,
env injection, docker build, health check) and shows the exact failure.

## Env for previews

Previews need the app's environment variables to run. Set your **Supabase** config
in the New Project dialog, and/or paste a full \`.env\` into the Deploy tab's
Environment field. Without them, pages that read \`process.env\` will 500.

The preview is your chance to iterate before the final gate.

Next: [Deploying](/learn/deploying).
`,
  },
  {
    slug: "deploying",
    track: "user",
    title: "Deploying",
    summary: "The Deploy tab: target → inventory → analysis → plan → approve → execute.",
    body: `
## The Deploy tab is the last stage

Deployment happens in the **Deploy** tab, after the preview looks right. The flow:

1. **Add Target** — register your server (host, SSH user/port, app name, deploy
   path, domain). This needs an SSH key an operator has configured.
2. **Run Inventory** — Automatron SSHes in and introspects what's already running.
3. **Run Analysis** — the Docker-AI agent proposes how to deploy given the inventory.
4. **Create Plan → Validate** — turn the analysis into a concrete, validated plan.
5. **Approve → Execute Deploy** — the production gate: acknowledge, approve, and run
   it. A run view shows each step, with **Rollback** if needed.

This is deliberately staged and gated so a deploy is never a surprise.

Next: [Iterating](/learn/iterating).
`,
  },
  {
    slug: "iterating",
    track: "user",
    title: "Iterating after the first build",
    summary: "Architect chat, Audit Code, and Build Check.",
    body: `
## Ask for changes in plain language

After planning, the **Architect chat** becomes a feedback channel. Describe a
change or report a bug and Automatron drafts new GitHub Issues for it (or asks a
clarifying question) — no need to write issues by hand.

## Health tools on the Issues tab

- **Audit Code** — asks the LLM to review the repo for problems and file issues.
- **Build Check** — runs \`npm run build\` in a container against the default branch
  and flags failures, with a one-click **Create GitHub Issue** for the error.
- **Sync** — reconciles issue/PR state from GitHub if anything looks stale.

That's the loop: chat → issues → build → review → preview → deploy → repeat.
`,
  },

  // ── Operator / Setup ────────────────────────────────────────────────────────
  {
    slug: "operator-environment",
    track: "operator",
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
    summary: "Automatron concepts in one place.",
    body: `
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
