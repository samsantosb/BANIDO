"""
Code Review Agent — pistola
Lê o diff de um PR, manda pro LLM e posta comentários inline no GitHub.
"""

import os
import sys
import json
import re
import textwrap
from dataclasses import dataclass, field
from typing import Optional

import httpx


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")

GITHUB_REPO = os.environ["GITHUB_REPOSITORY"]          # "owner/repo"
PR_NUMBER = int(os.environ["PR_NUMBER"])

# Prefer Claude, fallback to GPT-4o
LLM_PROVIDER = "anthropic" if ANTHROPIC_API_KEY else "openai"

OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o")
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-opus-4-5")

MAX_DIFF_CHARS = int(os.environ.get("MAX_DIFF_CHARS", "60000"))
MAX_COMMENTS_PER_FILE = int(os.environ.get("MAX_COMMENTS_PER_FILE", "5"))
MAX_FILES = int(os.environ.get("MAX_FILES", "20"))

GITHUB_API = "https://api.github.com"


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class ReviewComment:
    path: str
    line: int
    body: str
    severity: str = "suggestion"  # bug | security | performance | suggestion | style


@dataclass
class DiffFile:
    path: str
    patch: str
    additions: int = 0
    deletions: int = 0
    line_map: dict = field(default_factory=dict)  # display line → actual line number


# ---------------------------------------------------------------------------
# GitHub API helpers
# ---------------------------------------------------------------------------

def gh_headers() -> dict:
    return {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def get_pr_files() -> list[DiffFile]:
    url = f"{GITHUB_API}/repos/{GITHUB_REPO}/pulls/{PR_NUMBER}/files"
    files: list[DiffFile] = []

    page = 1
    while True:
        r = httpx.get(url, headers=gh_headers(), params={"per_page": 100, "page": page})
        r.raise_for_status()
        data = r.json()
        if not data:
            break

        for f in data:
            if f.get("status") in ("removed", "renamed"):
                continue
            patch = f.get("patch", "")
            if not patch:
                continue
            df = DiffFile(
                path=f["filename"],
                patch=patch,
                additions=f.get("additions", 0),
                deletions=f.get("deletions", 0),
            )
            df.line_map = _build_line_map(patch)
            files.append(df)

        page += 1
        if len(data) < 100:
            break

    return files[:MAX_FILES]


def _build_line_map(patch: str) -> dict[int, int]:
    """Map display line index (1-based within patch) → actual file line number."""
    line_map: dict[int, int] = {}
    current_line = 0
    display_idx = 0

    for raw_line in patch.splitlines():
        if raw_line.startswith("@@"):
            m = re.search(r"\+(\d+)", raw_line)
            if m:
                current_line = int(m.group(1)) - 1
            display_idx += 1
        elif raw_line.startswith("-"):
            display_idx += 1
        else:
            current_line += 1
            display_idx += 1
            line_map[display_idx] = current_line

    return line_map


def get_pr_commit_id() -> str:
    url = f"{GITHUB_API}/repos/{GITHUB_REPO}/pulls/{PR_NUMBER}"
    r = httpx.get(url, headers=gh_headers())
    r.raise_for_status()
    return r.json()["head"]["sha"]


def post_review(comments: list[ReviewComment], commit_id: str) -> None:
    if not comments:
        print("No comments to post.")
        return

    body_lines = [
        "## Code Review by AI Agent",
        "",
        f"Found **{len(comments)}** issue(s).",
        "",
        "| Severity | File | Line | Issue |",
        "|----------|------|------|-------|",
    ]
    for c in comments:
        emoji = {"bug": "🐛", "security": "🔒", "performance": "⚡", "suggestion": "💡", "style": "🎨"}.get(c.severity, "💬")
        short = c.body.split("\n")[0][:80]
        body_lines.append(f"| {emoji} {c.severity} | `{c.path}` | {c.line} | {short} |")

    review_body = "\n".join(body_lines)

    gh_comments = []
    for c in comments:
        gh_comments.append({
            "path": c.path,
            "line": c.line,
            "side": "RIGHT",
            "body": _format_comment(c),
        })

    payload = {
        "commit_id": commit_id,
        "body": review_body,
        "event": "COMMENT",
        "comments": gh_comments,
    }

    url = f"{GITHUB_API}/repos/{GITHUB_REPO}/pulls/{PR_NUMBER}/reviews"
    r = httpx.post(url, headers=gh_headers(), json=payload)
    if r.status_code not in (200, 201):
        print(f"GitHub API error {r.status_code}: {r.text}", file=sys.stderr)
        # Fallback: post as a single PR comment
        _post_fallback_comment(review_body)
        return
    print(f"Posted review with {len(gh_comments)} inline comment(s).")


def _format_comment(c: ReviewComment) -> str:
    emoji = {"bug": "🐛", "security": "🔒", "performance": "⚡", "suggestion": "💡", "style": "🎨"}.get(c.severity, "💬")
    return f"{emoji} **{c.severity.upper()}**\n\n{c.body}"


def _post_fallback_comment(body: str) -> None:
    url = f"{GITHUB_API}/repos/{GITHUB_REPO}/issues/{PR_NUMBER}/comments"
    r = httpx.post(url, headers=gh_headers(), json={"body": body})
    r.raise_for_status()
    print("Posted fallback summary comment.")


# ---------------------------------------------------------------------------
# LLM call
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = textwrap.dedent("""
    You are a world-class senior software engineer performing a pull request code review.
    Your job is to find real, actionable problems — not nitpick style for its own sake.

    Focus on:
    1. **Bugs** — logic errors, off-by-one, null/undefined dereferences, wrong conditions
    2. **Security** — exposed secrets, injection vulnerabilities, unsafe deserialization, insecure defaults
    3. **Performance** — O(n²) in disguise, missing indexes, N+1 queries, unnecessary allocations
    4. **Correctness** — incorrect algorithm, race conditions, missing error handling, resource leaks
    5. **Maintainability** — severely complex code without explanation, duplicated logic, magic numbers

    Do NOT comment on:
    - Minor style preferences (tabs vs spaces, quote style) unless the project already has a linter
    - Things that are perfectly fine just because they're different from how you'd write them
    - Missing documentation on obvious code

    Output a JSON array of objects. Each object must have:
    {
      "path": "<file path as given>",
      "line": <integer — the line number in the file for the new/changed code>,
      "severity": "<bug | security | performance | suggestion | style>",
      "body": "<clear, concise explanation in markdown — what's wrong, why, and how to fix it>"
    }

    Return ONLY the JSON array. No markdown fences, no prose outside the array.
    If there are no issues worth reporting, return an empty array: []
""").strip()


def call_llm(diff_text: str) -> list[dict]:
    user_message = (
        f"Review the following pull request diff and return your findings as a JSON array.\n\n"
        f"```diff\n{diff_text}\n```"
    )

    if LLM_PROVIDER == "anthropic":
        return _call_anthropic(user_message)
    return _call_openai(user_message)


def _call_openai(user_message: str) -> list[dict]:
    import httpx

    payload = {
        "model": OPENAI_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ],
        "temperature": 0.2,
        "max_tokens": 4096,
    }

    r = httpx.post(
        "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
        json=payload,
        timeout=120,
    )
    r.raise_for_status()
    content = r.json()["choices"][0]["message"]["content"]
    return _parse_llm_output(content)


def _call_anthropic(user_message: str) -> list[dict]:
    payload = {
        "model": ANTHROPIC_MODEL,
        "max_tokens": 4096,
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": user_message}],
        "temperature": 0.2,
    }

    r = httpx.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json=payload,
        timeout=120,
    )
    r.raise_for_status()
    content = r.json()["content"][0]["text"]
    return _parse_llm_output(content)


def _parse_llm_output(content: str) -> list[dict]:
    content = content.strip()

    # Strip markdown fences if the model wrapped the output anyway
    if content.startswith("```"):
        content = re.sub(r"^```[a-z]*\n?", "", content)
        content = re.sub(r"\n?```$", "", content)

    try:
        return json.loads(content)
    except json.JSONDecodeError as e:
        print(f"Failed to parse LLM output as JSON: {e}\nRaw output:\n{content[:500]}", file=sys.stderr)
        return []


# ---------------------------------------------------------------------------
# Core orchestration
# ---------------------------------------------------------------------------

def build_diff_text(files: list[DiffFile]) -> str:
    parts = []
    total = 0
    for f in files:
        chunk = f"--- {f.path}\n+++ {f.path}\n{f.patch}\n"
        if total + len(chunk) > MAX_DIFF_CHARS:
            parts.append(f"[diff truncated — too large]\n")
            break
        parts.append(chunk)
        total += len(chunk)
    return "\n".join(parts)


def map_comments_to_lines(
    raw: list[dict],
    files: list[DiffFile],
) -> list[ReviewComment]:
    file_map = {f.path: f for f in files}
    comments: list[ReviewComment] = []
    per_file: dict[str, int] = {}

    for item in raw:
        path = item.get("path", "")
        line = item.get("line")
        severity = item.get("severity", "suggestion")
        body = item.get("body", "").strip()

        if not path or not line or not body:
            continue

        df = file_map.get(path)
        if df is None:
            continue

        # The LLM gives us an actual file line number; validate it exists in the diff
        valid_lines = set(df.line_map.values())
        if not valid_lines:
            continue

        if line not in valid_lines:
            # Snap to nearest changed line
            line = min(valid_lines, key=lambda x: abs(x - line))

        per_file[path] = per_file.get(path, 0) + 1
        if per_file[path] > MAX_COMMENTS_PER_FILE:
            continue

        comments.append(ReviewComment(path=path, line=line, body=body, severity=severity))

    return comments


def run() -> None:
    print(f"Code Review Agent starting — {GITHUB_REPO} PR #{PR_NUMBER}")
    print(f"LLM provider: {LLM_PROVIDER}")

    files = get_pr_files()
    if not files:
        print("No reviewable files found in this PR.")
        return

    print(f"Reviewing {len(files)} file(s)…")
    diff_text = build_diff_text(files)

    raw_findings = call_llm(diff_text)
    print(f"LLM returned {len(raw_findings)} finding(s).")

    comments = map_comments_to_lines(raw_findings, files)
    print(f"Mapped to {len(comments)} valid inline comment(s).")

    commit_id = get_pr_commit_id()
    post_review(comments, commit_id)


if __name__ == "__main__":
    run()
