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
