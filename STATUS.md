# STATUS

Estado operacional detalhado (contas, IDs, links de posts reais) fica em `STATUS.local.md`, fora do Git.

## Funciona (verificado com publicações reais)

| Rede | Formatos validados em produção |
|---|---|
| Instagram | Feed, Story |
| Facebook (Página) | Post com imagem |
| YouTube | Short (vídeo agregado) |
| TikTok | Integração implementada; aguardando aprovação do app (sandbox) |
| Canva | OAuth e leitura de templates OK; autofill exige plano Pro (renderizador local em uso) |

- Pipeline completo: entrada → validação → dedup → score → copy por rede → arte → vídeo → publicação → status.
- Roteamento de grupos WhatsApp determinístico e testado; travas de Página e de canal.
- 110+ testes automatizados.

## Para outro agente continuar

1. Leia `STATUS.local.md` (máquina de operação) para o estado das contas.
2. `scripts\status.ps1` mostra o serviço; `data\logs
ikki.jsonl` tem os eventos com correlation id.
