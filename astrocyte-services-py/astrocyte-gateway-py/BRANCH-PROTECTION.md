# Branch protection (GitHub)

Use this so **`main`** cannot merge without CI.

## Recommended (single gate)

1. Repo **Settings** → **Rules** → **Rulesets** (or **Branches** → **Branch protection rule** on `main`).
2. **Require a pull request before merging** (optional but typical).
3. **Require status checks to pass** → **Add checks**:
   - Search for **`CI`** — this is the aggregate job at the end of `.github/workflows/ci.yml` that runs only when **`astrocyte-py`**, **`gateway-publish-test-gate`**, and their dependencies succeed.
4. Save the rule.

Requiring **`CI`** alone is enough: you do not need to list every matrix leg unless you want finer-grained blocking.

## Optional (granular checks)

If your GitHub UI lists nested jobs from the reusable workflow, you may see names like:

- `gateway-publish-test-gate / services-ci`
- `gateway-publish-test-gate / gateway-examples-matrix (tier1-minimal)`
- `gateway-publish-test-gate / pgvector-integration`

You can add these as **additional** required checks so a failure in one sub-job blocks merge even before the aggregate **`CI`** job runs (behavior depends on how GitHub orders checks). Most teams rely on **`CI`** only.

## Forks and contributors

Outside collaborators on forks run Actions in their fork; **your** **`main`** protection applies only to PRs into this repo when you require checks on this repository.
