# Executor nativo one-shot

`app/native_one_shot.py` coordena um unico Builder ou Validator sobre os
contratos existentes do Worker Pool. Ele nao registra workers, inicia servico,
le token, instala scheduler nem escolhe tarefas: usa exclusivamente claim exato.

## Fronteira de confianca

O chamador deve entregar um worktree local isolado, sem remoto, ja materializado
por Session Launcher e Command Gateway. `ExecutionBoundary` exige sessao, SHA,
`state_validated=true` e comprovacao do Gateway. HEAD, limpeza, arquivos e hashes
sao verificados antes da primeira chamada a API.

Neste incremento, `ExecutionBoundary` e somente uma interface de atestacao do
chamador; ela nao verifica criptograficamente recibos do Launcher/Gateway. Por
isso o modulo permanece biblioteca sem CLI ou runtime operacional. Conectar um
verificador real e testavel e gate obrigatorio antes de qualquer uso fisico.

O manifesto da fila contem somente identidade, SHAs, adapter e hashes de
arquivos. Ele nao pode fornecer comandos. Um adapter instalado localmente
declara paths, papeis e chaves de comandos; o digest desse contrato deve
coincidir com o manifesto. As funcoes associadas as chaves sao injetadas
localmente pelo executor. Handlers do ReqSys, comandos de teste e logica de
produto nao pertencem a este repositorio.

## Ciclo

- Builder: validar fronteira e drift; ler task; claim exato; validar o snapshot
  adquirido e lease; start; adapter local; conferir diff Git, paths, comandos,
  limpeza e SHA; handoff para validacao.
- Validator: validacoes identicas; claim exato; adapter independente em checkout
  distinto; conferir SHA produzido e imutabilidade; complete.
- Replay usa recibo local vinculado ao hash do manifesto, worker, papel e
  workspace, mas somente depois de consultar o estado canonico. Em tarefa
  cercada, a geracao do recibo tambem deve continuar sendo a geracao canonica;
  recibo de posse antiga e rejeitado. Attestation e transporte devem continuar
  vigentes; o latest checkpoint canonico deve coincidir com geracao, tentativa,
  etapa e descriptor, e o transporte verifica o artefato novamente. O replay
  nao chama claim nem adapter. Lock exclusivo impede duas execucoes cooperativas
  do mesmo papel.
- Falha apos lease chama `fail` somente com codigo fechado. Excecoes desconhecidas
  viram `adapter_failed`. O reconciliador existente continua responsavel por
  lease expirado, tentativas e quarentena.

## Caminho cercado opt-in

Uma task com `fencing_required=true` usa exclusivamente
`POST /v1/claims/fenced/exact`. O executor valida `lease_generation`,
`attempt_id` e `recovery_context`, e envia a mesma cerca em start, checkpoint,
handoff, complete e fail. Efeitos `pending` ou `unknown` e efeitos confirmados
que nao sejam o artefato esperado bloqueiam a retomada antes do adapter. O
contexto de recuperacao tambem vincula task, geracao anterior, tentativa,
sequencia e ledger ao latest checkpoint; divergencia cross-task falha fechada.
Mesmo quando o efeito mais recente foi reconciliado como `not_applied`, este
incremento nao autoriza repetir a publicacao: bloqueia antes de `start` para uma
decisao explicita de reexecucao, sem criar um ciclo de fail/requeue/quarentena.

Esse caminho exige duas capacidades vindas de configuracao local confiavel,
nunca do manifesto da task:

- o ID do adapter deve constar em `fenced_adapter_ids`, atestando que suas acoes
  sao locais/idempotentes ou possuem fencing independente;
- deve existir um `DurableArtifactTransport` injetado, com
  `preflight`, `publish`, `verify` e `materialize` e descriptor opaco
  `artifact_kind`/`artifact_ref`/`artifact_sha256`.

O executor registra a publicacao como efeito `pending`, publica e verifica pelo
transporte e so entao grava o checkpoint `confirmed`. Cada POST de checkpoint
tem GET/readback independente; timeout de POST consulta o estado canonico e
nunca repete a mutacao sem saber se ela ocorreu.

Se `publish` ou sua verificacao perder um resultado confiavel, o executor tenta
registrar o efeito como `unknown`, bloqueia a task para reconciliacao e preserva
o erro fechado original. Ele nao chama `fail`, nao repete a publicacao e nao
transforma a incerteza em sucesso.

O repositorio oferece o backend concreto `S3GitBundleArtifactTransport`, descrito
em [S3_GIT_BUNDLE_ARTIFACT.md](S3_GIT_BUNDLE_ARTIFACT.md). Ele empacota somente
o delta entre o checkpoint inicial e o commit produzido, junto do contexto
cercado, em um envelope CAS imutavel, usa escrita
condicional e sempre confirma os bytes por leitura integral. Antes do claim, o
executor chama `preflight`; a implementacao atual usa um probe CAS fixo para
validar configuracao e comprovar `PutObject` condicional e
`GetObject` integral no prefixo autorizado antes da lease. A publicacao repete
essas garantias sobre o artefato efetivo, com readback independente.

O backend nao e descoberto nem ativado automaticamente. A configuracao padrao
continua bloqueando tasks cercadas antes do claim quando o chamador nao injeta
um transporte. Tambem nao ha bucket, credencial ou rota fisica homologada neste
repositorio. Os testes exercitam o backend contra um S3 sintetico e comprovam
roundtrip entre workspaces Git distintos; isso nao equivale a durabilidade
cross-host em um provedor real.

## Fora de escopo

Nao existe adapter de produto, CLI operacional nem provisionamento automatico
do armazenamento neste incremento. Nao ha publicacao Git, push, PR, modelo de
IA, browser, Docker ou execucao fisica. Um adapter futuro do ReqSys deve ser
versionado e testado no proprio repositorio, com comandos fixos no registro local.

## Proteção adicional contra falso replay

A releitura de recibo exige identidade da sessão e correlação originais,
`gateway_verified=true` com tipo booleano, `replayed=false` no registro
persistido, hash de evidência bem formado e consistência dos caminhos/comandos
com o contrato do adapter. O replay também compara os caminhos declarados ao
diff Git real entre base e commit produzido, detectando inclusive listas vazias.
Edicoes que rompam esses vinculos validados, ou a copia do recibo para outra
sessao, falham sem claim nem execucao do handler. A consulta canonica antecede
qualquer autorizacao de replay; regressoes positivas e negativas sao executadas
no CI.

Essa checagem consulta a task e o latest checkpoint canonicos, mas **nao e
atestacao criptografica de Session Launcher / Gateway**. O roundtrip automatizado
do transporte S3 usa dois workspaces independentes e um servico de objetos
sintetico; ainda nao prova durabilidade entre hosts fisicos nem um bucket real.
O executor nao esta habilitado para tarefas fisicas, publicacao de PR ou aceite
E2E de produto: esses gates continuam obrigatorios antes do primeiro ciclo real.
