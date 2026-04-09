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

    # ── OBLITERADO ────────────────────────────────────────────────────────────

    # Credencial hardcoded — o clássico career-ender
    (re.compile(r'(?i)(password|passwd|secret|api_?key|token|private_?key|auth_?key|access_?key)\s*=\s*["\'][^"\']{6,}["\']'),
     "security", 3,
     "**What:** Credencial hardcoded no código-fonte.\n"
     "**Why it matters:** Qualquer pessoa com acesso ao repositório — atual ou futuro, colaborador ou atacante — tem essa chave. Tokens em histórico git são permanentes mesmo após remoção.\n"
     "**Fix:** Revogue a chave imediatamente. Use variáveis de ambiente ou um secret manager:\n"
     "```python\nimport os\nAPI_KEY = os.environ['API_KEY']  # nunca o valor direto\n```"),

    # eval() com input controlado pelo usuário → RCE direto
    (re.compile(r'^\+.*\beval\s*\(.*(?:request|input|params|query|body|user|data|payload)', re.MULTILINE),
     "security", 3,
     "**What:** `eval()` executando input controlado pelo usuário.\n"
     "**Why it matters:** Remote Code Execution direta. Um atacante pode rodar qualquer código no servidor.\n"
     "**Fix:** Remova `eval()`. Se precisar parsear expressões, use um parser seguro como `ast.literal_eval` para literais Python, ou uma biblioteca dedicada."),

    # exec() com input externo
    (re.compile(r'^\+.*\bexec\s*\(.*(?:request|input|params|query|body|user|data|payload)', re.MULTILINE),
     "security", 3,
     "**What:** `exec()` executando input controlado pelo usuário.\n"
     "**Why it matters:** Remote Code Execution direta. Equivalente a dar acesso root ao atacante.\n"
     "**Fix:** Remova. Sem exceção."),

    # shell=True com variável (command injection)
    (re.compile(r'(?:subprocess\.(run|call|Popen|check_output)|os\.system)\s*\([^)]*shell\s*=\s*True[^)]*[+%f]', re.MULTILINE),
     "security", 3,
     "**What:** `shell=True` com string construída dinamicamente — command injection.\n"
     "**Why it matters:** Um atacante pode injetar comandos arbitrários no sistema operacional via `; rm -rf /` ou similar.\n"
     "**Fix:** Passe uma lista de argumentos e remova `shell=True`:\n"
     "```python\nsubprocess.run(['comando', arg1, arg2], shell=False)\n```"),

    # ── EXILADO ───────────────────────────────────────────────────────────────

    # SQL por concatenação de string
    (re.compile(r'(?i)(\.execute|\.query|cursor\.execute)\s*\(\s*(?:f["\']|["\'].*\+|\w+\s*\+)'),
     "security", 2,
     "**What:** Query SQL construída por concatenação de string.\n"
     "**Why it matters:** SQL injection clássico. Um input `' OR '1'='1` destrói o WHERE. `'; DROP TABLE users; --` é literal.\n"
     "**Fix:** Use parâmetros preparados:\n"
     "```python\ncursor.execute('SELECT * FROM users WHERE id = %s', (user_id,))\n```"),

    # eval() sem input visível mas ainda perigoso
    (re.compile(r'^\+.*\beval\s*\('), "security", 2,
     "**What:** `eval()` presente no código.\n"
     "**Why it matters:** Mesmo sem input externo visível agora, qualquer refactor futuro que passe uma variável cria RCE instantaneamente. É uma bomba-relógio.\n"
     "**Fix:** Substitua por uma alternativa segura. `ast.literal_eval` para dados, `importlib` para módulos."),

    # Deserialização insegura (pickle com dados externos)
    (re.compile(r'(?i)pickle\.loads?\s*\(.*(?:request|body|data|payload|input|user)', re.MULTILINE),
     "security", 2,
     "**What:** `pickle.load/loads` executando dados externos.\n"
     "**Why it matters:** Pickle executa código Python arbitrário durante desserialização. RCE com um payload específico.\n"
     "**Fix:** Use JSON, MessagePack, ou Protocol Buffers para dados externos. Nunca pickle."),

    # Redirect para URL arbitrária (open redirect)
    (re.compile(r'(?i)(redirect|location)\s*[\(=]\s*.*(?:request|params|query|input|user)', re.MULTILINE),
     "security", 2,
     "**What:** Redirecionamento para URL controlada pelo usuário — open redirect.\n"
     "**Why it matters:** Usado em phishing: `https://seusite.com/redirect?to=https://evil.com`. Valida origem do token OAuth.\n"
     "**Fix:** Valide que a URL de destino pertence ao domínio permitido antes de redirecionar."),

    # Race condition clássica (TOCTOU)
    (re.compile(r'(?i)(os\.path\.exists|os\.access|os\.stat)\s*\([^)]+\)(?:.|\n){0,200}(?:open|write|delete|remove|rename)', re.MULTILINE),
     "bug", 2,
     "**What:** Padrão check-then-act (TOCTOU) em operação de arquivo.\n"
     "**Why it matters:** Entre o `os.path.exists()` e a operação, outro processo pode modificar o arquivo. Race condition clássica — especialmente perigosa com symlinks.\n"
     "**Fix:** Use operações atômicas: `open()` com flag `x` (exclusive create), ou trate a exceção diretamente:"),

    # Senha em log
    (re.compile(r'(?i)(log|logger|logging|print)\s*\(.*(?:password|passwd|secret|token|key|credential|auth)', re.MULTILINE),
     "security", 2,
     "**What:** Possível credencial sendo logada.\n"
     "**Why it matters:** Logs são frequentemente indexados, exportados para Datadog/Splunk/ELK, e acessíveis por muito mais pessoas que o banco de dados. PII em logs é um compliance nightmare (GDPR, LGPD).\n"
     "**Fix:** Remova o campo sensível do log, ou use uma classe wrapper que redact automaticamente."),

    # ── BANIDO ────────────────────────────────────────────────────────────────

    # except: pelado (Carmack: nunca engula exceções)
    (re.compile(r'^\+\s*except\s*:', re.MULTILINE),
     "bug", 1,
     "**What:** `except:` sem tipo captura `BaseException`, incluindo `KeyboardInterrupt`, `SystemExit`, e `GeneratorExit`.\n"
     "**Why it matters:** Impede o processo de ser encerrado normalmente. Engole erros de programação que deveriam explodir. Torna debugging impossível.\n"
     "**Fix:** Especifique a exceção: `except ValueError:`, ou use `except Exception:` como mínimo absoluto."),

    # except Exception sem re-raise nem log (Carmack: silent failures)
    (re.compile(r'^\+\s*except\s+Exception\s*(?:as\s+\w+)?\s*:\s*\n(?:\+[^\n]*\n)*\+\s*pass', re.MULTILINE),
     "bug", 1,
     "**What:** Exceção capturada e silenciosamente ignorada com `pass`.\n"
     "**Why it matters:** Falhas silenciosas são o pior tipo de bug. O código continua executando em estado inválido, corrompendo dados silenciosamente.\n"
     "**Fix:** Logue o erro no mínimo: `logger.exception('msg')` ou re-raise."),

    # Comparação de float com == (Carmack: imprecisão de ponto flutuante)
    (re.compile(r'^\+.*(?:float|[0-9]+\.[0-9]+)\s*==\s*(?:float|[0-9]+\.[0-9]+)', re.MULTILINE),
     "bug", 1,
     "**What:** Comparação de ponto flutuante com `==`.\n"
     "**Why it matters:** `0.1 + 0.2 == 0.3` é `False` em Python (e em toda linguagem com IEEE 754). Essa comparação vai falhar em condições específicas de forma não reproduzível.\n"
     "**Fix:** Use `math.isclose(a, b, rel_tol=1e-9)` ou compare com tolerância: `abs(a - b) < epsilon`."),

    # Mutable default argument (bug clássico Python)
    (re.compile(r'def\s+\w+\s*\([^)]*=\s*(?:\[\]|\{\}|list\(\)|dict\(\)|set\(\))', re.MULTILINE),
     "bug", 1,
     "**What:** Argumento padrão mutável (`[]` ou `{}`) em definição de função.\n"
     "**Why it matters:** Default arguments em Python são criados UMA vez quando a função é definida, não em cada chamada. A mesma lista/dict é compartilhada entre todas as chamadas — estado vaza entre invocações.\n"
     "**Fix:**\n"
     "```python\ndef f(items=None):  # correto\n    if items is None:\n        items = []\n```"),

    # Debug prints (Linus: não polua o output)
    (re.compile(r'^\+\s*(print\s*\(|console\.log\s*\(|System\.out\.print|fmt\.Print(?:ln|f)?\s*\(|var_dump\s*\(|dd\s*\()', re.MULTILINE),
     "style", 1,
     "**What:** Debug statement detectado.\n"
     "**Why it matters:** Debug prints em produção poluem logs, expõem dados internos, e degradam performance em hot paths.\n"
     "**Fix:** Remova ou substitua por `logger.debug()` com nível configurável."),

    # TODO/FIXME/HACK
    (re.compile(r'^\+.*(TODO|FIXME|HACK|XXX)\b', re.MULTILINE),
     "suggestion", 1,
     "**What:** TODO/FIXME em código sendo mergeado.\n"
     "**Why it matters:** TODO sem issue vinculada nunca é resolvido. Vira dívida técnica permanente.\n"
     "**Fix:** Resolva agora, ou abra uma issue e substitua por `# TODO: #<número-da-issue> descrição`."),

    # Magic numbers sem contexto
    (re.compile(r'^\+(?!.*(?:version|ver|v\d|index|idx|count|#|//|--|/\*)).*[^=!<>]\b(?<!\.)\b(?:86400|3600|1440|31536000|255|65535|1024|4096|8080|8443|9200|27017|5432|3306|6379)\b', re.MULTILINE),
     "suggestion", 1,
     "**What:** Magic number sem nome semântico.\n"
     "**Why it matters:** O próximo leitor não sabe de onde veio esse número. Em 6 meses, você também não vai saber.\n"
     "**Fix:** Extraia para uma constante com nome descritivo: `SECONDS_PER_DAY = 86400`."),

    # Comparação explícita com booleano
    (re.compile(r'^\+.*(?<!=)\s*==\s*(?:True|False)(?!\s*[=])', re.MULTILINE),
     "style", 1,
     "**What:** Comparação explícita com booleano (`== True` ou `== False`).\n"
     "**Why it matters:** Verboso e pode quebrar com objetos que implementam `__eq__` de forma inesperada.\n"
     "**Fix:** `if x:` em vez de `if x == True:`. `if not x:` em vez de `if x == False:`."),
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

            The project enforces the following rules. Every violation is flagged as BANIDO or worse.
            Internalize them — they override general best practices where they conflict.

            ---
            {guidelines.strip()}
            ---
        """)

    return textwrap.dedent(f"""
        You are a composite of the greatest code reviewers who have ever lived:

        - **Linus Torvalds**: you trace data flow from input to output looking for the exact moment
          things can go wrong. You hate unnecessary complexity. If code needs a comment to explain
          what it does, the code is wrong. You ask: "what happens when this input is adversarial?"
        - **John Carmack**: defensive programming. Every pointer can be null. Every index can be
          out of bounds. Every external call can fail. You follow the data. No magic.
        - **Antirez (Redis)**: the best code is code that doesn't exist. Complexity is a liability.
          You look for abstraction that earns nothing, for data structures chosen wrong, for code
          that does three things and should do one.
        - **Google SRE/Eng Practices**: every external call has a failure mode. What's the retry
          strategy? Is it idempotent? What happens at 10x load? Thundering herd? Partial failure?
        - **Joel Spolsky**: wrong code should look wrong. Naming must make incorrect usage impossible
          to write without noticing. You flag anywhere the code's surface hides a trap.
        - **Martin Fowler**: you recognize code smells instantly — Feature Envy, Shotgun Surgery,
          God Class, Primitive Obsession, Parallel Inheritance Hierarchies, Speculative Generality.
          You know when an abstraction is premature vs. necessary.
        - **The Pragmatic Programmer**: DRY violations, orthogonality failures, "tell don't ask"
          violations. You spot when a caller is doing an object's job for it.

        You do not praise. You do not sugarcoat. You either find real problems or you say nothing.
        You never comment on things that are purely a matter of taste.

        {guidelines_section}

        ## MENTAL CHECKLIST — run every item on every changed function/method

        **Correctness (Linus/Carmack lens)**
        - What happens when any input is null/undefined/empty/zero/negative/max-int?
        - What happens when any external call (DB, HTTP, file, time) fails or times out?
        - Is every error return value checked? Is every exception caught at the right level?
        - Are there off-by-one errors in loops, slices, indexes, pagination?
        - Are conditions using `=` instead of `==`? `or` instead of `and`? Wrong operator precedence?
        - Does the algorithm actually implement what the name claims?
        - Are there unreachable branches? Dead code? Code that always short-circuits?
        - Resource leaks: connections, file handles, locks, goroutines — are they always closed/released?
        - Is mutable shared state accessed without synchronization?
        - Are there race conditions (TOCTOU, check-then-act patterns)?

        **Security (Carmack/Google lens)**
        - Is any user-controlled input used in: SQL queries, shell commands, file paths, HTML output,
          XML/JSON deserialization, redirects, template rendering, log messages (log injection)?
        - Is authentication checked? Is authorization checked per-resource (not just per-endpoint)?
        - Are secrets, keys, or PII being logged, returned in responses, or stored insecurely?
        - Are cryptographic functions used correctly (right algorithm, proper IV, no ECB mode)?
        - Is there path traversal via `../` in file operations?
        - Are HTTP responses setting security headers? Are cookies HttpOnly/Secure/SameSite?
        - Is there SSRF risk (user-controlled URLs being fetched server-side)?
        - Are rate limits, timeouts, and request size limits enforced?

        **Performance (Google SRE / Antirez lens)**
        - Is there an N+1 query — a DB/API call inside a loop?
        - Does this create O(n²) behavior that was O(n) before?
        - Is there unbounded memory growth — appending to a list with no cap?
        - Is synchronous/blocking I/O used inside an async context?
        - Is there a cache stampede / thundering herd on cold start?
        - Are large objects serialized or copied unnecessarily?
        - Is there a missing database index for this query's WHERE clause?
        - Will this work correctly and efficiently at 10x the current load?

        **Design (Fowler / Pragmatic Programmer lens)**
        - Does this function do more than one thing? (Single Responsibility)
        - Is logic duplicated that should be extracted? (DRY)
        - Is the caller doing the object's job? (Tell Don't Ask)
        - Is this abstraction earning its keep, or hiding a simple thing behind fake sophistication?
        - Does the name accurately describe what this does AND what it doesn't do?
        - Are magic numbers or magic strings used instead of named constants?
        - Is there Speculative Generality — complexity built for requirements that don't exist yet?
        - Does this change make the codebase harder to understand for the next engineer?

        **Naming (Joel Spolsky lens)**
        - Does the name make incorrect usage look wrong?
        - Could a function named `getUser` silently fail and return null, when it should throw?
        - Is a boolean parameter used where two named functions would be clearer?
        - Are abbreviations used that lose meaning without context?

        ## SEVERITY CATEGORIES

        1. **security** — any exploitable vulnerability
        2. **bug** — incorrect behavior, data corruption, crashes, resource leaks
        3. **performance** — measurable perf regression or scalability cliff
        4. **guideline** — violation of this project's GUIDELINES.md
        5. **suggestion** — genuine design improvement that matters (not preference)
        6. **style** — only if it actively harms readability or violates an explicit linter rule

        ## GRAVITY SCALE

        Every `banido: true` finding must carry a `gravity` field:

        - **1 = BANIDO** — real problem, must be fixed before merge.
          Missing error handling, magic number that causes silent wrong behavior,
          resource not released, minor logic error, DRY violation that diverges, guideline violation.

        - **2 = EXILADO** — serious. Could cause data loss, service downtime, or security incident.
          SQL/command injection, auth bypass on non-critical path, race condition that corrupts data,
          N+1 that will collapse the DB at scale, unhandled exception that crashes the process,
          PII leaked in logs/responses, TOCTOU race on security check.

        - **3 = OBLITERADO** — catastrophic. Production-destroying, career-ending if shipped.
          Hardcoded credentials in source code, RCE vector (eval/exec with user input),
          complete auth bypass on critical endpoint, DROP TABLE without safeguard,
          infinite loop in request handler with no escape, private key committed, SSRF on internal network.

        `banido: false` findings (suggestion/style): omit `gravity` or set to null.

        ## COMMENT FORMAT

        The `body` field must be structured markdown following this exact template:

        **What:** One sentence — precisely what is wrong.
        **Why it matters:** One sentence — the real-world consequence if this ships.
        **Fix:**
        ```<language>
        // concrete corrected code — not pseudocode
        ```

        Be specific. Reference variable names, function names, and line numbers from the diff.
        Never write "consider" or "you might want to". State facts and show the fix.

        ## OUTPUT

        Return ONLY a JSON array. Each element:
        {{
          "path": "<file path exactly as shown in the diff>",
          "line": <integer — exact line number in the NEW file, right side of diff>,
          "severity": "<security | bug | performance | guideline | suggestion | style>",
          "banido": <true | false>,
          "gravity": <1 | 2 | 3 | null>,
          "body": "<structured markdown per the template above>"
        }}

        No markdown fences. No prose outside the array. If nothing is wrong, return: []
    """).strip()


# ---------------------------------------------------------------------------
# LLM calls
# ---------------------------------------------------------------------------

def call_llm(diff_text: str, guidelines: str, pr_meta: dict) -> list[dict]:
    system = build_system_prompt(guidelines)

    pr_title = pr_meta.get("title", "")
    pr_body = (pr_meta.get("body") or "").strip()
    pr_author = pr_meta.get("user", {}).get("login", "unknown")
    base_branch = pr_meta.get("base", {}).get("ref", "")
    head_branch = pr_meta.get("head", {}).get("ref", "")
    additions = pr_meta.get("additions", 0)
    deletions = pr_meta.get("deletions", 0)
    changed_files = pr_meta.get("changed_files", 0)

    context_block = textwrap.dedent(f"""
        ## PR CONTEXT

        **Title:** {pr_title}
        **Author:** {pr_author}
        **Branch:** `{head_branch}` → `{base_branch}`
        **Scope:** {changed_files} file(s) changed, +{additions}/-{deletions} lines

        **Description:**
        {pr_body if pr_body else "(no description provided)"}

        ---
        Use this context to understand the intent of the change.
        The stated intent does NOT excuse bad implementation.
        If the implementation is wrong for what it claims to do, that is a bug.
        If the implementation is correct but the approach itself is wrong, say so.
    """).strip()

    user = (
        f"{context_block}\n\n"
        "## DIFF\n\n"
        "Apply the full mental checklist to every changed function. "
        "Be ruthless. Find every real problem.\n\n"
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
    raw_findings = call_llm(diff_text, guidelines, pr_meta)
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
