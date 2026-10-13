"""Fixed Kubernetes resource surface; no user-supplied YAML, commands or host mounts."""
from .backend import API

MANAGED = "app.kubernetes.io/managed-by"
INSTANCE = "imageharbor.io/instance"
APP = "imageharbor.io/app"
ROLE = "imageharbor.io/role"


def labels(cfg, app):
    return {MANAGED: "imageharbor", INSTANCE: cfg.instance, APP: app}


def resource(cfg, app, kind, resource_name, payload=None, role="runtime"):
    meta = {"name": resource_name, "labels": {**labels(cfg, app), ROLE: role}}
    if kind != "Namespace":
        meta["namespace"] = cfg.namespace(app)
    return {"apiVersion": API[kind][0], "kind": kind, "metadata": meta, **(payload or {})}


def render(cfg, spec, revision):
    app = spec["id"]
    ns = resource(cfg, app, "Namespace", cfg.namespace(app), role="identity")
    ns["metadata"]["labels"].update({"pod-security.kubernetes.io/enforce": "restricted",
                                    "pod-security.kubernetes.io/enforce-version": "v1.35"})
    result = [ns]
    for vol in spec["volumes"]:
        result.append(resource(cfg, app, "PersistentVolumeClaim", "data-" + vol["name"], {
            "spec": {"accessModes": ["ReadWriteOnce"], "storageClassName": cfg.storage_class,
                     "resources": {"requests": {"storage": f"{vol['size_gi']}Gi"}}}}, role="data"))
    result.append(resource(cfg, app, "Service", "app", {"spec": {
        "type": "ClusterIP", "selector": labels(cfg, app),
        "ports": [{"name": "http", "port": spec["port"], "targetPort": "http", "protocol": "TCP"}]}}))
    egress = [{"to": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "kube-system"}},
                       "podSelector": {"matchLabels": {"k8s-app": "kube-dns"}}}],
               "ports": [{"protocol": "UDP", "port": 53}, {"protocol": "TCP", "port": 53}]}]
    egress.extend({"to": [{"ipBlock": {"cidr": cidr}}]} for cidr in spec["egress_cidrs"])
    result.append(resource(cfg, app, "NetworkPolicy", "app", {"spec": {
        "podSelector": {}, "policyTypes": ["Ingress", "Egress"], "egress": egress,
        "ingress": [{"from": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": cfg.proxy_namespace}},
                               "podSelector": {"matchLabels": {"app.kubernetes.io/name": "traefik"}}}],
                     "ports": [{"protocol": "TCP", "port": spec["port"]}]}]}}, role="policy"))
    container = {"name": "app", "image": spec["image"], "imagePullPolicy": "IfNotPresent",
                 "ports": [{"name": "http", "containerPort": spec["port"], "protocol": "TCP"}],
                 "env": [{"name": k, "value": v} for k, v in sorted(spec["env"].items())] + [
                     {"name": ref["env"], "valueFrom": {"secretKeyRef": {"name": ref["name"], "key": ref["key"], "optional": False}}} for ref in spec["secrets"]],
                 "resources": {"requests": {"cpu": f"{spec['resources']['cpu_m']}m", "memory": f"{spec['resources']['memory_mi']}Mi"},
                               "limits": {"cpu": f"{spec['resources']['cpu_m']}m", "memory": f"{spec['resources']['memory_mi']}Mi"}},
                 "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                                     "capabilities": {"drop": ["ALL"]}},
                 "readinessProbe": {"tcpSocket": {"port": "http"}, "periodSeconds": 5},
                 "startupProbe": {"tcpSocket": {"port": "http"}, "periodSeconds": 5, "failureThreshold": 30},
                 "volumeMounts": [{"name": "tmp", "mountPath": "/tmp"}] + [
                     {"name": v["name"], "mountPath": v["mount_path"]} for v in spec["volumes"]]}
    result.append(resource(cfg, app, "Deployment", "app", {"spec": {
        "replicas": 1, "revisionHistoryLimit": 3, "progressDeadlineSeconds": 180,
        "strategy": {"type": "Recreate"}, "selector": {"matchLabels": labels(cfg, app)},
        "template": {"metadata": {"labels": labels(cfg, app), "annotations": {"imageharbor.io/revision": str(revision)}},
                     "spec": {"automountServiceAccountToken": False, "enableServiceLinks": False,
                              "securityContext": {"runAsNonRoot": True, "runAsUser": spec["uid"], "runAsGroup": spec["uid"],
                                                  "fsGroup": spec["uid"], "seccompProfile": {"type": "RuntimeDefault"}},
                              "imagePullSecrets": [{"name": n} for n in spec["image_pull_secrets"]],
                              "containers": [container], "volumes": [{"name": "tmp", "emptyDir": {"sizeLimit": "64Mi"}}] + [
                                  {"name": v["name"], "persistentVolumeClaim": {"claimName": "data-" + v["name"]}} for v in spec["volumes"]]}}}}))
    if cfg.routing_enabled and spec["routes"]:
        result.append(resource(cfg, app, "Middleware", "source-allowlist", {"spec": {"ipAllowList": {"sourceRange": cfg.route_cidrs}}}))
        routes = []
        for idx, route in enumerate(spec["routes"]):
            p = route["path"]
            rule = f"Host(`{route['host']}`)"
            if p != "/":
                rule += f" && (Path(`{p}`) || PathPrefix(`{p}/`))"
            middlewares = [{"name": "source-allowlist"}]
            if route["prefix"] == "strip" and p != "/":
                middleware = "strip-" + str(idx)
                result.append(resource(cfg, app, "Middleware", middleware, {"spec": {"stripPrefix": {"prefixes": [p]}}}))
                middlewares.append({"name": middleware})
            routes.append({"kind": "Rule", "match": rule, "priority": 100 + len(p),
                           "middlewares": middlewares, "services": [{"name": "app", "port": spec["port"], "kind": "Service"}]})
        ingress = {"entryPoints": ["websecure" if spec["tls_secret"] else "web"], "routes": routes}
        if spec["tls_secret"]:
            ingress["tls"] = {"secretName": spec["tls_secret"]}
        result.append(resource(cfg, app, "IngressRoute", "app", {"spec": ingress}))
    return result
