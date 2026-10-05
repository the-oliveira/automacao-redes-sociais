# Integrações

## Entrada: `POST /api/promotions`

Header opcional `X-API-Key` (se `NIKKI_API_KEY` estiver definido). Aceita um objeto ou uma lista.

```json
{
  "external_id": "abc-123", "product_name": "Controle DualSense", "original_price": 449.90, "current_price": 329.00,
  "discount_percent": 27, "coupon": null, "product_url": "https://www.amazon.com.br/dp/B0BQXZ11B8",
  "affiliate_url": "https://...", "image_url": "https://...jpg", "store": "amazon", "category": "games",
  "sku": null, "popularity": 0.8, "received_at": "2026-10-05T09:00:00-03:00"
}
```

Resposta `202`: `{"promotion_id", "status": QUEUED|REJECTED|DUPLICATE, "correlation_id", "reason", "duplicate_of"}`.
`422` para JSON fora do contrato. Status de uma promoção: `GET /api/promotions/{id}`.

## Manda Boa

Não há webhook/API pública documentada acessível nesta sessão. O sistema funciona independente dele: qualquer
origem (Make, n8n, script, export) só precisa fazer `POST /api/promotions` no formato acima. Quando houver
acesso ao Manda Boa, a prioridade é webhook → API → Make/n8n apontando para este endpoint.
Para receber webhooks da internet, exponha o servidor (túnel/domínio) e defina `NIKKI_API_KEY`.

## Publishers

| Rede | API | Formatos | Observações |
|---|---|---|---|
| Facebook | Graph `/{page}/photos`, `/photo_stories`, `/video_reels` | post, story, reel | Só Página (D-02) |
| Instagram | Graph `/{ig}/media` + `/media_publish`, rupload | feed, story, reel | conta Business ligada à Página; 100 posts/24h |
| YouTube | Data API v3, upload resumível | short | só canal Nikki (D-03); quota ~6 uploads/dia no padrão |
| TikTok | Content Posting `video/init` FILE_UPLOAD | video | app não auditado publica só `SELF_ONLY` |
| Metricool | `/v2/scheduler/posts` + S3 upload | todos | requer plano com API (desativado, D-01) |

## Canva

App Connect próprio (OAuth com PKCE). Escopos: asset r/w, design content r/w, design meta r,
brandtemplate r. Fluxo: upload da imagem do produto → `POST /v1/autofills` (`create_from_design` com os
templates de feed e story definidos no `.env`) → `POST /v1/exports` PNG → download. Campos: PRODUCT_IMAGE,
PRODUCT_NAME, OLD_PRICE, CURRENT_PRICE_VALUE, DISCOUNT_TEXT, COUPON_TEXT, CTA_TEXT.
Requer MFA ativo na conta Canva para liberar a REST API.

## Analytics

`POST /api/analytics/collect` ou `python -m nikki analytics`: Instagram (likes/comentários), Facebook
(reações/comentários/compartilhamentos), YouTube (views/likes/comentários), TikTok (views/likes/comentários/shares).
Só números devolvidos pelas APIs.
