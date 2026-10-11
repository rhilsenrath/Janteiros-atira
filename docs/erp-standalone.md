# ERP básico e independente

Execute na raiz do repositório, com o mesmo proxy de modelos usado pelo webmail:

```powershell
.\.venv\Scripts\python.exe -m tools.run_erp
```

O login local vem de `ERP_USERNAME`, `ERP_PASSWORD` e `ERP_URL` no `.env`.
Em uma run da plataforma, as credenciais vêm do runtime atual.
O cliente de modelos reutiliza o transporte existente de Qwen/OpenAI; nenhum agente de mail é executado.

Fluxo: login e avisos → escolha de transação e busca pelo modelo → até dois materiais
selecionados pelo modelo → páginas e detalhes escolhidos pelo modelo → extrair e salvar.
As navegações usam links observados, sem URLs de materiais inventadas.
Sessões expiradas permitem até duas reautenticações.

O modelo recebe as observações atuais, os requisitos e os links permitidos em
cada etapa. Escolhe a busca pelo grupo fornecido, candidatos, páginas do material,
detalhes de estoque e tabelas de preços. `erp-navigation-decisions.json` registra
essas escolhas e justificativas.
As respostas de navegação usam identificadores (`o1`, `o2` etc.) limitados no
schema às opções da etapa. O código resolve esses identificadores para os links
observados. Cada etapa tem um objetivo específico; estoque não permite escolher
links de material ou preço fora das opções. O registro inclui opções, resposta,
URLs resolvidas e resultado da validação.
Login, avisos, leitura da legenda de status e paginação de condições já
selecionadas são auxiliares limitados. Valores e
seleções finais continuam sujeitos às validações existentes. Candidatos não
inspecionados impedem uma seleção certificada. Há mais chamadas ao modelo do que
no fluxo anterior; a próxima run no site deve validar as escolhas reais.

`examples/erp-task.json` contém um requisito e uma quantidade explicitamente de teste.
GR-8923 e seus materiais HZ-8405 e HZ-1609 foram vistos no ERP durante a preparação.
Os atributos do exemplo servem para testar a consulta; não substituem especificação e esclarecimentos oficiais.
Não há cálculo de proposta, seleção certificada de material ou confirmação de cobertura completa nesta versão.
O resultado inclui `output.selections` e o arquivo `erp-selection.json`: a seleção
compara exatamente bore, stroke, pressure, mounting e certification e exige status
ativo confirmado. `selected` identifica um único candidato; `no_match` indica que
nenhum dos candidatos lidos corresponde; `incomplete` exige dados ou leituras
adicionais; `ambiguous` indica mais de um candidato compatível. Todos os candidatos
continuam em `output.materials`. Essa seleção usa os requisitos de teste fornecidos,
não substitui a investigação dos demais agentes nem certifica a cobertura da busca.
Resultados permanecem `partial`, com campos desconhecidos em null e pendências explícitas.

A seleção de preço está em `output.price_selections` e `erp-price-selection.json`,
por posição. Usa a data com fuso do cenário (limites inclusivos), a quantidade em
peças e a maior faixa atingida. Quantidades em Satz exigem `pieces_per_set` do
requisito fornecido. Mantém condição original, moeda, unidade de cobrança e preço
base por peça sem arredondamento. Não aplica desconto, câmbio, taxas ou cálculo
final da proposta. Condições conflitantes não são escolhidas arbitrariamente.

O prazo aplicável fica em `output.lead_time_selections` e
`erp-lead-time-selection.json`. A demanda final é somada por material entre as
posições fornecidas. Se o estoque disponível cobre toda essa soma, todas usam
o prazo de estoque; caso contrário, todas usam o de produção. Mantém semanas
ou dias da fonte, sem dividir estoque entre posições. Dados desconhecidos ou
conflitantes deixam a decisão pendente. Compromissos especiais de entrega do
email e esclarecimentos do tender ainda não são aplicados neste teste básico.

```powershell
.\.venv\Scripts\python.exe -m tools.run_erp --max-materials 1 --timeout 300
.\.venv\Scripts\python.exe -m tools.run_erp --group GR-8923
.\.venv\Scripts\python.exe -m tools.run_erp --task-file examples/erp-task.json
```

Saída: `artifacts/erp-runs/<run-id>/`.
`erp-steps.json` registra páginas e ações; `erp-observations.json` guarda fontes;
`erp-material-runs.json` informa o resultado de cada material;
`erp-progress.json` é atualizado após salvar um material; `result.json` contém o resultado final.
Senhas são removidas dos diagnósticos. Nenhuma credencial vai no arquivo de exemplo.

## Taxa de preparação (`setup_fee`)

Investigação em 11/10/2026: as observações da run
`eb722bf0ad6e4c5a81e45b361481982c` para HZ-8405 e HZ-1609 não exibem
taxa de preparação. A conferência ao vivo de HZ-1609 em MM03 (Grunddaten,
Technische Daten e Disposition) e VK13 (Kopfdaten) também não mostrou esse valor
nem um link específico para consultá-lo. Isso limita a conclusão às telas e
materiais consultados; não comprova ausência de taxa ou valor zero.

O anexo distingue a taxa comum da taxa compartilhada: o email Rüstpools fornece
a taxa compartilhada do pool. Ela não substitui `MaterialCandidate.setup_fee`.
Enquanto não houver uma fonte explícita para a taxa comum, o campo permanece
`null` e aparece nas pendências. Não acrescentar navegação por transações
inventadas nem preencher zero para eliminar a pendência.
