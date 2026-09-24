# Migração inicial do Worker Pool

## Origem e rastreabilidade

- repositório de origem: `ericson-j-santos/reqsys-v2-enterprise-real`
- SHA usado no bootstrap inicial: `2053d761c66c50b4495567af90b91ef268c868af`
- SHA da `main` revalidada em 2026-09-24: `aaee3673173dceb84d22712aa76f4cb9daec7e5e`
- origem lógica: `services/codex-worker-pool/**`
- destino: `ericson-j-santos/engineering-worker-pool`
- HEAD do destino antes do contrato Work v1: `4063cca7b1c70581a8dc942200b0fe574daef03b`

A árvore do núcleo `services/codex-worker-pool/**` foi comparada entre o SHA inicial e a `main` revalidada. Os blobs de Dockerfile, README, `app/**`, `requirements.txt` e `tests/**` são idênticos entre os dois refs; portanto o bootstrap não perdeu mudança posterior do núcleo no ReqSys.

Os contratos SDD e o runbook atuais da origem ficam preservados, sem valores de segredo, em `docs/source-baseline/`. Eles são baseline histórico/contratual e não configuração operacional do repositório dedicado.

## Escopo já extraído

- API FastAPI e armazenamento SQLite;
- leases, recuperação, concorrência e watchdog de progresso;
- lanes, afinidade e handoff Builder -> Validator;
- idempotência e quarentena;
- CI próprio e smoke standalone;
- contrato público v1;
- Work Orchestrator `POST /v1/work` / `GET /v1/work/{work_id}`;
- baseline histórico da origem.

A implementação do ReqSys permanece intacta.

## Critério de equivalência

Antes de qualquer remoção no ReqSys:

1. CI deste repositório verde no SHA corrente;
2. testes positivos, negativos e concorrência aprovados;
3. smoke DEV no PC24x7 no mesmo SHA;
4. E2E consumidor ReqSys -> Worker Pool com leitura independente;
5. replay idempotente sem duplicidade;
6. contrato público versionado preservado e testado por compatibilidade;
7. rollback documentado e avaliável de forma fail-closed.

## Estado da integração

O consumidor ReqSys já prefere `/v1/work` e existe E2E portátil cross-repo real
com replay, leitura independente e controle negativo. Essa evidência reduz o
acoplamento ao Desktop para regressão de contrato, mas não substitui a prova
física PC24x7.

## Cutover e rollback

O procedimento canônico está em `docs/CUTOVER_ROLLBACK.md`. A política
máquina-verificável fica em `migration/cutover-policy-v1.json`.

Enquanto `physical_runtime_validated=false`, a decisão obrigatória é
`CUTOVER_BLOCKED` e a implementação legada não pode ser removida.

## Próximo incremento

- restaurar/validar o runtime físico PC24x7 e executar smoke/E2E no mesmo
  contrato/versão;
- materializar a evidência completa e obter `CUTOVER_READY`;
- tornar o consumidor estrito em incremento separado;
- somente depois preparar a remoção da duplicação no ReqSys, com rollback próprio.

## Fora de escopo

- segredos e tokens;
- startup/recovery físico de Noteri ou Desktop;
- regras de negócio do ReqSys;
- remoção do legado no ReqSys neste incremento.
