# Operação

## Ligar / desligar / status

```powershell
scripts\start.ps1     # supervisor oculto; reinicia o servidor se cair (backoff até 5 min)
scripts\stop.ps1
scripts\status.ps1    # JSON de /api/status + link do painel
scripts\install-autostart.ps1   # opcional: inicia ao logar no Windows (atalho em shell:startup)
```

Logs: `data\logs\nikki.jsonl` (eventos com `cid`), `data\logs\server.err.log`, `data\logs\supervisor.log`.

## Trocar o grupo do Instagram para a reserva

No `.env`: `NIKKI_INSTAGRAM_ACTIVE_GROUP=reserve` → `scripts\stop.ps1; scripts\start.ps1`.
Nada mais muda. Conteúdo já publicado com `/go/instagram` (quando `NIKKI_PUBLIC_BASE_URL` está definido)
passa a apontar para a reserva automaticamente.

## Reprocessar falhas

Painel → botão **retry** no job, ou **reprocessar falhas** na página da promoção. API: `POST /api/jobs/{id}/retry`.

## Ajustar regras

`config/rules.yaml`: pesos do score, faixas (skip/low/medium/high) e formatos por rede, janela de dedup,
espaçamento entre posts, tentativas e backoff, horários dos vídeos agregados. Reinicie após editar.

## Desligar uma rede

`NIKKI_NETWORKS=instagram,facebook` (sem tiktok/youtube) e reinicie.

## Modo QA

`NIKKI_QA_MODE=true` prefixa as legendas com "[Teste da operação da Nikki]". Deixe `false` na operação normal.

## Tokens que expiram

- YouTube: enquanto o app Google estiver em "Teste", reautorize a cada 7 dias em `/oauth/google/start`
  (some ao publicar o app no Google Auth Platform).
- TikTok: refresh automático (token de 24h, refresh de 365 dias).
- Meta: token de Página derivado de token longo não expira (expira se a senha mudar ou o app perder acesso).
