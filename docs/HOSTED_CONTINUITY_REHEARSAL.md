# Ensaio de checkpoint entre executores hospedados

## Escopo e limite

O workflow `hosted-continuity-rehearsal.yml` usa dois jobs hospedados separados,
cada um em um contêiner `python:3.12.15-bookworm`, e uma tarefa descartável
`example/hosted-continuity-rehearsal`. Reutiliza `WorkerPoolStore` sem modificar
o serviço instalado, a fila do TODO Global, as tasks ReqSys ou o transporte S3.

Este é um **ensaio serial de recuperação por snapshot**, não um failover
automático de um pool online nem homologação de computadores pessoais. O
coordenador descartável da primeira fase permanece ativo, recupera a lease
expirada e só então congela o banco. O segundo job restaura essa fotografia
em outro runner. A interrupção controlada ocorre no processo escritor do
primeiro job, não no coordenador nem em um serviço de produção.

## Fluxo verificável

1. Checkout do SHA da PR e das regras canônicas fixadas em
   `a430688c499077f5472841560f224c40766b0b78`. Antes de iniciar o launcher,
   conferir os SHA-256 fixados dos sete arquivos de governança, incluindo
   a política POSIX. Seu conteúdo deve coincidir integralmente com a revisão
   aprovada, sem gerar ou flexibilizar uma política para o ensaio.
2. Usar o checkout transitório em `GITHUB_WORKSPACE`, a governança em
   `.rehearsal-rules` e a política canônica desse checkout. Cada contêiner tem
   `/workspace` próprio, `REQSYS_WORKSPACE_ROOT=/workspace`, estado em
   `/workspace/.reqsys-command-gateway-posix` e worktrees reservados sob
   `/workspace/.chatgpt-workers`. Executar Session Launcher real, inspeção
   independente e Command Gateway real. Preservar o `GITHUB_SHA` nativo.
3. Executar os testes de contrato pelo Gateway. Exigir relatório com total
   maior que zero, resultado `OK`, zero falhas, zero erros e zero testes
   pulados; saída zero ou mensagem de sucesso isolada não são aceite.
4. Um processo cria somente uma tarefa cercada e publica um checkpoint local.
   Após readback independente, encerra com código deliberado 73. O controlador
   exige esse código, aguarda uma única expiração real limitada e executa a
   reconciliação existente.
5. Confirmar tarefa devolvida à fila e ausência de lease ativa. Produzir banco
   compacto com `VACUUM INTO`, removendo páginas livres, e encapsular snapshot,
   resultado público fixo, tarefa, geração, tentativa, run, SHA e hash.
6. Transmitir cápsula limitada a 128 KiB por output nativo do GitHub Actions.
   Esse transporte de ensaio não implementa `DurableArtifactTransport` e não
   substitui CAS/S3. Não requer bucket, credencial externa ou upload de artifact.
7. O segundo job exige `RUNNER_NAME` diferente, workspace novo e hashes
   independentes antes da restauração. Registrar também os hostnames dos
   contêineres, sem tratá-los como prova suficiente de runners distintos.
   O receptor não abre o filesystem do primeiro job.
8. Reivindicar a mesma tarefa com geração 2, conferir o checkpoint e rejeitar
   uma escrita com geração/tentativa antigas **mesmo usando a identidade e o
   token atuais**. Conferir que a rejeição não mudou tarefa nem checkpoint.
9. Exigir o controle positivo equivalente com a geração atual, replay de
   checkpoint sem duplicidade, resultado da etapa 2, Validator separado por
   papel e estado terminal relido em outra instância do Store. O Validator
   permanece no processo do receptor; não é um terceiro host independente.
10. Logs incluem `REHEARSAL_EVIDENCE`, SHA, run/tentativa, `producer_runner`,
    `receiver_runner`, hostnames dos contêineres, sessão, digest do snapshot
    do Gateway, digest da cápsula, quantidade de testes e resultados.

## Segurança

O ensaio usa a política POSIX canônica verificada antes do bootstrap e registra
`governance_profile=canonical_posix` e `policy_sha256`. O arquivo anterior
`config/hosted-rehearsal-gateway.json` permanece como histórico e não é usado
como política operacional deste caminho. Os controles de bootstrap, snapshot,
shell/inline, nomes sensíveis, HEAD e worktree continuam obrigatórios.

Como o contêiner e a fonte montada podem ter proprietários diferentes, cada
etapa governada recebe uma única configuração Git `safe.directory` via
`GIT_CONFIG_COUNT/KEY_0/VALUE_0`, vinculada ao `GITHUB_WORKSPACE` físico exato.
Essa configuração existe apenas no ambiente da etapa. Antes do Launcher,
o código confere chave, contagem e caminho, rejeitando curingas e outras
fontes. O Launcher continua como primeiro subprocesso, validando remoto,
SHA e sessão pela política canônica antes de executar o ensaio.

O acionamento aceita somente PR do proprietário no mesmo repositório público;
forks, outros atores, host físico e contexto divergente são rejeitados.
O processo filho não recebe tarefa, comando ou destino arbitrário. O teste usa
somente dados sintéticos públicos; não acessa produto, modelo ou segredo, nem
publica Git, faz merge ou altera serviços instalados.

A cápsula contém somente um banco descartável sem lease ativa e resultado
público fixo. Ela pode aparecer nos logs do GitHub ao ser transmitida por
outputs/ambiente; esse canal não oferece confidencialidade e nunca deve levar
dados de produto, tokens ou credenciais. O token efêmero da lease de teste é
liberado antes da exportação. Não registrar o conteúdo bruto do snapshot do
Gateway; a evidência contém apenas seu digest.

## Aceite e lacunas

A prova só conta após os **dois** jobs, no mesmo SHA, efetivamente executarem
Launcher/Gateway, testes não vazios sem skips, interrupção, integridade,
retomada, controle negativo e readback terminal. Job verde sem segunda fase,
output ausente, SHA antigo, mesmo runner, dado adulterado ou checkpoint
divergente não são aceite.

Mesmo com aprovação, manter `live_failover_verified=false` e
`s3_backend_verified=false`. Permanecem fora do aceite: coordenador durável
online independente dos executores, eleição por capacidade, injeção segura do
transporte CAS, perda física de um host e E2E de produto. Não usar este ensaio
para liberar a PR de inventário físico do Desktop.

Relacionado à issue #13 / TODO-30. Não criar outra fila.
