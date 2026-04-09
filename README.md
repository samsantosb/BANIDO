# BANIDO

AI-powered code review agent that automatically reviews every Pull Request using GPT-4o or Claude.

## How it works

```
PR opened/updated
      ↓
GitHub Actions triggers
      ↓
Agent fetches the PR diff
      ↓
Sends diff to LLM (GPT-4o or Claude)
      ↓
LLM returns structured findings
      ↓
Agent posts inline comments on the PR
```

The agent reviews for:

- **Bugs** — logic errors, null dereferences, wrong conditions
- **Security** — exposed secrets, injection vulnerabilities, unsafe defaults
- **Performance** — N+1 queries, unnecessary allocations, hidden O(n²)
- **Correctness** — race conditions, missing error handling, resource leaks
- **Maintainability** — overly complex code, duplicated logic, magic numbers

## Setup

### 1. Add your LLM API key as a GitHub Secret

Go to **Settings → Secrets and variables → Actions** in your repository and add:

| Secret | Description |
|--------|-------------|
| `ANTHROPIC_API_KEY` | Anthropic API key (recommended — uses Claude) |
| `OPENAI_API_KEY` | OpenAI API key (fallback — uses GPT-4o) |

You need at least one of them. If both are set, Claude is preferred.

### 2. The workflow runs automatically

The GitHub Actions workflow at `.github/workflows/code-review.yml` triggers on every new or updated PR. No additional configuration needed.

### 3. (Optional) Customize behavior

Copy `review_agent/config.example.yml` to `.review-agent.yml` in the root and adjust limits, ignored paths, and severity filters.

```yaml
max_comments_per_file: 5
max_files: 20
ignore_paths:
  - "**/*.lock"
  - "**/vendor/**"
```

## Repository structure

```
.github/
  workflows/
    code-review.yml       # GitHub Actions workflow
review_agent/
  reviewer.py             # Core agent logic
  requirements.txt        # Python dependencies (just httpx)
  config.example.yml      # Configuration reference
```

## Environment variables

All variables are set automatically by the workflow. For local testing:

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `GITHUB_TOKEN` | Yes | — | GitHub token (auto-injected by Actions) |
| `GITHUB_REPOSITORY` | Yes | — | `owner/repo` (auto-injected by Actions) |
| `PR_NUMBER` | Yes | — | PR number to review |
| `OPENAI_API_KEY` | One of | — | OpenAI API key |
| `ANTHROPIC_API_KEY` | One of | — | Anthropic API key |
| `OPENAI_MODEL` | No | `gpt-4o` | OpenAI model to use |
| `ANTHROPIC_MODEL` | No | `claude-opus-4-5` | Claude model to use |
| `MAX_DIFF_CHARS` | No | `60000` | Max diff size sent to LLM |
| `MAX_COMMENTS_PER_FILE` | No | `5` | Max inline comments per file |
| `MAX_FILES` | No | `20` | Max files reviewed per PR |

## Running locally

```bash
pip install httpx

export GITHUB_TOKEN="ghp_..."
export ANTHROPIC_API_KEY="sk-ant-..."   # or OPENAI_API_KEY
export GITHUB_REPOSITORY="owner/repo"
export PR_NUMBER=42

python review_agent/reviewer.py
```

## Limitations

- Very large PRs (> 60k chars of diff) are truncated to keep within LLM context limits.
- The agent does not have access to the full file context, only the changed lines.
- Auto-generated files (lockfiles, protobuf, minified assets) should be excluded via `ignore_paths`.
