# BANIDO

> O code review agent mais pistola do Brasil. Lê suas guidelines, analisa cada PR, e grita **BANIDO**, **EXILADO** ou **OBLITERADO** quando alguém codar merda.

---

## O que é isso

Um agent de code review que roda no GitHub Actions em cada Pull Request. Ele combina heurísticas estáticas rápidas com análise profunda via LLM (Claude ou GPT-4o) e posta comentários inline diretamente no PR — com veredicto, consequência, e fix concreto.

PRs com problema recebem `REQUEST_CHANGES` automaticamente. PRs limpos recebem `APPROVE`.

---

## Como funciona

```
PR aberto ou atualizado
         │
         ▼
GitHub Actions dispara o workflow
         │
         ▼
Agent busca o GUIDELINES.md do seu repo
         │
         ▼
Heurísticas rápidas varrem o diff
(credenciais, SQL injection, eval, except pelado, etc.)
         │
         ▼
Diff + Guidelines + contexto do PR são enviados ao LLM
         │
         ▼
LLM roda checklist mental em cada função alterada:
correctness, security, performance, design, naming
         │
         ▼
Findings mapeados para linhas exatas do PR
         │
         ▼
Comentários inline postados com veredicto + consequência + fix
         │
    ┌────┴────┐
    │         │
  LIMPO    PROBLEMA
    │         │
  APPROVE  REQUEST_CHANGES
```

---

## Os veredictos

| Veredicto | Quando | Comportamento |
|-----------|--------|---------------|
| 🚫 **BANIDO** | Problema real — precisa corrigir antes do merge | Bloqueia o PR |
| ☠️ **EXILADO** | Bug sério ou falha de segurança grave | Bloqueia o PR |
| 💀 **OBLITERADO** | Catastrófico — destrói produção, expõe credenciais, abre RCE | Bloqueia o PR |
| ✅ **APROVADO** | Nenhum problema encontrado | Aprova o PR |

---

## O que o agent detecta

### Heurísticas (antes mesmo do LLM)

| Padrão | Veredicto |
|--------|-----------|
| Credencial hardcoded (`api_key = "sk-..."`) | 💀 OBLITERADO |
| `eval(request.data)` — RCE direto | 💀 OBLITERADO |
| `subprocess(shell=True)` com string dinâmica | 💀 OBLITERADO |
| SQL por concatenação de string | ☠️ EXILADO |
| `pickle.loads` com dados externos | ☠️ EXILADO |
| Open redirect via URL do usuário | ☠️ EXILADO |
| Race condition TOCTOU em arquivo | ☠️ EXILADO |
| Credencial sendo logada | ☠️ EXILADO |
| `except:` sem tipo | 🚫 BANIDO |
| `except Exception: pass` silencioso | 🚫 BANIDO |
| Float comparado com `==` | 🚫 BANIDO |
| Mutable default argument (`def f(x=[])`) | 🚫 BANIDO |
| `print()` / `console.log()` em produção | 🚫 BANIDO |
| TODO/FIXME sem issue linkada | 🚫 BANIDO |
| Magic numbers sem nome | 🚫 BANIDO |

### Análise LLM (por cada função alterada)

O agent usa um checklist mental inspirado nos melhores revisores de código do mundo:

- **Correctness** (Linus / Carmack): inputs null/empty/zero/maxint, falhas externas, off-by-one, operadores trocados, race conditions, resource leaks
- **Security** (Carmack / Google): injection, auth vs authz, PII em logs, cripto incorreta, SSRF, TOCTOU
- **Performance** (Google SRE): N+1, O(n²) escondido, blocking I/O em async, unbounded memory, thundering herd
- **Design** (Fowler / Pragmatic): SRP, DRY, Tell Don't Ask, abstração prematura
- **Naming** (Spolsky): naming que esconde armadilhas, boolean params onde duas funções seriam melhores

---

## Como aparece no PR

### Comentário inline

```
# OBLITERADO

🔒 SECURITY

**What:** Credencial hardcoded no código-fonte.
**Why it matters:** Qualquer pessoa com acesso ao repo — atual ou futuro,
colaborador ou atacante — tem essa chave. Tokens em histórico git são
permanentes mesmo após remoção.
**Fix:**
```python
import os
API_KEY = os.environ['API_KEY']  # nunca o valor direto
```
```

### Summary do PR

```
# 💀 OBLITERADO — Code Review

Quality score: `░░░░░░░░░░` 0/10

💀 1 OBLITERADO — código catastrófico. Reescreva antes de qualquer merge.
☠️  2 EXILADO — muito ruim. Bug sério ou falha de segurança grave.
🚫 3 BANIDO — problema real que precisa ser corrigido.

| Veredicto     | Severidade     | Arquivo          | Linha | Resumo                              |
|---------------|----------------|------------------|-------|-------------------------------------|
| 💀 OBLITERADO | 🔒 security    | `api/client.py`  | 12    | Credencial hardcoded no código...   |
| ☠️ EXILADO    | 🐛 bug         | `db/queries.py`  | 47    | SQL por concatenação de string...   |
| 🚫 BANIDO     | ⚡ performance | `services/user.py`| 93   | N+1 query dentro do loop...         |
```

---

## Setup (2 minutos)

### Passo 1 — Adicione a API key como Secret

Vá em **Settings → Secrets and variables → Actions → New repository secret** no seu repositório e adicione:

| Secret | Descrição |
|--------|-----------|
| `ANTHROPIC_API_KEY` | Chave da Anthropic — usa Claude (recomendado) |
| `OPENAI_API_KEY` | Chave da OpenAI — usa GPT-4o (alternativa) |

Você precisa de **um ou outro**. Se os dois existirem, Claude é usado.

> **Onde pegar:**
> - Anthropic: [console.anthropic.com](https://console.anthropic.com) → API Keys
> - OpenAI: [platform.openai.com](https://platform.openai.com/api-keys)

### Passo 2 — Pronto

O workflow `.github/workflows/code-review.yml` já está no repo e dispara automaticamente em todo PR.

### Passo 3 — Customize o `GUIDELINES.md`

Edite o arquivo `GUIDELINES.md` na raiz do repo com as regras específicas do seu projeto. O agent lê esse arquivo antes de cada review e trata violações como **BANIDO** ou pior.

**Exemplos de regras que você pode adicionar:**

```markdown
## Arquitetura
- Controllers nunca acessam o banco diretamente — sempre via repository.
- Toda lógica de negócio fica na camada de service.

## Segurança
- Toda query usa ORM ou parâmetros preparados. Concatenação de string em SQL = OBLITERADO.
- Endpoints autenticados verificam autorização por recurso, não só autenticação.

## Performance
- Zero queries dentro de loop. Use batch, join ou cache.
- Toda listagem tem paginação com limite máximo configurável.

## Código
- Funções com mais de 40 linhas precisam de justificativa ou devem ser quebradas.
- Sem nomes genéricos: `x`, `tmp`, `data`, `obj`, `item`.
```

---

## Usando em outro repositório

O agent roda em qualquer repo sem precisar copiar o código. Basta adicionar o workflow:

### Opção A — Copiar o workflow (mais simples)

Crie o arquivo `.github/workflows/code-review.yml` no seu repo com o conteúdo abaixo:

```yaml
name: BANIDO Code Review

on:
  pull_request:
    types: [opened, synchronize, reopened]

concurrency:
  group: banido-review-${{ github.event.pull_request.number }}
  cancel-in-progress: true

jobs:
  review:
    name: BANIDO Code Review
    runs-on: ubuntu-latest
    permissions:
      contents: read
      pull-requests: write
    if: github.actor != 'dependabot[bot]' && github.actor != 'github-actions[bot]'
    steps:
      - name: Checkout
        uses: actions/checkout@v4
        with:
          fetch-depth: 0

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: Install dependencies
        run: pip install httpx

      - name: Download agent
        run: |
          curl -sSL https://raw.githubusercontent.com/samsantosb/BANIDO/main/review_agent/reviewer.py \
            -o reviewer.py

      - name: Run BANIDO Agent
        env:
          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
          OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}
          GITHUB_REPOSITORY: ${{ github.repository }}
          PR_NUMBER: ${{ github.event.pull_request.number }}
        run: python reviewer.py
```

Adicione a secret `ANTHROPIC_API_KEY` ou `OPENAI_API_KEY` no repositório e pronto.

### Opção B — Fork e mantenha suas customizações

1. Faça fork deste repositório
2. Edite o `GUIDELINES.md` com as regras do seu projeto
3. Use o repo como base para o workflow acima, trocando a URL do `curl` para o seu fork

---

## Rodando localmente

Útil para debugar ou testar o agent sem abrir um PR:

```bash
# Instalar dependência
pip install httpx

# Configurar variáveis
export GITHUB_TOKEN="ghp_..."                     # Personal Access Token com repo + pull_requests scope
export ANTHROPIC_API_KEY="sk-ant-..."             # ou OPENAI_API_KEY
export GITHUB_REPOSITORY="seu-usuario/seu-repo"
export PR_NUMBER=42                               # número do PR que quer revisar

# Rodar
python review_agent/reviewer.py
```

**Permissões necessárias no Personal Access Token:**
- `repo` (acesso ao repositório)
- `pull_requests` (postar comentários)

---

## Configuração avançada

Todas as variáveis abaixo são opcionais — o agent funciona sem nenhuma delas.

| Variável | Default | O que faz |
|----------|---------|-----------|
| `OPENAI_MODEL` | `gpt-4o` | Modelo OpenAI a usar |
| `ANTHROPIC_MODEL` | `claude-opus-4-5` | Modelo Claude a usar |
| `MAX_DIFF_CHARS` | `80000` | Máximo de caracteres do diff enviado ao LLM |
| `MAX_COMMENTS_PER_FILE` | `8` | Máximo de comentários inline por arquivo |
| `MAX_FILES` | `30` | Máximo de arquivos analisados por PR |
| `GUIDELINES_PATH` | `GUIDELINES.md` | Caminho do arquivo de guidelines no repo |

No workflow, adicione na seção `env` do step:

```yaml
env:
  ANTHROPIC_MODEL: "claude-opus-4-5"
  MAX_COMMENTS_PER_FILE: "5"
  GUIDELINES_PATH: "docs/GUIDELINES.md"
```

---

## Estrutura do repositório

```
BANIDO/
├── .github/
│   └── workflows/
│       └── code-review.yml     # Workflow do GitHub Actions
├── review_agent/
│   ├── reviewer.py             # Agent completo (~500 linhas)
│   ├── requirements.txt        # Dependência: httpx
│   └── config.example.yml      # Referência de variáveis
├── GUIDELINES.md               # Regras do projeto lidas pelo agent
└── README.md                   # Este arquivo
```

---

## Perguntas frequentes

**O agent bloqueia o merge automaticamente?**
Sim. PRs com problemas BANIDO/EXILADO/OBLITERADO recebem `REQUEST_CHANGES`, o que bloqueia o merge até o autor corrigir e o review ser redispensado.

**Posso usar sem pagar por API?**
Não. O agent precisa de uma API key da Anthropic ou OpenAI. Ambas têm planos pagos por uso — um review típico custa entre $0.01 e $0.05 dependendo do tamanho do PR e do modelo.

**O agent comenta em PRs de bots (Dependabot)?**
Não. O workflow ignora PRs de `dependabot[bot]` e `github-actions[bot]` automaticamente.

**O que acontece com PRs muito grandes?**
Diffs acima de 80.000 caracteres são truncados para o LLM. As heurísticas estáticas continuam rodando no diff completo.

**Posso mudar o idioma dos comentários?**
O system prompt está em inglês para maximizar a qualidade do LLM, mas você pode editar `build_system_prompt()` em `reviewer.py` e adicionar "Respond in Portuguese" ao final do prompt.

**Como eu desativo o agent temporariamente?**
Adicione `[skip review]` no título do PR, ou cancele o run manualmente na aba Actions. Você também pode desativar o workflow nas configurações do repositório.
