# Preflight de sessão nativa — vínculo com Gateway oficial

A função `app.native_gateway_preflight.attest_execution_boundary` é a
entrada para verificar a sessão do `NativeOneShotExecutor` antes de qualquer
operação de `get_task` ou `claim_exact`.

Ela exige código do `command_gateway` importado de instalação **confiável e
fixada**, hash SHA-256 esperado do código e política, ambos definidos pelo
executador/ambiente de confiança, nunca pela tarefa da fila.

Controles com leitura independente:

- política com bootstrap, snapshot e árvore limpa exigidos;
- reserva `materialized` do **mesmo** worktree, não da base ou de outra sessão;
- snapshot oficial com checksum, SHA, host, política e fonte do Gateway;
- HEAD e estado Git atual confrontados com o snapshot, sem mudança de arquivos;
- `inspect` efetivamente registrado no JSONL do Gateway para a sessão,
  correlação, worktree e SHA atuais;
- preflight e evento recentes (janela máxima de 300 s);
- reexecução somente leitura com resultado idêntico e controles negativos.

O CI da PR faz checkout da revisão oficial `ddd09dc616181c0aaf46d33a194e9b00a6989332`
em pasta isolada e exercita **as funções reais** `session_preflight`,
`enforce_session`, `git_state` e `inspect`. Testa SHA divergente, sessão
incorreta, evento inexistente/bloqueado, estado alterado, hash do código
incorreto e snapshot adulterado, com readback após restaurar o canário.

**Limite de segurança:** os arquivos locais protegidos por controles do host
não são assinatura criptográfica de um agente independente. A prova confirma
pré-condições e o uso anterior do Gateway para leitura; não autoriza que um
adaptador dispare comandos por terminal, execute remotamente, registre workers
ou faça claim de tarefas reais. A execução efetiva ainda deve passar pelo
Gateway governado, com recibo do próprio comando validado depois.

Também permanece pendente o contrato de remoto/origem do worktree do
`NativeOneShotExecutor`; não remover o bloqueio de remotos até atestar
origem e comandos autorizados, sem bypass.

## Canário integrado de fila e código (CI hospedado)

O mesmo teste com o Gateway fixado executa também uma **tarefa
sintética descartável**, atravessando a API FastAPI real (ASGI in-process),
SQLite, claim exato, Builder que cria um commit de correção e Validator em
clone e sessão separados. O teste confirma falha original, mudança funcional,
estado persistido por GET independente, 401 sem token, controle de SHA
divergente, replay sem duplicidade e bloqueio de claim após concluir.

Resultado explícito: `NATIVE_CHAIN_ISOLATED_PASSED`, com
`native_chain_real_product_issue=false` e
`native_chain_external_pr_created=false`. Essa prova demonstra o encadeamento
entre as bibliotecas, **não** uma tarefa real/PR/merge/DEV e não é o aceite
do primeiro ciclo autônomo. As alterações do fixture em CI não autorizam
executar adaptadores por subprocesso fora do Command Gateway no Desktop.

## Execução de comandos pelo Gateway, com recibo independente

O módulo \`app/native_gateway_commands.py\` integra o \`NativeOneShotExecutor\`
ao Command Gateway aprovado, sem interpretar comando da fila.
\`GovernedCommand\` declara apenas chaves locais/por papel com argv fixo.
Scripts Python de produto exigem SHA-256 fixado fora da tarefa.
Operações Git são restritas a add/commit/status previstos.
Cada chamada revalida sessão, política/código do Gateway, HEAD e o evento
\`run\` emitido pelo próprio Gateway, confrontando estado antes/depois.
Falhas, scripts alterados, comandos arbitrários, papéis diferentes e
recibos divergentes bloqueiam o processamento.

O CI usa um fixture Git descartável e agora passa a modificação, stage,
commit e verificação do Builder/Validator pelas funções reais de
\`command_gateway.execute\`. O efeito fica isolado no runner hospedado;
a fila, a Issue e o runtime do ReqSys permanecem intocados.

**Não libera produção:** um handler real precisa ser aprovado e pinado
separadamente no repositório de produto; ainda faltam sessão física,
Worker Pool instalado e tarefas com capacidade validada, ACL NTFS e
readback E2E do ReqSys.
