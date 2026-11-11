# Free Webhook Relay Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deploy a free Gitee Push Webhook → Hookdeck → Cloudflare Worker → GitHub Actions relay that validates events, triggers a fixed public execution repository, updates configured submodule gitlinks, and opens or updates a human-reviewed PR.

**Architecture:** Gitee Webhooks send authenticated push notifications to Hookdeck. Hookdeck forwards the request with a separate relay credential to a Cloudflare Worker. The Worker validates both credentials, filters and forwards a fixed `repository_dispatch` event to GitHub. GitHub Actions reads the trusted main repository `.gitmodules`, fetches configured submodule branches, updates only gitlinks, pushes a stable robot branch, and creates or updates one PR.

**Tech Stack:** Hookdeck, Cloudflare Workers, Wrangler, GitHub Actions, Git, Python standard library, Node.js built-in test runner.

**Spec:** `references/platforms.md` and the existing `SKILL.md` in this repository.

## Global Constraints

- Only `.gitmodules` entries with an explicit non-empty `branch` are automatically updated.
- Worker forwards a fixed event type and never trusts event data as a destination.
- Gitee and Hookdeck relay secrets are stored in their respective protected configuration and Cloudflare Worker Secrets; GitHub credentials remain in GitHub Actions or Worker Secrets and never in URLs, files, or logs.
- The main repository target branch receives changes through a human-reviewed PR.
- The public execution repository must not expose private repository URLs, SHA values, event bodies, or credentials in logs or artifacts.
- The workflow retains a manual dispatch path for recovery.

### Task 1: Worker validation and dispatch

**Files:**
- Create: `automation/worker.js`
- Test: `automation/tests/test_worker.mjs`

- [ ] **Step 1: Run the failing tests**

Run: `node --test automation/tests/test_worker.mjs`
Expected: FAIL because `automation/worker.js` does not exist.

- [ ] **Step 2: Implement the minimal Worker handler**

Implement `fetchHandler(request, env, runtime)` with `GET /health`, `POST /hooks/gitee`, constant-time token comparison, push-event filtering, fixed GitHub dispatch URL from `GITHUB_DISPATCH_REPO`, and body `{ "event_type": "gitee-submodule-push" }`.

- [ ] **Step 3: Run the tests**

Run: `node --test automation/tests/test_worker.mjs`
Expected: PASS.

### Task 2: Reconciliation planning primitives

**Files:**
- Create: `automation/reconcile.py`
- Test: `automation/tests/test_reconcile.py`

- [ ] **Step 1: Run the failing tests**

Run: `python3 -m unittest automation/tests/test_reconcile.py -v`
Expected: FAIL because `automation/reconcile.py` does not exist.

- [ ] **Step 2: Implement minimal parsing and planning**

Implement `parse_gitmodules(text)` and `build_update_plan(entries, old_gitlinks, new_gitlinks)`. Ignore entries without `branch`; require every planned path to be present in the trusted entry list; return only changed paths with old SHA, new SHA and branch.

- [ ] **Step 3: Run the tests**

Run: `python3 -m unittest automation/tests/test_reconcile.py -v`
Expected: PASS.

### Task 3: GitHub Actions workflow and runtime checks

**Files:**
- Create: `.github/workflows/submodule-sync.yml`
- Create: `automation/run_sync.py`
- Create: `automation/tests/test_run_sync.py`

- [ ] **Step 1: Add failing runtime tests**

Cover no-op behavior, explicit-branch filtering, all-or-nothing update planning, and rejection of an existing robot branch with an unsigned manual commit.

- [ ] **Step 2: Run the failing tests**

Run: `python3 -m unittest discover -s automation/tests -v`
Expected: FAIL because the runtime module does not exist.

- [ ] **Step 3: Implement the runtime**

Use a bare Git workspace, fetch the main target branch and each configured submodule branch, build gitlink-only changes, create a stable robot branch commit, and use the Gitee API to query/create/update the matching PR. Keep credentials in environment variables and use `GIT_ASKPASS` for Git authentication.

- [ ] **Step 4: Add the workflow**

Trigger on `repository_dispatch` with type `gitee-submodule-push` and `workflow_dispatch`. Set least-privilege workflow permissions, concurrency by target repository and branch, and invoke the runtime with secrets.

- [ ] **Step 5: Run all tests**

Run: `python3 -m unittest discover -s automation/tests -v && node --test automation/tests/test_worker.mjs`
Expected: PASS.

### Task 4: Skill checklist update

**Files:**
- Modify: `SKILL.md`
- Modify: `references/platforms.md`

- [ ] **Step 1: Add a generic preflight checklist**

Require the agent to proactively list, before configuration: main repository, target branch, every submodule path/repository/branch, robot branch, PR policy, Gitee Webhook permissions, Gitee read/write/PR permissions, Cloudflare Worker and Secret permissions, GitHub repository and dispatch token permissions, Actions Secret names, event filters, and acceptance cases.

- [ ] **Step 2: Validate the skill**

Run: `python3 /Users/user01/.codex/skills/.system/skill-creator/scripts/quick_validate.py /Users/user01/.agents/skills/submodule-auto-sync`
Expected: PASS.

### Task 5: Deployment and acceptance

**Files:**
- Modify: Worker deployment configuration and platform settings only after credentials are available.

- [ ] **Step 1: Verify credentials at input time without printing values**
- [ ] **Step 2: Deploy Worker and set Worker Secrets**
- [ ] **Step 3: Configure Hookdeck and Gitee Push Webhooks for the listed source repositories and branches**
- [ ] **Step 4: Set GitHub Actions Secrets and enable the workflow**
- [ ] **Step 5: Run a test push and verify Worker response, Actions run, robot branch, and PR**
- [ ] **Step 6: Verify duplicate, unrelated-branch, unauthorized, and no-op cases**
