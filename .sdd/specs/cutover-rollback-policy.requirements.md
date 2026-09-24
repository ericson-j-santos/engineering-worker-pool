# Cutover/Rollback Policy v1 — requisitos

## Objetivo

Fechar o critério de rollback da migração reqsys-v2-enterprise-real#2020 com uma
decisão fail-closed, reproduzível e independente de regras de negócio do ReqSys.

## Requisitos

1. A política deve ser versionada em `migration/cutover-policy-v1.json`.
2. A decisão deve ser somente `CUTOVER_READY` ou `CUTOVER_BLOCKED`.
3. Ausência, divergência ou identidade inválida de evidência deve bloquear.
4. `physical_runtime_validated=true` é obrigatório para liberar cutover.
5. CI dedicado, contrato v1, E2E consumidor, replay e leitura independente são
   obrigatórios.
6. `secrets_migrated_to_git` deve permanecer `false`.
7. O legado ReqSys deve permanecer presente durante a janela em que este rollback
   é usado.
8. A remoção do legado exige incremento separado.
9. O fallback de rollback permitido é somente o comportamento temporário já
   limitado a HTTP 404; 401, 503, erro de transporte ou incompatibilidade nunca
   podem virar fallback.
10. O avaliador não pode executar mutações, deploy, reboot, RBAC, escrita de
    segredo, exclusão de estado ou operação de produção.
11. O runbook deve registrar sequência de cutover, gatilhos de rollback e a
    invalidação desta estratégia depois da remoção do legado.
12. Testes devem cobrir pronto, bloqueio físico, segredo em Git e identidade
    ausente/inválida.

## Critérios de aceite

- JSON da política válido;
- suíte existente permanece verde;
- testes do avaliador positivos e negativos verdes;
- estado atual com `physical_runtime_validated=false` resulta em
  `CUTOVER_BLOCKED`;
- estado completo resulta em `CUTOVER_READY`;
- nenhuma mudança de API/runtime físico/segredo;
- CI do mesmo HEAD verde.
