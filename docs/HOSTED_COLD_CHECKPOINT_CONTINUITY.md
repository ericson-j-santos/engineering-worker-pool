# Ensaio de retomada fria entre executores hospedados

## Escopo e aceite

Este incremento da issue #13/TODO-30 valida a continuidade cercada já integrada
pela PR #34, sem aguardar Desktop, Noteri ou RP23. Ele não cria outro scheduler,
fila operacional ou adapter de produto. Não modifica o serviço existente.

O workflow `Hosted cold checkpoint continuity` usa dois jobs hospedados distintos.
A grava um checkpoint via HTTP autenticado na API Worker Pool real e encerra a
instância isolada. Somente depois do encerramento, faz backup consistente SQLite
pelo método `backup`, encapsula o estado e entrega a cápsula ao job B. B verifica
run, tentativa, SHA, regras, integridade e identidade de executor antes de restaurar.
O controller real recupera a lease vencida; B adquire geração 2 e C valida na geração 3.
C tem sessão e worktree independentes, mas executa no mesmo host hospedado de B.

O aceite exige o mesmo task_id, checkpoint preservado, tarefa terminal completed,
um único efeito no destino SQLite sintético, rejeição da lease antiga na API e
na escrita condicional do destino, enqueue/checkpoint idempotentes, rejeição de
claim após conclusão e leitura independente do estado SQLite e HTTP.

A, B e C abrem sessões reais pelo Session Launcher e executam a materialização
do payload descartável pelo Command Gateway. O verificador nativo existente
confere snapshot, evento de inspect, hashes, sessão e SHA; um SHA deliberadamente
errado deve ser recusado antes de qualquer claim. A política temporária é uma
fixture restrita do ensaio, seguindo o precedente do E2E nativo existente; não
substitui nem amplia a política operacional POSIX de `/workspace`.

## Cadeia consolidada e política canônica do cenário Store

No evento de PR, o workflow chama primeiro o cenário Store por referência local
ao mesmo commit: produtor Store, receptor Store, produtor HTTP e receptor HTTP.
O workflow Store aceita somente `workflow_call`; seus filtros de aplicação,
dependências e controles ficam no chamador. Falha ou skip indevido do Store
bloqueia o HTTP. O push da branch continua um preflight HTTP separado, não prova
a execução dos quatro jobs. O CI geral permanece um gate independente.

A reconciliação incorpora o script e os testes da PR #35 na revisão
`07e8ebe1f91d384f7d8c32519f11065f6f46b379`. O cenário Store usa diretamente a
política POSIX canônica, sete arquivos de governança verificados e confiança
Git limitada ao caminho exato da etapa. O cenário HTTP conserva sua fixture
restrita descrita acima; não atribuir a ele a homologação da política Store.

`tests/test_hosted_rehearsal_workflow.py` protege a cobertura de filtros e os
vínculos de política, caminhos e confiança Git. Os controles de coordenação
existentes continuam exigindo ambos os cenários e detectando disparo duplicado.
Toda alteração exige nova prova dos quatro jobs no SHA resultante.
Outputs e variáveis de ambiente podem aparecer nos logs: somente dados sintéticos
são permitidos. Esta consolidação não cria autoridade durável nem transporte privado.

## Segurança e transporte

O serviço escuta apenas loopback e usa banco temporário. Há duas credenciais
aleatórias e distintas, válidas somente para a instância descartável, removidas
no encerramento. Nenhum segredo de repositório, conta, bucket ou host é usado.
A cápsula contém a lease sintética expirada para testar rejeição de replay, nunca
credenciais operacionais. A existência de um CODEX_WORKER_POOL_DB herdado bloqueia
a inicialização da fixture; o repositório sintético aceito é fixo.

A cápsula passa por job outputs no mesmo run, limitada a 120 KB codificados e
2 MB descompactados, com SHA-256 e rejeição de conteúdo concatenado/corrompido.
A codificação não é criptografia e não deve receber dados reais. Não há upload
de artefatos, cache pip, S3, novo bucket ou alteração de retenção nesta rotina.
Job output é transporte limitado à execução; não é armazenamento operacional
persistente, canal de publicação, API pública ou prova de backup em provedor real.

## Limites que permanecem

Este é um ensaio de recuperação fria de uma instância isolada após parada
controlada e restauração consistente. Não comprova failover automático ao vivo,
crash antes da exportação, disputa simultânea entre hosts, retorno de um processo
A ainda vivo, backend S3, persistência operacional, instalação de workers nativos
ou ciclo de produto ReqSys. A rejeição tardia usa a lease real de A transportada
na fixture; não simula que o computador A voltou a ficar disponível.

O serviço é a implementação real, os jobs são executores reais e HTTP/SQLite/
Launcher/Gateway são exercitados sem mocks no ensaio. O payload de cálculo e o
destino do efeito são sintéticos. O NativeOneShotExecutor não é ativado como
runtime por este harness. A próxima homologação operacional deve integrar o
transporte durável autorizado e o runtime nativo, com gate próprio de credenciais,
capacidade, corte de claimers antigos e efeito cercado no destino real.

## Execução e regressão

A branch dedicada executa compilação, controles da cápsula, suíte determinística
completa e política de merge antes do ensaio, permitindo preflight antes de abrir
PR. A validação existente da PR permanece intacta, incluindo E2Es exclusivos de PR.
Não integrar automaticamente: conferir todos os checks/revisões no HEAD final.

`tests/test_hosted_checkpoint_continuity.py` cobre roundtrip sem mutação,
idempotência, digest/SHA/run/tentativa/regras divergentes, tipos inválidos,
campos extras/ausentes, escopo de produto recusado, lease divergente, limite de
descompressão, fluxo comprimido concatenado e execução fora do contexto hospedado.
Os bytes SQLite nos unitários são fixture de formato, não prova de persistência;
o E2E usa arquivos reais separados e confere o efeito após restaurar.

Registrar run/job/runner IDs e HEAD final na issue #13 e no TODO-30 existente.
Manter #2546/#2547 sem claim/requeue. Resultado parcial/bloqueado deve conservar
as evidências já obtidas e a fronteira ainda não homologada; não repetir execução
sem mudança material de pré-condição.
