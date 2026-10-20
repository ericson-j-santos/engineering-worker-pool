# Consulta governada do resultado remoto

`readback_remote_result` consulta um recibo existente sem conceder capacidade
de executar novamente uma tarefa. Usa o runtime remoto já configurado; não
cria cliente de protocolo, fila, agendador, serviço ou credencial alternativos.

## Causa e separação de responsabilidades

`RemoteNativeExecutor.run()` exige uma sessão com worktree ainda no SHA
inicial. Isso é correto para admitir uma nova execução. Depois que o Builder
cria um commit, chamar `run()` novamente não satisfaz esse preflight.
Reutilizar uma atestação antiga ou aceitar qualquer HEAD relaxaria a segurança.

A consulta usa a reserva e o snapshot originais, confere seus hashes, host,
identidade, correlação, validade e estado inicial, e inspeciona o checkout do
resultado pelo Gateway real. Somente o HEAD e o digest do índice podem diferir
do snapshot inicial; o worktree continua limpo, na mesma identidade e branch.
O executor existente ainda verifica manifesto, contrato, caminhos, SHA
produzido, checkpoint, geração de posse e estado canônico.

## API

Em código de implantação confiável, após um resultado confirmado:

```python
from app.native_remote_readback import readback_remote_result

confirmed = readback_remote_result(
    runtime,
    original_manifest,
    role="builder",
    worker_id="installed-builder",
)
```

`runtime` deve ser a composição `RemoteNativeExecutor` já configurada e
autorizada. O exemplo não provisiona uma sessão, identidade ou destino.
Para Validator, usar seu manifesto e sua própria sessão no SHA produzido.

A chamada retorna o recibo original com `replayed=True`. Ela não tenta
`run()` como fallback. Recibo ausente, alterado, expirado ou incompatível
mantém o fluxo bloqueado. A validade original de até 300 segundos não é
renovada por uma consulta. Um recibo de Builder de geração antiga pode ser
rejeitado depois que o Validator obtém outra geração, conforme o contrato
canônico já existente; não se ignora esse fence.

## Capacidades e concorrência

A API de consulta permite somente GET. O transporte permite somente verificar
o artefato, não fazer preflight de escrita, publicar ou materializar. Não são
fornecidos comandos de adapter. A criação/substituição de recibos é bloqueada.

O lock da tarefa/função permanece até a verificação final. O executor recebe
apenas um lock emprestado sem capacidade de liberar o lock real. O Gateway
mantém o lock do repositório durante a consulta e a releitura independente.
Mudança de recibo, política, módulo ou estado Git invalida o resultado.

A leitura de recibos exige POSIX, arquivo regular, dono corrente, modo privado,
um único hardlink e `O_NOFOLLOW`, com tamanho limitado. Windows continua
dependendo de uma implementação protegida especificamente qualificada.
Não copiar DPAPI, simular ACL ou contornar o preflight para usar essa API.

## Validação e limites

O CI existente contém uma etapa sequencial para
`scripts/remote_native_composition_e2e.py`, depois do checkout de regras fixadas.
Ela exercita factory, Gateway e comandos reais, TLS/Uvicorn, SQLite, Git e
`S3GitBundleArtifactTransport`, com Builder e Validator em worktrees separados.
O clone receptor é criado antes do resultado e só o obtém pelo transporte.
Testes unitários impedem a remoção ou supressão silenciosa dessa etapa.

O provedor S3 desse teste é explicitamente a fixture em memória já existente.
O perfil Gateway é o perfil descartável do E2E nativo, não a política POSIX
operacional. O teste não homologa S3 privado, identidade instalada, perda de
máquina, seleção automática de host ou tarefa de produto. Os recursos privados
existentes não são acessados por esse gate.

Resultados e lacunas devem ser registrados na PR/Issue #13/TODO-30 com o SHA
efetivamente testado. A presença deste documento não constitui evidência de
aprovação do CI ou autorização de merge/deploy.
