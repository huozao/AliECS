#!/usr/bin/env python3
"""Forward Docker-host traffic to the ECS-local WebDock reverse SSH tunnel."""

from __future__ import annotations

import os
import socket
import threading
from dataclasses import dataclass


@dataclass(frozen=True)
class ProxyConfig:
    bind_host: str = "172.17.0.1"
    bind_port: int = 11800
    target_host: str = "127.0.0.1"
    target_port: int = 11800
    backlog: int = 64


def _int_from_env(env: dict[str, str], key: str, default: int) -> int:
    raw = env.get(key, "").strip()
    if not raw:
        return default
    value = int(raw)
    if value <= 0:
        raise ValueError(f"{key} must be positive")
    return value


def parse_config(env: dict[str, str] | None = None) -> ProxyConfig:
    source = os.environ if env is None else env
    return ProxyConfig(
        bind_host=source.get("WEBDOCK_PROXY_BIND_HOST", ProxyConfig.bind_host).strip()
        or ProxyConfig.bind_host,
        bind_port=_int_from_env(source, "WEBDOCK_PROXY_BIND_PORT", ProxyConfig.bind_port),
        target_host=source.get("WEBDOCK_PROXY_TARGET_HOST", ProxyConfig.target_host).strip()
        or ProxyConfig.target_host,
        target_port=_int_from_env(source, "WEBDOCK_PROXY_TARGET_PORT", ProxyConfig.target_port),
        backlog=_int_from_env(source, "WEBDOCK_PROXY_BACKLOG", ProxyConfig.backlog),
    )


def _relay(src: socket.socket, dst: socket.socket) -> None:
    try:
        while True:
            chunk = src.recv(65536)
            if not chunk:
                break
            dst.sendall(chunk)
    except OSError:
        pass
    finally:
        for sock in (src, dst):
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def handle_client(client: socket.socket, config: ProxyConfig) -> None:
    with client:
        with socket.create_connection((config.target_host, config.target_port), timeout=10) as target:
            threads = [
                threading.Thread(target=_relay, args=(client, target), daemon=True),
                threading.Thread(target=_relay, args=(target, client), daemon=True),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()


def serve_forever(config: ProxyConfig) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((config.bind_host, config.bind_port))
        server.listen(config.backlog)
        print(
            "webdock-tunnel-proxy listening on "
            f"{config.bind_host}:{config.bind_port} -> "
            f"{config.target_host}:{config.target_port}",
            flush=True,
        )
        while True:
            client, _ = server.accept()
            threading.Thread(target=handle_client, args=(client, config), daemon=True).start()


def main() -> None:
    serve_forever(parse_config())


if __name__ == "__main__":
    main()
