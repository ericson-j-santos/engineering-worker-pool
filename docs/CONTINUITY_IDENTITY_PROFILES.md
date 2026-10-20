# Perfis OIDC da continuidade privada

O broker `continuityidentity` não aceita repositório, workflow, branch ou ator
fornecidos por uma tarefa. Ele reconhece apenas perfis compilados e mutuamente
exclusivos, além de exigir um SHA completo presente em `EWP_ALLOWED_SHAS`.

## Perfis

- `engineering-worker-pool-branch-v1`: compatibilidade da prova histórica da
  continuidade nativa na branch `feat/hosted-cold-checkpoint-resume-20261020`.
- `reqsys-handoff-v1`: produtor hospedado do ReqSys, exclusivamente
  `ericson-j-santos/reqsys-v2-enterprise-real`, evento
  `workflow_dispatch`, ref `refs/heads/main`, workflow
  `.github/workflows/codex-worker-pool-handoff.yml` e ator
  `github-actions[bot]`.

Ambos exigem GitHub-hosted runner, proprietário/IDs exatos, `workflow_sha==sha`,
proveniência reutilizável coerente quando presente, token recente e sujeito
`repo:<repository>:ref:<ref>` exato. Misturar campos de perfis diferentes
falha antes da leitura da configuração privada.

O perfil histórico continua exigindo que o SHA esteja em `EWP_ALLOWED_SHAS`.
O perfil ReqSys usa uma regra diferente porque a `main` avança: antes de ler
a configuração privada, o broker consulta exclusivamente a API pública oficial
do GitHub, exige que `refs/heads/main` seja exatamente o SHA assinado no OIDC
e que o endpoint do commit reporte `verification.verified=true` e
`reason=valid`. Redirect, erro HTTP, resposta grande/malformada, SHA divergente
ou commit não verificado falham fechados. Essa leitura não usa token GitHub nem
confia em branch protection local.

## Cliente

`scripts/native_private_config.py` usa
`EWP_IDENTITY_PROFILE` somente para conferir a identidade devolvida pelo
broker e para diagnóstico sanitizado. O padrão mantém o perfil histórico.
O handoff ReqSys deve definir explicitamente
`EWP_IDENTITY_PROFILE=reqsys-handoff-v1`.

A variável não concede acesso por si só: o broker autentica o JWT OIDC e aplica
a allowlist compilada. Para o perfil histórico, o SHA ainda precisa estar em
`EWP_ALLOWED_SHAS`. Para o ReqSys, o SHA precisa ser o `main` corrente e
verificado pela API GitHub no instante do bootstrap; a configuração de runtime
do Function não precisa ser regravada a cada avanço legítimo da `main`.

## Deploy

Alterar código no repositório não altera o Function ativo. O deployment do
broker é uma operação de segurança separada: construir o ZIP determinístico,
revalidar os testes, atualizar de forma explícita a lista de SHAs permitidos e
só então publicar a versão do Function. Não registrar `EWP_RUNTIME_CONFIG_JSON`
ou valores de credencial em Git, PR, logs ou chat.

A ativação do produtor ReqSys exige, além deste broker, que o workflow de
handoff use um runner hospedado, hidrate a configuração privada e execute uma
API Worker Pool local ligada à autoridade PostgreSQL. Até essa integração ser
validada, nenhuma task de produto deve ser admitida por esta mudança isolada.
