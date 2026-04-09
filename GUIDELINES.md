# GUIDELINES

Este arquivo é lido automaticamente pelo **BANIDO Code Review Agent** antes de cada review.
Adicione aqui as regras específicas do seu projeto. Qualquer violação será marcada como **BANIDO**.

---

## Geral

- Todo código novo deve ter testes unitários cobrindo o caminho feliz e pelo menos um caso de erro.
- Não deixe `TODO`, `FIXME`, ou `HACK` em código que vai para `main` sem uma issue rastreável linkada.
- Não suba código com `print()`, `console.log()`, ou `System.out.println()` — use o logger do projeto.
- Nunca faça commit de credenciais, tokens, API keys ou senhas. Use variáveis de ambiente ou secret managers.

## Arquitetura

- Respeite as camadas do projeto: nunca acesse a camada de dados diretamente de um controller/handler.
- Serviços não devem depender de outros serviços diretamente — use injeção de dependência.
- Não duplique lógica de negócio. Se você está copiando código, extraia para uma função/módulo compartilhado.

## Segurança

- Toda entrada do usuário deve ser validada e sanitizada antes de ser usada.
- Queries SQL devem usar parâmetros preparados — zero concatenação de strings.
- Endpoints autenticados devem verificar autorização (o usuário pode fazer isso com *este* recurso?), não só autenticação.
- Nunca exponha stack traces ou mensagens de erro internas para o cliente.

## Performance

- Não faça queries dentro de loops — resolva com joins, batch ou cache.
- Toda listagem paginável deve ter limite máximo de registros por página.
- Operações pesadas (email, processamento de arquivo, integrações externas) devem ser assíncronas.

## Código

- Funções com mais de 40 linhas precisam de justificativa clara ou devem ser quebradas.
- Nomes de variáveis e funções devem ser descritivos — sem `x`, `tmp`, `data` genérico.
- Magic numbers devem ser constantes nomeadas.
- Catch genérico (`except:`, `catch (e) {}`) é proibido — capture a exceção específica.
