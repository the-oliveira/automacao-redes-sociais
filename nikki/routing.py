"""Roteamento determinístico rede -> grupo de WhatsApp.

Regra absoluta: cada rede divulga SOMENTE o próprio grupo. Nenhuma IA participa
desta decisão. Todas as URLs vêm de config/whatsapp_groups.yaml.
"""

from __future__ import annotations

from dataclasses import dataclass

from nikki.settings import Settings, get_settings

NETWORKS = ("instagram", "facebook", "tiktok", "youtube")


class RoutingError(RuntimeError):
    pass


@dataclass(frozen=True)
class GroupDestination:
    network: str
    role: str  # primary | reserve
    whatsapp_url: str
    go_url: str | None  # /go/<rede> quando houver URL pública

    @property
    def key(self) -> str:
        return f"{self.network}:{self.role}"

    @property
    def cta_url(self) -> str:
        """URL que vai no conteúdo: redirecionador próprio se houver, senão o grupo."""
        return self.go_url or self.whatsapp_url


def all_group_urls(settings: Settings | None = None) -> dict[str, str]:
    """Mapa 'rede:papel' -> url, para auditoria e testes de vazamento."""
    s = settings or get_settings()
    return {f"{net}:{role}": url for net, roles in s.whatsapp_groups().items() for role, url in roles.items()}


def destination_for(network: str, settings: Settings | None = None) -> GroupDestination:
    s = settings or get_settings()
    network = network.lower().strip()
    if network not in NETWORKS:
        raise RoutingError(f"rede desconhecida para roteamento: {network!r}")
    groups = s.whatsapp_groups()
    roles = groups.get(network)
    if not roles:
        raise RoutingError(f"sem grupo configurado para {network}")
    role = s.instagram_active_group if network == "instagram" else "primary"
    url = roles.get(role)
    if not url:
        raise RoutingError(f"grupo {network}:{role} não configurado")
    _assert_unique(groups)
    go = f"{s.public_base_url.rstrip('/')}/go/{network}" if s.public_base_url else None
    return GroupDestination(network=network, role=role, whatsapp_url=url, go_url=go)


def foreign_urls(network: str, settings: Settings | None = None) -> list[str]:
    """URLs de grupos que NÃO podem aparecer em conteúdo desta rede."""
    s = settings or get_settings()
    return [url for net, roles in s.whatsapp_groups().items() if net != network for url in roles.values()]


def assert_text_routes_correctly(network: str, text: str, settings: Settings | None = None) -> None:
    """Falha se o texto citar grupo de outra rede (ou o grupo inativo do Instagram)."""
    s = settings or get_settings()
    dest = destination_for(network, s)
    forbidden = set(foreign_urls(network, s))
    if network == "instagram":
        forbidden |= {u for r, u in s.whatsapp_groups()["instagram"].items() if r != dest.role}
    leaked = [u for u in forbidden if _code(u) in text]
    if leaked:
        raise RoutingError(f"conteúdo de {network} cita grupo errado: {leaked}")


def _code(url: str) -> str:
    return url.rstrip("/").rsplit("/", 1)[-1]


def _assert_unique(groups: dict[str, dict[str, str]]) -> None:
    urls = [u for roles in groups.values() for u in roles.values()]
    if len(urls) != len(set(urls)):
        raise RoutingError("config/whatsapp_groups.yaml tem o mesmo grupo em mais de uma rede")
