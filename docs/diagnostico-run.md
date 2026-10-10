# Diagnóstico da execução local de 10/10/2026

Objetivo: identificar as correções necessárias para concluir uma primeira execução real,
antes de otimizar eficiência ou pontuação. Esta análise não comprova uma run de sucesso.

## Evidência examinada

- `artifacts/run.json`, `tender-evidence.json`, `tender-observations.json`,
  `mail-observations.json` e `events.jsonl`.
- Output da execução enviado pelo usuário, para separar a execução real dos eventos de
  testes que também foram anexados a `events.jsonl`.
- Autenticação, navegação, extração, dependências, limites, pricing e encerramento atuais.

Resultado real: Tender cancelado; Supplier, ERP e Mail falharam; Pricing permaneceu
pendente; `validation` e `receipt` são nulos. Nenhuma proposta validada/entregue está
comprovada. Não existem snapshots de autenticação do Supplier/ERP nos artefatos atuais.

## 1. Autenticação e desbloqueio da interface — prioridade máxima

Supplier atingiu o limite de 12 passos; ERP encerrou com autenticação não confirmada,
depois de timeout de navegação. Não há evidência suficiente para identificar seus
campos/botões problemáticos: os snapshots são gravados somente após autenticar.

Mail confirmou login, mas suas três observações têm texto idêntico. Todas mostram
`Gesperrt`, `Geschützter Bereich`, dois controles de desbloqueio e um aviso com
`Akzeptieren und fortfahren`. Login e acesso efetivo ao conteúdo são etapas diferentes.
Não há mensagem comercial aberta nesses snapshots.

O loop de autenticação envia apenas a observação atual e o papel do agente ao modelo;
não envia histórico de tentativas nem feedback dos erros. As instruções de slider
existem no prompt de investigação, mas não no de autenticação. O executor atualmente
atribui valor/dispara eventos ao range, enquanto o prompt recomenda sempre 100 sem
observar min/max. Não é possível afirmar a partir destes JSONs que esse mecanismo
desbloqueia os controles reais.

Correção proposta: salvar snapshots redigidos desde o início, registrar alvo/etapa/erro,
passar histórico e feedback, tratar avisos e verificações de acesso, usar limites
observados dos controles e exigir acesso ao conteúdo antes de procurar dados.

Verificação: os quatro sites autenticados e desbloqueados; Supplier mostra uma
especificação, ERP um catálogo e Mail consegue abrir o original de uma mensagem.

## 2. Tender não descobriu os documentos necessários — prioridade máxima

Tender publicou referência `VG-2027-7093`, mas `positions`, `clarifications` e
`documents` estão vazios. As quatro observações mostram lista de licitações, página
de detalhe, a mesma página com uma pesquisa de satisfação e o formulário de Bieterdaten
em `/submission`. Nenhuma observação tem `document` preenchido.

O menu visível `Verfahrensakte` não resultou em nenhuma lista de posições ou Q&A
preservada. O agente chegou à tela de entrega antes de obter os dados da proposta.
Os artefatos não permitem afirmar qual botão/menu ele tentou ou qual estava bloqueado.

Correção proposta: dar ao Tender uma sequência de coleta explícita: referência,
Verfahrensakte/documentos, posições, Q&A e alterações. Publicar a referência cedo com
uma extração pequena. Manter a fase de coleta fora do formulário de entrega. Detectar
observações repetidas e fornecer feedback para mudar de ação/menu.

Verificação: posições com quantidade, unidade e requirement_id, documentos/revisões
e esclarecimentos aplicáveis efetivamente observados; não apenas página de detalhe.

## 3. Encaminhamento de pendências inválido — defeito confirmado no código

As quatro pendências do Tender têm `agent_id: tender-investigator`. O coordenador
registra somente `tender`, `supplier`, `erp`, `mail`, `pricing`; sua reinvestigação
ignora destinos que não estão nesse registro. Esses pedidos nunca serão executados.
A limpeza das pendências em RunState também usa o nome do agente como chave.

Correção proposta: explicitar/constranger os IDs aceitos na resposta do modelo,
normalizar aliases reconhecidos antes de armazenar resultados e emitir erro útil
para destinos desconhecidos, em vez de ignorá-los. Consolidar pendências de um mesmo
agente para evitar uma execução longa para cada pergunta.

Verificação: uma pendência gerada pelo modelo chega ao agente correspondente e sai
da lista quando o dado solicitado é obtido.

## 4. Evidências volumosas sem dados de negócio — defeito confirmado

As 25 citações vêm de uma única observação. Há citações de `Angebotsfrist
(Europe/Athens)` marcadas como final_quantity, unit, amendments e outros campos.
A checagem existente confirma que a frase existe no texto, mas não demonstra que
ela sustenta o campo ao qual foi associada.

No resultado parcial, cerca de 9.2 mil dos 9.35 mil caracteres do objeto Tender
compactado são evidências. O registro inteiro é reenviado em cada decisão de
navegação; schemas e saídas anteriores também contêm evidências. O output real
mostra chamadas de 10–13 mil tokens, com uma extração de 128 segundos. A existência
da duplicação é comprovada; a parcela exata da demora causada por ela não é.

Correção proposta: enviar fatos e IDs de fontes na navegação, manter citações completas
em armazenamento separado, não pedir ao modelo Evidence completas com metadados
preenchidos pelo programa, reduzir duplicação e validar a cobertura de dados reais.
Uma publicação somente da referência precisa de uma citação pertinente, não 25.

Verificação: cada posição/regra tem dados e fonte pertinente; uma página vazia não
é contabilizada como cobertura. Registrar tokens por fase para confirmar a redução.

## 5. Limites e recuperação aumentam a espera

O limite atual é 600 segundos por tentativa de agente, com uma repetição permitida.
Isso inclui autenticação e espera por dependências. Mail gastou boa parte dessa janela
esperando Tender; aos ~610s Mail/Tender iniciaram nova tentativa. A mesma sessão é
reutilizada, mas a autenticação é executada novamente e o histórico de investigação
é recriado. Uma chamada individual do Qwen permite 360s e outra tentativa para
algumas falhas. Os logs só registram chamadas concluídas; esperas/falhas anteriores
podem ficar invisíveis. A lacuna ~72–323s não tem causa identificável nos registros.

Correção proposta: distinguir espera de dependência de trabalho ativo, preservar
histórico/estado nas recuperações, usar limites por fase e registrar início/erro/retry
das chamadas. Repetir a ação que falhou, quando seguro, sem reiniciar o raciocínio
inteiro. Garantir um orçamento final de cálculo e assinatura.

Verificação: falha transitória recupera a etapa; falta permanente encerra com motivo
claro; não há minutos de espera sem etapa identificada.

## 6. Observabilidade e encerramento

`model_call` não informa agente/fase. Eventos usam relógios de instâncias diferentes
de RunLogger. `events.jsonl` acumula testes e execuções sem identificador de execução.
As observações salvas não incluem controles nem ação selecionada, e os erros guardam
somente o tipo. Isso impede diagnosticar os loops de autenticação retrospectivamente.

O erro final Browser.close ocorreu durante limpeza; não explica as falhas anteriores.
É necessário impedir que erro secundário de limpeza mascare o motivo original.
O entrypoint também pode imprimir Workflow finished e retornar 0 quando não há
proposta/receipt; isso não deve ser apresentado como sucesso de negócio.

Correção proposta: run_id local, relógio comum, agente/fase/etapa/duração/status nos
eventos; snapshots com controles e feedback redigidos; resumo final distinguindo
coleta, cálculo, validação e entrega. Preservar resultados mesmo após cancelamento.

## Ordem para alcançar uma primeira run bem-sucedida

1. Diagnosticar e validar login/desbloqueio dos quatro sites, separadamente.
2. Validar Tender coletando posições/Q&A e Supplier coletando revisão aplicável.
3. Validar ERP coletando materiais/condições/estoque e Mail coletando originais comerciais.
4. Corrigir roteamento de pendências e reduzir payloads antes de repetir o fluxo inteiro.
5. Executar cálculo com dados reais, validar todas as posições e testar recuperação
   de chamada transitória sem perder a coleta.
6. Completar preenchimento, pedido de TAN atual, assinatura e recibo visível no portal.

Critério de sucesso: dados reais dos quatro sites, proposta calculada, validação válida
e receipt confirmado para a referência da execução. Custo, velocidade e pontuação
podem ser otimizados depois. Pricing/assinatura ainda não foram exercitados nessa run;
não se deve assumir que funcionarão só porque há testes locais.

Não basta aumentar timeout ou remover validações. O que falta nesta execução são
dados e navegação correta, não tolerância adicional a uma proposta incompleta.

## Validação das correções em 10/10/2026

Os quatro sites passaram no teste real de acesso (`35ace71ca76846d8a05df04a8067a2d0`).
Login e controles de acesso visíveis agora usam ações nativas, preservando a escolha
das buscas e documentos pelo modelo. O fluxo também passou a publicar a referência
antes de tentar extrair toda a licitação, liberando Supplier e Mail mais cedo.

Um teste direto no proxy revelou uma ação inválida quando o schema aparecia apenas
em `response_format`. O schema foi incluído na instrução textual, mantendo validação
local. Erros de formato agora retornam ao modelo com a própria resposta anterior.
O modo JSON alternativo fica memorizado quando o gateway rejeita `json_schema`.

Outros ajustes: histórico transitório inclui o valor da última busca, os painéis são
desbloqueados completamente antes da leitura e citações aceitam apenas diferenças
de espaços/caracteres invisíveis, preservando o trecho original como evidência.
Alterações de números ou palavras continuam sendo rejeitadas.

A execução `1eec1da52ba0489cbae65f7b8282e23a` chegou a extrair cinco posições,
mas Supplier falhou e Mail permaneceu incompleto. Ela foi interrompida para aplicar
as correções verificadas, e não representa sucesso do fluxo inteiro.

A execução seguinte, `263ce72940c54299bf093a393168ba95`, foi interrompida após
confirmar falhas de coleta e observações de frames ocultos de documentos fechados.
Seus arquivos foram preservados em `artifacts/history/probe-263ce729/`.
Não há ainda prova de cálculo completo, validação final ou recibo.
Nas próximas execuções, `artifacts/latest.json` aponta para a pasta exclusiva da
run; os arquivos na raiz são cópias compatíveis e podem conter dados antigos quando
um agente não chegou a produzir um resultado novo.

Os testes isolados do Supplier também ainda não provaram cobertura completa. A
execução `23feb68454a54df4b139135cf1bce1fe` terminou parcial após 440 segundos,
com apenas um requisito de uma revisão sem autoridade confirmada. Houve várias
extrações rejeitadas e uma resposta de navegação sem alvo. A execução
`0d2677f6249e4cac9152850fbd2cf341` entrou em aproximadamente cinco segundos,
mas falhou durante a correção de outra resposta sem alvo, sem coletar requisitos.

Correções adicionais: ignorar frames ocultos e ancestrais invisíveis; expor títulos
SVG e rótulos de acessibilidade; reconhecer o campo de recibo sem rótulo pelo nome
observado dentro do formulário específico de leitura. O schema de ações agora
explicita que cliques precisam de alvo e que navegações precisam de URL. Os passos
salvos passam a distinguir falhas de citação e seleção de fonte por categorias
fixas, sem gravar textos arbitrários de erros ou credenciais. Os 77 testes locais
passaram; isso não substitui verificar a coleta real e a entrega final.
