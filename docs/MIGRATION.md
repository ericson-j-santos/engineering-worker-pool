# Migração inicial do Worker Pool

## Origem e rastreabilidade

- repositório de origem: `ericson-j-santos/reqsys-v2-enterprise-real`
- SHA usado no bootstrap inicial: `2053d761c66c50b4495567af90b91ef268c868af`
- SHA da `main` revalidada em 2026-09-24: `aaee3673173dceb84d22712aa76f4cb9daec7e5e`
- origem lógica: `services/codex-worker-pool/**`
- destino: `ericson-j-santos/engineering-worker-pool`
- HEAD do destino antes deste incremento: `91e19c500d6f8ae46ab2c7b389de97ca2be0cb7e`

A árvore do núcleo `services/codex-worker-pool/**` foi comparada entre o SHA inicial e a `main` revalidada. Os blobs de Dockerfile, README, `app/**`, `requirements.txt` e `tests/**` são idênticos entre os dois refs; portanto o bootstrap não perdeu mudança posterior do núcleo no ReqSys.

Os contratos SDD e o runbook atuais da origem ficam preservados, sem valores de segredo, em `docs/source-baseline/`. Eles são baseline histórico/contratual e não configuração operacional do repositório dedicado.

## Escopo do bootstrap

Cópia inicial do núcleo autocontido:

- API FastAPI;
- armazenamento SQLite;
- leases e recuperação;
- watchdog de progresso material;
- lanes por repositório;
- afinidade de workers;
- contrato Builder -> Validator;
- idempotência e quarentena;
- testes de API e store;
- Dockerfile e dependências;
- CI próprio;
- smoke DEV standalone;
- baseline de requisitos, especificação e runbook da origem.

A implementação do ReqSys permanece intacta.

## Critério de equivalência

Antes de qualquer remoção no ReqSys:

1. CI deste repositório verde no SHA corrente;
2. testes positivos, negativos e concorrência aprovados;
3. smoke DEV no PC24x7 no mesmo SHA;
4. E2E consumidor ReqSys -> Worker Pool com leitura independente;
5. replay idempotente sem duplicidade;
6. contrato público versionado preservado e testado por compatibilidade;
7. rollback documentado.

## Próximo incremento

Extrair do baseline preservado um contrato público `v1` independente do ReqSys, com:

- schema/semântica explícitos para enqueue, claim, lease, handoff Builder -> Validator, snapshot e erros;
- teste de compatibilidade produtor/consumidor;
- adaptador do ReqSys apontando para o repositório/serviço dedicado com fallback temporário;
- E2E do consumidor no mesmo SHA/versão do Worker Pool;
- nenhuma remoção do legado enquanto equivalência e rollback não estiverem comprovados.

## Fora de escopo

- segredos e tokens;
- startup/recovery físico de Noteri ou Desktop;
- regras de negócio do ReqSys;
- remoção do legado no ReqSys.
