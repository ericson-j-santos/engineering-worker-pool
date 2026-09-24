# Work Orchestrator v1

## Objetivo

Consolidar a interface operacional do Engineering Worker Pool em um fluxo persistente e idempotente:

`Queue -> Worker/Dispatcher -> Builder -> Validator -> Evidence -> Governed Merge`.

A implementação deve reutilizar o scheduler, leases, Builder/Validator e persistência já existentes. Não duplicar o núcleo do Worker Pool.

## Requisitos

1. `POST /v1/work` cria uma unidade de trabalho idempotente sobre a task existente.
2. Replay da mesma identidade lógica não cria novo work nem nova task.
3. `GET /v1/work/{work_id}` expõe uma visão única do estágio atual sem `lease_token`.
4. A fase deve refletir o estado persistido real da task e do gate de merge.
5. Evidência só pode ser aceita após Validator concluir a task e deve apontar para o `produced_sha` exato.
6. Evidência exige leitura independente, caso positivo e controle negativo.
7. Evidência divergente, incompleta ou de outro SHA deve falhar fechado.
8. O merge permanece executado pelo Governed Merge Queue; o Work Orchestrator apenas registra de forma persistente seu resultado.
9. O resultado de merge deve exigir `expected_head_sha` igual ao SHA validado.
10. Resultado `merged=true` exige `merge_commit_sha` válido.
11. Um bloqueio de merge pode ser reavaliado posteriormente no mesmo work sem criar nova identidade.
12. Nenhum segredo, token ou credencial deve ser persistido na camada Work.

## Aceite

- teste E2E HTTP prova criação, replay, Builder, Validator, evidência, bloqueio de merge, merge e leitura independente final;
- teste negativo prova rejeição de evidência de SHA divergente;
- teste negativo prova rejeição quando um controle de evidência está ausente;
- teste negativo prova rejeição de `expected_head_sha` divergente;
- replay da evidência e do resultado de merge não produz efeito adicional;
- CI do SHA exato deve ficar verde.

## Fora de escopo

- executar merge diretamente contra GitHub;
- deploy ou promoção de ambiente;
- remoção do legado do ReqSys;
- runtime físico/PC24x7;
- armazenamento de credenciais.
