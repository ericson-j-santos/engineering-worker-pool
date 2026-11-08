# Issue 61 — autenticacao real da origem GitHub (DEV)

A CI publica usa runner Ubuntu padrao de custo incremental R$0 e executa
scripts/prepr_live_github_readback.py em dois jobs separados.
O script consome token efemero do GitHub Actions armazenado em arquivo privado.
O adaptador OfficialNoteriReadback utiliza API HTTPS oficial de leitura
para Noteri issue 42, main, branch e compare/ancestralidade. O HEAD e relido.

Versoes admitidas:
- main: e10b3a00a9b3ec803ed4a4cc8f4f8c62b06c8928
- branch: 1e60b4f3db1d5ad5d59dbed087f275870aa002ad
- TODO-54 / issue noteri-runtime 42.
- regras 1.6.20 SHA 86abe65b22781c2f608a7d7286ce3a28cd48aaaf.

Saida restrita: GITHUB_SOURCE_ATTESTED_ONLY; ready_for_pr=false;
model_executed=false; cas_materialized=false; host_ready=false.
Nao executa Qwen, nao publica objetos, nao envia comandos ao Noteri e nao
concede READY_FOR_PR nem autorizacao administrativa.

Mudanca na main/branch, issue fechada, token indisponivel ou diff fora do
escopo produz BLOQUEADO, sem alterar automaticamente os SHAs admitidos.
Proximas provas: Builder Qwen apropriado, CAS bundle real e Validator
independente SHA-bound e custo operacional comprovado.

Readmissão 08/11/2026: SHA anterior 515ede49... → 1e60b4f3db1d5ad5d59dbed087f275870aa002ad. Base main intacta, branch ahead=7/behind=0, diff 7 arquivos, arquivo do launcher com blob Git idêntico. CI Noteri #42488898078 passou. A CI ECP deve reproduzir novamente o baseline e autenticar nova origem; sem inferência real/CAS/READY por essa alteração.
