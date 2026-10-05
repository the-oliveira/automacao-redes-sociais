# Regras de conteúdo

## Score (0–100) — `config/rules.yaml › score`

Componentes 0..1 ponderados: desconto (curva até 50%), preço (economia em R$ + faixa de impulso), relevância
(categoria + palavras de hype), popularidade (entrada opcional), cupom, confiabilidade (loja, link de afiliado,
preço "de" presente), imagem, potencial de conteúdo, histórico de preços (gancho, peso 0 até haver dados).
Desconto isolado não basta: "pano de prato 60% OFF" pontua abaixo de "Booster Pokémon 30% OFF com cupom".

## Distribuição

| Score | Tier | Formatos |
|---|---|---|
| < 60 | skip | nenhum (fica registrado como SKIPPED) |
| 60–74 | low | Facebook post, Instagram story |
| 75–89 | medium | Instagram feed + story, Facebook post |
| 90+ | high | + Instagram reel, TikTok vídeo, YouTube Short |

Agregados ("5 achadinhos de hoje", "TCG", "gamer/geek", "quedas de preço"…) viram vídeo para IG Reel, TikTok,
YouTube Short e FB Reel nos horários de `aggregate.schedule`; a mesma promoção não se repete no mesmo tipo.

## Persona

Nikki: jovem, geek/gamer, casual, direta, sem fingir experiência pessoal com o produto. Headlines e CTAs variam
de forma determinística por promoção/rede. Toda legenda termina com o aviso de que preço/estoque podem mudar.

| Rede | Estilo | CTA |
|---|---|---|
| Instagram | visual, hashtags | "link na bio" + URL do grupo IG ativo |
| Facebook | link direto da oferta | grupo FB |
| TikTok | gancho curto, linguagem natural | "link no perfil" (sem URL por padrão) |
| YouTube | título ≤100 com #shorts, descrição com links, tags | grupo YT |

## Arte

Feed 1080×1350, story 1080×1920. Safe areas do story: 250px no topo e 320px na base. "R$" e o valor são medidos
separadamente e o valor encolhe até caber (validado de R$ 9,90 a R$ 12.999,90). O card do produto encolhe quando
o texto precisa de espaço; o produto nunca é cortado (contain).
