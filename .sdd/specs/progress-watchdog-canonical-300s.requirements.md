# Progress Watchdog canônico — 300 segundos

## Problema

O Worker Pool dedicado possuía comportamento de watchdog material, porém o valor padrão de `progress_stall_seconds` era 900 segundos. A regra canônica do Engineering Control Plane define 300 segundos como limite padrão para execução sem progresso material.

## Requisito

1. `WorkerPoolStore` deve usar 300 segundos como padrão quando não houver override explícito.
2. A aplicação deve usar 300 segundos como default de `CODEX_WORKER_POOL_PROGRESS_STALL_SECONDS`.
3. Overrides positivos continuam permitidos por configuração quando existir justificativa objetiva de projeto/ambiente.
4. Heartbeat, lease renewal e polling sem mudança não contam como progresso material.
5. Aos 299 segundos sem progresso, a tarefa continua ativa.
6. Aos 300 segundos, sem worker alternativo elegível, a tarefa deve sair do estado ativo e ficar explicitamente bloqueada.
7. A correção não altera produção, runtime físico, segredos, deploy ou contratos HTTP existentes.

## Critérios de aceite

- teste automatizado comprova o default de 300 segundos;
- teste automatizado comprova ausência de recuperação prematura aos 299 segundos;
- teste automatizado comprova bloqueio no limiar de 300 segundos quando não há rota alternativa;
- suíte completa do repositório permanece verde;
- CI do HEAD exato da PR fica verde.

## Risco e rollback

Risco principal: tarefas legítimas de longa duração sem checkpoints materiais podem ser bloqueadas mais cedo. Mitigação: workers devem registrar progresso material real; quando tecnicamente justificado, o timeout pode ser configurado explicitamente. Rollback: restaurar o valor anterior em configuração/código mediante evidência de necessidade, sem remover a lógica fail-closed do watchdog.
