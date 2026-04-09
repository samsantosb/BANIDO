"""
Code Review Agent — pistola
Lê GUIDELINES.md como contexto, faz review cirúrgico.

Veredictos por gravidade:
  BANIDO     — problema real que precisa ser corrigido
  EXILADO    — muito ruim, bug sério ou falha de segurança grave
  OBLITERADO — terrível, catastrófico, inaceitável em qualquer codebase
"""

import os
import sys
import json
import re
import textwrap
from dataclasses import dataclass, field
from pathlib import Path

import httpx


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")

GITHUB_REPO = os.environ["GITHUB_REPOSITORY"]
PR_NUMBER = int(os.environ["PR_NUMBER"])

LLM_PROVIDER = "anthropic" if ANTHROPIC_API_KEY else "openai"
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o")
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-opus-4-5")

MAX_DIFF_CHARS = int(os.environ.get("MAX_DIFF_CHARS", "80000"))
MAX_COMMENTS_PER_FILE = int(os.environ.get("MAX_COMMENTS_PER_FILE", "8"))
MAX_FILES = int(os.environ.get("MAX_FILES", "30"))
GUIDELINES_PATH = os.environ.get("GUIDELINES_PATH", "GUIDELINES.md")

GITHUB_API = "https://api.github.com"

SEVERITY_EMOJI = {
    "bug":         "🐛",
    "security":    "🔒",
    "performance": "⚡",
    "guideline":   "📋",
    "suggestion":  "💡",
    "style":       "🎨",
}

# Veredicto por gravidade (LLM retorna um campo "gravity": 1 | 2 | 3)
# 1 = ruim       → BANIDO
# 2 = muito ruim → EXILADO
# 3 = terrível   → OBLITERADO
VERDICT_BANIDO     = "BANIDO"
VERDICT_EXILADO    = "EXILADO"
VERDICT_OBLITERADO = "OBLITERADO"

VERDICT_BY_GRAVITY = {1: VERDICT_BANIDO, 2: VERDICT_EXILADO, 3: VERDICT_OBLITERADO}

VERDICT_HEADER = {
    VERDICT_BANIDO:     "# BANIDO",
    VERDICT_EXILADO:    "# EXILADO",
    VERDICT_OBLITERADO: "# OBLITERADO",
}

VERDICT_EMOJI = {
    VERDICT_BANIDO:     "🚫",
    VERDICT_EXILADO:    "☠️",
    VERDICT_OBLITERADO: "💀",
}

BANIDO_SEVERITIES = {"bug", "security", "performance", "guideline"}


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class ReviewComment:
    path: str
    line: int
    body: str
    severity: str = "suggestion"
    banido: bool = False
    verdict: str = VERDICT_BANIDO  # BANIDO | EXILADO | OBLITERADO


@dataclass
class DiffFile:
    path: str
    patch: str
    full_content: str = ""
    additions: int = 0
    deletions: int = 0
    line_map: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# GitHub API helpers
# ---------------------------------------------------------------------------

def gh_headers() -> dict:
    return {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def get_pr_meta() -> dict:
    r = httpx.get(f"{GITHUB_API}/repos/{GITHUB_REPO}/pulls/{PR_NUMBER}", headers=gh_headers())
    r.raise_for_status()
    return r.json()


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
            if f.get("status") == "removed":
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


def get_file_content(path: str, ref: str) -> str:
    """Fetch full file content at a given ref for richer context."""
    url = f"{GITHUB_API}/repos/{GITHUB_REPO}/contents/{path}"
    r = httpx.get(url, headers=gh_headers(), params={"ref": ref})
    if r.status_code != 200:
        return ""
    import base64
    data = r.json()
    if data.get("encoding") == "base64":
        try:
            return base64.b64decode(data["content"]).decode("utf-8", errors="replace")
        except Exception:
            return ""
    return ""


def get_guidelines() -> str:
    """Try to fetch GUIDELINES.md from the repo's default branch."""
    url = f"{GITHUB_API}/repos/{GITHUB_REPO}/contents/{GUIDELINES_PATH}"
    r = httpx.get(url, headers=gh_headers())
    if r.status_code != 200:
        # Also check if it exists locally (when running in Actions checkout)
        local = Path(GUIDELINES_PATH)
        if local.exists():
            return local.read_text(encoding="utf-8")
        return ""

    import base64
    data = r.json()
    if data.get("encoding") == "base64":
        try:
            return base64.b64decode(data["content"]).decode("utf-8", errors="replace")
        except Exception:
            return ""
    return ""


def _build_line_map(patch: str) -> dict[int, int]:
    """Map patch display index (1-based) → actual file line number (right/new side)."""
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


def post_review(comments: list[ReviewComment], commit_id: str, pr_meta: dict) -> None:
    if not comments:
        _post_approval(commit_id)
        return

    obliterado = [c for c in comments if c.verdict == VERDICT_OBLITERADO]
    exilado    = [c for c in comments if c.verdict == VERDICT_EXILADO]
    banido     = [c for c in comments if c.verdict == VERDICT_BANIDO and c.banido]
    suggestions = [c for c in comments if not c.banido]

    serious_count = len(obliterado) + len(exilado) + len(banido)

    # Score: OBLITERADO pesa 5, EXILADO pesa 3, BANIDO pesa 2, suggestion pesa 0.5
    penalty = len(obliterado) * 5 + len(exilado) * 3 + len(banido) * 2 + len(suggestions) * 0.5
    score = max(0, round(10 - penalty))
    score_bar = "█" * score + "░" * (10 - score)

    # Choose overall verdict label for the title
    if obliterado:
        title_verdict = "💀 OBLITERADO"
    elif exilado:
        title_verdict = "☠️  EXILADO"
    elif banido:
        title_verdict = "🚫 BANIDO"
    else:
        title_verdict = "💡 Sugestões"

    body_lines = [
        f"# {title_verdict} — Code Review",
        "",
        f"**Quality score:** `{score_bar}` {score}/10",
        "",
    ]

    if obliterado:
        body_lines += [
            f"**💀 {len(obliterado)} OBLITERADO** — código catastrófico, inaceitável. Reescreva antes de qualquer merge.",
            "",
        ]
    if exilado:
        body_lines += [
            f"**☠️  {len(exilado)} EXILADO** — muito ruim. Bug sério ou falha de segurança grave.",
            "",
        ]
    if banido:
        body_lines += [
            f"**🚫 {len(banido)} BANIDO** — problema real que precisa ser corrigido.",
            "",
        ]
    if suggestions:
        body_lines += [
            f"**{len(suggestions)}** sugestão(ões) de melhoria.",
            "",
        ]

    body_lines += [
        "| Veredicto | Severidade | Arquivo | Linha | Resumo |",
        "|-----------|----------|------|------|-------|",
    ]

    for c in comments:
        sev_emoji = SEVERITY_EMOJI.get(c.severity, "💬")
        v_emoji = VERDICT_EMOJI.get(c.verdict, "💬") if c.banido else "  "
        verdict_label = c.verdict if c.banido else "—"
        short = c.body.split("\n")[0][:90]
        # Strip verdict word from start of summary to avoid redundancy
        for w in (VERDICT_OBLITERADO, VERDICT_EXILADO, VERDICT_BANIDO):
            short = short.replace(w, "").strip()
        body_lines.append(
            f"| {v_emoji} {verdict_label} | {sev_emoji} {c.severity} | `{c.path}` | {c.line} | {short} |"
        )

    review_body = "\n".join(body_lines)

    gh_comments = [
        {
            "path": c.path,
            "line": c.line,
            "side": "RIGHT",
            "body": _format_comment(c),
        }
        for c in comments
    ]

    payload = {
        "commit_id": commit_id,
        "body": review_body,
        "event": "REQUEST_CHANGES" if serious_count > 0 else "COMMENT",
        "comments": gh_comments,
    }

    url = f"{GITHUB_API}/repos/{GITHUB_REPO}/pulls/{PR_NUMBER}/reviews"
    r = httpx.post(url, headers=gh_headers(), json=payload)

    if r.status_code not in (200, 201):
        print(f"GitHub API error {r.status_code}: {r.text}", file=sys.stderr)
        _post_fallback_comment(review_body)
        return

    action = "REQUEST_CHANGES" if serious_count > 0 else "COMMENT"
    print(f"Posted review ({action}) with {len(gh_comments)} inline comment(s).")


def _post_approval(commit_id: str) -> None:
    payload = {
        "commit_id": commit_id,
        "body": (
            "# BANIDO Code Review\n\n"
            "**Quality score:** `██████████` 10/10\n\n"
            "Código limpo. Nenhum problema encontrado. ✅"
        ),
        "event": "APPROVE",
    }
    url = f"{GITHUB_API}/repos/{GITHUB_REPO}/pulls/{PR_NUMBER}/reviews"
    r = httpx.post(url, headers=gh_headers(), json=payload)
    if r.status_code in (200, 201):
        print("PR approved — code is clean.")
    else:
        print(f"Could not post approval: {r.status_code}", file=sys.stderr)


def _format_comment(c: ReviewComment) -> str:
    emoji = SEVERITY_EMOJI.get(c.severity, "💬")
    if c.banido:
        v_header = VERDICT_HEADER[c.verdict]
        header = f"{v_header}\n\n{emoji} **{c.severity.upper()}**"
    else:
        header = f"{emoji} **{c.severity.upper()}**"
    return f"{header}\n\n{c.body}"


def _post_fallback_comment(body: str) -> None:
    url = f"{GITHUB_API}/repos/{GITHUB_REPO}/issues/{PR_NUMBER}/comments"
    r = httpx.post(url, headers=gh_headers(), json={"body": body})
    r.raise_for_status()
    print("Posted fallback summary comment.")


# ---------------------------------------------------------------------------
# Heuristic pre-scan (fast, before calling LLM)
# ---------------------------------------------------------------------------

# (pattern, severity, gravity, message)
# gravity 1=BANIDO  2=EXILADO  3=OBLITERADO
HEURISTIC_PATTERNS = [
    # OBLITERADO — credencial hardcoded é catastrófico
    (re.compile(r'(?i)(password|passwd|secret|api_?key|token|private_?key)\s*=\s*["\'][^"\']{4,}["\']'), "security", 3,
     "Credencial hardcoded no código-fonte. Qualquer pessoa com acesso ao repo tem essa chave. "
     "Revogue imediatamente e mova para variável de ambiente ou secret manager."),

    # OBLITERADO — eval com input externo
    (re.compile(r'^\+.*\beval\s*\(.*(?:request|input|params|query|body|user)', re.MULTILINE), "security", 3,
     "`eval()` executando input controlado pelo usuário. Remote Code Execution (RCE) direto. "
     "Remova imediatamente."),

    # EXILADO — SQL por concatenação
    (re.compile(r'(?i)(execute|query|cursor\.execute)\s*\(\s*[f"\'"].*\+'), "security", 2,
     "Concatenação de string em query SQL — SQL injection clássico. "
     "Use parâmetros preparados: `cursor.execute(query, (param,))`"),

    # EXILADO — eval sem input visível mas ainda perigoso
    (re.compile(r'^\+.*\beval\s*\('), "security", 2,
     "`eval()` executa código arbitrário. Extremamente perigoso com qualquer dado externo. "
     "Substitua por uma abordagem segura."),

    # BANIDO — except genérico
    (re.compile(r'^\+\s*except\s*:', re.MULTILINE), "bug", 1,
     "`except:` sem tipo captura `BaseException`, incluindo `KeyboardInterrupt` e `SystemExit`. "
     "Use `except Exception:` no mínimo, ou capture a exceção específica."),

    # BANIDO — debug print em produção
    (re.compile(r'^\+\s*(print\(|console\.log\(|System\.out\.print)', re.MULTILINE), "style", 1,
     "Debug statement detectado. Remova ou substitua por logging estruturado."),

    # BANIDO — TODO/FIXME em código novo
    (re.compile(r'^\+.*(TODO|FIXME|HACK|XXX)', re.MULTILINE), "suggestion", 1,
     "TODO/FIXME em código adicionado. Resolva antes do merge ou abra uma issue rastreável."),

    # BANIDO — comparação explícita com booleano
    (re.compile(r'^\+.*(==\s*True|==\s*False)', re.MULTILINE), "style", 1,
     "Comparação explícita com booleano. Use `if x:` em vez de `if x == True:`."),
]


def run_heuristics(files: list[DiffFile]) -> list[ReviewComment]:
    found: list[ReviewComment] = []
    for df in files:
        for pattern, severity, gravity, message in HEURISTIC_PATTERNS:
            for m in pattern.finditer(df.patch):
                patch_before = df.patch[: m.start()]
                patch_line_idx = patch_before.count("\n") + 1
                line = df.line_map.get(patch_line_idx)
                if line is None:
                    valid = list(df.line_map.values())
                    if not valid:
                        continue
                    line = valid[0]
                banido = severity in BANIDO_SEVERITIES
                verdict = VERDICT_BY_GRAVITY.get(gravity, VERDICT_BANIDO)
                found.append(
                    ReviewComment(
                        path=df.path,
                        line=line,
                        body=message,
                        severity=severity,
                        banido=banido,
                        verdict=verdict,
                    )
                )
    return found


# ---------------------------------------------------------------------------
# System prompt builder
# ---------------------------------------------------------------------------

def build_system_prompt(guidelines: str) -> str:
    guidelines_section = ""
    if guidelines.strip():
        guidelines_section = textwrap.dedent(f"""
            ## PROJECT GUIDELINES

            The project has specific guidelines that ALL code must follow. Violations are BANIDO.
            Study them carefully and flag any violation:

            ---
            {guidelines.strip()}
            ---
        """)

    return textwrap.dedent(f"""
        You are the most brutal, precise, and unforgiving senior software engineer in existence.
        Your job: find every real problem in this PR diff. You do not praise. You do not sugarcoat.
        You either find problems or you say nothing.

        {guidelines_section}

        ## REVIEW PRIORITIES (in order)

        1. **security** — credentials in code, injection vectors, insecure deserialization,
           missing auth/authz, unsafe defaults, path traversal, open redirects
        2. **bug** — logic errors, off-by-one, null/undefined dereferences, incorrect conditions,
           wrong algorithm, missing edge cases, broken error handling, resource leaks
        3. **performance** — N+1 queries, missing pagination, O(n²) hidden in loops,
           synchronous I/O blocking async code, unnecessary large allocations, missing caching
        4. **guideline** — violations of the project's own GUIDELINES.md rules
        5. **suggestion** — non-obvious design improvements that genuinely matter
        6. **style** — only flag style if it actively harms readability or contradicts a stated linter

        ## RULES

        - DO flag: real bugs, security holes, perf cliffs, guideline violations, subtle correctness issues
        - DO NOT flag: personal style preferences, renaming for its own sake, missing comments on obvious code
        - Every finding must include: what is wrong, WHY it matters, and a concrete fix or code example
        - For `bug`, `security`, `performance`, `guideline`: set `"banido": true`
        - For `suggestion`, `style`: set `"banido": false`
        - Line numbers must refer to the NEW file (right side of diff, lines starting with `+`)
        - Be precise: point to the exact line, not a nearby one

        ## GRAVITY SCALE

        Every `banido: true` finding must have a `gravity` field (1, 2, or 3):

        - **1 = BANIDO** — real problem, needs fixing before merge
          Examples: missing error handling, magic number causing silent bugs, resource not closed,
          guideline violation, minor logic error

        - **2 = EXILADO** — serious bug or significant security flaw
          Examples: SQL injection, auth bypass, race condition that corrupts data,
          off-by-one that causes crashes, unhandled exception that takes down the service,
          exposed sensitive data in logs/responses

        - **3 = OBLITERADO** — catastrophic, production-destroying, or unforgivable
          Examples: hardcoded credentials committed to repo, RCE vector (eval with user input),
          deleting production data without confirmation, complete auth bypass on critical endpoint,
          infinite loop with no escape in request handler, dropping entire database tables

        `banido: false` findings (suggestion/style) must omit `gravity` or set it to null.

        ## OUTPUT FORMAT

        Return ONLY a JSON array. Each element:
        {{
          "path": "<file path exactly as shown>",
          "line": <integer — exact new-file line number>,
          "severity": "<security | bug | performance | guideline | suggestion | style>",
          "banido": <true | false>,
          "gravity": <1 | 2 | 3 | null>,
          "body": "<markdown — what is wrong, why it matters, how to fix it with a code example>"
        }}

        No markdown fences. No prose outside the array. If nothing is wrong, return: []
    """).strip()


# ---------------------------------------------------------------------------
# LLM calls
# ---------------------------------------------------------------------------

def call_llm(diff_text: str, guidelines: str) -> list[dict]:
    system = build_system_prompt(guidelines)
    user = (
        "Review the following pull request diff. Be ruthless. Find every real problem.\n\n"
        f"```diff\n{diff_text}\n```"
    )

    if LLM_PROVIDER == "anthropic":
        return _call_anthropic(system, user)
    return _call_openai(system, user)


def _call_openai(system: str, user: str) -> list[dict]:
    payload = {
        "model": OPENAI_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.1,
        "max_tokens": 8192,
    }
    r = httpx.post(
        "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
        json=payload,
        timeout=180,
    )
    r.raise_for_status()
    content = r.json()["choices"][0]["message"]["content"]
    return _parse_llm_output(content)


def _call_anthropic(system: str, user: str) -> list[dict]:
    payload = {
        "model": ANTHROPIC_MODEL,
        "max_tokens": 8192,
        "system": system,
        "messages": [{"role": "user", "content": user}],
        "temperature": 0.1,
    }
    r = httpx.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json=payload,
        timeout=180,
    )
    r.raise_for_status()
    content = r.json()["content"][0]["text"]
    return _parse_llm_output(content)


def _parse_llm_output(content: str) -> list[dict]:
    content = content.strip()
    if content.startswith("```"):
        content = re.sub(r"^```[a-z]*\n?", "", content)
        content = re.sub(r"\n?```$", "", content)
    try:
        result = json.loads(content)
        if isinstance(result, list):
            return result
        return []
    except json.JSONDecodeError as e:
        print(f"Failed to parse LLM output: {e}\nRaw:\n{content[:800]}", file=sys.stderr)
        return []


# ---------------------------------------------------------------------------
# Diff builder
# ---------------------------------------------------------------------------

def build_diff_text(files: list[DiffFile]) -> str:
    parts = []
    total = 0
    for f in files:
        chunk = f"=== {f.path} (+{f.additions}/-{f.deletions}) ===\n{f.patch}\n"
        if total + len(chunk) > MAX_DIFF_CHARS:
            parts.append(f"[truncated — diff too large, showing first {total} chars]\n")
            break
        parts.append(chunk)
        total += len(chunk)
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Comment resolution
# ---------------------------------------------------------------------------

def resolve_llm_comments(raw: list[dict], files: list[DiffFile]) -> list[ReviewComment]:
    file_map = {f.path: f for f in files}
    comments: list[ReviewComment] = []
    per_file: dict[str, int] = {}

    for item in raw:
        path = item.get("path", "")
        line = item.get("line")
        severity = item.get("severity", "suggestion")
        body = (item.get("body") or "").strip()
        banido = bool(item.get("banido", False)) or severity in BANIDO_SEVERITIES
        gravity = item.get("gravity") or (1 if banido else None)
        verdict = VERDICT_BY_GRAVITY.get(gravity, VERDICT_BANIDO) if banido else VERDICT_BANIDO

        if not path or not line or not body:
            continue

        df = file_map.get(path)
        if df is None:
            continue

        valid_lines = set(df.line_map.values())
        if not valid_lines:
            continue

        if line not in valid_lines:
            line = min(valid_lines, key=lambda x: abs(x - line))

        per_file[path] = per_file.get(path, 0) + 1
        if per_file[path] > MAX_COMMENTS_PER_FILE:
            continue

        comments.append(
            ReviewComment(path=path, line=line, body=body, severity=severity, banido=banido, verdict=verdict)
        )

    return comments


def deduplicate(
    heuristic: list[ReviewComment],
    llm: list[ReviewComment],
) -> list[ReviewComment]:
    """Merge both lists, dropping near-duplicate (same file + line) entries."""
    seen: set[tuple[str, int]] = set()
    result: list[ReviewComment] = []

    for c in heuristic + llm:
        key = (c.path, c.line)
        if key in seen:
            continue
        seen.add(key)
        result.append(c)

    verdict_order = {VERDICT_OBLITERADO: 0, VERDICT_EXILADO: 1, VERDICT_BANIDO: 2}
    result.sort(key=lambda c: (
        verdict_order.get(c.verdict, 3) if c.banido else 4,
        c.path,
        c.line,
    ))
    return result


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run() -> None:
    print(f"BANIDO Code Review Agent — {GITHUB_REPO} PR #{PR_NUMBER}")
    print(f"LLM: {LLM_PROVIDER} / {'anthropic' == LLM_PROVIDER and ANTHROPIC_MODEL or OPENAI_MODEL}")

    pr_meta = get_pr_meta()
    commit_id = pr_meta["head"]["sha"]
    base_ref = pr_meta["base"]["ref"]
    print(f"Base: {base_ref} | Commit: {commit_id[:8]}")

    guidelines = get_guidelines()
    if guidelines:
        print(f"Loaded GUIDELINES.md ({len(guidelines)} chars)")
    else:
        print("No GUIDELINES.md found — proceeding without project guidelines context.")

    files = get_pr_files()
    if not files:
        print("No reviewable files in this PR.")
        return

    total_additions = sum(f.additions for f in files)
    total_deletions = sum(f.deletions for f in files)
    print(f"Reviewing {len(files)} file(s) (+{total_additions}/-{total_deletions} lines)")

    print("Running heuristic pre-scan…")
    heuristic_comments = run_heuristics(files)
    print(f"  Heuristics found {len(heuristic_comments)} issue(s).")

    print("Calling LLM…")
    diff_text = build_diff_text(files)
    raw_findings = call_llm(diff_text, guidelines)
    print(f"  LLM returned {len(raw_findings)} finding(s).")

    llm_comments = resolve_llm_comments(raw_findings, files)
    all_comments = deduplicate(heuristic_comments, llm_comments)

    obliterado_count = sum(1 for c in all_comments if c.verdict == VERDICT_OBLITERADO)
    exilado_count    = sum(1 for c in all_comments if c.verdict == VERDICT_EXILADO)
    banido_count     = sum(1 for c in all_comments if c.verdict == VERDICT_BANIDO and c.banido)
    suggestion_count = sum(1 for c in all_comments if not c.banido)
    print(
        f"Final: {len(all_comments)} total | "
        f"💀 OBLITERADO={obliterado_count} ☠️  EXILADO={exilado_count} "
        f"🚫 BANIDO={banido_count} 💡 suggestions={suggestion_count}"
    )

    post_review(all_comments, commit_id, pr_meta)


if __name__ == "__main__":
    run()
