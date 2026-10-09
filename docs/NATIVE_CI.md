# Validacao nativa sem navegador ou acesso aos computadores

## Decisao e escopo

Em 09/10/2026, o proprietario reiterou que a conexao do Opera Browser Connector
ja falhou repetidamente e nao deve ser solicitada novamente como requisito para
continuar o worker gratuito. Operacoes que possuem API GitHub nao dependem de
Opera, Remote Desktop Commander, ChatGPT Work ou de um runner fisico.

O CI deste repositorio ja executa compilacao e pytest em `ubuntu-latest`.
Este incremento acrescenta uma entrada nativa ao mesmo workflow, sem criar
scheduler, servico persistente, executor de comandos arbitrarios ou fila paralela.

## Entrada restrita

Apos integracao na branch padrao, a integracao GitHub pode publicar o comentario
exato `/worker validate` na Issue #13 deste repositorio. Nao e necessario pedir
ao proprietario que copie comandos ou configure navegador.

O evento precisa ser `issue_comment/created`, de `ericson-j-santos`, tipo User,
em Issue aberta (nao PR), no repositorio publico exato. Qualquer sufixo, outra
issue, outro usuario, bot, edicao ou identidade divergente e recusado. O filtro
do workflow e repetido pelo modulo puro `app.ci_entrypoint` antes dos testes.

O checkout e imutavel e vinculado ao SHA do evento. A permissao permanece
`contents: read`; as credenciais de checkout nao sao persistidas. A concorrencia
dos comentarios usa grupo separado do CI de push/PR e nao cancela a execucao
corrente. A repeticao da admissao e pura: nao cria tasks de produto, nao altera
os computadores e nao gera chamadas a modelos. Comentarios novos ainda podem
gerar novas validacoes; isto nao e um deduplicador universal de eventos.

## Evidencia e aceite

Cada execucao registra no resumo SHA, evento, comentario, run_id e resultado.
Logs de compilacao e pytest devem ser inspecionados; aceite de comentario ou
workflow iniciado nao comprova sucesso.

Antes de habilitar a rota como operacional:
1. aprovar testes positivos, negativos, replay e regressao no HEAD da PR;
2. integrar somente apos os gates e autorizacao aplicaveis;
3. testar um comentario real na main e comprovar o run e os testes no mesmo SHA;
4. confirmar que um comentario invalido nao inicia o job de validacao.

A PR permite testar codigo e CI por API, mas nao comprova o evento de comentario
na main antes de sua integracao. Nenhuma permissao de merge e concedida aqui.

## Limites

Este comando executa validacoes do repositorio. Nao gera codigo por IA, nao
despacha trabalho real do TODO Global, nao implanta o Worker Pool e nao recupera
Desktop ou Noteri. A ligacao fila -> executor -> modelo local -> artefato validado
continua um aceite separado.

Preferir testes descartaveis vinculados ao desenvolvimento deste repositorio,
sem runners maiores, novos servicos pagos ou credenciais de inferencia. Armazenamento
e demais limites da conta continuam aplicaveis; nao prometer custo ilimitado.

Pendencias fisicas continuam nos repositorios de runtime. Uma rota fisica
indisponivel nao transforma Opera ou RDC em requisito das validacoes hospedadas.
