# Migração de roteamento PostgreSQL — decisão DEV (v1 → v2)

Issue: https://github.com/ericson-j-santos/engineering-worker-pool/issues/60

## Estado verificável

A autoridade PostgreSQL v1 (`app/migrations/postgres_authority_v1.sql`, `SCHEMA_VERSION=1`) usa identidade e `schema_sha256` vinculados ao SQL imutável. Não alterar a migration original nem atualizar a tabela `authority_meta` em operação sem versão/cutover. A API atual, enquanto usar esta autoridade, admite somente `execution_mode=eligible_pool` (inclusive clientes antigos que omitem o campo); `pinned_host` recebe conflito antes da escrita. SQLite nativo tem duas modalidades; isso **não comprova** PC físico ou continuidade cross-host.

## Sequência governada para PostgreSQL v2 (ainda NÃO executável)

1. Inventariar versão do banco, schema, `authority_id`, locks/leases ativos, filas, checkpoints, consumidores, execuções em andamento, espaço, PKs/FKs e estratégia de recuperação; confirmar `expected_rules_sha` e `base_sha`.
2. Comprovar backup consistente, armazenamento privado, teste de restauração separado, SHA-256 integral, reversibilidade, janela de manutenção, disponibilidade do operador e custo incremental literalmente zero (hard stop quando aplicável). Ausência de qualquer evidência = BLOQUEADO.
3. Definir arquivo **novo e imutável** de schema v2, versionamento/digest distintos e migração transacional v1→v2 com verificação inicial do v1, `ALTER TABLE tasks ADD COLUMN execution_mode TEXT NOT NULL DEFAULT 'eligible_pool'` e `target_host TEXT` com checks, atualização CAS de `authority_meta`, proteção de locks, rollback de transação em falha e upgrade explicitamente owner-approved. Não adicionar DDL a inicialização de runtime.
4. Garantir leitor/worker legado v1 em paralelo durante rollout: nenhuma task `pinned_host` entra na autoridade v1; desativar writers incompatíveis antes do cutover. Após upgrade, verificar identidade/schema/digest exatos por leitura independente e preservar fallback de restauração, sem downgrade destrutivo.
5. Testar PostgreSQL real **isolado DEV**: pooled legado, pinned correto, pinned no host errado, Validator independente, concorrência, claim exato, fencing, watchdog, replay idempotente, migração a partir de dados v1, backup/restore e negative test de digest divergente; falha mantém v1 e marca BLOCKED.
6. E2E físico (um host e alternância em dois hosts distintos) somente após Session Launcher/Gateway, worktrees exclusivos, posse única, source SHA, perfis NORMAL, orçamento R$0,00 e autorização apropriada. Sem testes no ambiente real, manter PARCIAL.

### Gate de publicação

O presente incremento corrige regressão de compatibilidade com v1 **em código** e não constitui a migração v2. O novo SQL/DDL, se desenvolvido, exige PR e aceite versionado próprio ou checkpoint na mesma Issue #60; não editar `postgres_authority_v1.sql` para mascarar o problema. Sem CAS/Validator/testes SHA-bound, não abrir PR draft nem efetuar merge.
