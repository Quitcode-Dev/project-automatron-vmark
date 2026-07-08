import type { Lesson } from "./lessons";

/**
 * Foundations track — sets the "you direct & review, the AI builds" persona and
 * teaches just enough Git/GitHub for a non-developer to review AI work.
 * Screenshots referenced here are captured into /public/learn/*.png.
 */
export const foundationsLessons: Lesson[] = [
  {
    slug: "your-role",
    track: "foundations",
    title: "Your role: you direct, the AI builds",
    summary: "You're not the coder — you're the person who decides what to build and checks the result.",
    audience: "reviewer",
    minutes: 3,
    body: `
Automatron is built for **you to run a software project without writing code**.
You don't need to be a developer. Think of yourself as the **product owner and
reviewer**: you bring the requirements, an AI engineering team builds them, and
you check and approve the result.

> [!ROLE] What you actually do
> Just three things, over and over:
> 1. **Point Automatron at your requirements.** Your repo already holds the
>    product spec — the PRD, epics and user stories, written in plain product
>    language. The Architect translates that into a technical plan; you don't
>    write it.
> 2. **Approve** the plan before any work starts.
> 3. **Review** each change the AI proposes, and approve it (or send it back).

## Who does what

- **You** — bring the requirements (already written up in your repo), approve
  the plan, review pull requests, and decide when to preview and when to ship.
  You are the final say.
- **The Architect (AI)** — translates your product spec (PRD, epics, user
  stories) into a concrete technical plan of work.
- **The Builder (AI)** — writes the actual code for each task.
- **The Reviewer (AI)** — checks each change and leaves you a plain-language
  verdict, so you're never reading raw code alone.

> [!TIP] You are always in control
> Nothing is built until you **Approve the plan**, and nothing ships until you
> decide to deploy. The AI proposes; you dispose.

## What you'll want to understand

Because the AI works the way real software teams do — on GitHub, using
**branches** and **pull requests** — you'll get much more out of Automatron if
you understand those few ideas. That's the next lesson, in plain terms.

> [!CHECK] Before moving on
> You should be comfortable with the idea that **you review and approve, you
> don't write code.** If that clicked, continue to [Git & GitHub, for reviewers](/learn/git-github-basics).
`,
  },
  {
    slug: "git-github-basics",
    track: "foundations",
    title: "Git & GitHub, for reviewers",
    summary: "Repos, commits, branches, pull requests and merges — in plain language, only what a reviewer needs.",
    audience: "reviewer",
    minutes: 6,
    prerequisites: ["your-role"],
    body: `
The AI does its work on **GitHub**, the same way professional teams do. You don't
need to *use* Git — but you'll review its output, so here are the five words that
matter, in plain terms.

## Repository ("repo")
A **repository** is your project — all its files plus the full history of every
change ever made. Each Automatron project is connected to one GitHub repo.

## Commit
A **commit** is a single saved change, with a short message describing it (e.g.
*"add login form"*). The history is just a stack of commits. You rarely look at
individual commits — you review changes in bundles (pull requests, below).

## Branch
A **branch** is a safe parallel copy of the project. The AI never edits your live
project directly — it makes a branch, does the work there, and only after you
approve does it get combined back in. Your main, working version lives on a
branch usually called **\`main\`**.

> [!NOTE] Why branches matter to you
> Because the AI works on a branch, a half-finished or wrong change **can't break
> your live project**. You review the branch first. If it's bad, you throw the
> branch away — no harm done.

## Pull request ("PR")
A **pull request** is the AI saying: *"here's a change I'd like to add — please
review it."* It shows you exactly what changed (the **diff**) and lets you
approve or reject. **This is where you spend most of your time as a reviewer.**

![A GitHub pull request showing the "Files changed" diff — green lines were added, red lines removed.](/learn/github-pr-files.png)

## Merge
**Merging** a pull request means *accepting* it — the change moves from the
branch into your main project. Once you merge, it's part of the app.

> [!TIP] The whole loop in one sentence
> The AI makes a **branch**, bundles its work into a **pull request** for you to
> review, and once you **merge** it, the change becomes part of your project.

> [!CHECK] Check yourself
> Can you answer: *"Where does the AI's unfinished work live, and how do I accept
> it?"* (Answer: on a branch, inside a pull request; you accept it by merging.)
> Next: [How Automatron uses Git](/learn/how-automatron-uses-git).
`,
  },
  {
    slug: "how-automatron-uses-git",
    track: "foundations",
    title: "How Automatron uses Git (the flow)",
    summary: "The branch-and-PR flow Automatron follows, and where your two approval moments sit.",
    audience: "reviewer",
    minutes: 5,
    prerequisites: ["git-github-basics"],
    body: `
Automatron follows a standard team workflow (often called **Gitflow**). Here's
the exact path a single task takes — and where **you** step in.

## The path of one task

1. You **approve the plan** → Automatron creates a GitHub **Issue** for the task.
2. The AI builder does the work on a branch named **\`agent-sdk/fix-<number>\`**
   (or, if you use GitHub Copilot, Copilot uses its own branch).
3. The branch is opened as a **pull request** against your **\`main\`** branch.
4. Automatron **AI-reviews** the pull request and posts a plain-language verdict.
5. **You review and merge** — the change lands on \`main\`.
6. Merging triggers a **build check** to confirm the app still compiles.

> [!DO] Your two approval moments
> 1. **Approve the plan** — before any code is written.
> 2. **Review & merge each pull request** — before each change becomes part of
> the app. (Then you preview, then you deploy — two more decisions that are
> entirely yours.)

## What the branch names tell you

- **\`main\`** — your real, current project. Everything merged lands here.
- **\`agent-sdk/fix-42\`** — the AI's in-progress work for issue #42. Safe to
  review; nothing here affects \`main\` until you merge.
- Some projects also use a **\`develop\`** branch as a staging area before \`main\`.

> [!TIP] You'll never be lost
> Automatron shows each issue's status right on the board — *Working…*, *PR
> Ready*, *Review Passed / Changes Needed*, *Merged* — so you always know whose
> turn it is (yours or the AI's).

> [!CHECK] Ready to review
> If you understand *branch → pull request → your review → merge*, you're ready
> for the hands-on lesson: [Reviewing a pull request](/learn/reviewing-a-pull-request).
`,
  },
  {
    slug: "reviewing-a-pull-request",
    track: "foundations",
    title: "Reviewing a pull request",
    summary: "Step-by-step: open the PR, read the AI review, skim the diff, and approve & merge.",
    audience: "reviewer",
    minutes: 7,
    prerequisites: ["how-automatron-uses-git"],
    body: `
This is the skill you'll use most. Good news: you don't have to understand every
line of code — Automatron's AI review does the deep read and gives you a
verdict. Your job is to sanity-check and decide.

## Step 1 — Open the pull request
On the **Issues** tab, an issue with a PR shows a **PR #n** link (and, in
Automatron, a **Request AI Review** / **Approve & Merge** button). Click through
to open it on GitHub.

![An Automatron issue card with its pull-request actions.](/learn/issue-card-actions.png)

## Step 2 — Read the AI review first
Automatron posts an **"Automatron AI Review"** comment on the PR (and shows a
Passed / Changes-Needed badge on the card). Read this before anything else — it's
written for you, in plain language: what the change does, and whether it looks
correct.

![Automatron's AI review verdict on an issue.](/learn/ai-review.png)

> [!TIP] Trust, but verify
> A **Passed** verdict means the AI reviewer found no problems. It's usually
> right — but you're the product owner, so still do the quick check in Step 3.

## Step 3 — Skim the "Files changed" tab
On the PR, open **Files changed**. You don't need to read code — look for:

> [!CHECK] What to check (30 seconds)
> - Does the **PR title/description** match what you actually asked for?
> - Are the **changed files** roughly what you'd expect (e.g. a "login page" task
>   touching login-related files, not deleting half the app)?
> - Did the **AI review** flag anything you care about?

![The GitHub "Files changed" view — additions in green, deletions in red.](/learn/github-pr-files.png)

## Step 4 — Preview before you merge (optional but recommended)
Automatron can spin up the change as a **live preview** from its branch, so you
can click through it before it becomes part of the app. Use **Preview branch** on
the issue card.

## Step 5 — Approve & merge (or send it back)
- **Looks good?** Click **Approve & Merge** (or the green **Merge** button on
  GitHub). The change lands on \`main\`.
- **Not right?** Use **Re-implement** (send it back to the builder with your
  notes) instead of merging.

> [!TIP] The green "Merge" button
> On GitHub, once a pull request is approved you click the green **Merge pull
> request** button at the bottom of the **Conversation** tab to accept it.
> Because you own the repo, that button is yours — Automatron's **Approve &
> Merge** simply takes you straight to it.

> [!WARNING] Merging is the commitment point
> Everything before merging is reversible (just don't merge). **Merging** is when
> the change becomes part of your project — so it's the moment to be sure. When in
> doubt, preview first or send it back.

> [!DO] Practice
> Next time an issue reaches **PR Ready**, walk these five steps once. After one
> or two, it takes under a minute. Continue to the [Getting Started](/learn/what-is-automatron)
> track to run a full project.
`,
  },
];
