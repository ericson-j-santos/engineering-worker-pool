# Cliente HTTP local do executor nativo — preparação sem execução física

O módulo `app/native_pool_http.py` implementa `WorkerApi` para o
`NativeOneShotExecutor`, reusando a API Worker Pool v1 existente.

- **Origem**: somente `http://127.0.0.1:<porta numérica explícita>`.
  Sem URL de manifesto, DNS, proxy, redirect ou transporte arbitrário no caminho
  operacional. Um cliente injetado é permitido somente ao harness isolado.
- **Autenticação**: Bearer lido a cada operação de arquivo privado POSIX, dono
  do processo, sem links simbólicos, arquivos com múltiplos hard links ou
  permissões para terceiros. Nenhum segredo em log/exception/artifact.
  **Windows bloqueado por padrão** até existir atestação real da ACL NTFS;
  permissão POSIX não constitui verificação Windows.
- **Claim**: somente `POST /v1/claims/exact`, task ID validado.
  Nenhum registro de workers, enqueue/requeue, criação de lane, scheduler,
  merge, deploy ou token provisioning.
- **Pós-condições**: operações start/validation/complete/fail exigem
  confirmação de estado via GET independente. HTTP 200 isolado não aprova.
  POST com transporte incerto não é repetido automaticamente.
- **Contrato**: resultado com identidade/estado inconsistentes falha fechado,
  resposta desproporcional, redirecionamento, 401, 5xx ou timeout também.
- **Testes**: FastAPI real + SQLite isolado, Builder/Validator exact claim,
  estado terminal e replay negativo; falhas de token, alvo, esquema e transporte.

**Limites**: a execução neste cliente não autentica por si o Session Launcher,
Command Gateway, identidade da Issue, elegibilidade do Builder/Validator,
modelo de IA nem o adaptador real do ReqSys. Não deve ser instalado no Desktop
nem reivindicar as tasks #2546/#2547 antes de atestação e cutover governado.

A prova definitiva exige tarefa real no SHA vigente, worktrees separados,
patch/commit, Validator independente, PR/checks/merge e E2E funcional DEV.
