# Roteamento WhatsApp

Fonte única: `config/whatsapp_groups.yaml`. Nenhuma URL de grupo existe em outro arquivo de código.

| Rede | Grupo |
|---|---|
| Instagram (primary) | `…/EXAMPLEinstagramPRIMARY01` |
| Instagram (reserve) | `…/EXAMPLEinstagramRESERVE01` |
| Facebook | `…/EXAMPLEfacebookPRIMARY001` |
| TikTok | `…/EXAMPLEtiktokPRIMARY00001` |
| YouTube | `…/EXAMPLEyoutubePRIMARY0001` |

Regras (todas cobertas por `tests/test_routing.py`):

1. `destination_for(rede)` é determinístico; Instagram usa `NIKKI_INSTAGRAM_ACTIVE_GROUP` (primary|reserve).
2. Toda legenda passa por `assert_text_routes_correctly`: falha se citar grupo de outra rede ou a reserva inativa.
3. A arte nunca contém URL de grupo.
4. O carregamento recusa a config se o mesmo grupo aparecer em duas redes.
5. `/go/<rede>` redireciona (302) para o grupo ativo e conta cliques. Só entra nas legendas quando
   `NIKKI_PUBLIC_BASE_URL` aponta para um endereço público.
