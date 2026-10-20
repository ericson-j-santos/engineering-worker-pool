# Continuidade nativa com autoridade externa — DEV

## Objetivo e fronteira da prova

`scripts/native_shared_continuity.py` executa a continuidade de uma tarefa sintética
usando os componentes do produto: `PostgresWorkerPoolStore`, o Controller real,
`app.main`, `LocalWorkerApi`, o Launcher/Gateway canônicos,
`NativeGatewayCommandRegistry`, `NativeOneShotExecutor` e
`S3GitBundleArtifactTransport.from_environment()`.

A autoridade PostgreSQL e o armazenamento S3 privado devem estar qualificados
antes do ensaio e permanecer fora dos executores. A e B são processos nativos
em dois runners hospedados distintos. Cada executor possui sua própria fachada
HTTP, restrita a IPv4 loopback, e Controller. Ambas as fachadas usam o mesmo
schema e a mesma identidade verificada da autoridade PostgreSQL.

O único repositório de tarefa é o fixture
`ci-isolated/native-fixture`. Seus três arquivos vêm de
`app.native_runtime.SYNTHETIC_FILES`; o perfil é fixo. O ensaio não recebe
comandos, adaptadores ou arquivos de produto por manifesto. Não altera tarefas
de produto nem autoriza cutover de produto.

## Sequência executada

1. **Definição durável antes de A.** O supervisor gera apenas a BASE Git
   determinística do fixture, calcula a identidade canônica da tarefa pelo
   Store e grava o manifesto original, seu SHA256, a BASE, o SHA do código e
   o SHA das regras em PostgreSQL. Relê esse documento por conexão nova antes
   de enfileirar a tarefa. A repetição de enqueue precisa retornar a mesma
   tarefa sem criá-la novamente.
2. **A real.** Um processo líder inicia uma nova sessão/grupo POSIX; sua API,
   Controller, Launcher, Gateway e comandos filhos herdam esse grupo.
   O runtime só registra o worker depois da atestação real. A adquire a
   geração 1, inicia a tarefa, produz o commit e publica o bundle no S3
   concreto. O checkpoint de publicação pendente é seguido do checkpoint
   `builder_result` confirmado.
3. **Checkpoint confirmado e perda da árvore de A.** Uma camada ASGI exclusiva
   do ensaio deixa a rota real de checkpoint concluir a transação PostgreSQL,
   persiste o descritor e os metadados da lease em C e somente então grava uma
   barreira privada e retém a resposta antes do handoff. O supervisor envia
   SIGSTOP ao grupo inteiro, verifica a árvore em `/proc`, relê a
   tarefa/checkpoint por conexão READ ONLY e materializa o bundle em uma BASE
   nova por outro cliente S3. Entre SIGSTOP e SIGKILL não há transação de
   escrita nem consulta de snapshot: um Controller congelado pode ainda deter
   a trava de escrita. O manifesto, o checkpoint, o descritor e os metadados
   antigos de lease já estão em C antes de SIGKILL. O supervisor mata todo o
   grupo, exige zero processos vivos do grupo e porta local fechada e só então
   persiste a evidência de término. Um descendente fora do grupo bloqueia a prova.
4. **B independente.** O outro runner recebe somente a identidade do run e a
   configuração de acesso qualificada. Lê a definição no PostgreSQL e exige
   `RUNNER_NAME` diferente. Reconstrói a BASE pelos arquivos versionados;
   nenhum arquivo da execução de A é transferido. Seu Controller real
   recupera a lease quando o relógio PostgreSQL permite; o supervisor apenas
   observa GETs. A história precisa registrar
   `lease_expired_recovered` antes da geração 2.
5. **Retomada pelo runtime.** O Builder B usa o manifesto original e o
   checkpoint da geração 1. O caminho de retomada do NativeOneShot materializa
   o bundle, confirma o mesmo artefato na geração 2 e faz o handoff. O recibo
   da sessão B deve registrar `runtime_commands_invoked=[]` e
   `runtime_command_count=0`: nenhum comando do adaptador é invocado pelo
   Registry nessa sessão. A materialização continua executando as operações
   Git reais necessárias para verificar e aplicar o bundle. A cadeia de
   checkpoints deve conter somente uma publicação pendente, na geração 1.
6. **Fencing na autoridade e no destino.** Depois do checkpoint confirmado de B
   e antes de liberar sua resposta, a fachada executa três requisições HTTP
   reais de renovação que devem retornar 409: lease original de A;
   credencial atual de B com geração antiga; credencial atual de B com
   attempt antigo. A tarefa deve permanecer idêntica. O destino sintético
   repete os três controles em PostgreSQL. Sua função valida `store._lease`
   e aplica o efeito na mesma transação `store._tx`, sob a trava da
   autoridade. Só a posse atual aplica o efeito. A repetição idêntica não
   reaplica; `applications` permanece 1.
7. **Validator e leitura final independentes.** O Validator B usa outro
   worker, sessão e worktree. Sua fonte começa em uma BASE nova e recebe o
   artefato de S3 com o manifesto produtor; o manifesto do Validator descreve
   os bytes produzidos. O runtime verifica o bundle e executa a validação
   real na geração 3. Depois de encerrar a API/Controller de B, outro
   interpretador lê a autoridade em transação READ ONLY, verifica
   tentativas/checkpoints/efeito e materializa novamente S3 em uma BASE nova.

A camada ASGI é somente instrumentação de teste em torno das rotas existentes.
Não contém fila, leasing ou resolução de checkpoint alternativos. O timeout
fixo de 8 segundos de LocalWorkerApi permanece; os controles de B são
síncronos e falham se não concluírem dentro desse orçamento. Não há avanço
artificial de relógio ou endpoint remoto para o Worker Pool.

## Execução governada e contexto

O bootstrap hospedado precisa concluir a validação canônica antes de chamar
o harness pelo Gateway da sessão EWP. O checkout das regras deve estar no SHA
pinado; a política POSIX é usada byte a byte a partir desse checkout. O harness
verifica novamente HEAD, árvore limpa e os blobs do Launcher, Gateway e
política antes de importar o Launcher. Nenhuma política temporária é gerada.

O runtime usa:

- `REQSYS_WORKSPACE_ROOT=/workspace`;
- `REQSYS_GATEWAY_STATE_DIR=/workspace/.reqsys-command-gateway-posix`;
- diretórios do ensaio sob `/workspace/.chatgpt-workers/`;
- `EXPECTED_SHA` igual a `GITHUB_SHA` e ao HEAD real;
- `EXPECTED_RULES_SHA` igual ao HEAD real do checkout de regras;
- push ou workflow_dispatch na branch
  `feat/hosted-cold-checkpoint-resume-20261020`, no repositório e proprietário
  fixados no código;
- identidade de run/attempt e `RUNNER_NAME` provenientes do GitHub.

Os argumentos do comando versionado, dentro da invocação governada, são:

```text
python -B scripts/native_shared_continuity.py --phase producer --root /workspace/.chatgpt-workers/native-<run>-<attempt>-a --rules-root <checkout-validado-das-regras>
python -B scripts/native_shared_continuity.py --phase consumer --root /workspace/.chatgpt-workers/native-<run>-<attempt>-b --rules-root <checkout-validado-das-regras>
```

O job consumidor depende do sucesso do produtor. Não existe output de job
contendo cápsula, snapshot, lease, manifesto ou credenciais. O run_id/run_attempt
é suficiente para localizar a definição durável na autoridade. A concorrência
do workflow deve impedir dois ensaios simultâneos no mesmo lane sintético,
sem cancelar uma prova já em execução.

## Pré-requisitos da autoridade e credenciais

O operador inicializa explicitamente o backend PostgreSQL, sua migração e
identidade. O runtime não recebe credenciais de operador e não executa DDL.
O schema deve ser exclusivo deste ensaio DEV, sem claimers legados. O driver
governado verifica a atestação real do Launcher/Gateway antes de habilitar o
fencing nesse schema por `store.enable_fenced_cutover`, com a correlação
atestada. Essa preparação antecede a primeira admissão de tarefa. O harness
exige que o fencing já esteja habilitado; as fachadas locais não o habilitam
durante sua inicialização. A configuração do app usa:

- `CODEX_WORKER_POOL_BACKEND=postgres`;
- `CODEX_WORKER_POOL_DATABASE_URL_FILE`: arquivo privado0600 do runtime;
- `CODEX_WORKER_POOL_DB_SCHEMA` e `CODEX_WORKER_POOL_AUTHORITY_ID`: iguais nos
  dois runners e conferidos pelo backend;
- `CODEX_WORKER_POOL_FENCED_CUTOVER_ATTESTED=0` ou ausente: as fachadas
  apenas usam a fila DEV preparada, sem tentar atualizar `pool_control`.
  A habilitação descrita acima exige temporariamente o grant correspondente
  no driver autorizado; esse grant pode ser removido após a preparação.
  O ensaio não declara cutover de produto.

O arquivo de URL pode ser o mesmo apontado por
`EWP_TEST_POSTGRES_URL_FILE`; nenhum DSN aparece em argumento, output ou
recibo. O backend exige TLS verificado. A configuração S3 vem dos campos
`CODEX_ARTIFACT_S3_BUCKET`, `CODEX_ARTIFACT_S3_REGION`,
`CODEX_ARTIFACT_S3_PREFIX` e `CODEX_ARTIFACT_S3_ENDPOINT`. O token Neon de
armazenamento é vinculado à branch do provedor, com permissões
`storage:read` e `storage:write`; a permissão de escrita também permite
excluir objetos. O provedor não restringe esse token ao bucket/prefixo do
ensaio. O bucket e o prefixo são limites da configuração da aplicação e
devem ser conferidos pelo operador. A cadeia boto3 real resolve as
credenciais; o harness não injeta cliente de teste.

O broker precisa verificar a identidade OIDC do GitHub e os claims pinados do
workflow antes de devolver somente a credencial runtime PostgreSQL e a
credencial S3 da branch dedicada. A validade curta do JWT OIDC limita o acesso
ao broker; ela não expira as credenciais estáticas PostgreSQL/S3 entregues.
A qualificação, a rotação e a revogação dessas credenciais pertencem ao operador.

Em cada fase real, o ponto de entrada importa
`scripts.native_private_config.runtime_environment()` e aplica o dicionário
ao ambiente antes de validar o contexto. O helper lê somente a configuração
privada previamente hidratada pelo driver. Payload do broker, JWT OIDC,
variáveis AWS e conteúdo dos arquivos privados nunca são impressos. A fachada
repõe suas próprias configurações locais, inclusive os dois arquivos distintos
de API e reconciliação, depois dessa hidratação. Importar o módulo para rodar
os contratos unitários não exige credenciais.

O operador cria também estas tabelas dentro do schema da autoridade:

```sql
CREATE TABLE native_continuity_runs (
  run_key TEXT PRIMARY KEY,
  source_sha TEXT NOT NULL,
  rules_sha TEXT NOT NULL,
  authority_id TEXT NOT NULL,
  task_id TEXT UNIQUE NOT NULL,
  base_sha TEXT NOT NULL,
  manifest_json TEXT NOT NULL,
  manifest_sha256 TEXT NOT NULL,
  phase TEXT NOT NULL,
  producer_runner TEXT,
  consumer_runner TEXT,
  artifact_json TEXT,
  produced_sha TEXT,
  stale_lease_json TEXT,
  process_evidence_json TEXT,
  final_receipt_json TEXT
);
CREATE TABLE native_continuity_effects (
  task_id TEXT NOT NULL,
  effect_id TEXT NOT NULL,
  payload_sha256 TEXT NOT NULL,
  applied_generation INTEGER NOT NULL,
  applied_attempt TEXT NOT NULL,
  applications INTEGER NOT NULL CHECK (applications = 1),
  PRIMARY KEY (task_id, effect_id)
);
```

A role runtime recebe SELECT e INSERT nessas duas tabelas. Na tabela de runs,
UPDATE é limitado às colunas `phase`, `consumer_runner`, `artifact_json`,
`produced_sha`, `stale_lease_json`, `process_evidence_json` e
`final_receipt_json`. Não recebe UPDATE das colunas de definição, nem
DELETE, nem UPDATE/DELETE no efeito. A imutabilidade da definição depende
também desses grants, que devem ser conferidos antes do ensaio. Metadados da
lease antiga são privados e usados exclusivamente no controle de replay.

A leitura final abre outro processo e outra conexão READ ONLY com a mesma role
runtime. Independência de processo/conexão não significa uma segunda identidade
de acesso ou armazenamento de outro provedor.

## Critérios de sucesso e limites

O recibo `NATIVE_SHARED_CONTINUITY_OK` só é gravado depois de confirmar:

- tarefa completed na geração 3 e tentativas 1→2→3;
- mesma referência/digest do artefato nos resultados de A, B e Validator;
- zero comandos do adaptador invocados pelo Registry na sessão Builder B;
- manifesto produtor íntegro em C, anterior à admissão da tarefa;
- término da árvore de A e de sua API; dois RUNNER_NAME distintos;
- recuperação por relógio real e registros de fim de tentativa consistentes;
- três rejeições de autoridade e três de destino, sem mutação tardia;
- uma aplicação do efeito e replay idempotente;
- leitura PostgreSQL READ ONLY após encerrar B, mais nova materialização S3;
- rejeição de descritor com digest divergente e de bundle apresentado com
  manifesto de outra tarefa, sem alterar o workspace da verificação negativa.

O segundo controle de artefato baixa o objeto real e valida sua identidade.
O primeiro recusa um descritor incompatível. Eles não corrompem objetos do
bucket e não simulam uma falha do provedor.

O supervisor de observação de A sobrevive para medir SIGKILL e registrar o
resultado do ensaio. Ele não é autoridade de tarefa/lease/artefato e não fornece
estado a B. O processo do GitHub runner também sobrevive. Esta prova não
declara destruição de VM, falha física de host, perda de toda a infraestrutura
Neon ou desastre regional. Nenhum container é necessário.

O destino é um efeito sintético transacional na autoridade externa, exercitado
pela interface de aplicação que valida a lease. Não qualifica um adaptador
de produto nem acesso SQL arbitrário de atores hostis. Um destino de produto
precisa de seu próprio contrato de fencing e readback.

O transporte S3 usa endereçamento por conteúdo, escrita condicional e
verificação de integridade. Isso não torna o token do provedor imune a
sobrescrita ou exclusão fora desse contrato. O ensaio cerca a referência
canônica e o efeito transacional; não afirma que um PUT bruto feito com
credencial antiga será rejeitado por número de geração. Um objeto órfão não
se torna checkpoint aceito. O tratamento existente de efeito
pendente/desconhecido permanece bloqueante.

`tests/test_native_shared_continuity.py` contém contratos unitários de
documento, identidade, privacidade, processo e ordenação ASGI. O teste Git
constrói duas BASEs reais, mas não qualifica provedor ou runtime. As fixtures
ASGI e de inventário não são evidência de PostgreSQL/S3. O E2E real só passa
quando produtor, consumidor, atestações e leitura independente terminam no
mesmo SHA. Os testes de contrato exigidos pelo bootstrap devem executar N>0
casos, OK e zero skips; um job que apenas importa o módulo não serve como prova.
