# Executar apenas o Webmail

Após o reset, a implementação fica em `agents/mail.py`, com navegação e runtime
próprios em `tools/mail_navigation.py` e `tools/mail_runtime.py`. Os controladores
de Tender, Supplier, ERP e o Orchestrator mantêm a base reiniciada.

Para verificar apenas o Webmail:

```powershell
uv run --no-sync python -m unittest discover -s tests -p "test_mail*.py" -v
```

Arquivos antigos não rastreados que sobreviveram ao reset podem depender de APIs
que já não existem. O comando acima seleciona os testes da implementação atual.

O executor usa o MailAgent, o modelo e o webmail reais. Não inicia Tender, Supplier,
ERP, Pricing ou assinatura. A única entrada substituindo outro agente é a referência
fornecida explicitamente; ela precisa existir no cenário atual. Não há respostas
comerciais simuladas nem submissão de proposta.

Na primeira janela, mantenha o proxy:

```powershell
atira models proxy
```

Na segunda janela, carregue o cenário e execute:

```powershell
atira scout --env | Out-String | Invoke-Expression
uv run --no-sync python -m tools.run_mail --reference "REFERENCIA_DO_CENARIO"
```

O executor resolve qual entrada de acesso é o webmail. Se necessário, selecione
explicitamente seu `siteId` do runtime com `--site-id "ID_DO_WEBMAIL"`.
Localmente abre somente um Chromium. `HEADLESS=1` oculta essa janela.
Com endpoints CDP, conecta somente ao navegador escolhido por `--browser-number`
(1 por padrão), sem fechar o navegador gerenciado ao sair.
O contrato de acesso atual continua recebendo as quatro entradas exportadas pelo
scout; os outros três websites não são abertos.

## Investigação direcionada

```powershell
uv run --no-sync python -m tools.run_mail --reference "REFERENCIA_DO_CENARIO" --question "Verifique as regras de embalagem e suas condições de validade" --timeout 480 --max-steps 60
```

Cada invocação começa uma investigação nova. Para fornecer posições e chaves
comerciais adicionais, crie um JSON compatível com `AgentTask`, por exemplo:

```json
{
  "action": "investigate",
  "tender": {
    "reference": "REFERENCIA_DO_CENARIO",
    "positions": [
      {"position_id": "10", "packaging_key": "CHAVE_OBSERVADA"}
    ]
  },
  "question": "Investigue as regras aplicáveis à chave de embalagem informada"
}
```

```powershell
uv run --no-sync python -m tools.run_mail --task-file "mail-task.json"
```

Use valores do cenário atual. Entradas fornecidas não são evidências verificadas
pelo executor. Não coloque credenciais no arquivo da tarefa.

## Resultados

Cada execução tem uma pasta própria em `artifacts/mail-runs/<run_id>/`, contendo
entrada, site selecionado, eventos, passos, observações, evidências e `result.json`.
Credenciais conhecidas são redigidas. Os artefatos contêm conteúdo comercial do cenário.
O executor não sobrescreve os resultados da execução completa.

Código de saída 0 significa que o MailAgent declarou `complete`; 1 indica resultado
parcial, bloqueio ou falha. Isso usa os critérios atuais do agente, não é uma prova
independente de cobertura comercial. O limite de tempo preserva os fatos já publicados.
A execução também respeita a expiração do runtime, reservando 60 segundos.

`mail-progress.json` mostra o checklist gerado a partir do brief, os fatos acumulados,
fontes já extraídas e registros descartados com motivo. Cada assunto fica pendente,
encontrado, verificado ou sem registros aplicáveis após revisão da busca.
Novas chaves recebidas durante a execução completa ampliam o plano sem apagar progresso.

O agente recebe sua memória nas decisões de navegação. Reextrações de fontes já
processadas são bloqueadas; conclusão pode reutilizar os fatos acumulados. Regras
precisam de corpo original, referência exata, remetente e data; registros futuros
são descartados. Mensagens têm identidade estável e horários exibidos com fuso
observado são normalizados com tzdata. Desconto condicional permanece no texto da regra,
sem virar um percentual incondicional.

A escolha dos assuntos, a interpretação de autoridade e a declaração de busca
completa ainda dependem do modelo. A próxima execução real precisa verificar
cobertura, mensagens originais e resolução de substituições.

O plano aceita somente assuntos de mensagens: desconto, cláusulas, aprovação técnica,
entrega, embalagem, preparação compartilhada, bônus, câmbio e índices. O domínio dos
remetentes vem da conta do cenário ou da conta exibida; não é inferido do nome da empresa.
`last_rejections` informa motivo, referência/remetente esperados e IDs observados.
Após duas extrações rejeitadas das mesmas fontes, o agente precisa observar conteúdo
novo ou avançar para outro assunto. IDs de observação são convertidos em IDs de mensagem
quando a fonte original permite identificá-la sem ambiguidade.

## Reiniciar a aba entre assuntos (experimental)

```powershell
uv run --no-sync python -m tools.run_mail --reference "REFERENCIA_DO_CENARIO" --restart-between-topics
```

Ao concluir/adiar um assunto ou selecionar uma busca de outro assunto, salva o progresso,
fecha somente a aba usada pelo webmail, abre uma nova no mesmo contexto e verifica o
login novamente. Cookies podem manter a sessão; se aparecer o formulário, injeta as
credenciais reais. Não fecha o navegador gerenciado. As evidências e os assuntos pendentes
permanecem na memória. O modelo continua escolhendo buscas e mensagens. O limite total
de tempo permanece o mesmo; reinícios não tratam timeouts do modelo.

## Fallback por latência: Qwen → OpenAI

No mesmo terminal da run, configure `OPENAI_API_KEY`. Não coloque a chave no
repositório ou em arquivos de tarefa. Em PowerShell 7:

```powershell
$env:OPENAI_API_KEY = Read-Host "Chave OpenAI" -MaskInput
$env:MAIL_QWEN_TIMEOUT = "10"
$env:MAIL_OPENAI_MODEL = "gpt-4.1-mini"
```

Com a chave presente, se o Qwen não responder em 10 segundos, cancela a chamada e
envia as mesmas instruções e dados para a OpenAI. O fallback usa JSON mode com a
mesma validação Pydantic e uma tentativa de corrigir a resposta. Correções permanecem
na OpenAI para essa solicitação; a próxima solicitação começa pelo Qwen novamente.
O limite original da fase (90/120 segundos) vale para a chamada OpenAI; o limite global
da run continua valendo. Sem chave, mantém o comportamento anterior do Qwen.
`OPENAI_BASE_URL` configura o endpoint, inclusive um proxy de medição exigido pelo
ambiente Atira; sem essa variável, usa `https://api.openai.com/v1`.
Os eventos `model_provider_fallback` e `model_call` identificam provedor/modelo.
Cancelamento manual/global não dispara fallback. Falhas da OpenAI não geram outro
fallback. Os testes usam clientes simulados; nenhuma chamada paga foi realizada na
verificação. O fluxo de reinício entre assuntos pode ser usado junto com o fallback.

### Configuração local em `.env`

O executor carrega `.env` na raiz do repositório antes de criar o cliente de modelos.
Preencha `OPENAI_API_KEY=` com sua chave. O arquivo é ignorado pelo Git. Variáveis já
configuradas no terminal têm prioridade; o `.env` não substitui o runtime do scout.
`MAIL_QWEN_TIMEOUT=10` e `MAIL_OPENAI_MODEL=gpt-4.1-mini` podem ser configurados ali.
Não use credenciais Qwen antigas do scout neste arquivo; o scout fornece o acesso atual.

Após um timeout do Qwen, as próximas cinco solicitações usam diretamente a OpenAI;
depois volta a testar o Qwen. `MAIL_OPENAI_HOLD_CALLS` ajusta esse número (0 desativa).
O evento `model_provider_cooldown` registra esse uso. Extrações de listas sem corpo
original são bloqueadas antes de chamar o modelo, orientando abrir o original.
Buscas explicitamente vazias ainda permitem revisão de ausência de registros.

As decisões de navegação agora usam MailDecision: topic_id e stage obrigatórios, além
da ação. O controlador não deduz o assunto pelas palavras da busca. O prompt de modo
informa se mudanças de assunto reiniciam a aba ou usam a URL de retorno. O remetente
pode ser recuperado de um cabeçalho Von/From observado quando corresponde à autoridade
do brief; um remetente diferente nunca é substituído. Rejeições registram os remetentes
observados e os campos de autoridade devolvidos pelo modelo para diagnóstico.

## Fluxo atual simplificado

O executor agora sempre tenta cada tema do plano uma vez, em ordem: abre/autentica,
pesquisa e abre mensagens escolhidas pelo modelo, salva regras aceitas e fecha a aba.
Na primeira extração útil passa ao próximo tema. Falhas/limites também fecham a aba e
seguem, sem revisitar o tema nesta run. O orçamento de passos e tempo é dividido entre
os temas. `mail-topic-runs.json` registra salvo, incompleto, limite ou falha por tema.
O flag `--restart-between-topics` é mantido por compatibilidade; o reinício por tema
agora é sempre usado. Não há contador de adiamento nem troca livre de tema pelo modelo.
Regras e evidências continuam validadas. Coleta parcial não garante cobertura completa.

### Sequência controlada pelo código

O modelo devolve termos (MailSearch). O código acrescenta a referência, preenche o
campo observado e submete com Enter, confirmando q na URL de resultados. Depois o
modelo escolhe somente entre controles permitidos (MailChoice): candidatos, arquivo,
paginação e abertura de original/cabeçalhos. Mensagens de assinatura são excluídas.
Quando o corpo original está disponível, o código chama a extração automaticamente.
Observações sem mudança são limitadas; sem avanço o tema termina incompleto.
MailDecision não é mais usado no loop atual. O fluxo continua um tema por aba.

A busca agora exige selecionar um assunto exato sustentado pelo brief ou pela página
observada (por exemplo Rabattfreigabe, Technikfreigabe, Devisenfixing). O modelo não
pode acrescentar descrições ou traduzir o assunto. Referências repetidas são removidas;
o controlador acrescenta uma vez a referência. Há uma correção curta se a escolha for
inválida. Sem assunto sustentado, o tema registra falha, sem inventar termos.
