# Cutover e rollback — Engineering Worker Pool

## Objetivo

Controlar a transição do Worker Pool legado dentro do ReqSys para o repositório
`engineering-worker-pool` sem transformar ausência de evidência em autorização
de remoção.

A política máquina-verificável fica em `migration/cutover-policy-v1.json` e é
avaliada por `scripts/evaluate_cutover_readiness.py`. O avaliador é somente
leitura: não altera runtime, Git, segredos, banco ou ambiente.

## Estado evidenciado em 2026-09-24

O contrato `engineering-worker-pool/v1`, o consumidor ReqSys `/v1/work` e o
E2E portátil cross-repo já foram comprovados. A prova física PC24x7, porém,
continua pendente porque o runner `DESKTOP-PDQK954` não adquire o handoff
governado.

Portanto a entrada atual é `physical_runtime_validated=false` e a decisão
obrigatória é `CUTOVER_BLOCKED`. Enquanto esse estado persistir, **não remover**
a implementação legada do ReqSys.

## Evidências obrigatórias

O cutover só pode produzir `CUTOVER_READY` quando o mesmo conjunto de evidência
contiver:

1. CI verde do repositório dedicado no SHA informado;
2. contrato público v1 compatível;
3. E2E consumidor ReqSys aprovado;
4. E2E/smoke físico aprovado no runtime canônico;
5. replay idempotente;
6. leitura independente;
7. confirmação de que nenhum segredo foi migrado para Git;
8. implementação legada ainda presente, permitindo rollback durante a janela de
   transição;
9. `reqsys_sha`, `worker_pool_sha`, `contract_version` e
   `correlation_id` válidos.

Evidência de outro SHA, versão ou ambiente não libera o cutover.

## Sequência de cutover

1. Fixar os SHAs ReqSys e Worker Pool e a versão do contrato.
2. Validar CI do Worker Pool e compatibilidade v1.
3. Executar E2E ReqSys -> Worker Pool com replay e leitura independente.
4. Executar o E2E físico PC24x7 no mesmo contrato/versão.
5. Materializar um JSON de evidência e executar:
   `python scripts/evaluate_cutover_readiness.py --policy migration/cutover-policy-v1.json --evidence <arquivo>`.
6. Prosseguir somente com `CUTOVER_READY`.
7. Em incremento versionado separado, tornar o consumidor estrito
   (`--require-contract-v1` e `--require-work-v1`) e revalidar.
8. Remover a implementação legada do ReqSys somente em outro incremento, depois
   de nova evidência do mesmo conjunto e autorização aplicável.

## Rollback durante a janela de migração

O rollback existe somente enquanto `legacy_reqsys_present=true`.

Se houver incompatibilidade de contrato, regressão do E2E consumidor, falha do
runtime físico, falha de leitura independente ou perda de idempotência:

1. interromper o cutover e registrar `CUTOVER_BLOCKED`;
2. reverter a mudança versionada de cutover que habilitou os requisitos estritos
   v1, restaurando apenas o fallback temporário já limitado a HTTP 404;
3. manter o Worker Pool dedicado e seu estado para diagnóstico; **não excluir**
   banco, tarefas ou evidências;
4. não copiar token/segredo para Git e não relaxar gates de evidência;
5. reexecutar positivo, negativo, replay e leitura independente;
6. abrir correção incremental antes de tentar novo cutover.

O rollback não autoriza deploy de produção, RBAC, reboot, force-push ou operação
destrutiva.

## Depois da remoção do legado

Esta política de rollback deixa de ser suficiente quando o legado for removido.
A PR de remoção deve trazer sua própria estratégia de reversão e não pode usar
`legacy_reqsys_present=true` depois que essa afirmação deixar de ser verdadeira.

## Evidência mínima a registrar

- `reqsys_sha`;
- `worker_pool_sha`;
- `contract_version`;
- `correlation_id`;
- resultado dos E2Es consumidor e físico;
- replay e leitura independente;
- decisão `CUTOVER_READY` ou `CUTOVER_BLOCKED`;
- blockers, quando existirem;
- confirmação `production_touched=false` e `secrets_mutated=false`.
