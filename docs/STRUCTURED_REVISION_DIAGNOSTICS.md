# Diagnóstico de revisão antes da rejeição

`StructuredOllama.revision_observer` é um callback opcional e confiável.
Por padrão não há escrita em arquivo. No E2E público, o callback grava
`revision-diagnostic.json` atomicamente e o wrapper o copia mesmo em falha.

O registro tem três fases: `received`, `guard` e `accepted` ou `rejected`.
Recepção é registrada antes da montagem. Antes do guard são registrados
hash, tamanho, número de nós e até 16 tipos/posições AST fora da allowlist.
Uma falha inclui o estágio e um código fechado; código-fonte, valores
literais, nomes de variáveis, traceback, prompt e resposta bruta não são
gravados. Fonte inválida ou excessiva é classificada sem copiar exceções.
Falha do gravador é terminal e sanitizada, nunca silenciosamente ignorada.

O registro é vinculado pelo E2E ao SHA, correlation_id e task_id. O hash da
resposta permite confrontá-lo com o transporte. O diagnóstico não executa
código, não aprova candidato nem substitui os guards, os 42 casos ou o
validador independente. Uma lista AST vazia não implica aprovação: limites
de tamanho/profundidade ou verificações específicas ainda podem bloquear.

Os testes usam propostas e transportes sintéticos identificados. O aceite
real exige artefato do mesmo HEAD produzido pelo modelo local e, para
qualificar o reparador, 42/42, patch reaplicável e regressão independente.
A instrumentação não modifica modelo, schema de geração, testes de aceite,
timeout, permissões, fila, computadores ou política da main.

## Correção de geração baseada no diagnóstico real

O run `38108465920`, HEAD `e1d915b9785294d75e8a291420258c975526e027`,
preservou a revisão bloqueada como metadados: `candidate_grammar`, um
`Subscript` na linha 9/coluna 88 da função montada, 282 nós (sem truncamento).
O diagnóstico permaneceu disponível depois da rejeição.

A restrição de geração de `edits[].new` agora impede colchetes, e a instrução
de revisão exige `get`/`list()`. O campo `old` não recebe essa restrição, para
continuar descrevendo exatamente a origem. São mantidos seis edits, os mesmos
limites, testes e guards de execução. Não se normaliza nem executa código
rejeitado. Essa mudança corrige a ausência de restrição na geração, mas só
um novo E2E real pode comprovar a convergência do reparador.
