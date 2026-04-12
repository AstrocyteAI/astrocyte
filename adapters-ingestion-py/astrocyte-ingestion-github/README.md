# astrocyte-ingestion-github

**Poll** driver for Astrocyte `sources:` — ingests **GitHub repository issues** (not pull requests) via the [REST API](https://docs.github.com/en/rest/issues/issues#list-repository-issues).

## Install

```bash
pip install astrocyte-ingestion-github
# or
pip install 'astrocyte[poll]'
```

## Config (`astrocyte.yaml`)

```yaml
sources:
  gh_issues:
    type: poll
    driver: github
    path: octocat/Hello-World          # owner/repo
    interval_seconds: 120              # >= 60 (GitHub API rate limits)
    target_bank: engineering
    auth:
      token: ${GITHUB_TOKEN}           # classic PAT or fine-grained token (issues read)
    extraction_profile: builtin_text   # optional
# Optional: GitHub Enterprise Server API root
#    url: https://github.example.com/api/v3
```

The adapter sets `Authorization: Bearer …` and uses `since` (max `updated_at` from the last response) to limit traffic. Each issue is retained as text `[GitHub #N] title` plus body; metadata includes `github.issue_id`, `number`, `html_url`, `updated_at`, `author`.

Principal for bank resolution: `sources.*.principal` if set; otherwise `github:<author_login>` from the issue.

## Entry point

Registers as **`github`** under **`astrocyte.ingest_poll_drivers`** (same discovery pattern as stream drivers).
