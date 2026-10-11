# Janteiros · do RFQ ao bid assinado

Sistema de agentes para o desafio **From RFQ to bid** (Atira): lê uma licitação pública em quatro sites
alemães (portal de licitação, portal do fornecedor, ERP e webmail), aplica as regras do brief e entra um
lance precificado e assinado com TAN no configurador do portal, sem ninguém tocar em nada.

O desenho segue o documento *MVP Janteiros: do RFQ ao bid assinado*: sete papéis ligados por um caderno onde
cada fato entra com a sua origem.

```
                    ┌──────────────┐  plano, relógio, go
          brief ──▶ │  Supervisor  │◀──────────────────────────────┐
                    └──────┬───────┘   vigia em código + modelo    │
          missões por necessidade                                  │ prontidão
     ┌──────────────┬──────┴───────┬──────────────┐                │
┌────▼────┐   ┌─────▼────┐   ┌─────▼────┐   ┌─────▼─────┐   ┌──────┴──────┐
│ Tender  │   │Fornecedor│   │   ERP    │   │  Correio  │   │   Motor     │
│(portal) │   │ (portal) │   │(SAP-like)│   │ (webmail) │   │ de regras   │
└────┬────┘   └─────┬────┘   └─────┬────┘   └─────┬─────┘   │  (código)   │
     └──── registrar_fato (com origem) ───────────┴────────▶└──────┬──────┘
                         O CADERNO                                 │ resposta
                                                            ┌──────▼──────┐
                                    TAN ◀── correio ─────── │ Despachante │──▶ bid assinado
                                                            └─────────────┘
```

## Princípios

- **O modelo decide, o código executa e calcula.** O modelo (Qwen3.8-Flash-Next) escolhe para onde ir, o que
  buscar e o que importa. Helpers só leem a página escolhida, paginam, abrem documentos, limpam obstáculos e
  preenchem. Todas as regras do brief rodam no motor (`janteiros/engine.py`), nunca no modelo.
- **Especialista por tipo de software, missão por necessidade.** Cada especialista sabe navegar um tipo de site
  (manual em `janteiros/manuals/`). O Supervisor diz *o que* achar, nunca onde clicar; o que faltar depois da
  primeira onda vira missão de acompanhamento ("nova rota") no site mais provável.
- **Supervisão por exceção.** Um vigia em código acompanha cada passo (sem progresso, repetição, volta ao login,
  orçamento) e só chama o modelo do Supervisor quando vê problema.
- **Fato sem origem não entra.** Toda ferramenta `record_*` passa por um auditor em código (tipos, formatos de
  número de-DE/en-US, datas em UTC, referência exata) e grava URL, trecho e passo.
- **Só uma peça faz o que não tem volta.** O despachante é o único que envia, depois do go, e pede o TAN só no
  resumo; o diálogo de confirmação do navegador só é aceito quando ele arma.
- **Sempre entregar.** A coleta congela com 8 minutos restantes; posições incompletas são omitidas
  explicitamente e o lance é assinado.

## Estrutura

| Caminho | O que é |
| --- | --- |
| `agent.py` | Ponto de entrada (`uv run python agent.py`) |
| `agent.json` | Contrato do sandbox: setup, run, modelos, 4 navegadores |
| `janteiros/supervisor.py` | Supervisor: plano, ondas de missões, vigia de tempo, go, despachante |
| `janteiros/planner.py` | Leitura do brief pelo modelo, validada em código contra o texto |
| `janteiros/team.py` | Estado compartilhado, ajuda do Supervisor (dicas/respostas) e construção das missões |
| `janteiros/specialist.py` | O loop observar → decidir → agir → verificar → registrar, com o vigia |
| `janteiros/tools.py` | Ferramentas do modelo e o auditor que transforma chamadas em fatos |
| `janteiros/notebook.py` | O caderno: fatos crus com origem e estado de verificação |
| `janteiros/engine.py` | Motor de regras: função pura caderno → resposta, planilha e prontidão |
| `janteiros/dispatcher.py` | Despachante: configurador, TAN e assinatura |
| `janteiros/browser/reader.js` | Leitor anti-isca: DOM de todos os frames → texto com elementos numerados |
| `janteiros/browser/porteiro.js`, `kit.py` | Porteiro (login, cookies, erro, pop-up, portão) e ações |
| `janteiros/manuals/*.md` | Manuais por tipo de site e de armadilhas (padrões, nunca valores) |
| `tests/` | Testes do motor com um cenário inventado |
| `scripts/dev_mission.py` | Roda uma missão sozinha contra os sites de scouting |
| `scripts/lint_fairplay.py` | Procura no pacote valores do cenário público (lista fora do pacote) |

## Rodar localmente

```sh
uv sync
atira login                        # uma vez
atira models proxy                 # em outro terminal: Qwen e Clef locais, grátis
eval "$(atira scout --challenge rfq --env)"   # logins de scouting + brief
uv run python agent.py             # run completo; saídas em runs/<data>/
JANTEIROS_HEADFUL=1 uv run python agent.py    # vendo os navegadores
uv run python scripts/dev_mission.py tender   # uma missão só (tender, supplier, mail-approvals, ...)
uv run pytest -q                   # testes do motor
```

Cada run deixa em `runs/<data>/`: `events.jsonl` (cada passo, chamada de modelo e intervenção),
`notebook.jsonl` (o caderno), `answer.json`, `worksheet.json` (como cada número foi calculado) e `summary.json`.

## Testar no sandbox

```sh
uv run python scripts/lint_fairplay.py          # antes de cada envio
atira test-run . --challenge rfq                 # run de teste com nota por campo
```

## Fair play

O pacote não contém nenhuma referência, número de material, preço, desconto, login, TAN, URL ou rótulo do
cenário público. O que o scouting ensinou virou padrão nos manuais, nunca passo fixo. O linter compara o pacote
com uma lista de valores mantida em `local/` (fora do git e do pacote).
