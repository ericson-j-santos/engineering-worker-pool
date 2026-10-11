# Revisões vinculadas a linhas da proposta

O run 38109052315/HEAD 47ec50ac terminou com `revision_edit_not_unique`.
A cópia literal do trecho anterior produzida pelo modelo não identificou uma
ocorrência única. O diagnóstico não distingue ausência de correspondência de ambiguidade nessa
versão; nenhuma das duas é tratada como autorização de escrita.

O provedor passa a pedir `anchor` e `new`. Os identificadores L2, L3 etc.
são construídos a partir da função normalizada e vinculada ao fingerprint
do feedback. L1, a assinatura, nunca é editável. O schema enumera somente
linhas existentes; repetição de âncora, campo desconhecido e fonte divergente
são bloqueados. A substituição não procura texto e não reancora por heurística.

Cada `new` substitui uma linha original. A primeira linha é relativa à margem
do alvo; o montador acrescenta apenas a indentação original. Linhas adicionais
podem conter blocos relativos. Todas as posições se referem à origem imutável:
uma substituição não desloca as âncoras das outras. Limite de seis edits,
limites de tamanho, assinatura, guard AST, 42 casos e duas propostas permanecem.

O contrato legado `apply_local_edits` continua disponível e coberto por testes.
Somente o transporte da revisão do modelo usa as âncoras. Não há solução pronta,
normalização de código proibido ou mudança da função de produto no repositório.
O diagnóstico continua sem código-fonte, prompts ou credenciais e é preservado
mesmo quando uma âncora ou o guard é rejeitado. A aprovação ainda depende do
E2E real, patch reaplicável, regressão independente e replay.

## Entradas sem efeito e repetição real

Uma revisão pode mencionar uma linha sem modificá-la junto com outras mudanças.
O montador não rejeita o conjunto por essa entrada isolada: compara a montagem
com a origem e, em seguida, compara a AST completa. Conjunto inteiramente
inalterado e diferenças apenas cosméticas continuam bloqueados antes de testes
do candidato ou gravação. A identidade/duplicidade das âncoras e todos os
controles de segurança permanecem. Não há aproveitamento automático de proposta
que reprovou testes; apenas montagem atômica do conjunto submetido.


## Prevenção de cabeçalho órfão

No run 38137690003 a montagem retornou `candidate_syntax`:
`unexpected_indent` na linha 28, após linha anterior sem `:`.
O texto da revisão não foi persistido, portanto não é possível atribuir
com certeza qual âncora causou o problema.

Como proteção contra essa classe de erro, uma âncora cujo texto original
termina em `:` (abre bloco) só admite substituição cuja última linha
não vazia também termine em `:`. Assim, o corpo existente não é deixado
órfão por uma edição isolada. A regra é restritiva: não cria ou reindenta
código, não corrige automaticamente a resposta do modelo e não afrouxa
nenhum guard. Substituições multilinha válidas continuam aceitas.
Os 42 casos e as políticas de segurança permanecem inalterados.


## Contrato de saída condicionado pela âncora

O run 38138410985 demonstrou que uma instrução textual não basta:
o modelo retornou uma alteração com `revision_block_header`, e o
montador a bloqueou antes de gerar qualquer patch.
Agora o JSON Schema enviado ao Ollama separa âncoras de cabeçalho
(cuja linha original abre um bloco com `:`) das demais. Apenas
esses cabeçalhos exigem que o novo texto termine em `:`.
A opção `oneOf` associa cada âncora à restrição correspondente;
o validador de montagem continua independente e obrigatório.
A implementação não altera o modelo, os 42 casos ou as regras AST.
Nenhuma tarefa real da fila é despachada por esse teste.


## Edição estruturada de guarda com retorno antecipado

O E2E `38140932676`, HEAD `68d1f168`, concluiu a chamada
de revisão, mas rejeitou `revision_block_header` novamente.
Isso demonstra que o contrato de saída condicionado por `oneOf`
não assegurou, sozinho, o preservamento do cabeçalho.

Uma nova variante tipada aceita `{anchor, guard, return_expression}`
somente em cabeçalhos `if` existentes. O modelo escolhe ambas as
expressões. O montador emite a estrutura fixa `if <guard>: return
<return_expression>` antes do cabeçalho original, mantendo a função,
assinatura, indentação e bloco existentes. Não há solução pronta,
substituição de código pelo executor nem normalização silenciosa.
As expressões e a função inteira continuam submetidas à AST original,
aos 42 casos, ao SHA de origem, ao limite de seis edições e à
validação independente. Entradas com índice, quebras de linha ou
âncoras erradas são rejeitadas antes da montagem.


## Otimização da gramática de inferência — evidência de 11/10/2026

Os runs [38140932676](https://github.com/ericson-j-santos/engineering-worker-pool/actions/runs/38140932676) e
[38142010897](https://github.com/ericson-j-santos/engineering-worker-pool/actions/runs/38142010897)
distinguem duas situações: rejeição por \`revision_block_header\` e, depois,
ausência de conclusão da chamada \`/api/chat\` de revisão até o limite governado.
A gramática \`oneOf\` com três variantes é uma hipótese de alto impacto no custo
do modelo local, ainda não demonstrada por benchmark isolado.

A entrada \`format\` enviada ao Ollama passa a usar \`inference_schema\`, um
envelope com âncora enumerada, no máximo seis edições, limites de tamanho e
restrição de colchetes. Não há \`oneOf\` no envelope de geração. A presença
dos campos opcionais na geração **não autoriza** combinações arbitrárias:
\`assemble_anchors\` continua exigindo uma das duas formas exatas
(\`anchor/new\` ou \`anchor/guard/return_expression\`) e, para cabeçalhos
originais, continua exigindo preservação de \`:\` e da sintaxe Python.

Portanto, uma proposta malformada pode sair do modelo, mas **deve continuar
sendo rejeitada** pelo guard independente. Assinatura, AST, execução isolada,
42 casos e ausência de fallback sintético permanecem inalterados. O ganho de
tempo e a aceitação do modelo real só poderão ser afirmados depois de um novo
E2E no SHA corrigido.
