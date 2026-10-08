# Engineering Worker Pool

Componente reutilizável do **Engineering Control Plane** para orquestração de workers, leases, concorrência, recuperação, watchdog e execução governada.

## Responsabilidade

Este repositório contém a implementação compartilhada do Worker Pool / Engineering Orchestrator. Produtos como o ReqSys devem consumir contratos públicos versionados, sem incorporar a implementação interna do pool.

## Fronteiras

Não pertencem a este repositório:

- regras de negócio do ReqSys;
- runtime físico específico do Noteri ou Desktop PC24x7;
- segredos, tokens ou credenciais;
- lógica de deploy/promoção de produto;
- Command Gateway e regras operacionais globais, mantidos em `ericson-j-santos/chatgpt-operational-rules`.

## Migração

A origem inicial é `ericson-j-santos/reqsys-v2-enterprise-real`. A migração é incremental e fail-closed: a implementação legada no ReqSys só pode ser removida após equivalência funcional comprovada, CI próprio, smoke DEV e E2E consumidor no mesmo SHA/versão.

## Segurança

Nunca versionar segredos, tokens, credenciais, identificadores privados de infraestrutura ou dados pessoais desnecessários.

## Watchdog de progresso

O limite padrão de estagnação é **300 segundos (5 minutos)**, alinhado a `chatgpt-operational-rules/rules/progress-watchdog.md`. Heartbeat, renovação de lease e polling sem mudança não reiniciam esse relógio. Ao atingir o limite, a tarefa deve ser reroteada quando houver alternativa elegível ou bloqueada/falhada de forma explícita e auditável.

## Reconciliação autônoma

O processo da API mantém um `ReconciliationController` interno que executa, por padrão, a cada 5 segundos. Cada ciclo reúne em uma transação a recuperação de leases expirados e o watchdog de progresso material.

Isso remove a dependência de novos claims, snapshots ou chamadas manuais aos endpoints de recovery para liberar trabalho preso. Os caminhos antigos continuam existindo como rede de segurança e compatibilidade.

O intervalo pode ser ajustado por `CODEX_WORKER_POOL_RECONCILE_INTERVAL_SECONDS` e deve ser de pelo menos 0,1 segundo. O `/health` expõe somente estado operacional sanitizado do reconciliador.

A autorrecuperação semântica permanece responsabilidade do Worker Pool. Docker, systemd ou Kubernetes podem reiniciar o processo, mas não substituem regras de lease, progresso material, reroteamento, bloqueio e quarentena.

## Contrato público v1

O contrato estável para consumidores fica em `contracts/v1/` e pode ser descoberto em runtime por `GET /v1/contract`. Dentro de `v1`, somente mudanças aditivas compatíveis são permitidas; mudanças incompatíveis exigem nova versão maior.


## Admissão de incrementos de vários repositórios (preparação)

O adaptador puro `app/portfolio_todo.py` converte uma tarefa **PENDENTE** do TODO Global
em payload de `POST /v1/tasks` **somente após conferência independente** de
Issue GitHub aberta (não PR), vínculo exato `github:owner/repo#numero`, branch
padrão, SHA completo e observação recente (até cinco minutos). O chamador
confiável deve consultar as APIs oficiais do GitHub e do TODO Global; dados
fornecidos por usuário ou por evento não são evidência de GitHub verificada.

Nenhum adaptador se conecta ao GitHub/Notion sozinho, executa comandos, enfileira
automaticamente, altera repositórios ou faz merge. `prepare_portfolio_task`
apenas retorna payload validado e com `request_id` estável derivado da chave do
TODO. A API existente garante idempotência de `repository + issue_number +
request_id` e mantém `max_in_flight` por repositório, recuperação de lease,
Builder/Validator e watchdog. Integrar um consumidor autenticado e testar o
caminho externo real serão incrementos separados, sem deslocar o ReqSys P0.

O E2E automatizado em `tests/test_portfolio_todo.py` valida o fluxo sintético
adaptador → API → SQLite → replay → leitura independente, além de controles
negativos. Isso **não** certifica a conexão real com Notion/GitHub ou workers
físicos. Os registros anteriores sem `#issue_number` permanecem inelegíveis
até o vínculo ser comprovado e atualizado na fonte canônica.

## Produtor de admissão TODO Global → GitHub → Worker Pool (DEV)

O módulo app/portfolio_bridge.py é executado sob demanda, somente em DEV.
Consulta a página do TODO Global no Notion (GET /v1/pages/{id}, API 2025-09-03),
valida a data source canônica e obtém Issue e HEAD da branch padrão nas APIs
oficiais do GitHub. O payload passa pela função prepare_portfolio_task.
Antes de enviar, lê novamente o TODO e o HEAD para detectar divergências.

Credenciais residem em arquivos protegidos fora do Git: as variáveis
PORTFOLIO_NOTION_TOKEN_FILE, PORTFOLIO_GITHUB_TOKEN_FILE e
PORTFOLIO_WORKER_TOKEN_FILE apontam para os arquivos; nunca inserir tokens
na linha de comando, nos logs ou no repositório.

O padrão é diagnóstico/dry-run. A opção --execute exige PORTFOLIO_ENV=dev,
PORTFOLIO_WORKER_URL usando somente loopback, e lane preexistente enabled=true
com max_in_flight=1. Retornos 201/200 do Worker Pool só são aceitos após
GET /v1/tasks/{task_id} confirmar identidades. O enqueue não comprova execução
dos workers ou conclusão de uma tarefa.

Exemplo de diagnóstico local, somente depois do session bootstrap e Gateway:

    python -m app.portfolio_bridge --page-id UUID_DA_PAGINA --data-source-id UUID_DO_TODO_GLOBAL

Apenas no DEV autorizado, com lane pronta, acrescentar --execute.
Não executar em produção, não provisionar secrets automaticamente, e não
publicar APIs ou novos serviços. Testes em tests/test_portfolio_bridge.py
usam upstreams sintéticos e API Worker Pool real com SQLite isolado.
A integração contínua por evento e o E2E com Notion/GitHub reais e PC24x7
continuam pendentes.
