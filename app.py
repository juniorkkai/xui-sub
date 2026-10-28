#!/usr/bin/env python3
"""xui-sub - subscription transformer for 3x-ui.

Serves HTTPS /sub/<subId>:
  * Clash/Mihomo UAs get a full profile (proxies pulled from 3x-ui + local template)
  * every other UA gets the raw subscription body (base64 link list) unchanged

No database access, no panel API, no cache. Every request fetches the real
subscription from the local 3x-ui subscription server.
"""

from __future__ import annotations

import argparse
import hmac
import json
import logging
import os
import re
import signal
import ssl
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPSHandler, ProxyHandler, Request, build_opener

import tomllib
import yaml
from jinja2 import Environment, FileSystemLoader, StrictUndefined, TemplateError

__version__ = "1.1.0"

LOG = logging.getLogger("xui-sub")

TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{4,128}$")
CLASH_UA_RE = re.compile(r"(?i)(clash|mihomo)")

# UA used towards 3x-ui. /mihomo/ forces YAML; a non-Clash UA forces base64.
CLASH_UA = "clash-verge/v2.4.9"
RAW_UA = "v2rayN/6.31"

PASS_HEADERS = {
    "subscription-userinfo": "Subscription-Userinfo",
    "profile-update-interval": "Profile-Update-Interval",
    "profile-title": "Profile-Title",
    "support-url": "Support-Url",
    "profile-web-page-url": "Profile-Web-Page-Url",
    "announce": "Announce",
}


# --------------------------------------------------------------------------- #
# configuration
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ServerCfg:
    listen: str
    port: int
    cert: str
    key: str
    path_prefix: str
    cert_check_interval: int
    request_timeout: int


@dataclass(frozen=True)
class UpstreamCfg:
    base: str
    clash_path: str
    raw_path: str
    timeout: float
    verify_tls: bool
    host: str
    max_body: int
    max_concurrency: int


@dataclass(frozen=True)
class TemplateCfg:
    dir: str
    clash: str


@dataclass(frozen=True)
class BrandingCfg:
    title: str
    update_interval: int


@dataclass(frozen=True)
class SecurityCfg:
    allowed_tokens: tuple[str, ...]


@dataclass(frozen=True)
class Config:
    path: Path
    server: ServerCfg
    upstream: UpstreamCfg
    templates: TemplateCfg
    branding: BrandingCfg
    security: SecurityCfg


def _expect(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _table(data: dict, key: str) -> dict:
    value = data.get(key, {})
    _expect(isinstance(value, dict), f"[{key}] must be a table")
    return value


def _str(data: dict, key: str, default: str) -> str:
    value = data.get(key, default)
    _expect(isinstance(value, str), f"{key} must be a string")
    return value


def _int(data: dict, key: str, default: int) -> int:
    value = data.get(key, default)
    _expect(isinstance(value, int) and not isinstance(value, bool), f"{key} must be an integer")
    return value


def _float(data: dict, key: str, default: float) -> float:
    value = data.get(key, default)
    _expect(isinstance(value, (int, float)) and not isinstance(value, bool), f"{key} must be a number")
    return float(value)


def _bool(data: dict, key: str, default: bool) -> bool:
    value = data.get(key, default)
    _expect(isinstance(value, bool), f"{key} must be a boolean")
    return value


def _str_list(data: dict, key: str) -> list[str]:
    value = data.get(key, [])
    _expect(isinstance(value, list), f"{key} must be an array")
    result = []
    for item in value:
        _expect(isinstance(item, str) and bool(item), f"{key} entries must be non-empty strings")
        result.append(item)
    return result


def load_config(path: Path) -> Config:
    with path.open("rb") as fh:
        data = tomllib.load(fh)

    server = _table(data, "server")
    upstream = _table(data, "upstream")
    templates = _table(data, "templates")
    branding = _table(data, "branding")
    security = _table(data, "security")

    prefix = _str(server, "path_prefix", "/sub/")
    if not prefix.startswith("/"):
        prefix = "/" + prefix
    if not prefix.endswith("/"):
        prefix += "/"

    cfg = Config(
        path=path,
        server=ServerCfg(
            listen=_str(server, "listen", "0.0.0.0"),
            port=_int(server, "port", 443),
            cert=_str(server, "cert", "/root/cert/ip/fullchain.pem"),
            key=_str(server, "key", "/root/cert/ip/privkey.pem"),
            path_prefix=prefix,
            cert_check_interval=_int(server, "cert_check_interval", 300),
            request_timeout=_int(server, "request_timeout", 15),
        ),
        upstream=UpstreamCfg(
            base=_str(upstream, "base", "https://127.0.0.1:3770").rstrip("/"),
            clash_path=_str(upstream, "clash_path", "/mihomo/{sub_id}"),
            raw_path=_str(upstream, "raw_path", ""),
            timeout=_float(upstream, "timeout", 6.0),
            verify_tls=_bool(upstream, "verify_tls", False),
            host=_str(upstream, "host", ""),
            max_body=_int(upstream, "max_body", 2 * 1024 * 1024),
            max_concurrency=_int(upstream, "max_concurrency", 8),
        ),
        templates=TemplateCfg(
            dir=_str(templates, "dir", "/etc/xui-sub/templates"),
            clash=_str(templates, "clash", "clash.yaml.j2"),
        ),
        branding=BrandingCfg(
            title=_str(branding, "title", ""),
            update_interval=_int(branding, "update_interval", 12),
        ),
        security=SecurityCfg(
            allowed_tokens=tuple(_str_list(security, "allowed_tokens")),
        ),
    )
    _validate_config(cfg)
    return cfg


def _validate_config(cfg: Config) -> None:
    _expect(1 <= cfg.server.port <= 65535, "server.port out of range")
    _expect(bool(cfg.server.cert) and bool(cfg.server.key), "server.cert and server.key are required")
    _expect(Path(cfg.server.cert).is_file(), f"certificate not found: {cfg.server.cert}")
    _expect(Path(cfg.server.key).is_file(), f"private key not found: {cfg.server.key}")
    _expect(cfg.server.cert_check_interval >= 5, "server.cert_check_interval must be >= 5")
    _expect(cfg.server.request_timeout >= 1, "server.request_timeout must be >= 1")
    _expect(cfg.upstream.base.startswith(("http://", "https://")), "upstream.base must be http(s)")
    _expect("{sub_id}" in cfg.upstream.clash_path, "upstream.clash_path must contain {sub_id}")
    _expect("{sub_id}" in cfg.upstream.raw_path, "upstream.raw_path must contain {sub_id}")
    _expect(cfg.upstream.timeout > 0, "upstream.timeout must be > 0")
    _expect(cfg.upstream.max_body >= 1024, "upstream.max_body is too small")
    _expect(1 <= cfg.upstream.max_concurrency <= 64, "upstream.max_concurrency must be 1..64")
    _expect(Path(cfg.templates.dir).is_dir(), f"template dir not found: {cfg.templates.dir}")
    _expect(
        (Path(cfg.templates.dir) / cfg.templates.clash).is_file(),
        f"clash template not found: {cfg.templates.clash}",
    )
    _expect(cfg.branding.update_interval >= 1, "branding.update_interval must be >= 1")
    for token in cfg.security.allowed_tokens:
        _expect(bool(TOKEN_RE.fullmatch(token)), f"invalid token in allowed_tokens: {token!r}")


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _redact(token: str) -> str:
    return token[:4] + "\u2026" if len(token) > 4 else "\u2026"


def _redact_path(path: str) -> str:
    path = urlsplit(path).path
    marker = "/sub/"
    if marker in path:
        before, _, after = path.partition(marker)
        token = after.split("/", 1)[0]
        return before + marker + _redact(token)
    return path


def _lower_headers(headers) -> dict[str, str]:
    if headers is None:
        return {}
    return {key.lower(): value.strip() for key, value in headers.items()}


def _header_safe(value: str) -> str:
    value = value.replace("\r", " ").replace("\n", " ").strip()
    try:
        value.encode("latin-1")
        return value
    except UnicodeEncodeError:
        return quote(value, safe="")


def _yaml_inline(value) -> str:
    return yaml.safe_dump(
        value,
        allow_unicode=True,
        default_flow_style=True,
        sort_keys=False,
        width=10**9,
    ).strip()


def _human_bytes(value: int | None) -> str:
    if value is None:
        return "\u4e0d\u9650"
    if value < 1024:
        return f"{value} B"
    number = float(value)
    for unit in ("KB", "MB", "GB", "TB"):
        number /= 1024.0
        if number < 1024.0:
            return f"{number:.2f} {unit}"
    return f"{number:.2f} PB"


LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "::1", "[::1]"}


def classify_ua(ua: str) -> str:
    return "clash" if CLASH_UA_RE.search(ua or "") else "raw"


def build_env(template_dir: str) -> Environment:
    env = Environment(
        loader=FileSystemLoader(template_dir),
        autoescape=False,
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
        auto_reload=True,
    )
    env.filters["yaml_inline"] = _yaml_inline
    env.filters["jsonstr"] = lambda value: json.dumps(value, ensure_ascii=False)
    return env


def parse_userinfo(value: str | None) -> dict:
    usage = {
        "present": False,
        "upload": 0,
        "download": 0,
        "total": 0,
        "expire": 0,
        "used": 0,
        "remaining": None,
        "percent": None,
        "unlimited": True,
        "upload_h": "0 B",
        "download_h": "0 B",
        "total_h": "\u4e0d\u9650",
        "used_h": "0 B",
        "remaining_h": "\u4e0d\u9650",
        "expire_h": "\u4e0d\u9650",
        "expire_iso": "",
    }
    if not value:
        return usage

    raw: dict[str, str] = {}
    for part in value.split(";"):
        if "=" not in part:
            continue
        key, val = part.split("=", 1)
        raw[key.strip().lower()] = val.strip()

    if not any(key in raw for key in ("upload", "download", "total", "expire")):
        LOG.warning("invalid Subscription-Userinfo header: %r", value)
        return usage

    try:
        upload = int(raw.get("upload", "0"))
        download = int(raw.get("download", "0"))
        total = int(raw.get("total", "0"))
        expire = int(raw.get("expire", "0"))
    except ValueError:
        LOG.warning("invalid Subscription-Userinfo header: %r", value)
        return usage

    used = upload + download
    remaining = max(total - used, 0) if total > 0 else None
    percent = round(used / total * 100, 1) if total > 0 else None
    expire_iso = ""
    if expire > 0:
        expire_iso = (
            datetime.fromtimestamp(expire, tz=timezone.utc)
            .astimezone()
            .strftime("%Y-%m-%d %H:%M")
        )

    usage.update(
        present=True,
        upload=upload,
        download=download,
        total=total,
        expire=expire,
        used=used,
        remaining=remaining,
        percent=percent,
        unlimited=(total == 0),
        upload_h=_human_bytes(upload),
        download_h=_human_bytes(download),
        total_h=_human_bytes(total) if total > 0 else "\u4e0d\u9650",
        used_h=_human_bytes(used),
        remaining_h=_human_bytes(remaining) if remaining is not None else "\u4e0d\u9650",
        expire_h=expire_iso or "\u4e0d\u9650",
        expire_iso=expire_iso,
    )
    return usage


def _error(status: int, message: str) -> tuple[int, bytes, dict[str, str]]:
    return status, (message + "\n").encode("utf-8"), {
        "Content-Type": "text/plain; charset=utf-8",
        "Cache-Control": "no-store",
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
    }


class UpstreamError(Exception):
    """The 3x-ui subscription server could not be used."""


# --------------------------------------------------------------------------- #
# TLS certificate hot reload
# --------------------------------------------------------------------------- #


class CertReloader:
    def __init__(self, cert: str, key: str, interval: int):
        self.cert = cert
        self.key = key
        self.interval = interval
        self._lock = threading.Lock()
        self._ctx: ssl.SSLContext | None = None
        self._stamp: tuple | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.reload(initial=True)

    def _current_stamp(self) -> tuple:
        stamps = []
        for path in (self.cert, self.key):
            stat = os.stat(path)
            stamps.append((stat.st_mtime_ns, stat.st_size))
        return tuple(stamps)

    def _build(self) -> ssl.SSLContext:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.set_alpn_protocols(["http/1.1"])
        ctx.load_cert_chain(self.cert, self.key)
        return ctx

    def reload(self, initial: bool = False) -> None:
        with self._lock:
            try:
                stamp = self._current_stamp()
                ctx = self._build()
            except Exception:
                if initial:
                    raise
                LOG.exception("TLS certificate reload failed, keeping the current one")
                return
            self._ctx = ctx
            self._stamp = stamp
            LOG.info("TLS certificate loaded from %s", self.cert)

    @property
    def context(self) -> ssl.SSLContext:
        ctx = self._ctx
        if ctx is None:  # pragma: no cover - only before the initial load
            raise RuntimeError("TLS context is not loaded")
        return ctx

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, name="cert-reloader", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                if self._current_stamp() != self._stamp:
                    self.reload()
            except OSError:
                LOG.warning("TLS certificate files are missing, keeping the current one")

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)


# --------------------------------------------------------------------------- #
# application
# --------------------------------------------------------------------------- #


class App:
    def __init__(self, config_path: Path):
        self._lock = threading.RLock()
        self.config_path = config_path
        self.config = load_config(config_path)
        self._config_stamp = self._stat_stamp(config_path)
        self.env = build_env(self.config.templates.dir)
        self.upstream_ssl = self._build_upstream_ssl(self.config)
        self.opener = self._build_opener()
        self.cert = CertReloader(
            self.config.server.cert,
            self.config.server.key,
            self.config.server.cert_check_interval,
        )
        self.sem = threading.BoundedSemaphore(self.config.upstream.max_concurrency)

    @staticmethod
    def _stat_stamp(path: Path) -> tuple | None:
        try:
            stat = path.stat()
            return stat.st_mtime_ns, stat.st_size
        except OSError:
            return None

    @staticmethod
    def _build_upstream_ssl(cfg: Config) -> ssl.SSLContext:
        if cfg.upstream.verify_tls:
            return ssl.create_default_context()
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx

    def _build_opener(self):
        return build_opener(ProxyHandler({}), HTTPSHandler(context=self.upstream_ssl))

    def reload_config(self, force: bool = False) -> None:
        stamp = self._stat_stamp(self.config_path)
        with self._lock:
            if not force and stamp == self._config_stamp:
                return
            try:
                new = load_config(self.config_path)
            except Exception as exc:
                LOG.error("config reload failed, keeping the previous config: %s", exc)
                self._config_stamp = stamp
                return

            old = self.config
            if (new.server.listen, new.server.port, new.server.cert, new.server.key) != (
                old.server.listen,
                old.server.port,
                old.server.cert,
                old.server.key,
            ):
                LOG.warning("listen/port/certificate changes require a service restart")
            if new.server.cert_check_interval != old.server.cert_check_interval:
                LOG.warning("cert_check_interval changes require a service restart")
            if new.upstream.max_concurrency != old.upstream.max_concurrency:
                LOG.warning("upstream.max_concurrency changes require a service restart")
            if new.templates.dir != old.templates.dir:
                self.env = build_env(new.templates.dir)
            if new.upstream.verify_tls != old.upstream.verify_tls:
                self.upstream_ssl = self._build_upstream_ssl(new)
                self.opener = self._build_opener()

            self.config = new
            self._config_stamp = stamp
            LOG.info("config reloaded from %s", self.config_path)

    @staticmethod
    def _token_allowed(token: str, allowed: tuple[str, ...]) -> bool:
        encoded = token.encode("utf-8")
        for item in allowed:
            if hmac.compare_digest(encoded, item.encode("utf-8")):
                return True
        return False

    # ---- request handling ------------------------------------------------- #

    def handle(self, raw_path: str, headers) -> tuple[int, bytes, dict[str, str]]:
        self.reload_config()
        cfg = self.config
        path = urlsplit(raw_path).path
        prefix = cfg.server.path_prefix
        if not path.startswith(prefix):
            return _error(404, "not found")

        token = path[len(prefix):].rstrip("/")
        if not TOKEN_RE.fullmatch(token):
            return _error(404, "not found")
        if cfg.security.allowed_tokens and not self._token_allowed(token, cfg.security.allowed_tokens):
            return _error(404, "not found")

        ua = headers.get("User-Agent", "") if headers is not None else ""
        if classify_ua(ua) == "clash":
            return self._handle_clash(token, cfg)
        return self._handle_raw(token, cfg)

    def _handle_clash(self, token: str, cfg: Config) -> tuple[int, bytes, dict[str, str]]:
        url = cfg.upstream.base + cfg.upstream.clash_path.replace("{sub_id}", quote(token, safe=""))
        try:
            status, upstream_headers, body = self._fetch(url, CLASH_UA, cfg)
        except UpstreamError as exc:
            LOG.warning("clash upstream failed for %s: %s", _redact(token), exc)
            return _error(502, "bad gateway")

        if status == 404:
            return _error(404, "not found")
        if status != 200:
            LOG.warning("clash upstream returned %s for %s", status, _redact(token))
            return _error(502, "bad gateway")

        try:
            document = yaml.safe_load(body)
        except (yaml.YAMLError, UnicodeDecodeError, ValueError) as exc:
            LOG.warning("invalid upstream yaml for %s: %s", _redact(token), exc)
            return _error(502, "bad gateway")

        if not isinstance(document, dict):
            LOG.warning("upstream yaml for %s is not a mapping", _redact(token))
            return _error(502, "bad gateway")

        raw_proxies = document.get("proxies")
        if not isinstance(raw_proxies, list):
            LOG.warning("upstream yaml for %s has no proxies list", _redact(token))
            return _error(502, "bad gateway")

        proxies = []
        name_counts: dict[str, int] = {}
        for item in raw_proxies:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            proxy_type = item.get("type")
            if not isinstance(name, str) or not name.strip():
                continue
            if not isinstance(proxy_type, str) or not proxy_type.strip():
                continue
            name_counts[name] = name_counts.get(name, 0) + 1
            if name_counts[name] > 1:
                item = dict(item)
                item["name"] = f"{name} #{name_counts[name]}"
            proxies.append(item)

        if not proxies:
            return _error(404, "not found")

        usage = parse_userinfo(upstream_headers.get("subscription-userinfo"))
        try:
            text = self.render_clash(proxies, usage, token, cfg)
        except TemplateError:
            LOG.exception("failed to render clash template")
            return _error(500, "internal error")

        return 200, text.encode("utf-8"), self._response_headers(
            upstream_headers, "application/yaml; charset=utf-8", cfg
        )

    def _handle_raw(self, token: str, cfg: Config) -> tuple[int, bytes, dict[str, str]]:
        url = cfg.upstream.base + cfg.upstream.raw_path.replace("{sub_id}", quote(token, safe=""))
        try:
            status, upstream_headers, body = self._fetch(url, RAW_UA, cfg)
        except UpstreamError as exc:
            LOG.warning("raw upstream failed for %s: %s", _redact(token), exc)
            return _error(502, "bad gateway")

        if status == 404:
            return _error(404, "not found")
        if status != 200:
            LOG.warning("raw upstream returned %s for %s", status, _redact(token))
            return _error(502, "bad gateway")

        stripped = body.lstrip()
        if not stripped:
            return _error(404, "not found")
        if stripped.startswith(b"<"):
            LOG.warning("raw upstream returned html for %s", _redact(token))
            return _error(502, "bad gateway")
        if stripped.startswith(b"proxies:"):
            LOG.warning("raw upstream returned yaml for %s", _redact(token))
            return _error(502, "bad gateway")

        return 200, body, self._response_headers(
            upstream_headers, "text/plain; charset=utf-8", cfg
        )

    def _fetch(self, url: str, ua: str, cfg: Config) -> tuple[int, dict[str, str], bytes]:
        if not self.sem.acquire(timeout=max(1.0, cfg.upstream.timeout)):
            raise UpstreamError("too many concurrent upstream requests")
        try:
            request = Request(
                url,
                headers={
                    "User-Agent": ua,
                    "Accept": "*/*",
                    "Connection": "close",
                },
            )
            if cfg.upstream.host:
                request.add_header("Host", cfg.upstream.host)
            try:
                with self.opener.open(request, timeout=cfg.upstream.timeout) as response:
                    body = response.read(cfg.upstream.max_body + 1)
                    if len(body) > cfg.upstream.max_body:
                        raise UpstreamError("upstream body is too large")
                    return response.status, _lower_headers(response.headers), body
            except HTTPError as exc:
                body = exc.read(cfg.upstream.max_body) or b""
                return exc.code, _lower_headers(exc.headers), body
            except (URLError, OSError, ValueError) as exc:
                raise UpstreamError(f"upstream request failed: {exc}") from exc
        finally:
            self.sem.release()

    def render_clash(
        self,
        proxies: list[dict],
        usage: dict,
        token: str,
        cfg: Config,
    ) -> str:
        template = self.env.get_template(cfg.templates.clash)
        return template.render(
            proxies=proxies,
            usage=usage,
            title=cfg.branding.title,
            generated_at=datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %z"),
            update_interval=cfg.branding.update_interval,
            sub_id=token,
        )

    @staticmethod
    def _response_headers(
        upstream_headers: dict[str, str],
        content_type: str,
        cfg: Config,
    ) -> dict[str, str]:
        headers = {
            "Content-Type": content_type,
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
        }
        for key, name in PASS_HEADERS.items():
            value = upstream_headers.get(key)
            if value:
                headers[name] = _header_safe(value)
        if "Profile-Update-Interval" not in headers:
            headers["Profile-Update-Interval"] = str(cfg.branding.update_interval)
        if cfg.branding.title and "Profile-Title" not in headers:
            headers["Profile-Title"] = _header_safe(cfg.branding.title)
        return headers

    # ---- startup check ---------------------------------------------------- #

    def check(self) -> None:
        cfg = self.config
        if not cfg.upstream.host or cfg.upstream.host.strip().lower() in LOOPBACK_HOSTS:
            print(
                "WARN: upstream.host is empty or loopback; "
                "3x-ui will generate 'localhost' node addresses"
            )
        dummy = [
            {
                "name": "test-node",
                "type": "vless",
                "server": "127.0.0.1",
                "port": 443,
                "uuid": "00000000-0000-0000-0000-000000000000",
                "network": "tcp",
                "tls": True,
                "udp": True,
                "servername": "example.com",
                "client-fingerprint": "chrome",
                "reality-opts": {"public-key": "test", "short-id": "0123"},
            }
        ]
        usage = parse_userinfo("upload=0; download=0; total=0; expire=0")
        text = self.render_clash(dummy, usage, "testtoken", cfg)
        document = yaml.safe_load(text)
        _expect(isinstance(document, dict), "rendered config is not a YAML mapping")
        _expect(isinstance(document.get("proxies"), list) and document["proxies"], "no proxies rendered")
        _expect(
            isinstance(document.get("proxy-groups"), list) and document["proxy-groups"],
            "no proxy-groups rendered",
        )
        _expect(isinstance(document.get("rules"), list) and document["rules"], "no rules rendered")
        print(
            "OK: %d proxy, %d groups, %d rules; listen=%s:%d; cert=%s; template=%s"
            % (
                len(document["proxies"]),
                len(document["proxy-groups"]),
                len(document["rules"]),
                cfg.server.listen,
                cfg.server.port,
                cfg.server.cert,
                cfg.templates.clash,
            )
        )


# --------------------------------------------------------------------------- #
# HTTP server
# --------------------------------------------------------------------------- #


class SubServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 64

    def __init__(self, address, handler, app: App):
        self.app = app
        super().__init__(address, handler)

    def get_request(self):
        sock, address = super().get_request()
        try:
            sock.settimeout(self.app.config.server.request_timeout)
            return self.app.cert.context.wrap_socket(sock, server_side=True), address
        except Exception:
            sock.close()
            raise

    def handle_error(self, request, client_address):
        LOG.debug("connection error from %s: %s", client_address[0], sys.exc_info()[1])


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "xui-sub"
    sys_version = ""
    timeout = 20

    def version_string(self) -> str:
        return self.server_version

    def do_GET(self) -> None:
        self._serve(head=False)

    def do_HEAD(self) -> None:
        self._serve(head=True)

    def _method_not_allowed(self) -> None:
        body = b"method not allowed\n"
        self._send(
            405,
            body,
            {
                "Content-Type": "text/plain; charset=utf-8",
                "Allow": "GET, HEAD",
                "Cache-Control": "no-store",
            },
            head=False,
        )

    do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _method_not_allowed

    def _serve(self, head: bool) -> None:
        started = time.monotonic()
        try:
            status, body, headers = self.server.app.handle(self.path, self.headers)
        except Exception:
            LOG.exception("unhandled error for %s", _redact_path(self.path))
            status, body, headers = _error(500, "internal error")

        self._send(status, body, headers, head)
        LOG.info(
            "%s %s %s %s %dB %.0fms",
            self.client_address[0],
            status,
            classify_ua(self.headers.get("User-Agent", "")),
            _redact_path(self.path),
            len(body),
            (time.monotonic() - started) * 1000.0,
        )

    def _send(self, status: int, body: bytes, headers: dict[str, str], head: bool) -> None:
        try:
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if not head:
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, fmt: str, *args) -> None:
        LOG.debug("%s - %s", self.client_address[0], fmt % args)


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="xui-sub subscription transformer")
    parser.add_argument("-c", "--config", default="/etc/xui-sub/config.toml")
    parser.add_argument("--check", action="store_true", help="validate config, certificate and template, then exit")
    parser.add_argument("--version", action="version", version=f"xui-sub {__version__}")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=os.environ.get("XUI_SUB_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(message)s",
    )

    try:
        app = App(Path(args.config).resolve())
    except Exception as exc:
        LOG.error("cannot load configuration: %s", exc)
        return 1

    if args.check:
        try:
            app.check()
        except Exception as exc:
            LOG.error("check failed: %s", exc)
            return 1
        return 0

    if not app.config.upstream.host or app.config.upstream.host.strip().lower() in LOOPBACK_HOSTS:
        LOG.warning(
            "upstream.host is empty or loopback; "
            "3x-ui may generate 'localhost' node addresses"
        )

    try:
        server = SubServer((app.config.server.listen, app.config.server.port), Handler, app)
    except OSError as exc:
        LOG.error(
            "cannot listen on %s:%s: %s",
            app.config.server.listen,
            app.config.server.port,
            exc,
        )
        return 1

    app.cert.start()

    def shutdown(signum, frame) -> None:
        LOG.info("received signal %s, shutting down", signum)
        threading.Thread(target=server.shutdown, daemon=True).start()

    def reload(signum, frame) -> None:
        LOG.info("received SIGHUP, reloading configuration and certificate")
        threading.Thread(target=reload_worker, daemon=True).start()

    def reload_worker() -> None:
        app.reload_config(force=True)
        app.cert.reload()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, reload)

    LOG.info(
        "xui-sub %s listening on %s:%s (prefix %s)",
        __version__,
        app.config.server.listen,
        app.config.server.port,
        app.config.server.path_prefix,
    )
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        LOG.info("stopping")
        app.cert.stop()
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
