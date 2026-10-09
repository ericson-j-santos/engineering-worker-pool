# Worker de código local — primeiro incremento executável

## Escopo real

`app/local_code_worker.py` implementa um reparador limitado de **uma função
numérica pura** em um repositório Git descartável, sem remoto, em branch
`worker/*`. Recebe tarefa confiável, exige testes que reproduzam a falha,
solicita uma proposta ao Ollama local, gera código Python, valida, cria um
commit local e devolve patch e recibo verificável. Não contém uma solução
pré-escrita como fallback do modelo.

Não é agente programador geral. Nesta versão não edita repositórios de produto,
não publica PRs automaticamente, não consome o TODO Global real e não instala
serviços em Noteri/Desktop. O próximo escopo de programação só deve ser ampliado
com contratos e testes próprios, nunca liberando comandos arbitrários do modelo.

## Contrato e segurança

A tarefa confiável fixa `task_id`, caminho `src/<nome>.py`, nome e parâmetros
da função, instrução, SHA base, hash do conteúdo anterior e casos de teste.
O modelo só fornece JSON com uma expressão; não escolhe caminho, comandos,
testes, dependências ou destinos de rede.

A gramática aceita parâmetros numéricos limitados, comparações, operações
`+`, `-`, `*` e condicionais. Bloqueia chamadas, imports, atributos, indexação,
loops, coleções, strings, atribuições, potência e código fora desse escopo.
A árvore é limitada em tamanho/profundidade antes da compilação com builtins
vazios. Esse é um contrato restrito, não um sandbox para Python arbitrário.

O worker exige Git limpo, branch de trabalho, ausência de remoto e hooks ativos,
arquivo regular sem link simbólico/hardlink, hashes exatos e estado separado
do repositório. Um lock exclusivo impede duas execuções cooperativas usando o
mesmo diretório de estado. A repetição do mesmo recibo verifica HEAD, arquivo
e testes de novo, sem nova chamada ao modelo. Interrupções deixam trabalho
incompleto bloqueado; não há retry automático nem remoção automática de lock
abandonado. O chamador deve manter o mesmo diretório de estado e tratar recuperação.

O provedor `LocalOllama` desta qualificação exige o container descartável fixo,
lê `OLLAMA_NO_CLOUD=1`, inventário de um único modelo, digest e pesos GGUF.
Após uma única inferência, confere o processo local pelo `/api/ps`.
Não usa chave de serviço de IA e não oferece fallback cloud.
A autorização de runtime e a admissão da tarefa pertencem ao chamador confiável.

## E2E descartável, vinculado ao desenvolvimento

O CI existente executa primeiro toda a suíte. Em PR do próprio repositório,
executa depois `scripts/local_worker_e2e.py`:

1. Obtém as regras canônicas no SHA fixado e cria uma política somente para
   diretórios temporários deste runner. Mantém os controles; não modifica
   políticas ou hosts existentes.
2. Usa Session Launcher real e exige `SESSION_LAUNCH_OK`, SHA e estado válido.
   O fluxo mutável passa pelo Command Gateway em um worktree reservado.
3. Inicia API Worker Pool real por HTTP em loopback com SQLite e token
   efêmeros. Registra dois workers lógicos distintos, Builder e Validator,
   sob a sessão governada do teste. Isso não prova dois hosts físicos.
4. Enfileira uma tarefa de **fixture explicitamente sintética** e comprova
   replay. A função defeituosa é `clamp(value, lower, upper)`.
5. O Builder chama um modelo Ollama real e produz arquivo/patch/commit locais.
   Não há resposta de IA simulada no E2E real.
6. O Validator usa outro checkout no SHA produzido e outro processo para
   executar os testes confiáveis. Confere a resposta por leitura da API,
   rejeita evidência com SHA errado e comprova que existe só uma tarefa.
7. Publica somente evidência sanitizada, especificação da fixture,
   código anterior/posterior, patch e bootstrap resumido. Nunca publica
   token, lease ou banco. Não executa push, merge ou deploy.

Artefatos exigidos: `evidence.json`, `task.json`, `before.py`, `after.py`,
`change.patch` e `bootstrap.json`. O sucesso exige
`LOCAL_CODE_E2E_PASSED` no SHA atual, patch íntegro, testes que falhavam antes
e passam depois, modelo local comprovado e Validator independente.
Unidade com HTTP sintético não substitui esse E2E.

O runtime usa CPU em runner padrão descartável; não é um serviço contínuo de
inferência hospedada. Docker é usado exclusivamente nesse teste, não como
decisão de migração da infraestrutura física. A prova de integração com modelo
compacto não estabelece qualidade para programação complexa, custo ilimitado
ou prontidão de operação permanente.

## Referências técnicas

- Ollama Chat API: https://docs.ollama.com/api/chat
- Modelo local carregado: https://docs.ollama.com/api/ps
- Desabilitar nuvem: https://docs.ollama.com/faq
- Modelo de qualificação: https://ollama.com/library/qwen2.5-coder:0.5b
