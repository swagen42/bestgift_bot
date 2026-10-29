from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit
from urllib.request import getproxies, proxy_bypass

from httpx import create_ssl_context
from httpx_socks import AsyncProxyTransport
from telegram.request import HTTPXRequest


def create_request(*, polling: bool = False) -> HTTPXRequest:
    """Учитывает системный прокси, включая SOCKS4, и исключения NO_PROXY."""
    proxies = getproxies()
    proxy = proxies.get("https") or proxies.get("all")
    if proxy_bypass("api.telegram.org"):
        proxy = None
    # Сохраняем SSL_CERT_FILE/SSL_CERT_DIR, но выбираем маршрут сами: HTTPX
    # иначе разбирает даже неиспользуемые SOCKS4 URL в других переменных.
    ssl_context = create_ssl_context()
    options = {"trust_env": False, "verify": ssl_context}
    if proxy:
        parsed = urlsplit(proxy)
        if parsed.scheme in {"socks4", "socks4a", "socks5", "socks5h"}:
            # HTTPX сам не поддерживает SOCKS4. Отдельный transport также исключает
            # повторную автоматическую обработку того же адреса внутри HTTPX.
            scheme = {"socks4a": "socks4", "socks5h": "socks5"}.get(parsed.scheme, parsed.scheme)
            normalized = urlunsplit(parsed._replace(scheme=scheme))
            transport = AsyncProxyTransport.from_url(normalized, rdns=True, verify=ssl_context)
            options["transport"] = transport
        else:
            options["proxy"] = proxy
    return HTTPXRequest(connection_pool_size=1 if polling else 256, httpx_kwargs=options)


async def check_network_configuration() -> None:
    """Создает и закрывает клиенты без HTTP-запросов, проверяя настройки прокси."""
    request = create_request()
    try:
        polling_request = create_request(polling=True)
        await polling_request.shutdown()
    finally:
        await request.shutdown()
