# BANIDO

AI-powered code review agent. Lê seu `GUIDELINES.md`, analisa cada PR e grita **BANIDO** quando alguém codar merda.

## Como funciona

```
PR aberto/atualizado
        ↓
GitHub Actions dispara
        ↓
Agent lê GUIDELINES.md do repo
        ↓
Roda heurísticas rápidas no diff (patterns de problemas conhecidos)
        ↓
Manda diff + guidelines pro LLM (Claude ou GPT-4o)
        ↓
LLM retorna findings estruturados em JSON
        ↓
Agent posta comentários inline no PR
PRs com problema recebem REQUEST_CHANGES
PRs limpos recebem APPROVE
```

### O que aparece no PR

Cada problema sério recebe:

```
# BANIDO

🐛 BUG

`except:` sem tipo captura BaseException incluindo KeyboardInterrupt e SystemExit.
Use `except Exception:` no mínimo, ou capture a exceção específica.
```

E um summary com score de qualidade:

```
# BANIDO Code Review

Quality score: `████░░░░░░` 4/10

3 BANIDO issue(s) — código inaceitável que precisa ser corrigido antes do merge.
2 sugestão(ões) de melhoria.
```

### O que o agent detecta

| Categoria | Exemplos |
|-----------|---------|
| 🔒 Security | Credenciais hardcoded, SQL injection, `eval()`, endpoints sem authz |
| 🐛 Bug | Erros de lógica, null dereference, `except:` genérico, resource leaks |
| ⚡ Performance | N+1 queries, loops com I/O, falta de paginação, O(n²) escondido |
| 📋 Guideline | Qualquer violação das regras do seu `GUIDELINES.md` |
| 💡 Suggestion | Melhorias reais de design que importam |
| 🎨 Style | Só quando prejudica leitura ou viola linter configurado |

## Setup

### 1. Adicione sua API key como Secret do GitHub

**Settings → Secrets and variables → Actions → New repository secret**

| Secret | Provedor |
|--------|---------|
| `ANTHROPIC_API_KEY` | Anthropic (recomendado — usa Claude) |
| `OPENAI_API_KEY` | OpenAI (fallback — usa GPT-4o) |

Basta um dos dois. Se os dois existirem, Claude é usado.

### 2. Pronto

O workflow `.github/workflows/code-review.yml` já está configurado e roda automaticamente em todo PR.

### 3. Customize o `GUIDELINES.md`

Edite o arquivo `GUIDELINES.md` na raiz do repo com as regras específicas do seu projeto.
O agent vai lê-lo antes de cada review e tratar violações como **BANIDO**.

```markdown
# GUIDELINES

## Arquitetura
- Controllers não acessam o banco diretamente — sempre via service/repository.
- ...

## Segurança
- Toda query usa parâmetros preparados. Concatenação de string em SQL = BANIDO imediato.
- ...
```

## Estrutura

```
.github/
  workflows/
    code-review.yml       # GitHub Actions workflow
review_agent/
  reviewer.py             # Agent completo (~300 linhas)
  requirements.txt        # Dependência: httpx
  config.example.yml      # Referência de variáveis de configuração
GUIDELINES.md             # Regras do projeto lidas pelo agent
```

## Variáveis de ambiente

| Variável | Obrigatória | Default | Descrição |
|----------|-------------|---------|-----------|
| `GITHUB_TOKEN` | Sim | auto | Injetado pelo Actions |
| `GITHUB_REPOSITORY` | Sim | auto | Injetado pelo Actions |
| `PR_NUMBER` | Sim | auto | Injetado pelo Actions |
| `OPENAI_API_KEY` | Um dos dois | — | OpenAI key |
| `ANTHROPIC_API_KEY` | Um dos dois | — | Anthropic key |
| `OPENAI_MODEL` | Não | `gpt-4o` | Modelo OpenAI |
| `ANTHROPIC_MODEL` | Não | `claude-opus-4-5` | Modelo Claude |
| `MAX_DIFF_CHARS` | Não | `80000` | Limite de chars do diff enviado ao LLM |
| `MAX_COMMENTS_PER_FILE` | Não | `8` | Máx de comentários inline por arquivo |
| `MAX_FILES` | Não | `30` | Máx de arquivos analisados por PR |
| `GUIDELINES_PATH` | Não | `GUIDELINES.md` | Caminho do arquivo de guidelines |

## Rodando localmente

```bash
pip install httpx

export GITHUB_TOKEN="ghp_..."
export ANTHROPIC_API_KEY="sk-ant-..."   # ou OPENAI_API_KEY
export GITHUB_REPOSITORY="owner/repo"
export PR_NUMBER=42

python review_agent/reviewer.py
```
