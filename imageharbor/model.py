"""Strict, bounded input. App records contain secret REFERENCES, never values."""
from __future__ import annotations

import ipaddress
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit


class HarborError(Exception):
    """Safe user-facing error; never include raw subprocess output or input values."""


NAME = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*\Z")
ENV = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
SENSITIVE = re.compile(r"SECRET|PASSWORD|PASSWD|TOKEN|API_?KEY|PRIVATE_?KEY|CREDENTIAL", re.I)
DOMAIN = re.compile(r"(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
PATH = re.compile(r"/(?:[A-Za-z0-9_~-]+(?:/[A-Za-z0-9_~-]+)*)?\Z")
IMAGE = re.compile(r"(?:docker\.io/)?((?:[a-z0-9]+(?:[._-][a-z0-9]+)*/)?[a-z0-9]+(?:[._-][a-z0-9]+)*)(?::([A-Za-z0-9_][A-Za-z0-9_.-]{0,127}))?(?:@sha256:([a-f0-9]{64}))?\Z")


def obj(value, allowed, required=()):
    if not isinstance(value, dict) or set(value) - set(allowed) or set(required) - set(value):
        raise HarborError("Invalid object fields; consult the documented schema")
    return value


def name(value, max_len=40):
    if not isinstance(value, str) or len(value) > max_len or not NAME.fullmatch(value):
        raise HarborError("Invalid resource identifier")
    return value


def integer(value, low, high):
    if type(value) is not int or not low <= value <= high:
        raise HarborError("Integer outside allowed range")
    return value


def boolean(value):
    if type(value) is not bool:
        raise HarborError("Expected a boolean")
    return value


def string(value, limit=1024):
    if not isinstance(value, str) or len(value) > limit or any(ord(c) < 32 for c in value):
        raise HarborError("Invalid or oversized text")
    return value


def domain(value):
    if not isinstance(value, str) or not DOMAIN.fullmatch(value.lower()):
        raise HarborError("Expected an exact DNS hostname; wildcards are unsupported")
    return value.lower()


def path(value):
    if not isinstance(value, str) or not PATH.fullmatch(value) or "//" in value:
        raise HarborError("Expected a normalized path without query, escapes, or traversal")
    return value


def sequence(value, limit=16):
    if not isinstance(value, list) or len(value) > limit:
        raise HarborError("Invalid or oversized list")
    return value


def networks(value):
    try:
        return [str(ipaddress.ip_network(v, strict=True)) for v in sequence(value)]
    except (ValueError, TypeError):
        raise HarborError("Invalid CIDR") from None


def safe_url(value):
    try:
        return urlsplit(string(value, 500))
    except ValueError:
        raise HarborError("Malformed URL") from None


@dataclass
class Config:
    instance: str = "lab"
    base_domain: str = "apps.example.test"
    backend: str = "fake"
    state_dir: Path = field(default_factory=lambda: Path(".local"))
    kube_context: str | None = None
    kubeconfig: str | None = None
    routing_enabled: bool = False
    route_cidrs: list[str] = field(default_factory=list)
    allow_public_routes: bool = False
    allow_http: bool = False
    require_digest: bool = True
    allow_volume_delete: bool = False
    log_safe_apps: list[str] = field(default_factory=list)
    proxy_namespace: str = "imageharbor-proxy"
    storage_class: str = "local-path"

    @classmethod
    def load(cls, filename):
        source = Path(filename).resolve()
        try:
            data = json.loads(source.read_text())
        except (OSError, ValueError):
            raise HarborError("Cannot read instance JSON configuration") from None
        obj(data, cls.__dataclass_fields__)
        cfg = cls(**data)
        name(cfg.instance, 12)
        cfg.base_domain = domain(cfg.base_domain)
        if cfg.backend not in ("fake", "kubectl"):
            raise HarborError("Unsupported backend")
        cfg.state_dir = (source.parent / string(data.get("state_dir", ".local"), 4096)).resolve()
        for flag in (cfg.routing_enabled, cfg.allow_public_routes, cfg.allow_http,
                     cfg.require_digest, cfg.allow_volume_delete):
            boolean(flag)
        cfg.route_cidrs = networks(cfg.route_cidrs)
        if cfg.routing_enabled and not cfg.route_cidrs:
            raise HarborError("Routing requires an explicit source CIDR allowlist")
        if not cfg.allow_public_routes:
            for cidr in cfg.route_cidrs:
                net = ipaddress.ip_network(cidr)
                if not net.is_private or net.prefixlen == 0:
                    raise HarborError("Public routes require explicit allow_public_routes")
        name(cfg.proxy_namespace, 63)
        name(cfg.storage_class, 63)
        for app in sequence(cfg.log_safe_apps, 100):
            name(app)
        if cfg.backend == "kubectl" and (not cfg.kube_context or not cfg.kubeconfig):
            raise HarborError("Real backend requires explicit kube_context and kubeconfig")
        if cfg.kube_context:
            string(cfg.kube_context, 200)
        if cfg.kubeconfig:
            cfg.kubeconfig = str((source.parent / string(cfg.kubeconfig, 4096)).resolve())
        return cfg

    def namespace(self, app):
        return f"ih-{self.instance}-{name(app)}"


def validate_spec(raw, cfg: Config):
    obj(raw, ("id", "purpose", "image", "port", "repo", "env", "secrets", "volumes",
              "routes", "path_support", "canonical_url", "tls_secret", "resources",
              "egress_cidrs", "uid", "image_pull_secrets"), ("id", "purpose", "image", "port"))
    spec = {"id": name(raw["id"]), "purpose": string(raw["purpose"], 500),
            "port": integer(raw["port"], 1024, 65535)}
    match = IMAGE.fullmatch(raw["image"]) if isinstance(raw["image"], str) else None
    if not match or not (match[2] or match[3]) or (match[2] or "").lower() in {"latest", "main", "master", "dev", "edge", "stable"}:
        raise HarborError("Use a versioned Docker Hub image, preferably an immutable sha256 digest")
    if cfg.require_digest and not match[3]:
        raise HarborError("This instance requires images pinned by digest")
    repository = match[1] if "/" in match[1] else "library/" + match[1]
    spec["image"] = "docker.io/" + repository + (":" + match[2] if match[2] else "") + ("@sha256:" + match[3] if match[3] else "")
    repo = raw.get("repo")
    if repo is not None:
        parsed = safe_url(repo)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise HarborError("Repository metadata must be an HTTPS URL without credentials or query")
    spec["repo"] = repo
    env = raw.get("env", {})
    if not isinstance(env, dict) or len(env) > 64:
        raise HarborError("Invalid environment map")
    for key, val in env.items():
        if not isinstance(key, str) or not ENV.fullmatch(key) or SENSITIVE.search(key):
            raise HarborError("Sensitive environment names must use secret references")
        text = string(val, 4096)
        if re.search(r"://[^/\s]+:[^/\s]+@|-----BEGIN .*PRIVATE KEY", text):
            raise HarborError("Credential-bearing values must use secret references")
    spec["env"] = dict(env)
    secrets = []
    for ref in sequence(raw.get("secrets", []), 64):
        obj(ref, ("env", "name", "key"), ("env", "name", "key"))
        if not isinstance(ref["env"], str) or not ENV.fullmatch(ref["env"]):
            raise HarborError("Invalid secret environment identifier")
        name(ref["name"], 63)
        if not isinstance(ref["key"], str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,253}", ref["key"]):
            raise HarborError("Invalid secret key reference")
        if ref["env"] in env or any(s["env"] == ref["env"] for s in secrets):
            raise HarborError("Duplicate environment identifier")
        secrets.append(dict(ref))
    spec["secrets"] = secrets
    volumes = []
    for vol in sequence(raw.get("volumes", []), 8):
        obj(vol, ("name", "mount_path", "size_gi"), ("name", "mount_path", "size_gi"))
        name(vol["name"], 40)
        path(vol["mount_path"])
        integer(vol["size_gi"], 1, 20)
        if vol["name"] == "tmp" or overlaps(vol["mount_path"], "/tmp") or any(v["name"] == vol["name"] or overlaps(v["mount_path"], vol["mount_path"]) for v in volumes):
            raise HarborError("Duplicate volume or invalid mount point")
        volumes.append(dict(vol))
    spec["volumes"] = volumes
    spec["path_support"] = boolean(raw.get("path_support", False))
    routes = []
    for route in sequence(raw.get("routes", [])):
        obj(route, ("host", "subdomain", "path", "prefix"), ("path", "prefix"))
        if ("host" in route) == ("subdomain" in route):
            raise HarborError("Route needs exactly one of host or subdomain")
        host = domain(route["host"]) if "host" in route else domain(name(route["subdomain"], 63) + "." + cfg.base_domain)
        prefix = route["prefix"]
        if prefix not in ("strip", "preserve"):
            raise HarborError("Prefix policy must be strip or preserve")
        route_path = path(route["path"])
        if route_path != "/" and not spec["path_support"]:
            raise HarborError("Non-root routes require explicit path_support")
        if any(r["host"] == host and overlaps(r["path"], route_path) for r in routes):
            raise HarborError("Routes within one application overlap")
        routes.append({"host": host, "path": route_path, "prefix": prefix})
    spec["routes"] = routes
    canonical = raw.get("canonical_url")
    if routes and canonical is None:
        scheme = "http" if cfg.allow_http and not raw.get("tls_secret") else "https"
        canonical = scheme + "://" + routes[0]["host"] + routes[0]["path"]
    if canonical is not None:
        url = safe_url(canonical)
        if url.scheme not in (("http", "https") if cfg.allow_http else ("https",)) or url.netloc != url.hostname or url.query or url.fragment:
            raise HarborError("Invalid canonical URL")
        route_path = url.path or "/"
        if not any(r["host"] == url.hostname and r["path"] == route_path for r in routes):
            raise HarborError("Canonical URL must identify one configured route")
    spec["canonical_url"] = canonical
    tls = raw.get("tls_secret")
    if tls is not None:
        name(tls, 63)
    if cfg.routing_enabled and routes and not cfg.allow_http and not tls:
        raise HarborError("Enabled HTTPS routes require a namespace-local TLS secret reference")
    if cfg.routing_enabled and canonical is not None and url.scheme != ("https" if tls else "http"):
        raise HarborError("Canonical URL scheme must match the configured route transport")
    spec["tls_secret"] = tls
    resources = raw.get("resources", {})
    obj(resources, ("cpu_m", "memory_mi"))
    spec["resources"] = {"cpu_m": integer(resources.get("cpu_m", 100), 10, 2000),
                         "memory_mi": integer(resources.get("memory_mi", 128), 32, 2048)}
    spec["egress_cidrs"] = networks(raw.get("egress_cidrs", []))
    spec["uid"] = integer(raw.get("uid", 10001), 1, 65534)
    spec["image_pull_secrets"] = [name(v, 63) for v in sequence(raw.get("image_pull_secrets", []), 4)]
    if len(json.dumps(spec).encode()) > 32_000:
        raise HarborError("Application metadata exceeds 32 KB")
    return spec


def overlaps(first, second):
    return first == "/" or second == "/" or first == second or first.startswith(second + "/") or second.startswith(first + "/")
