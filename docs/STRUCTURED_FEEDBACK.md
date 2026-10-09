# Feedback estruturado e detecção de candidato repetido

Complementa `STRUCTURED_CODE_WORKER.md` na PR #19. Não amplia a gramática de execução
nem altera a tarefa pública, seus 42 casos de qualificação ou o limite de duas propostas.

## Diagnóstico e revisão

O processo isolado devolve contagens e falhas com índice original do caso e observação:
valor JSON tipado ou categoria fechada de exceção. Nunca devolve mensagens de exceção,
traceback, repr de objetos ou ambiente. Valores longos são identificados por tamanho e
SHA-256, com truncamento explícito. Contagens, tipos, índices e categorias são conferidos
pelo processo chamador; relatório incoerente falha fechado.

O envelope interno de proposta acrescenta `feedback`, separado da instrução e dos casos
imutáveis. Contém o fingerprint AST do candidato reprovado, quantidade total de falhas
e até oito casos com entrada, esperado e observado, limitado a 12 KB. Essa seleção limita
o contexto enviado ao modelo, NÃO o conjunto de testes executado. Todos os casos originais
continuam obrigatórios. O contrato público da tarefa não aceita esse campo adicional.

A revisão envia ao Ollama uma conversa explícita: instrução original, resposta anterior
como mensagem assistant e falhas como nova mensagem user. Solicita alterações localizadas
na função anterior e preservação do comportamento aprovado. O fingerprint vincula o
feedback ao código; casos/expectativas divergentes bloqueiam antes de qualquer inferência.
Não há função correta pronta, remendo automático da resposta ou fallback pago.

## Repetição e segurança

Após a segunda resposta, a AST normalizada é comparada às propostas já avaliadas. Repetição
idêntica ou disfarçada por espaços, comentários ou estilo de aspas resulta em
`candidate_repeated` ANTES de repetir a validação e sem escrever código/commit/patch.
Uma segunda chamada ao modelo é necessária para descobrir que ele repetiu a resposta;
não existe terceira chamada automática. Isso não é deduplicação persistente entre
execuções: o chamador deve continuar impedindo reruns sem mudança de pré-condição.

Uma resposta diferente, mas ainda reprovada, resulta em `candidate_tests_failed`.
Sintaxe, segurança, infraestrutura e mudança concorrente continuam terminais. O primeiro
relatório já contém as observações; não são criados subprocessos adicionais por caso.

## Validação

O corpus `tests/fixtures/structured_repeated_proposal.json` preserva uma resposta
REPROVADA real do run 37990483368, não uma solução para o executor. Os testes reproduzem
37/42 e os índices 7, 20, 21, 36 e 41; conferem mensagens, categorias, valores, deduplicação,
imutabilidade da tarefa, patch isolado e replay. O controle positivo unitário utiliza uma
proposta sintética explicitamente identificada. Somente o E2E com modelo local real e
artefato de aprovação pode homologar a geração. A suíte unitária não substitui esse aceite.

O E2E e o workflow existentes são reutilizados. Esta mudança não aplica proteção da main,
não consome TODO Global live e não implanta serviço ou altera Noteri/Desktop.
