# AGENTS.md

Este repositório contém o Worker Pool compartilhado do Engineering Control Plane.

Antes de trabalho técnico:
1. consultar a branch main de `ericson-j-santos/chatgpt-operational-rules`;
2. aplicar as regras de Engineering Control Plane, E2E, GitHub/CI e progress watchdog;
3. preservar compatibilidade do contrato público durante a migração do ReqSys;
4. não incorporar regras de negócio do ReqSys nem runtime físico específico de host;
5. não versionar segredos, tokens ou credenciais;
6. exigir testes positivos, negativos, idempotência, lease, concorrência e watchdog para mudanças funcionais;
7. exigir smoke/E2E aplicável no mesmo SHA antes de remover a implementação legada do ReqSys.
