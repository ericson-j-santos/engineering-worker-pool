# Merge governado por evento do Worker Pool

Este mecanismo reduz a dependência do chat para integrar um incremento
normal de código após a aprovação real do CI. **Não altera configurações
administrativas**, pois o repositório continua com `allow_auto_merge=false`.

O workflow recebe eventos `workflow_run: completed` de CI ou canário e
executa exclusivamente código da `main` confiável, jamais do HEAD de uma
PR sob token de escrita. Valida o autor owner, repositório e branch de origem,
tipo de evento, marcador explícito no corpo da PR, base `main` protegida,
`behind_by=0`, testes realmente executados, App do GitHub Actions,
canário quando seu path filter corresponde, ausência de reviews reprovados,
mais SHA exato antes e durante o merge.

Uma PR fica elegível somente com uma branch
`governed/<id-escopo-estavel>` e marcador:

`<!-- engineering-worker-pool:governed-automerge-v1 -->`

A automação **não** aceita forks, PRs draft, base obsoleta, SHA divergente,
arquivos de workflow, alteração da própria política, componentes centrais de
segurança ou check ausente, skipped, neutral ou falho. Outros PRs continuam
sujeitos à integração normal. `workflow_run` não executa código da PR com
permissões elevadas, e a API GitHub recebe `sha` como precondição.

O check `candidate-canary` só é obrigatório para os caminhos cobertos pelo
workflow de canário. Para uma mudança docs-only sem canário, ainda é exigido
um CI real no mesmo SHA. A proteção de branch do GitHub nunca é dispensada.
Qualquer recusa da API mantém a PR aberta e registra `GOVERNED_MERGE_BLOCKED`.

**Aceite:** primeiro validar as regressões e integrar este incremento sem
utilizar a nova automação; depois publicar uma PR real e estritamente limitada
com marcador/branch governados, confirmar pickup do evento, merge com SHA
esperado, HEAD pós-merge e execução CI na nova `main`. Até essa prova,
classificar o auto-merge como `IMPLEMENTED_NOT_E2E_PROVEN`.

Não autoriza automaticamente deploy, host, acesso a credenciais, execução de
trabalhadores, claim de task, alteração de banco nem ambiente HML/PROD.

**Identidade estável dos workflows:** GitHub pode sobrescrever o campo
`workflow_run.name` com o `run-name` dinâmico no evento `completed`.
O gate compara `workflow_id` e `path` oficiais:
CI 365494718 (`.github/workflows/ci.yml`) e canário 383149508
(`.github/workflows/candidate-canary.yml`), ambos com SHA, branch e
repositório de origem exatos. O teste preventivo usa nomes dinâmicos reais
e rejeita IDs/caminhos, branch ou repositório divergentes.


## Validação explícita após o merge

O piloto real da PR #32 comprovou a integração automática pelo GitHub Actions,
mas mostrou que uma operação de merge efetuada com o `GITHUB_TOKEN` não
aciona o CI normal por `push`. A execução posterior por `issue_comment`
validou apenas compilação/testes; **não** substitui um CI pós-merge próprio.

Depois de confirmar o HEAD da `main`, o workflow chama a API GitHub
`createWorkflowDispatch` **somente** para `.github/workflows/ci.yml`,
com `ref=main`, `expected_merge_sha` e `expected_merge_pr` tipados.
O próprio CI rejeita branch, evento, repositório, SHA ou PR divergentes
antes dos testes, usando o código versionado
`scripts/verify_postmerge_dispatch.cjs`.

O registro `GOVERNED_POSTMERGE_CI_DISPATCH_ACCEPTED` significa apenas
despacho aceito; a homologação requer o **novo** workflow
`event=workflow_dispatch`, SHA idêntico ao merge, passo de identidade
executado e job `test` terminal `success`.
Se a API recusar o dispatch, o merge permanece registrado mas a
automação falha com `GOVERNED_POSTMERGE_CI_DISPATCH_FAILED`, sem fingir
CI pós-merge aprovado. Isso é separado do aceite E2E do produto.

A permissão do token é `actions: write` somente no workflow confiável
de merge, já com permissão para atualizar PR; a CI de código continua
com `contents: read`.
