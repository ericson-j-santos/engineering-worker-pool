# Ensaio de checkpoint entre executores hospedados

## Escopo e coordenação

Este cenário verifica a perda do processo escritor com o controlador sobrevivente.
Reutiliza `WorkerPoolStore`, o script e os testes originados na PR #35.
A recuperação HTTP após parada do serviço/controlador, da PR #36, é um cenário
diferente; nenhum dos dois substitui o outro.

Na branch consolidada da PR #36, `hosted-continuity-rehearsal.yml` é um
workflow reutilizável (`workflow_call`), chamado pelo
`hosted-checkpoint-continuity.yml` da mesma revisão. Ele não recebe outro
disparo automático de PR. Os jobs `produce` e `resume` continuam em executores
hospedados diferentes; só depois de ambos passarem o chamador inicia seu
produtor HTTP e seu consumidor, também em dois jobs diferentes.
Não há fan-out entre os dois cenários nem dois runs equivalentes de PR.

A atualização preserva o histórico da PR #35 como segundo pai do commit
de consolidação. Não altera a branch original da #35, a main, a fila do TODO
Global, as tasks ReqSys ou o transporte S3.

## Fluxo verificável

1. Checkout do SHA da PR e regras canônicas fixadas. Session Launcher real,
   worktree exclusivo e Command Gateway real, incluindo inspeção independente.
2. Testes de contrato pelo Gateway, sem dependências externas adicionais.
3. Um processo cria somente uma tarefa cercada e publica um checkpoint local.
   Após readback independente, encerra com código deliberado 73. O controlador
   exige esse código, aguarda uma única expiração real limitada e executa a
   reconciliação existente.
4. Confirmar tarefa devolvida à fila e ausência de lease ativa. Produzir banco
   compacto com `VACUUM INTO`, removendo páginas livres, e encapsular snapshot,
   resultado público fixo, tarefa, geração, tentativa, run, SHA e hash.
5. Transmitir cápsula limitada a 128 KiB por output nativo do GitHub Actions.
   Não criar bucket, credencial, serviço ou upload de artifact. Esse transporte
   não implementa `DurableArtifactTransport` e não substitui CAS/S3.
6. Outro job exige identidade de host diferente, workspace novo e hashes
   independentes antes da restauração. Não abre filesystem do primeiro job.
7. Reivindicar a mesma tarefa com geração 2, conferir o checkpoint e rejeitar
   uma escrita com geração/tentativa antigas mesmo usando a identidade e o
   token atuais. A rejeição não pode mudar tarefa nem checkpoint.
8. Exigir o controle positivo equivalente com geração atual, replay de
   checkpoint sem duplicidade, resultado da etapa 2, Validator independente
   por papel e estado terminal relido em outra instância do Store.

## Segurança e observabilidade

O perfil do Gateway é limitado ao layout fixo do GitHub-hosted Linux deste
repositório e a worktrees separados. Preserva bootstrap, snapshot, bloqueio de
shell/inline, nomes sensíveis e HEAD. Não amplia a política de hosts pessoais.
O acionamento aceita somente PR do proprietário no mesmo repositório público;
forks, outros atores, host físico e contexto divergente são rejeitados.

O processo filho não recebe tarefa, comando ou destino arbitrário. Não há rede
no teste de dados, modelo, dados de produto, publicação Git, merge, mudança de
DNS, privilégio ou leitura de segredo. A lease é liberada antes do snapshot;
não há servidor autenticado exposto nem credencial operacional na cápsula.

`REHEARSAL_EVIDENCE` contém SHA, run/tentativa, hosts, sessão, snapshot do Gateway,
digest e resultados. O script não imprime explicitamente a cápsula, mas o GitHub
a registra no cabeçalho de variáveis de ambiente do job receptor. Portanto,
**os logs não são privados, e base64/zlib não criptografam**.
Somente a fixture sintética descartável é permitida nessa rota.

## Aceite e lacunas

A prova exige os dois jobs no mesmo SHA com Launcher/Gateway, testes,
interrupção, integridade, retomada, controle negativo e readback terminal.
Na PR consolidada, os dois cenários completos são obrigatórios; o CI geral
continua sendo outro gate. O push da branch testa a regressão e o cenário
HTTP, mas não substitui o cenário de escritor exclusivo do evento de PR.

`tests/test_continuity_workflow_coordination.py` rejeita trigger duplicado,
referência móvel, dependência removida, skip indevido na PR, permissões ampliadas,
testes suprimidos, perda de instalação de dependências e afinidade física.
Preserva os controles próprios dos dois harnesses.

Continuam `live_failover_verified=false` e `s3_backend_verified=false`.
Os snapshots seriais não comprovam coordenador durável online independente,
eleição por capacidade, transporte privado, perda da VM inteira ou E2E de produto.
Não sobrescrever uma fila viva com esses snapshots nem liberar a PR #71 do Desktop.

A próxima etapa operacional depende de uma autoridade canônica de tarefa/posse
e do armazenamento privado autorizado, sem criar credenciais ou ampliar loopback
por inferência. Reutilizar issue #13 / TODO-30; não criar outra fila ou ensaio.
