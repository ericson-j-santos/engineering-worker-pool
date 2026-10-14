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
