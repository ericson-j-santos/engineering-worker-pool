# CAS de recibo RAG por issue — handoff durável (DEV)

## Escopo e status

Incremento aditivo do Worker Pool: `app/issue_rag_receipt_cas.py` publica e relê **somente o recibo de contexto** `worker.rag.issue-context-v1`, separado do bundle Git que contém o patch. Reutiliza `GitHubObjectCAS` e a branch pública estritamente vinculada à issue já autorizada. Não é um executor, não faz claim, não cria PR, não publica o patch e não chama Qwen.

Este código é **preparação revisável** até prova de backend GitHub com identidade real e duas máquinas/checkouts governados. Teste simulado de dois clientes não equivale a armazenamento remoto real nem a retomada entre hosts.

## Contrato de segurança e durabilidade

- Nenhum URL de GitHub, bucket, branch, conteúdo, comando ou credencial é escolhido pelo modelo. O destino permanece fixado pelo mesmo `IssueBinding` do CAS existente.
- Apenas citação curta de `README.md`, `AGENTS.md` ou `docs/ISSUE_QWEN7B_DRAFT_HANDOFF.md` **públicos** pode entrar no recibo. Sem conteúdo privado, segredo, issue body ou prompt integral em branch pública.
- O envelope tipado `issue_rag_context_v1` vincula `task_id`, `repository`, `issue_number`, `request_id`, `correlation_id`, `base_sha`, contrato/hash do adapter, hash do arquivo original, `lease_generation`, `attempt_id`, `effect_id` e recibo RAG completo. Esses metadados são operacionais públicos e não constituem credenciais.
- O cliente reconsulta autorização literal OWNER, issue aberta e HEAD da `main` **antes** da escrita. A versão fixada por SHA/contrato é a fonte, e o recibo passa por `bind_issue_rag` com leitura independente dos bytes antes da publicação.
- Pré-flight realiza a escrita condicional e leitura integral do objeto de prova já definido no transporte Git bundle. Sem sucesso nessa fase, não há publicação/claim autorizado.
- O CAS publica somente por chave de SHA-256 e `IfNoneMatch="*"`. Replicação/replay não faz overwrite: o segundo cliente precisa ler e comparar bytes completos antes de declarar sucesso. Timeout/resultado HTTP incerto exige readback; ausência ou divergência bloqueiam sem repetição cega.
- A materialização exige `DurableArtifact` tipado (`cas+issue-rag://sha256/...`), mesmo envelope/tarefa/lease e nova leitura do objeto e da fonte, validando Git blob SHA, SHA-256, URL e linha.
- Os clientes writer e independent devem ter **instâncias e portas distintas**. Isso detecta uso do mesmo objeto, mas **não comprova** que são identidades ou computadores independentes: somente o E2E físico e readback pela API da conta podem fazer essa afirmação.
- O `GitHubObjectCAS` aceita agora exatamente três tipos de arquivo `preflight-v1`, `git-bundle-v1`, `issue-rag-v1` e **no máximo três adições** na branch da issue. Quarta adição bloqueia. Branches legadas com somente dois objetos continuam aceitas. Nenhum objeto anterior é alterado ou removido.

## Fluxo futuro autorizado

1. Conferir issue, comentário OWNER na main atual, baseline com falha real, `Session Launcher`, `Command Gateway` e lease/fencing na task real.
2. Planejar fonte RAG com Qwen físico, recuperar e validar recibo issue-bound, executar o pré-flight CAS **antes** de reivindicar lease.
3. Persistir e reler o recibo, vinculando o artefato ao checkpoint autorizado com fencing.
4. Builder usa apenas o recibo materializado; faz reparo real e publica **outro objeto CAS** com patch/commit por SHA, seguindo o contrato durável global.
5. Validator em host/checkout independente baixa ambos os artefatos, faz 42/42 + 52/52, comprova SHA do commit, registra READY_FOR_PR.
6. Abrir **exatamente uma PR draft** produzida pelo agente (não esta PR de infraestrutura), reler API/head/diff e executar replay sem chamadas adicionais ao modelo nem duplicações.

Não executar publish/cas da issue #8 enquanto não existir aprovação OWNER para o **SHA corrente**. A autorização antiga vinculada a SHA anterior não é transferível. A PR de código não constitui autorização de trabalho físico, merge ou gasto. Custo incremental máximo R$ 0,00, sem Docker físico.

## Critérios de aceite deste incremento

- Tests: positivo, controle de schema/lease/base/task, operação não permitida, limite de branch, adulteração, replay, timeout antes/depois, leitura de fonte independente e nenhuma publicação sem pré-flight.
- CI Windows/Linux: testes específicos e regressões no HEAD, sem skipped relevantes.
- CI e mocks demonstram somente contrato. E2E GitHub real com credencial efêmera e duas instâncias, e readback físico com CAS continuam PENDENTES.

## Limite de concorrência

Além da contagem anterior à escrita, o GitHub CAS reconsulta a branch depois
da resposta de PUT. Se outra operação alterar o HEAD ou exceder o limite,
o fluxo bloqueia em vez de transformar a simples existência do objeto em
sucesso. Esse controle é **readback fail-closed, não compare-and-swap atômico**:
a API de Contents não oferece garantia aqui de serialização entre todos os
agentes. Um objeto órfão pode existir após corrida; não reexecutar cegamente.

## Consumo durável pelo Builder/Validator

A função `app/issue_rag_durable_adapter.py::adapter_from_durable_issue_rag`
materializa o recibo a partir do descritor `DurableArtifact` antes de criar
o adaptador Qwen. Ela exige `IssueBinding`, manifesto BASE, hash dos bytes
da origem, identidade do adapter, `lease_generation`, `attempt_id` e
`effect_id`. O `IssueRAGReceiptCAS.materialize` relê o objeto completo e
recupera/valida a fonte por cliente independente. Só depois o recibo
materializado é entregue ao `IssueQwenNativeAdapter`, que volta a conferi-lo
imediatamente antes da inferência ou validação.

No host Validator, o `base_manifest` que acompanha o recibo permanece
vinculado ao SHA **anterior** da tarefa; o `AdapterContext` normal do Validator
usa separadamente o SHA produzido. O recibo não autoriza publicar código,
não produz patch nem substitui os 42 casos e 52 regressões. A biblioteca
continua a exigir que o chamador governe autenticação, sessão, posse de
lease/fencing e independência física real dos hosts. Os testes deste elo
são simulações de dois clientes, não um teste de campo.
