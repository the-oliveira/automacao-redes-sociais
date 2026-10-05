# Decisões

- **D-01 Publicação via APIs diretas, não Metricool (2026-10-04).** A conta Metricool está num plano
  sem API (Configurações › API pede upgrade). O dono escolheu APIs oficiais: Meta Graph (IG+FB), YouTube Data API
  v3, TikTok Content Posting. O cliente Metricool (`nikki/integrations/metricool.py`, baseado no swagger oficial,
  com upload S3) continua no código: `NIKKI_PUBLISHER=metricool` reativa quando houver plano com API.
- **D-02 Facebook só na Página.** Toda escrita usa token de Página e caminho iniciado pelo ID da Página/IG; `/me`
  e o ID pessoal são bloqueados antes da chamada; a Página é escolhida por ID ou nome "nikki", nunca "a primeira".
- **D-03 YouTube só no canal da Nikki.** Canal verificado no OAuth e antes de cada upload (`channels?mine=true`).
- **D-04 Imagens do Instagram hospedadas como foto não publicada da Página** (URL do CDN do Facebook), evitando
  hospedagem de terceiros. Vídeos vão por upload resumível direto do arquivo.
- **D-05 Fila no SQLite**, worker em thread no mesmo processo. Sem Redis/Celery. Jobs independentes por rede/formato.
- **D-06 Publicação direta acontece no horário do slot** (o worker só executa jobs vencidos); espaçamento mínimo
  por rede em `config/rules.yaml`.
- **D-07 Arte nunca contém URL de grupo** (é compartilhada entre redes); o grupo só aparece na legenda, gerada
  pelo roteador determinístico e verificada por `assert_text_routes_correctly`.
- **D-08 Canva com fallback local.** Autofill usa `create_from_design` (cria design novo; template mestre intacto).
  Se o Canva falhar, a arte local (mesmos campos) é usada e o evento `canva_failed` é registrado.
- **D-09 Preço "de" nunca é inventado.** Sem `original_price`, a arte mostra só o % (se informado) ou "OFERTA".
- **D-10 TikTok sem URL na legenda por padrão** (`NIKKI_TIKTOK_URL_IN_CAPTION=false`): CTA "link no perfil".
- **D-11 IA fora das regras críticas.** Copy usa variação determinística de headlines/CTAs; nenhuma IA decide grupo.
