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

