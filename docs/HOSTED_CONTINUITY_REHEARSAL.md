# Ensaio de checkpoint entre executores hospedados

## Escopo e limite

O workflow `hosted-continuity-rehearsal.yml` usa dois jobs hospedados separados
e uma tarefa descartável `example/hosted-continuity-rehearsal`. Reutiliza
`WorkerPoolStore` sem modificar o serviço instalado, a fila do TODO Global,
as tasks ReqSys ou o transporte S3.

Este é um **ensaio serial de recuperação por snapshot**, não um failover
automático de um pool online nem homologação de computadores pessoais. O
coordenador descartável da primeira fase recupera a lease expirada antes de
congelar o banco. O segundo job restaura essa fotografia em outra máquina.
A interrupção controlada ocorre no processo escritor do primeiro job, não
no processo do coordenador nem em um serviço de produção.

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
   de ensaio não implementa `DurableArtifactTransport` e não substitui CAS/S3.
6. Outro job exige identidade de host diferente, workspace novo e hashes
   independentes antes da restauração. Não abre filesystem do primeiro job.
7. Reivindicar a mesma tarefa com geração 2, conferir o checkpoint e rejeitar
   uma escrita com geração/tentativa antigas **mesmo usando a identidade e o
   token atuais**. Conferir que a rejeição não mudou tarefa nem checkpoint.
8. Exigir o controle positivo equivalente com a geração atual, replay de
   checkpoint sem duplicidade, resultado da etapa 2, Validator independente
   por papel e estado terminal relido em outra instância do Store.
9. Logs incluem `REHEARSAL_EVIDENCE`, SHA, run/tentativa, hosts, sessão,
   snapshot do Gateway, digest da cápsula e resultados. A cápsula não é
   impressa. Ela contém somente um banco de teste sem lease ativa.

## Segurança

O perfil do Gateway é limitado ao layout fixo do GitHub-hosted Linux deste
repositório e a worktrees separados. Preserva bootstrap, snapshot, bloqueio de
shell/inline, nomes sensíveis e HEAD. Não amplia a política de hosts pessoais.
O acionamento aceita somente PR do proprietário no mesmo repositório público;
forks, outros atores, host físico e contexto divergente são rejeitados.

O processo filho não recebe tarefa, comando ou destino arbitrário. Não há
rede no teste de dados, modelo, dados de produto, publicação Git, merge,
mudança de DNS, privilégio ou leitura de segredo. O token efêmero interno da
lease descartável é liberado antes de exportar o snapshot; não há servidor
autenticado exposto nem credencial técnica dentro da cápsula.

## Aceite e lacunas

A prova só conta após os **dois** jobs, no mesmo SHA, efetivamente executarem
Launcher/Gateway, testes, interrupção, integridade, retomada, controle negativo
e readback terminal. Job verde sem segunda fase, output ausente, SHA antigo,
mesmo host, dado adulterado ou checkpoint divergente não são aceite.

Mesmo com aprovação, manter `live_failover_verified=false` e
`s3_backend_verified=false`. Permanecem fora do aceite: coordenador durável
online independente dos executores, eleição por capacidade, injeção segura do
transporte CAS, perda física de um host e E2E de produto. Não usar este ensaio
para liberar a PR de inventário físico do Desktop.

Relacionado à issue #13 / TODO-30. Não criar outra fila.
