# Executor nativo one-shot

`app/native_one_shot.py` coordena um único Builder ou Validator sobre os
contratos existentes do Worker Pool. Ele não registra workers, inicia serviço,
lê token, instala scheduler nem escolhe tarefas: usa exclusivamente claim exato.

## Fronteira de confiança

O chamador deve entregar um worktree local isolado, sem remoto, já materializado
por Session Launcher e Command Gateway. `ExecutionBoundary` exige sessão, SHA,
`state_validated=true` e comprovação do Gateway. HEAD, limpeza, arquivos e hashes
são verificados antes da primeira chamada à API.

O manifesto da fila contém somente identidade, SHA, adapter e hashes de arquivos.
Ele não pode fornecer comandos. Um adapter instalado localmente declara paths,
papéis e chaves de comandos; o digest desse contrato deve coincidir com o
manifesto. As funções associadas às chaves são injetadas localmente pelo executor.
Handlers do ReqSys, comandos de teste e lógica de produto não pertencem a este
repositório.

## Ciclo

- Builder: validar fronteira e drift → ler task → claim exato → start → adapter
  local → conferir paths/comandos/SHA → handoff para validação.
- Validator: validações idênticas → claim exato → adapter independente → conferir
  SHA já produzido → complete.
- Replay usa recibo local vinculado ao hash do manifesto, sem nova chamada à API
  ou ao adapter. Lock exclusivo impede duas execuções cooperativas do mesmo papel.
- Falha após lease chama `fail` com código fechado. O reconciliador existente
  continua responsável por lease expirado, tentativas e quarentena.

## Fora de escopo

Não existe adapter de produto neste incremento. Não há CLI operacional, cliente
HTTP/token-file, registro persistente, publicação Git, push, PR, modelo de IA,
browser, Docker ou execução física. Um adapter futuro do ReqSys deve ser
versionado e testado no próprio repositório, com comandos fixos no registro local.
