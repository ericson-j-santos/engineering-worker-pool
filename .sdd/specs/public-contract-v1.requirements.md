# Public Contract v1 — requisitos

## Objetivo

Publicar um contrato versionado e independente do ReqSys para o Engineering Worker Pool, protegendo consumidores durante a migração da issue reqsys-v2-enterprise-real#2020.

## Requisitos

1. O contrato deve se identificar como `engineering-worker-pool/v1`.
2. O runtime deve expor `GET /v1/contract` sob a mesma autenticação das demais operações `/v1/**`.
3. O descritor deve informar nome, versão, service name e política de compatibilidade.
4. O manifesto machine-readable deve listar operações, modelos mínimos, erros e invariantes de idempotência/lease/handoff/redaction.
5. Dentro de `v1`, somente mudanças aditivas compatíveis são permitidas.
6. Mudança incompatível exige nova versão maior.
7. O contrato não pode conter dependência de regras de negócio, caminhos internos ou identidade do ReqSys.
8. Nenhum valor de token, segredo ou credencial pode ser versionado.
9. Teste automatizado deve falhar se uma operação declarada desaparecer ou se campos obrigatórios de requests deixarem de corresponder aos modelos runtime.
10. Teste E2E de contrato deve provar descoberta da versão, enqueue/replay idempotente, Builder -> Validator, leitura independente final e ausência de `lease_token` em leitura pública.

## Critérios de aceite

- `contracts/v1/contract.json` válido e independente de ReqSys;
- `GET /v1/contract` autenticado retorna `contract_version=v1`;
- `/health` inclui `contract_version=v1` como campo aditivo;
- guard de rotas e modelos verde;
- E2E de semântica central verde;
- suíte completa e CI do HEAD exato verdes;
- nenhum segredo adicionado.

## Rollback

A mudança é aditiva. Em caso de regressão antes de consumo externo, reverter o commit/PR do contrato. Depois que consumidor v1 estiver ativo, qualquer remoção do contrato deve ser tratada como breaking change e não pode ser feita sem migração/versionamento explícitos.
