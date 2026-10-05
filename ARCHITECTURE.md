# Arquitetura

Um processo Python (FastAPI + thread de worker), SQLite com WAL, sem filas externas.

```
POST /api/promotions ─▶ intake (validação → normalização → dedup → persistência)  [no request, sem rede]
                               │ status QUEUED
                               ▼
worker (a cada 5s) ─▶ score → tier/canais → copy por rede → mídia (Canva | local) → jobs (1 por rede/formato)
                               │
                               ▼
worker ─▶ jobs vencidos → Publisher (direct | metricool | dry_run) → PUBLISHED / RETRYING / FAILED
       ─▶ sync de status (a cada 5 min) • agregados nos horários de config/rules.yaml
```

## Módulos

| Módulo | Papel |
|---|---|
| `nikki/settings.py` | `.env` (segredos/toggles) + `config/*.yaml` (regras) |
| `nikki/routing.py` | rede → grupo WhatsApp, determinístico; guarda anti-vazamento |
| `nikki/pipeline/intake.py` | contrato `PromotionIn`, validação, normalização, deduplicação/idempotência |
| `nikki/pipeline/scoring.py` | score 0–100 com pesos configuráveis; gancho de histórico de preços |
| `nikki/pipeline/copy.py` | persona + adaptadores `Instagram/Facebook/TikTok/YouTubeContentAdapter`; agregados |
| `nikki/media/` | Canva autofill (com fallback) + renderizador local + vídeo ffmpeg |
| `nikki/pipeline/orchestrator.py` | fila no banco, slots por rede, retry com backoff, falha parcial, sync |
| `nikki/publishing.py` | `DirectPublisher`, `MetricoolPublisher`, `DryRunPublisher` |
| `nikki/integrations/` | clientes Meta, YouTube, TikTok, Canva, Metricool; tokens OAuth no banco |
| `nikki/web/` | API, `/go/<rede>`, OAuth callbacks, painel |
| `nikki/analytics.py` | métricas reais por rede (sem estimativas) |

## Estados

Promoção: `QUEUED → CONTENT_READY → MEDIA_READY → SCHEDULING → SCHEDULED | PUBLISHED | PARTIAL_FAILURE | RETRYING | FAILED`
(ou `REJECTED`, `SKIPPED`). Job: `PENDING → SCHEDULED | PUBLISHED | DRY_RUN`, ou `RETRYING → FAILED`.

## Tabelas

`promotions`, `content_variants`, `media_assets`, `publication_jobs`, `publication_attempts`,
`channel_destinations`, `system_events`, `aggregate_posts`, `analytics_snapshots`, `kv` (tokens OAuth/estado).
Tipos portáveis (JSON, DateTime) — trocar `NIKKI_DATABASE_URL` para PostgreSQL funciona sem mudar código.
