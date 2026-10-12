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


## Limite de custo computacional da revisão no CPU (11/10/2026)

No E2E `38143653191` (HEAD `42a73a2`), o artefato validado
`11703519256` registrou `phase=revision`, `step=generation`,
`event=started` e nenhum evento terminal antes do encerramento do gateway
em aproximadamente 285 segundos. O compactador de esquema não bastou.

A menor hipótese reversível a verificar é limitar somente a segunda geração
(`num_predict` de 768 para 384 tokens e `num_ctx` de 8192 para 4096),
preservando temperatura zero, seed, o modelo Qwen2.5-Coder 7B, a quantidade
de propostas, o primeiro prompt e todos os limites de execução. A resposta
modelada continua sujeita a parser JSON completo, esquema exato de edições,
AST, SHA, 42 casos, replay e validação independente; respostas truncadas
falham fechadas e nunca são aplicadas ou apresentadas como sucesso.

Esse limite reduz o trabalho computacional máximo por revisão, mas não prova
que a revisão passará nos testes nem que o modelo completará dentro do prazo.
O resultado real depende de novo E2E no SHA corrigido, sem reruns no SHA
anterior, serviços pagos ou aumento de timeout.

## Transporte JSON leve para revisão — 11/10/2026

A execução [38184529245](https://github.com/ericson-j-santos/engineering-worker-pool/actions/runs/38184529245) no HEAD `4769d8fb02c3b5e6439faeab5f97e11a847e7cca` concluiu compilação, suíte e E2E numérico. O artefato `11719540574` (SHA-256 `111fd678ee28028676fa32c9d74f8b47b4d5a5c5875240fc33fd01a2e7060a3e`) mostra a revisão parada na geração, sem retorno terminal e sem patch/evidência de aceite. Reduzir o prompt e limitar tokens/contexto não eliminou o bloqueio.

Hipótese reversível: a gramática JSON Schema dinâmica tem custo elevado para a revisão do modelo 7B em CPU. Apenas a segunda proposta passa a usar o modo oficial `format: "json"` do Ollama, sem passar esquema dinâmico ao decodificador. Isso **não confere confiança** ao JSON gerado: `apply_anchored_edits` continua executando as mesmas validações estritas de formato de edição, IDs imutáveis, fingerprint, limite de seis edições, cabeçalhos, sintaxe, AST e assinatura. Os 42 casos, replay, patch reaplicável e regressão independente continuam obrigatórios. Modelo, seed, parâmetros de tempo, limite de tokens e modo da proposta inicial não mudam.

A otimização só será aprovada após E2E real no SHA corrigido. Respostas incompletas, JSON fora do contrato ou propostas não autorizadas continuam bloqueadas, sem fallback sintético e sem merge automático.

## Âncoras tipadas sem regex no decodificador — 11/10/2026

No SHA `5231b98ae8fa56c3bd4743e0408e8765a3a8b90d`, o E2E real
[38187336402](https://github.com/ericson-j-santos/engineering-worker-pool/actions/runs/38187336402)
com `format: "json"` concluiu a geração da revisão e foi rejeitado por
`revision_anchor_invalid`, antes de aplicar código. O artefato
`11719578972` foi conferido (SHA-256 do ZIP:
`cd0e7a0fe5782607f03d7e8d9ae91781527fa5f4543aab14c01e09a14ae65505`).
O registro não distingue identificador desconhecido de guarda usada fora
de cabeçalho `if`; não presumir qual ocorreu.

A saída da revisão passa a usar um *schema leve*: somente os IDs de linhas
exatas são enumerados; expressões e substituições são strings sem regex ou
limites caros na gramática do modelo. A instrução também fornece a lista
`GUARDABLE_IF_ANCHORS_JSON`, montada exclusivamente das linhas `if`
existentes na proposta vinculada ao SHA. Essas listas são dicas à geração,
não permissões adicionais. O montador `assemble_anchors` e
`apply_anchored_edits` continuam a rejeitar IDs ausentes/duplicados,
guarda em cabeçalho incompatível, formato não tipado, comprimentos
excessivos, alterações da assinatura, sintaxe/AST proibida e repetição da
mesma AST. Não existem reancoragem por aproximação, fallback de solução
pronta, novas tentativas ou relaxamento de 42 casos.

A eficácia depende do E2E real no novo HEAD, incluindo patch, replay e
regressões independentes. Esta mudança não autoriza merge.

## Substituições sem guardas geradas pelo modelo — 11/10/2026

O E2E real [38188056623](https://github.com/ericson-j-santos/engineering-worker-pool/actions/runs/38188056623)
no SHA `6b0bc272c75f33d4e9020b4ce5ac78658154b67e` validou
as duas propostas, incluindo a revisão com sintaxe/AST aprovadas. Porém,
`candidate_tests_failed` bloqueou a aprovação dos 42 casos. O artefato
`11719534827` (ZIP SHA-256
`1c51d7ac6e1fbdc4210b086a7a6582db29b048f58205ea868c896665531439ed`)
permitiu examinar a proposta: a revisão adicionou retornos antecipados
para condições válidas, tornando a função semanticamente incorreta.

A geração da segunda proposta passa a permitir exclusivamente objetos
`{anchor,new}`, vinculados a linhas válidas, em até seis substituições.
O modelo recebe instruções explícitas para inserir verificação de dados
inválidos antes de um `if` original sem remover o cabeçalho do bloco.
O formato tipado `{anchor,guard,return_expression}` continua aceito pelo
montador sob os controles anteriores para manter o contrato de execução
existente, mas não é anunciado nem permitido pelo esquema enviado ao modelo.
As regras AST, o SHA do candidato, o orçamento de execução, os 42 casos,
a verificação independente e o replay são mantidos. Nenhuma resposta
sintética substitui a inferência.

A efetividade desta redução de ambiguidade depende do E2E no novo HEAD;
não declarar mergeável com base apenas em testes unitários.

## Retirada do esquema dinâmico da geração — 11/10/2026

No E2E [38188624910](https://github.com/ericson-j-santos/engineering-worker-pool/actions/runs/38188624910),
com o SHA `666790c596b591d81234c03f13eedc838e69e286`, o
artefato `11720840600` (ZIP SHA-256
`019c5b03066edea246aae3b68f7bf4939646a79bb3899eead6bfee4fa6626d7c`)
mostrou a geração da revisão iniciada, mas não concluída antes do limite.
Mesmo o esquema restrito a `anchor/new` continuou lento no modelo 7B em CPU.

A correção mantém o prompt de substituições `{anchor,new}` e as linhas
imutáveis, mas usa `format: "json"` para a segunda geração, como na
execução `38187336402`, em que a inferência foi completada. A restrição
a apenas `anchor/new` é agora uma instrução ao modelo; o contrato
independente de montagem continua aceitando os dois formatos previamente
validados, sempre com limites, âncoras vinculadas ao fingerprint,
sintaxe/AST e testes. Nenhuma alteração ou fallback é aplicada à resposta
reprovada. Não se promete melhora sem novo E2E no SHA atual.

Este ajuste evita repetir a hipótese comprovadamente insuficiente de
esquema dinâmico, sem mexer em timeout, quantidade de propostas, modelo,
casos de aceite, isolamento ou política de merge.
