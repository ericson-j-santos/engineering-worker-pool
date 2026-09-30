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

