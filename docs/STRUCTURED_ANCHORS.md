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
