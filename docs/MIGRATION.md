# Migração inicial do Worker Pool

## Origem imutável

- repositório: `ericson-j-santos/reqsys-v2-enterprise-real`
- SHA de origem: `2053d761c66c50b4495567af90b91ef268c868af`
- origem lógica: `services/codex-worker-pool/**`
- destino: `ericson-j-santos/engineering-worker-pool`

## Escopo deste incremento

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
- CI próprio.

A implementação do ReqSys permanece intacta.

## Critério de equivalência

Antes de qualquer remoção no ReqSys:

1. CI deste repositório verde no SHA corrente;
2. testes positivos, negativos e concorrência aprovados;
3. smoke DEV no PC24x7 no mesmo SHA;
4. E2E consumidor ReqSys -> Worker Pool com leitura independente;
5. replay idempotente sem duplicidade;
6. contrato versionado preservado;
7. rollback documentado.

## Fora de escopo

- segredos e tokens;
- startup/recovery físico de Noteri ou Desktop;
- regras de negócio do ReqSys;
- remoção do legado no ReqSys.
