# Política de destino das demandas — DEV v1

Referência: [Issue #60](https://github.com/ericson-j-santos/engineering-worker-pool/issues/60).
Contrato opcional aditivo ao `POST /v1/tasks` (também consumido por `POST /v1/work`).

| execution_mode | target_host | Comportamento |
| --- | --- | --- |
| `eligible_pool` (default) | ausente/null | Qualquer worker elegível do pool, respeitando role, perfil, regras, Gateway, heartbeat e afinidade |
| `pinned_host` | identidade do host registrada | Builder e Validator devem reivindicar no mesmo host exato, mas em workers distintos |

O `target_host` é uma **identidade de host**, não hostname/IP arbitrário para estabelecer conexão.
A API não acessa PCs diretamente e não transforma o valor recebido em comando, caminho, URL, SSH ou runner label.
Só registros de worker confiáveis, preflight físico real e Command Gateway podem comprovar prontidão.
`pinned_host` sem host, host malformado, `eligible_pool` com host ou modo desconhecido são rejeitados.
A política é imutável para a identidade lógica `repository|issue_number|request_id`: replay divergente retorna 409, sem alterar a task.

## Compatibilidade e persistência

- Migração aditiva de SQLite: tasks preexistentes recebem `execution_mode=eligible_pool` e `target_host=NULL`.
- Task fixada permanece na fila quando nenhum worker do host escolhido é elegível; não existe fallback silencioso para outros hosts.
- Validator diferente do Builder é obrigatório; no modo fixo, ambos precisam estar no mesmo host, mas devem possuir sessões/worktrees independentes.
- Watchdog considera alternativa **somente** no host fixo; se não houver, bloqueia e libera lease no limite de 300 s, sem falsear conclusão.
- O snapshot e o GET autenticado incluem o destino (sem token/segredo). Locks, lanes, fairness e fences existentes permanecem.
- `eligible_pool` não é sinônimo de Codex Cloud. O Codex Cloud é rota externa com admissão/cota/custo próprios, ainda não conectada por este incremento.
- A criação/registro de workers por API não prova identidade física nem elegibilidade real. Sem `SESSION_LAUNCH_OK`, `state_validated=true`, origem SHA verificada, perfil NORMAL e Gateway válido, bloquear efeitos no host.

## Aceite e evidência

Testar modo fixo/compartilhado, Builder/Validator, claim exato, perfil ESTUDO, regras divergentes, replay, concorrência, watchdog com/sem alternativa, migração SQLite, API e leitura independente.
CI e simulação em SQLite não comprovam o E2E nos PCs. Exigir teste físico SHA-bound em Desktop/Noteri somente após sua qualificação e autorização; sem esse teste o estado é PARCIAL.
Custo incremental máximo R$0,00; não iniciar cobrança, deploy, merge, instalações, alterações de serviço ou privilégios.
