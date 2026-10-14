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
  workspace, sem nova chamada a API ou ao adapter. Lock exclusivo impede duas
  execucoes cooperativas do mesmo papel.
- Falha apos lease chama `fail` somente com codigo fechado. Excecoes desconhecidas
  viram `adapter_failed`. O reconciliador existente continua responsavel por
  lease expirado, tentativas e quarentena.

## Fora de escopo

Nao existe adapter de produto neste incremento. Nao ha CLI operacional, cliente
HTTP/token-file, registro persistente, publicacao Git, push, PR, modelo de IA,
browser, Docker ou execucao fisica. Um adapter futuro do ReqSys deve ser
versionado e testado no proprio repositorio, com comandos fixos no registro local.
