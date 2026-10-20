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

### Continuidade cercada opt-in

Tasks criadas com `fencing_required=true` ficam fora das rotas de claim legadas
do serviço atualizado. Elas só
podem ser adquiridas por `POST /v1/claims/fenced/exact`, que devolve
`lease_generation` monotônica e um `attempt_id` novo por claim. As mutações de
worker sob lease exigem o token, a geração e a tentativa atuais.
O banco também rejeita uma claim cercada cujo `UPDATE` não grave
`lease_generation+1` e um `attempt_id` novo. O produtor atual insere o registro em
`task_attempts` na mesma transação.

Esse trigger faz o SQL do binário anterior falhar fechado, mas o `SELECT` antigo
ainda pode escolher repetidamente uma task cercada e bloquear tasks legadas atrás
dela. Por isso, a admissão cercada permanece desabilitada no banco até a
attestation persistida de que claimers antigos foram drenados e a fila é exclusiva.
Operação mista de claimers antigos e novos na mesma fila não é suportada.
O runtime persiste essa attestation somente quando iniciado com
`CODEX_WORKER_POOL_FENCED_CUTOVER_ATTESTED=1`; sem ela, a API responde 409 e o
trigger continua sendo a defesa de banco.

`POST /v1/tasks/{task_id}/checkpoints` registra somente metadados e referências.
Um replay byte-semanticamente equivalente é idempotente e não renova o marco de
progresso. Um efeito `pending` ou `unknown` impede reroteamento automático até
reconciliação. A geração protege as mutações do pool; destinos externos ainda
precisam aplicar o mesmo fence no ponto do efeito.
`unknown` é terminal para novos checkpoints do worker com o mesmo `effect_id`.
Somente a reconciliação autenticada separadamente o resolve. Checkpoints worker
`confirmed` e `not_applied` exigem `effect_result_ref`.

A reconciliação pós-lease usa
`POST /v1/tasks/{task_id}/effects/{effect_id}/reconcile` e autentica com uma
credencial técnica distinta da credencial dos workers. Ela é idempotente por
tarefa, efeito e geração. `readback_ref` referencia a evidência; quem chama a rota
deve executar e governar a leitura independente real.

Toda claim cercada inclui `recovery_context.latest_checkpoint` e um ledger em
`recovery_context.effects`. O consumidor não repete itens reconciliados como
`confirmed`; itens `not_applied` podem ser repetidos sob a geração recém-adquirida.

## Arquivos

- `contract.json`: manifesto machine-readable do contrato.
- `../../app/contract.py`: identidade/descritor runtime.
- `../../tests/test_contract_v1.py`: guard de compatibilidade entre manifesto e produtor real.

Consumidores devem fixar a versão `v1` e falhar fechado se o descritor runtime não retornar essa versão.
