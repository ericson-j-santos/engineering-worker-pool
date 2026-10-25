# GitHub Actions token broker para CAS da issue #8 (DEV)

**Estado:** contrato em revisão; não publicar nem declarar o agente autônomo completo sem o E2E da issue até a PR gerada pelo modelo.

## Causa-raiz e menor correção

O run físico [40163933127](https://github.com/ericson-j-santos/desktop-pc24x7-runtime/actions/runs/40163933127) comprovou Dell, Session Launcher, Gateway e ambiente Python. Parou em `github_private_token_reference_missing` antes de gravar no GitHub. O `GITHUB_TOKEN` do runtime é limitado ao próprio repositório; **não** pode ser tratado como token de escrita no Worker Pool.

A solução nativa e sem PAT manual é separar os domínios:

- **Dell:** Qwen7B, Builder, Validator, patch Git e evidência sem token GitHub de escrita no processo do modelo.
- **Worker Pool / GitHub-hosted:** etapa de escrita restrita a um único objeto público não sensível, usando `GITHUB_TOKEN` efêmero somente no job `publish` e apenas para o repositório Worker's próprio; não há checkout nem execução de código da PR nesse job com permissões de escrita.
- **Readback independente:** job separado com `contents: read`, verifica bytes, SHA-256, SHA Git blob, ancestralidade e somente um arquivo novo na branch CAS.

O fluxo deste incremento é apenas **prova do backend CAS**; a transferência de artefato real do modelo entre Dell e host de publicação, a fila e os dois leases não estão ligados por ele. Não se deve apresentar o probe fixo como patch de produto nem como E2E do agente.

## Identidade e escopo fixos

- Worker Pool `main@958990e201c18219a8284f37437ed308c95d1a71`.
- Fonte `app/portfolio_bridge.py` blob Git `d9a25c574163c49f695e774609949160d94f4490`, ainda com a falha real.
- Issue #8 aberta, [autorização owner #6398567143](https://github.com/ericson-j-santos/engineering-worker-pool/issues/8#issuecomment-6398567143) vinculada ao SHA-base.
- Única branch de CAS: `agent/cas-qwen7b-issue-8-958990e201c1`, baseada no SHA imutável acima.
- Único destino permitido: `evidence/cas/qwen7b-cas-v1/preflight-v1/sha256/<prefixo>/<digest>.probe`, usando `CWP-S3-PREFLIGHT-V1\n`.
- `main` protegida permanece intocada; sem `force-push`, remoção, merge ou deploy.

## Admissão física de tokens

O workflow `.github/workflows/issue-qwen7b-cas-token-broker-dev.yml` tem um job de contrato com credenciais persistidas desabilitadas. Os jobs de escrita estão inicialmente **desarmados**: exigem evento `pull_request.edited`, origem no próprio repositório, ator proprietário, PR draft, transição de marcador **ausente → presente**, HEAD/base atuais, origem e comentário do owner revalidados e os checks `test` / `native-contract` executados com sucesso no SHA da PR. O marcador não está no corpo inicial da PR.

O job `publish` tem `contents: write`; não faz checkout da PR e só escreve o blob imutável esperado. O job `independent-readback` tem `contents: read`, roda em executor hospedado distinto e compara o conteúdo retornado pela API oficial. Em falha de autorização/concorrência/digest, parar sem retry cego; uma repetição autorizada só pode reconciliar o mesmo objeto, jamais criar outro commit ou caminho.

## Matriz de aceitação

| Gate | Prova esperada |
|---|---|
| Sintaxe e validação do JS | Compilação executada pelo `node --test`, Linux e Windows |
| Controle positivo | Mock de GitHub cria branch + um objeto CAS; readback confirma |
| Negativo | Revogação do owner, mudança de SHA/main/issue, branch divergente e adulteração de bytes bloqueiam |
| Idempotência | Mesma identidade duas vezes não grava novo arquivo nem commit |
| Externo real | Job autorizado no Worker Pool cria objeto CAS via API oficial |
| Independência | Outro job, sem escrita, confirma objeto/branch por readback |
| E2E do agente | **Não aplicável a este gate de armazenamento**; permanece P0 separado: issue→lease→Qwen7B→patch→Validator→PR draft |

**Custo:** repo público e runners padrão hospedados pelo GitHub; nenhum artefato Actions armazenado, nenhum serviço externo/credencial nova. Não usar larger runners ou habilitar cobrança. **Status funcional geral:** PARCIAL até PR de produto gerada autonomamente e lida de forma independente.
