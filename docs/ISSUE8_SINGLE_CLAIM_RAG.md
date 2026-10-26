# Issue #8 — citação RAG de um único critério de aceite

## Incidente reproduzido no Dell

O run físico `40378386092` no SHA `427e8ea914e8fd1cf8de464e2e8c18131a46a5a5` passou por OWNER/HEAD, Launcher, Gateway e Qwen local, mas foi bloqueado em pesquisa por `issue8_rag_insufficient_evidence`. O modelo recusou a seleção. O bloqueio foi correto e não deve ser convertido artificialmente em sucesso.

A pergunta anterior exigia uma citação que comprovasse **42/42 casos do Builder e 52 regressões do Validator simultaneamente**. Na fonte `docs/ISSUE_QWEN7B_DRAFT_HANDOFF.md` do SHA da main, esses fatos aparecem separadamente nas linhas 17 e 18. O contrato atual aceita apenas **uma linha citada**. Perguntar pelos dois fatos e exigir uma única linha torna a tarefa de seleção ambígua mesmo quando o documento está íntegro.

## Correção e prevenção

- Reformular apenas a pergunta de pesquisa para espelhar literalmente o sujeito da fonte: quantos casos após o reparo o código atual exige para o perfil. A resposta está documentada na linha 17, cujo contexto contém `Builder` e `42/42`; não atribuir ao Builder um verbo de aprovação que a linha não declara.
- O gate independente de 52/52 regressões **permanece obrigatório** no Validator e não depende do texto do RAG. Nenhum critério de aceite foi retirado nem alterado.
- A pesquisa continua limitada à fonte SHA-bound `docs/ISSUE_QWEN7B_DRAFT_HANDOFF.md`, sem fallback para outros documentos, comandos do modelo, CAS ou publicação de PR.
- Testes positivos e negativos verificam a citação exata e preservam a recusa explícita do Qwen: `insufficient` continua falha fechada.
- Uma nova etapa no CI oficial, após o carregamento do Qwen7B verdadeiro, consulta a issue #8, a autorização OWNER e a main atuais por API GET oficial, executa Qwen, exige a linha com `Builder` e `42/42` e relê fonte/recibo em cliente independente. Uma PR/merge que altere o SHA da main torna essa etapa inelegível e exige nova autorização.
- O E2E com Ollama rodando em runner hospedado ainda **não é o E2E físico** do Dell/Noteri, nem prova CAS Git bundle, Validator físico ou PR criada pelo agente.

## Critérios pendentes

Após CI real aprovado no SHA final, revalidar SHA/OWNER e integrar a correção à main governada. Somente depois disso a PR física #107 do Dell poderá fixar a nova main e autorização, executar uma sonda única e exigir readback independente. O fluxo completo exige Qwen produzir reparo real, CAS durável de patch, 42/42 + 52/52 em Validator independente, exatamente uma PR draft do agente e replay idempotente.

Custo adicional máximo R$0,00. Nenhum Docker em host físico, merge/deploy implícito, publicação CAS ou alteração de produção.

## Diagnóstico adicional da seleção — 26/10/2026

A primeira execução real da PR #57 ([CI #40385255809](https://github.com/ericson-j-santos/engineering-worker-pool/actions/runs/40385255809)) terminou em `failure` na etapa `Real issue8 Qwen7B acceptance citation`, com `issue8_real_qwen_evidence_unproved`. Portanto, a redução da pergunta para um único fato **não comprovou** a correção. O sistema não pode aceitar uma evidência apenas porque a documentação contém palavras-chave.

Foi acrescentado um `diagnostic_sink` opcional, vazio por padrão, no produtor da pesquisa. Ele registra somente os caminhos da allowlist, a lista limitada de números das linhas **efetivamente encaminhadas ao seletor** e sua decisão enumerada (`no_candidates`, `insufficient` ou `supported`). Não armazena texto, prompt, modelo em texto livre, URL, comando ou saída bruta; não altera o recibo assinado do RAG, a política de fonte, a seleção do Qwen nem a regra de insuficiência.

O teste real no CI confere esses metadados com o documento recuperado novamente pela API oficial em um cliente separado, incluindo SHA de Git blob e digest SHA-256. A observação `source_contains_acceptable_line` é calculada independentemente e informa se algum dos números apontava para a linha que contém **Builder** e **42/42**. Mesmo nessa hipótese, a recusa explícita da IA permanece `failure`, e o CI não publica CAS, reparo, branch ou PR.

A identificação das linhas diferencia ausência de trecho de recusa apesar de trecho relevante. Se a linha 17 tiver sido apresentada e o Qwen recusá-la, a hipótese de que a falta dessa linha é a causa perde sustentação; o próximo diagnóstico deverá investigar instruções de seleção/formato e robustez da inferência, sem forçar `supported`. Qualquer mudança no contrato de citações múltiplas só deve ocorrer após prova de que uma única linha não pode fundamentar a consulta real. Todos os gates do Validator (42/42 e 52/52), custo zero, não uso de Docker físico e proibição de merge/deploy continuam vigentes.


## Continuidade após integração da PR #57

A PR #57 foi integrada à `main@be69b27455f99606a03ce5cba6ca809e1082f2b4` e o [CI pós-merge #40399596220](https://github.com/ericson-j-santos/engineering-worker-pool/actions/runs/40399596220) concluiu com sucesso. A árvore Git da main integrada corresponde à árvore do HEAD revisado `ee4aaed2c303eb3dce7bf3e5c3e7f66b6b57bd35`. A autorização OWNER da issue #8 para a nova main existe no [comentário #6427072933](https://github.com/ericson-j-santos/engineering-worker-pool/issues/8#issuecomment-6427072933). Nenhum comentário de autorização foi reproduzido.

Este incremento de diagnóstico separado não reabre nem substitui a PR #57. O teste CI usa somente a `main` integrada, a autorização atual e uma sonda GET-only do Qwen. Ele registra os **números** das linhas apresentadas ao seletor, relaciona esses números à documentação SHA-bound por leitura independente e mantém `insufficient_evidence` como falha. Não publica texto recuperado, segredos, CAS, PR gerada pelo modelo ou alterações em produção.

A decisão de adicionar citações múltiplas depende da evidência resultante: se o critério `Builder/42/42` estiver em uma única linha apresentada e o Qwen a citar corretamente, não há demonstração de necessidade de duas citações para essa consulta. Os 52/52 testes do Validator continuam obrigação separada do gate funcional e não podem ser omitidos para facilitar o RAG.
