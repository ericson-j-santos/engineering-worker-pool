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
