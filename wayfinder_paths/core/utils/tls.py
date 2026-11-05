"""The same reviewed CA bundle for SDK HTTP and JSON-RPC transports."""

import ssl
from functools import cache

import certifi


@cache
def verified_ssl_context() -> ssl.SSLContext:
    # Supplying cafile avoids loading an older platform trust store alongside it.
    return ssl.create_default_context(cafile=certifi.where())
