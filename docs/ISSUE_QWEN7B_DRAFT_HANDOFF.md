# Issue → Qwen7B → PR draft: contrato de publicação (DEV)

Estado: **preparação revisável; não há ciclo físico issue→PR homologado neste incremento**.

## Foco e arquitetura

Reutilizar o **Worker Pool** para admissão, idempotência, leases, Builder, Validator, reconciliação e watchdog. O **Runtime Platform** fornece somente o transporte físico, o Session Launcher, o Command Gateway e o Ollama nativo. A camada \`app/issue_draft_pr.py\` faz a verificação da autorização de uma **issue real existente** e prepara uma PR em rascunho via uma porta de GitHub API confiável. Não interpreta texto da issue como shell nem habilita um agente arbitrário.

Este primeiro perfil, \`bridge.property.v1\`, aceita somente o reparo de \`app/portfolio_bridge.py::_property\` no repositório \`ericson-j-santos/engineering-worker-pool\`. Esse é exatamente o limite do reparador estruturado que aprovou o canário físico de 25/10/2026. Não trata qualquer issue, outro projeto ou tarefa ReqSys automaticamente como executável. Uma issue cuja falha já foi corrigida na main permanece bloqueada por \`no_failing_baseline\`, sem gastar inferência.

## Admissão e prova exigida

1. Uma integração **autenticada** consulta a API oficial do GitHub para obter issue aberta, não PR; comentário do proprietário; HEAD da branch main; e hora de observação no máximo cinco minutos anterior. Uma entrada enviada pelo usuário ou pelo modelo **não é um snapshot autenticado**.
2. Somente o comentário literal, na própria issue, é elegível: \`/local-agent authorize bridge.property.v1 sha:<SHA40_DA_MAIN>\`. O texto da issue não é uma instrução executável. O código não publica esse comentário automaticamente.
3. O \`request_id\` é estável por issue, perfil e SHA; correlação e branch também são determinísticos.
4. O consumidor de TODO/Worker Pool faz \`POST /v1/tasks\` usando o contrato já existente; a execução só inicia com lane DEV explicitamente habilitada, claim exato, lease, worktree isolado, \`SESSION_LAUNCH_OK\`, \`state_validated=true\` e Command Gateway. \`app/issue_draft_pr.py\` **não realiza** esse despacho; o adaptador futuro deve fazê-lo.
5. Builder com Ollama \`qwen2.5-coder:7b\` local, digest fixado, sem modelo ou API externa. O executor produz artefato/commit independente; o código atual exige baseline que falhe e 42/42 casos após reparo.
6. Validator independente verifica o **mesmo conteúdo/SHA** e executa regressões reais em outro checkout (no mínimo 52 aprovadas), sem skipped. \`regression_checker\` é uma interface: esta biblioteca ainda **não autentica** que a função fornecida veio de um checkout físico independente.
7. Antes da primeira escrita GitHub, \`publish_draft\` reconsulta o estado da issue, comentário, HEAD e conteúdo no SHA base e executa novamente as provas de patch/regressão. Só cria uma branch \`agent/qwen7b-issue-<n>-<prefixoSHA>\` ausente ou retoma a mesma branch sem colisão.
8. Compara por API os arquivos alterados: exclusivamente \`app/portfolio_bridge.py\`. Cria **draft PR** para \`main\` e faz leitura independente de PR, branch, SHA e conteúdo. Não faz merge, deploy ou atualização forçada.
9. Se ocorrer timeout após efeito externo, interromper. Ler a branch e PR atuais antes da próxima tentativa; divergência, PR não draft, SHA obsoleto, perda de autorização, regressão, escopo inesperado ou URL inadequada bloqueiam. Nenhum polling/heartbeat cria progresso.

## Limites deste incremento

- Há apenas o **contrato puro e seus testes**, sem adaptador GitHub HTTP concreto, sem credenciais, sem despachante ou workflow de issue, sem publicação por modelo e sem mudança no runtime do Dell.
- O CI para essa PR comprova a política em memória/fixtures, **não** comprova a integração GitHub nem inferência real. O canário físico anterior permanece evidência válida **somente** para o reparo fixo, não para issue→PR.
- O fluxo físico novo exige unir a API oficial autenticada ao Worker Pool, revalidar custo e capacidades, e executar uma issue real que ainda possua falha comprovada; não inventar um defeito só para aprovar o gate.
- Não desativar Docker nos **workflows legados hospedados** da main por causa desse incremento. O caminho nativo do Dell é sem Docker e nenhuma dependência de container é introduzida pelo novo contrato.
- Não integrar como equivalente ao Work/Codex antes de comprovar: issue real → admissão e lease → Qwen local → testes e Validator independente → PR draft na API → replay/retomada sem duplicidade, tudo vinculado a run/job, base/produced/head SHAs, correlação e readback.
