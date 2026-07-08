import type { Lesson } from "./lessons";

/**
 * Getting Started track — the reviewer's end-to-end workflow, rewritten as
 * step-by-step guides (not descriptions). Slugs are unchanged so existing
 * deep-links (StageCoach, InfoTips, checklist) keep working. Screenshots are
 * captured into /public/learn/*.png.
 */
export const gettingStartedLessons: Lesson[] = [
  {
    slug: "what-is-automatron",
    track: "user",
    title: "What Automatron does",
    summary: "The 5 stages your project moves through — and where you step in.",
    audience: "reviewer",
    minutes: 3,
    prerequisites: ["your-role"],
    body: `
Automatron takes a GitHub repository and a description of what you want, and runs
the whole build for you. **You direct and review; the AI plans, writes, and
checks.** (New to that idea? Start with [Your role](/learn/your-role).)

## The five stages
Every project moves left-to-right — the tracker at the top of a project shows
where you are:

1. **Intake** — you connect a repo and describe the work.
2. **Plan** — the Architect drafts a plan. **You approve it** (gate #1).
3. **Build** — each task becomes an issue; the AI writes it and opens a pull
   request. **You review and merge** each one.
4. **Preview** — the app runs live so you can click through it.
5. **Deploy** — you ship it, from the Deploy tab.

![The Automatron dashboard with the project list.](/learn/dashboard.png)

> [!ROLE] Your touch points
> Approve the plan → review each PR → preview → deploy. Four kinds of decisions,
> all yours. Everything in between is the AI's job.

> [!DO] Next
> [Create your first project](/learn/create-your-first-project).
`,
  },
  {
    slug: "create-your-first-project",
    track: "user",
    title: "Create your first project",
    summary: "Step-by-step through the Connect Repository dialog.",
    audience: "reviewer",
    minutes: 4,
    prerequisites: ["what-is-automatron"],
    body: `
> [!DO] Do this
> 1. On the dashboard, click **New Project**.
> 2. Paste your **GitHub Repository URL**.
> 3. Give it a **name** (auto-filled from the repo).
> 4. *(Optional)* add **Figma** design links and **Supabase** keys.
> 5. Pick the **Architect** and **Reviewer** models.
> 6. Click **Connect & Plan**.

![The New Project dialog.](/learn/new-project-modal.png)

## What each field is for
- **GitHub Repository URL** — the repo Automatron will build in. It should have a
  README (or a \`docs/PRD.md\`) describing what you want. An operator has already
  set up the access token; you just paste the URL.
- **Figma (optional)** — attach designs so the plan and UI match them.
- **Supabase (optional)** — your database keys, so generated code runs against
  your real data instead of guesses.
- **Architect / Reviewer models** — the AI models that plan and review. Bigger =
  better and pricier; the defaults are fine to start.

> [!TIP] The repo describes the goal
> The clearer your repo's README/PRD, the better the plan. You can also refine
> everything later in the Architect chat — you're not locked in.

> [!CHECK] After clicking Connect & Plan
> You'll land on the project page and the Architect starts planning. Next:
> [The plan & your first approval](/learn/plan-and-approval).
`,
  },
  {
    slug: "plan-and-approval",
    track: "user",
    title: "The plan & your first approval",
    summary: "Review the plan the Architect wrote, then approve it — gate #1.",
    audience: "reviewer",
    minutes: 4,
    prerequisites: ["create-your-first-project"],
    body: `
Once the project starts, the Architect writes a **plan** — a breakdown of the
work into **Epics → Stories → Tasks**. Nothing is built until you approve it.

> [!DO] Do this
> 1. Open the **Plan** tab and read **PLAN.md**.
> 2. Not right? Edit it, or ask for changes in the **Chat** tab.
> 3. Happy? Click **Approve Plan** in the top bar.

![The Plan tab showing PLAN.md.](/learn/plan-tab.png)

## What the plan contains
- **Epic** — a big area of work (e.g. "Authentication").
- **Story** — a user-facing slice of an epic.
- **Task** — one concrete change. Each task becomes **one GitHub issue** after
  you approve.

> [!ROLE] This is your cheapest control point
> Fixing the *plan* is far cheaper than fixing the *code* later. Read it, and
> don't approve until it matches what you actually want.

> [!CHECK] After approving
> Automatron creates the GitHub issues and the project enters the **Build**
> stage. Next: [The build loop](/learn/the-build-loop).
`,
  },
  {
    slug: "the-build-loop",
    track: "user",
    title: "The build loop",
    summary: "Assign an agent, watch the PR appear, review it, and merge.",
    audience: "reviewer",
    minutes: 6,
    prerequisites: ["plan-and-approval", "reviewing-a-pull-request"],
    body: `
Open the **Issues** tab — this is where most of your time goes. Each issue is one
task, and each moves through the same little loop.

> [!DO] For each issue
> 1. **Assign Copilot** *or* **Implement** — an AI agent starts writing the code.
> 2. Wait for the status to reach **PR Ready** (a pull request appears).
> 3. **Request AI Review** — the Reviewer checks it and posts a verdict.
> 4. *(Optional)* **Preview branch** — see the change running before merging.
> 5. **Approve & Merge** if it's good, or **Re-implement** to send it back.

![The Issues board with issue cards and their actions.](/learn/issues-board.png)

## Assign Copilot vs Implement
- **Assign Copilot** — hands the issue to GitHub's Copilot coding agent (needs a
  Copilot plan that includes it).
- **Implement** — uses Automatron's own built-in builder (your Anthropic key). No
  Copilot plan required.

Either way you get a **pull request** to review.

> [!TIP] You don't review alone
> Reviewing PRs is the core skill — and Automatron's AI review does the heavy
> read for you. If you haven't yet, do [Reviewing a pull request](/learn/reviewing-a-pull-request);
> it's a 60-second routine once you've done it twice.

## The extra buttons
- **Audit Code** — ask the AI to scan the repo and file issues for problems.
- **Build Check** — compile the app on the default branch and flag failures.
- **Sync** — refresh issue/PR status from GitHub if it looks stale.

> [!CHECK] When all issues are merged
> Move on to [Preview](/learn/preview) to see the whole thing running.
`,
  },
  {
    slug: "preview",
    track: "user",
    title: "Preview your app",
    summary: "See the app running before you ship — and what to look for.",
    audience: "reviewer",
    minutes: 3,
    prerequisites: ["the-build-loop"],
    body: `
Automatron builds and runs your app so you can click through it before shipping —
either a single PR's branch (from its issue) or the whole project.

> [!DO] Do this
> 1. Open the **Preview** tab.
> 2. Click **Launch Preview** (or **Restart Preview** to rebuild).
> 3. Use **Open Preview** to open it in a new browser tab.

![The Preview tab with the running app.](/learn/preview-tab.png)

> [!CHECK] What to look for
> Click through the main flows as a user would. Does it look and behave like you
> asked? Note anything off — you'll turn it into a new issue via chat.

> [!WARNING] If the preview won't start
> Open the **Activity** tab — it logs every step (clone, build, health check) and
> shows the exact error. Missing environment values (e.g. Supabase keys) are the
> most common cause.

> [!DO] Next
> When it looks right, [Deploy](/learn/deploying).
`,
  },
  {
    slug: "deploying",
    track: "user",
    title: "Deploying",
    summary: "Ship the app from the Deploy tab — target, plan, approve, execute.",
    audience: "reviewer",
    minutes: 5,
    prerequisites: ["preview"],
    body: `
Deployment is the **final stage**, done from the **Deploy** tab once the preview
looks right.

> [!DO] Do this
> 1. **Add Target** — register your server (host, SSH user, app name, domain).
> 2. **Run Inventory** — Automatron inspects what's already on the server.
> 3. **Run Analysis** — the AI proposes how to deploy.
> 4. **Create Plan → Validate** — turn it into a concrete, checked plan.
> 5. **Approve → Execute Deploy** — ship it, with a live run view and rollback.

![The Deploy tab.](/learn/deploy-tab.png)

> [!ROLE] The production gate
> This is deliberately staged and gated so a deploy is never a surprise. You
> approve the plan before it runs, and you can roll back if needed.

> [!CHECK] After executing
> Watch the run steps complete. If anything fails, the run view shows where — and
> you can roll back. Next: [Iterating](/learn/iterating).
`,
  },
  {
    slug: "iterating",
    track: "user",
    title: "Iterating after launch",
    summary: "Request changes in plain language; the AI files the issues.",
    audience: "reviewer",
    minutes: 3,
    prerequisites: ["deploying"],
    body: `
After the first build, you keep steering with the **Architect chat** — no need to
write issues by hand.

> [!DO] To request a change or report a bug
> 1. Open the **Chat** tab.
> 2. Describe the change or bug in plain language.
> 3. The Architect drafts the matching **GitHub issue(s)** for you.
> 4. Work them through the same [build loop](/learn/the-build-loop): assign →
>    review → merge.

![The Architect chat.](/learn/chat-tab.png)

> [!TIP] Health tools
> - **Audit Code** finds problems and files issues.
> - **Build Check** compiles the app and flags failures with a one-click issue.

> [!CHECK] The whole loop
> chat → issues → build → **review** → preview → deploy → repeat. You direct and
> review at every turn; the AI does the building.
`,
  },
];
