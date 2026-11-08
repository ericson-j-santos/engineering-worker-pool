# Noteri #42 — ponte de admissão ao Worker Pool (pré-PR)

## Escopo (08/11/2026)

Esta é uma rota **opt-in e fail-closed**, não um dispatch automático.

Repositório: `ericson-j-santos/noteri-runtime`  
Issue: `#42`, TODO canônico: `TODO-54`  
Origem admitida: `fix/noteri-probe-rules-sha-20261106@078927faf7bf970e9158631003dc2804bd26c390`  
Regras: `chatgpt-operational-rules/main@86abe65b22781c2f608a7d7286ce3a28cd48aaaf`

`app/noteri42_prepr_admission.py::admit_exact` liga **somente quando
todas as autoridades reais estão qualificadas** a rota do Noteri ao
transporte já existente `LocalWorkerApi`. Não cria fila, serviço, worker,
token, task sem aprovação ou scheduler paralelo.

## Leitura antes do único POST

1. Fonte: callback configurado como API oficial GitHub com Issue aberta,
   `main`, branch, SHA, ancestralidade e sete arquivos do diff conferidos.
2. TODO Global: reader autêntico do registro canônico `TODO-54`,
   status explícito `AUTORIZADO_PARA_EXECUCAO`, mesmo SHA e correlação.
   O estado existente `BLOQUEADO` **não autoriza** enfileirar.
3. Aprovação: leitura autenticada e independente da autoridade do proprietário
   vinculada à Issue #42 e ao SHA. Texto em issue ou JSON do modelo não basta.
4. Custo: incremento exato R$ 0,00, franquia válida e **hard stop contra
   excedente**; ignorar plano ou estimativa não comprovada.
5. Host: identidade física, perfil NORMAL, `SESSION_LAUNCH_OK`,
   `state_validated=true`, Gateway, lease e worktree exclusivos,
   dependências nativas, digest completo do Qwen7B local e
   `ready_for_work=true`.
6. Fila: lane existente do Noteri, habilitada, `max_in_flight=1`.
7. Releitura das cinco autoridades **antes do POST**. Drift aborta.

A identidade `request_id` é derivada do SHA confiável em código, estável
para idempotência. A branch destino e os parâmetros do POST são constantes;
não aceitam comandos, modelo, destino ou caminhos provenientes da Issue.
O transporte executa uma única solicitação e relê a task pelo identificador
retornado. Resultado ambíguo **não autoriza retry**: reconciliar no
estado canônico antes de qualquer tentativa nova.

## Fronteira ainda não conectada

Não existe, nesta branch, entrypoint operando com os leitores autenticados de
TODO Global, owner approval, custos ou runtime. A exigência é intencional:
em ausência desses leitores, `admit_exact` bloqueia antes da fila.

O servidor nativo pode não ter a lane Noteri habilitada ou cutover fenced;
não criar nem habilitar automaticamente. O workflow físico vigente de stage
na main Noteri usa hashes antigos e não comprova um executor da Issue #42.

Depois da admissão real, o Builder deve usar `NativeOneShotExecutor`
com adapter de reparo específico, CAS `S3GitBundleArtifactTransport`,
manifest/lease/fencing e registro idempotente. O transporte de proposta
`Noteri42NativeQwen` e a montagem de candidato `noteri42_candidate`
são **somente componentes**, não um Builder/Validator habilitado.
Exigir commit real do modelo, CAS durável e readback, Validator independente,
gates de regressão e decisão governada antes de `READY_FOR_PR`.

## Qualidade e evidência

A CI `pre-pr-gate-contract.yml` executa mocks, contratos de HTTP,
admissão, negativos e replay em dois jobs. Esses testes não produzem tarefa
real, patch do modelo, gasto, host, CAS ou PR. Apenas a execução com
conectores e estado reais pode certificar o E2E operacional.

Rota reversível: código apenas na branch existente do Worker Pool;
`main`, runtime físico e Noteri permanecem sem mutação neste incremento.
