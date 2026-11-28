# CI cost control — 2026-11-28

The November account usage report records 690 Linux runner minutes: 360 for ci.yml, 266 for codeql.yml. Ruleset Protect main (21209425) has required contexts backend, landing, secrets, frontend, analyze (javascript-typescript), analyze (python). Do not alter the ruleset or required context names.

Draft PRs keep the full secrets job. The other existing CI and CodeQL jobs exit explicitly nonzero at their first step; a draft is NOT validated, and no success is reported for deferred checks. The backend may still incur service-container startup overhead. All original steps run on ready_for_review, subsequent ready PR updates, main, and manual dispatch. The existing CodeQL weekly schedule stays in place. Concurrency cancels only obsolete CodeQL runs for the same PR; main/scheduled/manual CodeQL runs retain independent groups.

Full backend integration (PostgreSQL and Redis), lint, typing, migrations, tests, audit, frontend/landing tests and builds, secret history scan and both CodeQL languages are unchanged. GitHub Pages and application deployment workflows are untouched. No security updates are delayed; existing Dependabot groups remain unchanged.

Before merging, validate: required contexts remain identical; a ready PR gets fresh complete checks; a draft receives explicit deferred failures and still runs secrets; both CodeQL languages pass on ready PR; scheduled and main checks remain independent. Do not treat skipped jobs, draft status, prior revision results, or billing-blocked runs as release evidence. No deployment until the actual required checks are green. Roll back by reverting the CI-only commit.

Old branches retain their old workflows until individually updated. No mass rebase, closure, or merge of existing work. Measure minutes after the new policy is merged; no guaranteed savings.
