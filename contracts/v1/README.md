# Engineering Worker Pool — Public Contract v1

Este diretório define o contrato público estável do Worker Pool. Ele é independente de qualquer produto consumidor.

## Identidade

- contrato: `engineering-worker-pool`
- versão: `v1`
- endpoint de descoberta: `GET /v1/contract`
- autenticação das operações `/v1/**`: Bearer token provisionado em runtime fora do Git

## Compatibilidade

Dentro de `v1`, alterações são somente aditivas e compatíveis. Uma mudança é considerada incompatível quando remove/renomeia operação, muda método/caminho existente, torna campo opcional obrigatório, remove campo obrigatório de resposta ou altera semântica de idempotência, lease, handoff Builder -> Validator, redaction ou códigos de erro.

Mudanças incompatíveis exigem nova versão maior do contrato.

## Semântica essencial

- enqueue lógico é idempotente por `repository + issue_number + request_id`;
- criação retorna HTTP 201 e replay retorna HTTP 200 preservando o mesmo `task_id`;
- toda task exige `base_sha` explícito;
- Builder entrega `produced_sha` antes do Validator;
- Validator deve ser diferente do Builder;
- leitura pública da task e snapshot não podem revelar `lease_token`;
- heartbeat e renovação de lease provam liveness, não progresso material;
- bloqueio libera lease/capacidade;
- conclusão deve ser comprovável por leitura independente de `GET /v1/tasks/{task_id}`.

## Arquivos

- `contract.json`: manifesto machine-readable do contrato.
- `../../app/contract.py`: identidade/descritor runtime.
- `../../tests/test_contract_v1.py`: guard de compatibilidade entre manifesto e produtor real.

Consumidores devem fixar a versão `v1` e falhar fechado se o descritor runtime não retornar essa versão.
