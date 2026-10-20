# Cliente HTTP local do executor nativo — preparação sem execução física

O módulo `app/native_pool_http.py` implementa `WorkerApi` para o
`NativeOneShotExecutor`, reusando a API Worker Pool v1 existente.

- **Origem**: somente `http://127.0.0.1:<porta numérica explícita>`.
  Sem URL de manifesto, DNS, proxy, redirect ou transporte arbitrário no caminho
  operacional. Um cliente injetado é permitido somente ao harness isolado.
- **Autenticação**: Bearer [REDACTED] a cada operação de arquivo privado POSIX, dono
  do processo, sem links simbólicos, arquivos com múltiplos hard links ou
  permissões para terceiros. Nenhum segredo em log/exception/artifact.
  **Windows bloqueado por padrão** até existir atestação real da ACL NTFS;
  permissão POSIX não constitui verificação Windows.
- **Claim**: tasks legadas usam `POST /v1/claims/exact`; tasks cercadas usam
  exclusivamente `POST /v1/claims/fenced/exact`, sempre com task ID validado.
  A resposta cercada deve conter geracao, tentativa e contexto de recuperacao.
  Nenhum registro de workers, enqueue/requeue, criação de lane, scheduler,
  merge, deploy ou token provisioning.
- **Pós-condições**: operações start/validation/complete/fail exigem
  confirmação de estado via GET independente. HTTP 200 isolado não aprova.
  Geracao e tentativa sao propagadas juntas nas mutacoes cercadas.
  Checkpoints aceitam somente campos limitados e fazem GET/readback canonico.
  Se o POST de checkpoint tiver resultado de transporte incerto, o cliente
  consulta o checkpoint mais recente e exige igualdade de todos os campos
  semanticos enviados; ele nao repete o POST. Demais POSTs incertos tambem nao sao
  repetidos automaticamente.
- **Efeito ambiguo**: o cliente expoe a mutacao cercada `block`; o executor a
  usa depois de registrar `unknown`, liberando o lease para reconciliacao sem
  converter a incerteza em uma tentativa comum de `fail`.
- **Contrato**: resultado com identidade/estado inconsistentes falha fechado,
  resposta desproporcional, redirecionamento, 401, 5xx ou timeout também.
- **Testes**: FastAPI real + SQLite isolado, rotas legada e cercada,
  checkpoints, troca de geracao, rejeicao stale, estado terminal e replay
  negativo; falhas de token, alvo, esquema, transporte e readback divergente.

**Limites**: a execução neste cliente não autentica por si o Session Launcher,
Command Gateway, identidade da Issue, elegibilidade do Builder/Validator,
modelo de IA nem o adaptador real do ReqSys. Não deve ser instalado no Desktop
nem reivindicar as tasks #2546/#2547 antes de atestação e cutover governado.
O cliente tambem nao fornece armazenamento duravel de artefato: o executor
cercado permanece bloqueado sem um transporte confiavel injetado e um adapter
explicitamente atestado pela configuracao local.

A prova definitiva exige tarefa real no SHA vigente, worktrees separados,
patch/commit, Validator independente, PR/checks/merge e E2E funcional DEV.
