# Worker Pool 1.4 — preflight somente leitura do canário DEV

Esta sonda é uma etapa de preparação, não um deploy nem uma homologação da migração. Código: scripts/candidate_readonly_canary.py.

## Pré-condições governadas

- Desktop PC24x7 DEV, sessão validada e Command Gateway allowlisted.
- Candidato 1.4.0 já iniciado em loopback separado do legado: IP 127.0.0.1 e porta != 8097.
- Volume SQLite restaurado em cópia isolada, imagem candidata fixada por SHA/digest e isolamento conferido por Docker inspect independente.
- Checkout do engineering-worker-pool no SHA esperado e token de canário mantido somente em arquivo privado. No Windows, comprovar ACL restritiva fora da sonda.
- Nunca apontar o canário ao banco ou ao arquivo de token do serviço em uso.

## Critério verificado

- Health healthy, contrato v1, OpenAPI 1.4.0 e rota de diagnostics existente.
- Sem token a rota autenticada deve retornar HTTP 401; com token o contrato deve ser v1.
- Tasks ReqSys #2546 e #2547: identidade/issue/lane corretas, estado queued, attempt_count=0, sem lease nas leituras antes e depois de diagnostics.
- Diagnostics deve declarar sqlite_mode_ro, claim_performed=false, reconciliation_requested=false. ID desconhecido deve retornar HTTP 404.
- Apenas requisições GET; redirecionamentos, proxies, portas não permitidas e token exposto são bloqueados.

## Evidência e limites

O recibo fica em artifacts/candidate-readonly-canary/evidence.json e não inclui tokens, worker IDs, banco ou leases.

Mesmo com CANDIDATE_READONLY_PREFLIGHT_OK, os campos runtime_source_sha_attested, candidate_image_digest_attested, isolated_candidate_canary_passed, rollback_rehearsal_passed e cutover_ready permanecem false. O SHA informado identifica somente o checkout da sonda. A procedência real da imagem, a restauração física e o isolamento do container exigem verificação externa.

A rota não cria Builder/Validator, não assume tarefas, não muda o legado 1.2, não comprova E2E consumidor ou replay real, e não libera cutover. A migração continua sujeita a migration/cutover-policy-v1.json, canário físico, rollback e autorização separada para eventual restart/cutover do DEV.

Os testes locais verificam contrato positivo, 401, versão/identidades incorretas, URL/porta insegura, redirect bloqueado, SHA divergente, arquivo de token protegido, repetição e sanitização. Eles não substituem o teste físico.
