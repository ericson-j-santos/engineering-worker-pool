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

Há uma proposta inicial e no máximo uma correção, somente após falha semântica
real nos testes. A correção recebe a função reprovada e os mesmos casos, com os
que falharam primeiro. Sintaxe, segurança, ausência de mudança ou infraestrutura
bloqueiam sem retry. Não há código de correção pronto como fallback. Replay confere código/commit/patch e testes, sem nova inferência.
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

A resposta no HEAD 0722e54 foi bloqueada por AugAssign; o gerador passa a
excluir também adição, solicitando append/join. A gramática de execução
continua inalterada. Acrescentou-se no máximo uma correção após validação
semântica real, com registro de cada proposta e revalidação de concorrência.
Essa ampliação substitui a limitação histórica de uma única proposta; não
autoriza repetir falhas de segurança, sintaxe ou infraestrutura. O cliente
limita cada inferência a 180 segundos; o E2E continua limitado a cinco minutos.


## Revisão por função completa vinculada à origem — 11/10/2026

O E2E real 38189156767 no SHA ce75ae3ce15dc19dfe932d3043b835c17a8be563
rejeitou uma revisão montada com erro de sintaxe (candidate_syntax,
linha 7), sem gravar patch. O artefato 11720796343 (SHA-256
153d40cfb2b3bc2686410dcee462d9d84cde3f9bdf92d110c0a8b3183331f750)
continha somente a primeira proposta registrada, o feedback e metadados
sanitizados da revisão. Uma primeira proposta produzida pelo mesmo modelo
já havia validado 37 dos 42 casos. Erros de montagem por linhas e guardas
introduziram regressões sem aproximar o aceite.

Para evitar inconsistência de cabeçalho/indentação ao unir trechos, a
segunda e última proposta do modelo passa a retornar uma função inteira
em JSON com a chave function, vinculada ao fingerprint do candidato
inicial e aos resultados reais dos testes falhos. A função inteira
é revalidada antes de retornar ao executor: objeto JSON exato, tamanho,
assinatura, AST, identidade do candidato e bloqueio de repetição. Um
diagnóstico sanitizado preserva apenas hashes, categorias/posições e códigos
de rejeição, inclusive em erro sintático. O executor volta a validar a
função completa e os mesmos 42 casos, exige um único arquivo modificado,
commit local, patch reaplicável, replay sem nova inferência e regressões
independentes no E2E.

Não há correção pré-programada, execução de resposta insegura, fallback,
terceira chamada, novo modelo, aumento de tempo ou alteração de runtime.
Os contratos históricos de edição por âncora permanecem disponíveis para
chamadores explícitos e seus testes, mas deixam de ser a forma usada
pela segunda geração do modelo. A saída final permanece bloqueada até
que o novo SHA comprove todos os requisitos no E2E real; a main e sua
proteção administrativa não são modificadas.


## Orçamento de inferência alinhado ao limite do Gateway — 12/10/2026

No E2E [38197666618](https://github.com/ericson-j-santos/engineering-worker-pool/actions/runs/38197666618),
HEAD aacc98ae188422fe21519ffdd1674f648e986883, a compilação, os testes e
o E2E numérico passaram. O artefato 11724920154 (SHA-256
69facd3ee65a84e1bfa1d5276f6f45cdf715757fa82e46f01234df251109a7c4)
comprovou SESSION_LAUNCH_OK e uma proposta inicial completa; a segunda
geração foi iniciada, mas o Gateway encerrou o processo após 285 segundos
globais, sem patch nem aprovação. Duas inferências sequenciais de até
180 segundos cada podem exigir até 360 segundos, além da validação;
o teto global anterior era, portanto, menor do que o orçamento já
permitido aos dois pedidos.

A correção não altera modelo, prompts, segurança nem o número de propostas:
- prazo do Gateway: 420 segundos (duas inferências de até 180 s,
  mais 60 s de validação/IO);
- wrapper: 435 segundos, para observar uma falha fechada do Gateway;
- etapa E2E no CI: 8 minutos;
- job completo no CI: 14 minutos, incluindo preparação do runner,
  download dos pesos e E2E numérico.

O Gateway governado continua com teto configurado de 900 s e a inferência
individual permanece limitada a 180 s, inferior ao watchdog de 300 s sem
avanço material. A primeira proposta registrada é progresso material,
não heartbeat; a segunda chamada também permanece limitada e pode ser
rejeitada. Não há polling, retry adicional ou tratamento de sucesso falso.
O runner padrao ubuntu-latest neste repositorio publico nao requer runner
pago; armazenamento de artefatos continua sujeito aos limites do GitHub.

O aceite continua exigindo função real 42/42, patch reaplicável, replay,
regressão independente e evidência no SHA atual. A mudança é de orçamento,
não prova funcional, e não autoriza alterar a main ou fazer merge.
