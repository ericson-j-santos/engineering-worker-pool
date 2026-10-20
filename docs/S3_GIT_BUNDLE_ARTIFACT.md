# Transporte S3 de artefato Git cercado

`app/s3_git_bundle_artifact.py` implementa `DurableArtifactTransport` sobre uma
API S3 compatível. O transporte é uma biblioteca injetável do executor nativo;
ele não cria bucket, não instala credenciais, não inicia worker e não habilita
uma tarefa cercada sozinho.

## Construção confiável

O chamador autorizado constrói o transporte fora do manifesto da tarefa:

```python
from app.s3_git_bundle_artifact import S3GitBundleArtifactTransport

artifact_transport = S3GitBundleArtifactTransport.from_environment()
```

Depois injeta `artifact_transport` em `NativeOneShotExecutor`. Não existe CLI ou
runtime operacional que faça essa ligação automaticamente neste incremento.

As configurações de roteamento aceitas são:

| Variável | Uso |
| --- | --- |
| `CODEX_ARTIFACT_S3_BUCKET` | Bucket privado obrigatório. |
| `CODEX_ARTIFACT_S3_REGION` | Região; pode ser suprida por `AWS_REGION` ou `AWS_DEFAULT_REGION`. |
| `CODEX_ARTIFACT_S3_PREFIX` | Prefixo exclusivo do pool; padrão `codex-artifacts`. |
| `CODEX_ARTIFACT_S3_ENDPOINT` | Endpoint S3 compatível opcional; pode ser suprido por `AWS_ENDPOINT_URL_S3` e precisa usar HTTPS. |
| `CODEX_ARTIFACT_S3_MAX_BYTES` | Limite do envelope; padrão 64 MiB, mínimo 1 KiB e máximo 1 GiB. |

O cliente usa SigV4, addressing path-style, verificação TLS e uma única tentativa
por chamada. As credenciais vêm exclusivamente da cadeia padrão do `boto3`, por
exemplo identidade da instância ou variáveis protegidas do processo. Valores
secretos não pertencem ao manifesto, à referência CAS, à linha de comando, aos
logs ou ao repositório.

Endpoints genéricos vindos de `AWS_ENDPOINT_URL` ou de configuração compartilhada
do botocore são ignorados. Somente o endpoint S3 específico, lido e validado pelas
variáveis da tabela, pode substituir o endpoint derivado da região.

## Contrato de armazenamento

O objeto contém um único envelope binário: magic/version, cabeçalho JSON
canônico estrito e `git bundle` v2 incremental. O bundle carrega somente os
objetos entre `workspace_sha` e o commit produzido; o checkpoint inicial deve
aparecer como seu único prerequisite e `HEAD` como sua única referência. Assim,
o artefato não republica todo o histórico alcançável do repositório. O cabeçalho
do envelope vincula:

- manifesto completo da tarefa, inclusive repositório, base, checkpoint inicial,
  adapter e hashes anteriores;
- `lease_generation`, `attempt_id` e `effect_id`;
- SHA produzido, paths alterados, comandos tipados e hash da evidência;
- tamanho e SHA-256 do bundle.

O SHA-256 do envelope define tanto a chave quanto a referência opaca:

```text
<prefix>/git-bundle-v1/sha256/<dois-primeiros>/<sha256>.artifact
cas+git-bundle://sha256/<sha256>
```

Endpoint, bucket, região e prefixo nunca aparecem no descriptor persistido pelo
Worker Pool. Publicação usa `If-None-Match: *`; o objeto não possui alias mutável
como `latest`. Depois de qualquer resposta do `PutObject`, inclusive erro ou
timeout, o transporte faz `GetObject` integral e compara os bytes:

- bytes idênticos confirmam sucesso idempotente;
- objeto divergente é conflito CAS;
- ausência ou leitura inconclusiva mantém `artifact_publish_ambiguous`.

O executor registra essa última condição como efeito `unknown` e bloqueia a
tarefa. Um executor antigo pode no máximo deixar um objeto CAS órfão; o objeto
não concede posse e não altera o checkpoint canônico.

## Preflight e permissões

`NativeOneShotExecutor` chama `preflight()` antes de adquirir a lease. A
implementação usa um único probe CAS fixo e pequeno sob
`<prefix>/preflight-v1/sha256/`: envia `PutObject` com `If-None-Match: *` e aceita
somente criação confirmada ou `412/PreconditionFailed` explícito; depois executa
`GetObject` limitado e compara os bytes integrais. Assim, configuração,
autorização de escrita condicional e leitura do prefixo são comprovadas antes da
posse. `AccessDenied`, timeout ou resposta inconclusiva falham fechados como
`durable_artifact_preflight_failed`, mesmo que outra credencial já tenha criado
o probe. O objeto é imutável e há no máximo um por prefixo.

O bucket deve permanecer privado, com acesso público bloqueado e transporte TLS
obrigatório. A identidade técnica precisa do menor privilégio possível:

- `PutObject` e `GetObject` somente em `<prefix>/preflight-v1/sha256/*` e
  `<prefix>/git-bundle-v1/sha256/*`;
- nenhuma permissão de sobrescrita lógica, listagem ampla, deleção ou
  administração do bucket quando não for indispensável.

Políticas de retenção/lifecycle podem remover objetos órfãos depois do prazo de
recuperação definido, sem transformar cache local na cópia canônica.

## Materialização e validação

O download exige `ContentLength` válido, aplica limite enquanto lê os chunks e
recalcula o SHA-256 do envelope e do bundle. JSON duplicado ou não canônico,
referência divergente, replay entre tarefas e resultado incompatível falham
fechados.

Antes do checkout, o transporte valida bundle, ancestralidade da base e do
checkpoint inicial, raiz real do repositório e ausência de remotos. Git é
executado sem shell, com hooks e configuração global/sistêmica desativados. Um
workspace sujo, conteúdo ignorado ou filtro vindo de `.gitattributes` ou
`.git/info/attributes` impede a materialização. Ao final, HEAD, limpeza e paths
alterados precisam coincidir com o resultado assinado pelo envelope.

## Evidência disponível e gates pendentes

`tests/test_s3_git_bundle_artifact.py` cobre roundtrip entre dois repositórios
locais independentes, publishers concorrentes, replay idempotente, timeout antes
e depois da gravação, conflito, adulteração, filtros Git, arquivos ignorados,
replay cross-task, ancestralidade e limites. Os testes usam um S3 sintético em
memória; não fazem acesso de rede nem comprovam credenciais, bucket privado,
política de menor privilégio ou recuperação entre máquinas reais.

`tests/test_native_one_shot.py` também prova o caminho integrado: um executor
fenced publica pelo transporte concreto e outro executor, em clone independente
sem remote nem hardlinks, retoma o checkpoint, materializa o mesmo SHA e não
repete o adapter nem a publicação.

Antes do primeiro uso físico ainda são obrigatórios:

1. provisionar ou selecionar um bucket privado autorizado;
2. instalar uma identidade técnica limitada ao prefixo;
3. injetar o transporte no runtime que constrói o executor;
4. executar prova E2E entre dois hosts, incluindo interrupção, readback e
   rejeição de posse vencida;
5. registrar a versão, o ambiente e as evidências na fonte canônica.
