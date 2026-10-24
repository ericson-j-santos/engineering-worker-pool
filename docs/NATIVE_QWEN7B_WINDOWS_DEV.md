# Piloto nativo de Qwen2.5-Coder 7B — Desktop Windows DEV

## Finalidade e escopo

Integrar o modelo GGUF **já instalado** no Dell PC24x7 ao reparador estruturado do
Engineering Worker Pool, **sem Docker no Dell**. O piloto não libera um agente
geral, tarefas reais do TODO Global, publicação de PR, merge ou deploy.

A falha testada pertence a um arquivo **real** do Worker Pool no SHA fixado:
`app/portfolio_bridge.py::_property`. Os 42 casos de aceitação de
`scripts/structured_acceptance.py` são usados sem modificar o contrato.
A função atual deve reprovar a linha de base. O modelo sugere uma nova função;
o executor restringe a gramática, compara o hash de origem, executa os mesmos
testes, prepara um commit **somente em clone descartável sem remoto** e registra
um patch. Um checkout e processo separados validam a revisão produzida,
reexecutam os 42 casos e as regressões existentes. Replay não pode chamar o
modelo novamente.

## Fronteiras de execução

- **Ollama:** somente HTTP loopback `127.0.0.1:11434`, com
  `trust_env=False` e sem redirects ou qualquer fallback de provedor.
- **Modelo:** `qwen2.5-coder:7b`, pesos GGUF, digest SHA-256 completo
  confirmado no inventário e no `/api/ps`; um prefixo de **pelo menos
  12 hexadecimais** deve coincidir com `NATIVE_MODEL_DIGEST_PREFIX`.
  O piloto do Desktop de 24/10 identificou o prefixo `dae161e27b0e`.
- **Opt-in explícito:** somente `NATIVE_OLLAMA_DEV=1`, em sessão DEV
  governada, e `NATIVE_MODEL_DIGEST_PREFIX` válido.
- **Importante:** a flag no processo cliente **não comprova** que a configuração
  global do serviço Ollama impede conexões externas. O código só permite
  chamadas locais; uma qualificação independente do serviço é requisito
  separado para declarar isolamento global.
- **Hospedagem:** Windows do Desktop somente por runner dedicado, Session
  Launcher e Command Gateway, worktree reservado, risco 2, SHA esperado,
  `correlation_id`, sem terminal irrestrito, sem segredos, sem Docker.
- **Runtime:** Python >=3.12, Git, dependências declaradas e Ollama local
  existentes; caso faltem, bloquear sem instalar automaticamente.
- **CI:** `native-qwen7b-windows-contract.yml` usa `windows-latest` hospedado
  para validação estática, sintaxe, contratos positivos/negativos e isolamento
  do validador Windows. Não faz inferência real; não usa Docker.
- **A execução física deve ser coordenada por um workflow dedicado no**
  `desktop-pc24x7-runtime`, com checkout deste repositório em SHA exato,
  bootstrap, Gateway, watchdog e evidência sanitizada. Não usar o workflow de
  smoke/runner legado como substituto sem comprovação de equivalência.

## Aceite auditável

1. CI Windows executado no HEAD exato, incluindo controles negativos; nenhuma
   etapa crítica `skipped`.
2. Prova física de pickup, bootstrap `SESSION_LAUNCH_OK`, identidade do Dell
   e Command Gateway ativo.
3. Linha de base da função real reprova, sem alterar a origem.
4. Uma ou duas propostas **reais** do modelo produzem um patch limitado à
   função e à origem esperada; sem solução predefinida no executor.
5. Todos os 42 casos aprovam, regressões existentes aprovam; validador
   independente confirma o commit produzido em cópia separada.
6. Replay comprova `model_calls=0`; ausência de push, merge, deploy e
   alterações ao código original; finalização do job com evidência e readback.
7. A integração com fila TODO Global **não está coberta** por este primeiro
   piloto e continua pendente.

## Reversibilidade

Toda correção de código ocorre em um clone temporário `worker/*` sem remote,
sob um worktree da sessão governada. Falha deixa o produto intacto.
Não desabilitar modelos, serviços, privilégios ou proteção de branches.
Nenhuma compra ou assinatura autorizada.

**Estado inicial:** implementação proposta; E2E físico pendente. Não declarar
funcional antes dos critérios acima.
