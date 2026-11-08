# Issue #61 — pré-PR CAS/Validator (DEV, implementação parcial)

Fonte: https://github.com/ericson-j-santos/engineering-worker-pool/issues/61
Consumidor: https://github.com/ericson-j-santos/noteri-runtime/issues/42
Regras: chatgpt-operational-rules v1.6.20, main@86abe65b22781c2f608a7d7286ce3a28cd48aaaf
Base: engineering-worker-pool/main@e22aabb14e1a646e274c331e83cc06c470607a38

## Código publicado na branch isolada

O módulo app/pre_pr_cas_gate.py define um coordenador reutilizável e fail-closed,
sem scheduler concorrente, com:

- PrePRIntent vinculado a Issue/TODO, request_id, correlation_id,
  idempotency_key, repositório, base/candidate/source SHAs e paths permitidos.
- Readback autenticado de Issue, TODO, autorização tipada, base/head,
  task existente e checkpoint final com fencing, builder e validator distintos.
- Exigência de execução real de modelo, provider audit verificável, run_id,
  digest, baseline que falha e calls > 0. Recibo retrospectivo não basta.
- Envelope CAS canônico com Git bundle, patch, bytes before/after, SHA-256,
  origem/base/produced/published SHA e retenção solicitada de 30 dias.
- Escrita CAS condicional e releitura integral por cliente separado, mais
  materialização Git/checkout independentes e verificação de integridade,
  ancestralidade, paths autorizados e diff não vazio.
- Validator independente no SHA exato, zero nova inferência, testes e
  regressão executados, sem skipped. Revalidação de base/head/fencing.
- READY_FOR_PR idempotente somente após ledger persistido e relido, CAS,
  validator, autorização e custo incremental igual a R$0 com overage bloqueado.
- Replay de READY sem modelo, nova escrita CAS ou testes repetidos.
- Publicação em função separada, limitada a uma PR draft com readback de
  state, head/base, arquivos/diff e idempotency_key. Sem merge/deploy.

O arquivo tests/test_pre_pr_cas_gate.py contém testes com transportes falsos
que exercitam política positiva/negativa e replay. Esses testes NÃO são
evidência de execução real, assinatura do modelo, CAS, GitHub ou Validator.

## Incompatibilidades reais encontradas

1. A rota LocalWorkerApi.enqueue_fenced_issue de app/native_pool_http.py tem
   allowlist fixa para ericson-j-santos/engineering-worker-pool e o perfil
   agent/qwen7b-issue-*; ela não pode admitir Noteri sem um adaptador
   adicional de escopo restrito, revisado, usando a mesma autoridade.
2. O serviço LocalWorkerApi é loopback; não foi comprovado um backend
   disponível numa rota sem host físico.
3. A task atual não persiste model_run_id nem fence_id; o gate reutiliza
   builder_worker_id, validator_worker_id e checkpoint final, com a
   auditoria de modelo consultada por task_id. Falta provedor autenticado.
4. As portas Authority, ModelAudit, GitObjectSource, CasWriter, CasReader,
   Validator, DecisionLedger e DraftGitHub ainda não possuem implementações
   de produção qualificadas neste incremento.
5. A CI desta branch não foi executada e nenhuma PR foi aberta. Não tratar
   a criação de arquivos, teste simulado ou CI antiga como READY_FOR_PR.

## Consumidor — estado e SHA imutável

- Repo: ericson-j-santos/noteri-runtime, Issue #42, TODO-54.
- Base main: e10b3a00a9b3ec803ed4a4cc8f4f8c62b06c8928
- Branch: fix/noteri-probe-rules-sha-20261106
- HEAD candidato: 515ede49cc7a8b80609c64ef76c23372239a637f
- ahead=6; behind=0; sete arquivos alterados.
- correlation_id: noteri-issue42-prepr-cas-route-20261107
- idempotency_key: engineering-control-plane:noteri42:prepr-cas:515ede49

Não existem nesta execução task_id admitido, auditoria real do modelo,
artifact_ref do patch com releitura, Validator no SHA exato nem READY_FOR_PR.
Não atribuir retrospectivamente origem de modelo aos seis commits existentes.

## Próxima sequência segura — Engineering Control Plane/Codex

1. Ligar autoridade oficial/read-only Issue/TODO/HEAD à rota e exigir
   autorização assinada/atual vinculada aos sete arquivos.
2. Estender admissão tipada do Noteri na fila já existente, sem mudar a
   rota Qwen nem criar novo scheduler; testar created=false, lease e fencing.
3. Executar reparo real via modelo sobre baseline reproduzível; guardar
   run, modelo/digest, chamadas e sua proveniência em armazenamento auditável.
4. Guardar bundle/patch e objetos Git em CAS durável, reler integralmente
   e materializar checkout separado; validar hashes e ancestralidade.
5. Executar Validator independente, controles negativos, regressão e replay
   no SHA corrente; comprovar custo zero sem overage.
6. Persistir e reler READY_FOR_PR. Só então abrir uma única PR draft do
   Noteri e conferir por API base/head/diff/unicidade.
7. Atualizar TODO-54 via Event Gateway canônico e reconfirmar persistência;
   sem Event Gateway acessível, registrar sincronização pendente.
8. Antes de integrar o incremento do Worker Pool, rodar suíte completa no
   próprio HEAD, CI aplicável, E2E real e revisão independente.

Restrições: sem Noteri físico, UAC, reboot, merge, deploy, Docker, GUI,
instalação, segredos ou gasto desconhecido. Fail-closed.
