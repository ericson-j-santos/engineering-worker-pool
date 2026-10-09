# Worker de funções estruturadas — escopo delimitado

Complementa o reparador numérico existente com `app/structured_code_worker.py`.
Recebe uma tarefa confiável com caminho, função, parâmetros, SHA, hash anterior,
instrução e exemplos JSON de entrada/saída. O modelo só fornece o corpo de uma
função compatível. Assinatura anotada e bytes fora da função são preservados.

## Capacidade acrescentada

Objetos, listas, strings, valores JSON simples, condicionais, atribuições locais
e no máximo um loop/gerador. Apenas builtins explícitos e métodos get, strip,
join e append. Não aceita importação, I/O, introspecção, chamada arbitrária,
recursão, while, decorators ou alterações em outros arquivos. Não é sandbox
para Python arbitrário nem agente de programação geral.

A função é verificada por AST e testada num processo Python isolado (`-I -S`),
sem variáveis de credenciais, com CPU/memória/tempo limitados. O processo repete
as verificações antes da execução e não importa o módulo de origem. Neste
incremento a execução exige Linux com resource limits; não instala serviço.

O Git de trabalho deve ser cópia independente, sem remoto, configuração
executável ou hooks ativos, limpa e em branch worker/*. Lock cooperativo e
estado único por repositório são responsabilidade conjunta do chamador. Mudança
concorrente bloqueia a escrita. O resultado é commit LOCAL, patch e recibo;
nenhuma chamada publica o patch ou faz merge.

Há uma proposta do modelo por tentativa de execução, sem retry invisível e
sem código de correção pronto como fallback. Erro de sintaxe, segurança ou
aceite bloqueia. Replay confere código/commit/patch e testes, sem nova inferência.
Falhas durante Git deixam a cópia incompleta bloqueada, nunca forçam reset.

## Problema real usado no E2E

A função `_property` do arquivo existente `app/portfolio_bridge.py`, usado na
admissão do TODO Global, falha em JSON malformado e converte tipos indevidos em
texto. O teste clona o checkout real no SHA atual sem alterar o arquivo para
introduzir defeito; 42 casos confiáveis definem o contrato de normalização.

O CI existente reutiliza o mesmo Ollama descartável, sem nova fila ou workflow.
O novo E2E abre sua própria sessão/worktree com Session Launcher e Command
Gateway e exige SHA exato. O modelo local qwen2.5-coder:7b propõe a correção. O modelo 1.5b da prova
numérica é descarregado/removido antes, evitando inferências simultâneas.
Depois de validar os 42 casos, o executor cria patch/commit na cópia. Outro
checkout/processo confere os casos e executa os testes existentes do bridge.
O checkout original permanece intacto. Artefatos são conferidos novamente
fora do executor governado, inclusive hashes e identidade da fonte.

**Não confundir:** a fonte do código e o defeito são reais; não houve consumo
live de TODO, aplicação do patch à main, teste de Notion externo, despacho de
produto ou implantação nos hosts. O E2E anterior da fila numérica é preservado;
a integração da nova capacidade com consumo live da fila é um aceite separado.

## Evidências e aceite

Exigir evidence.json, task.json, before.py, after.py, change.patch e bootstrap.json
no mesmo SHA/run/correlação. proposal.json contém somente proposta derivada do
código público de teste e não credenciais. Comprovar falha anterior, aprovação
posterior, ausência de modificação fora da função, validação independente e
replay. Uma PR verde sem artefato íntegro não conclui o aceite.

A proteção administrativa da main permanece um gate separado. Não alterar
rulesets, habilitar auto-merge ou marcar TODO concluído por este incremento.

## Ajuste após execução real

O primeiro E2E estruturado no HEAD 0d9afcc foi bloqueado por candidate_syntax:
o modelo 1.5b copiou a função defeituosa sem a palavra def. A resposta foi
preservada no artefato e não normalizada silenciosamente para passar. O
provedor estruturado passou a 7b (Apache-2.0, catálogo oficial Ollama) e o
prompt explicita a assinatura exigida. Os 42 casos e os guards permanecem
inalterados. Cada execução continua tendo somente uma proposta.
Fonte: https://ollama.com/library/qwen2.5-coder:7b

A proposta seguinte (HEAD 8c681bc) usou Break/Subscript, ainda proibidos,
e repetiu parte do defeito. Foi rejeitada. O pedido ao modelo agora é
construído a partir do contrato comportamental e de todos os exemplos,
sem retransmitir o corpo defeituoso como texto a ser copiado. O executor
continua vinculando a alteração ao hash exato do arquivo original. Nenhum
corpo de solução é fornecido ao provedor; os mesmos 42 casos são exigidos.

O HEAD e919c2a também produziu Subscript. A gramática do executor não foi
ampliada para aceitá-lo. A saída de geração passa a exigir o prefixo def e
a excluir colchetes via pattern no JSON Schema; o prompt solicita get e
list(). A validação AST permanece independente e obrigatória, mesmo quando
o provedor aceita o schema. Os requisitos de campo inválido e concatenação
sem separador foram explicitados; não existe substituição da proposta.
