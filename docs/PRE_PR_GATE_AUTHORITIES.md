# Issue #61 — Fonte única pré-PR (integração DEV, sem READY)

A Issue #61 é o alvo de integração canônico. Os contratos de schema
(`app/pre_pr_gate.py`) e CAS (`app/prepr_evidence_gate.py`) agora
residem na **mesma branch**, com verificações positivas/negativas em CI.
A branch anterior `fix/prepr-independent-evidence-stage-20261107`
permanece como checkpoint e não deve produzir PR concorrente.

Diferenciar:
- `base_sha`: main do repositório de destino, atualmente e10b3a00...
- `source_sha`: checkout/branch admitido, Noteri@515ede49...
- `produced_sha`: commit novo produzido **pelo Builder real**, ainda não publicado na PR.
- `task_manifest.base_sha`: igual a `source_sha`; `workspace_sha` igual à mesma origem.
- `effect_id`: derivado da tarefa + `produced_sha` segundo `NativeOneShotExecutor`.
- A API GitHub precisa confirmar `base_sha` e `source_sha`, enquanto
  o `produced_sha` deve ser obtido do CAS e materializado no Validator.
  Não exigir publicação do commit no remoto antes do gate.

## Adaptador GitHub autenticado — incremento da Issue #61

O arquivo `app/prepr_github_readback.py` implementa um leitor oficial GET-only
para o primeiro consumidor `noteri-runtime#42`. Exige token por arquivo
protegido e recusa alterações inesperadas de Issue, base `main`, branch,
ancestralidade e lista de arquivos da comparação GitHub. A camada não
interpreta o conteúdo da Issue como autorização, não cria task, modelo,
artefato ou PR e não executa workflow do Noteri.

A requisição de evidências operacionais da função `check()` passa a exigir
`source_changed_paths` (diff da branch de origem, conferido com GitHub)
separadamente de `changed_paths` (subconjunto autorizado para reparo por
modelo e declarado no `TaskManifest`). O primeiro pode incluir exatamente
o workflow aprovado no Noteri; o segundo segue o conjunto de caminhos
permitidos pelo adaptador nativo, sem arquivos ocultos. Um caminho de reparo
fora do diff de origem é bloqueado neste incremento. Nada disso converte
testes HTTP simulados em prova operacional.

O CI público roda as verificações de HTTP, escopo, replay e recusa de token
ausente em ambos os jobs, usando `httpx.MockTransport`. Um runner hospedado
não tem automaticamente a credencial de leitura de outro repositório ou
as autoridades Qwen, TODO, CAS e Validator. **Não inferir READY_FOR_PR.**

Somente uma integração futura de provedores **reais e autenticados**
(GitHub/TODO, runtime Qwen, Git CAS, Validator independente e custo
atestado R$0) pode avaliar evidência operacional. Os callbacks de teste
são sintéticos e não conferem identidade a prova declarada por JSON.
`check()` retorna SOMENTE `PREPR_EVIDENCE_VERIFIED_ONLY`,
`ready_for_pr=false`; `evaluate_pre_pr()` retorna
`SCHEMA_ONLY_UNVERIFIED`, `draft_pr_authorized=false`.
Nenhum destes está autorizado a publicar PR, executar modelo ou host.
Não há chamada Qwen, CAS publicado ou E2E físico nesta alteração.

Issue principal: https://github.com/ericson-j-santos/engineering-worker-pool/issues/61
Dependência: https://github.com/ericson-j-santos/noteri-runtime/issues/42
Regras v1.6.20/main@86abe65b22781c2f608a7d7286ce3a28cd48aaaf
