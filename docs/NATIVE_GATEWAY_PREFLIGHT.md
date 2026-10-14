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
