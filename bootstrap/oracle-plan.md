# Bootstrap posterior, bajo autorización específica

Nada de este documento se ejecutó en Oracle. No contiene IPs, usuarios, claves o
IDs de tenancy. Antes de instalar, decidir una instancia/cluster dedicados o un
plan explícito de coexistencia, presupuesto de disco y backups. No migrar ni detener servicios existentes para hacer lugar. El bootstrap no requiere builds en
Oracle ni expone el MCP públicamente.

## Entorno

Para laboratorio local opcional: Docker + k3d + kubectl + Helm, al menos 2 GiB de
RAM disponibles para el nodo y varios GB de disco libres para runtime e imágenes.
`sh bootstrap/local.sh --create-local` rechaza reutilizar el nombre o sobrescribir
configs, exporta kubeconfig sólo a `personal/` y usa API loopback sin puertos de
apps publicados. Un fallo deja el cluster recién creado visible para diagnóstico;
no ejecuta cleanup automático. No borrar ese cluster cuando contenga datos
necesarios: los volúmenes están dentro del nodo Docker.

Para Oracle posterior: Linux ARM64/x86-64 compatible con K3s, RAM y disco medidos,
acceso administrativo sólo durante instalación; luego usuario de control plane
con grants revisados. Preferible 4 GiB RAM para OS + K3s + apps pequeñas. Elegir
la arquitectura antes de pinnear digests. Pin verificado de K3s:
`v1.35.9+k3s1`, release del 30-09-2026;
[release notes](https://docs.k3s.io/release-notes/v1.35.X). Una instalación nueva
puede seguir el [quick start oficial](https://docs.k3s.io/quick-start), con flags
`--disable=traefik --disable=servicelb`; no usar el instalador en un host ya
gestionado sin revisar sus efectos. El bootstrap local fija el equivalente Docker
`rancher/k3s:v1.35.9-k3s1`.

Mantener API Kubernetes inaccesible desde Internet y firewall/security lists
cerradas. La definición de networking de la instancia depende del entorno y no
se configura aquí. No abrir 80/443/6443 ni cambiar grants automáticamente. K3s
ServiceLB publica puertos del host si se habilita: por eso se deshabilita.
[Networking K3s](https://docs.k3s.io/networking/networking-services).

## Instalar proxy después de crear el cluster

Revisar [traefik-values.yaml](traefik-values.yaml) y el RBAC renderizado del chart.
Comandos de ejemplo, con archivos/contextos decididos por el operador:

```sh
helm template imageharbor-traefik traefik \
  --repo https://traefik.github.io/charts --version 41.6.1 \
  --namespace imageharbor-proxy --values bootstrap/traefik-values.yaml

# Mutación, sólo después de autorizarla:
helm upgrade --install imageharbor-traefik traefik \
  --repo https://traefik.github.io/charts --version 41.6.1 \
  --namespace imageharbor-proxy --create-namespace \
  --kubeconfig personal/kubeconfig --kube-context CONTEXTO_EXPLICITO \
  --values bootstrap/traefik-values.yaml --wait --timeout 180s
```

El chart instala sus CRDs/RBAC. El proxy atiende sólo Services internos. Revisar
por separado [controller-rbac.yaml](controller-rbac.yaml) y proporcionar un
kubeconfig de grants reducidos mediante un flujo autorizado. No pasar el
kubeconfig administrador a un agente. TLS y pull secrets son namespace-locales y
no los crea el control plane. Configurar instancia en `personal/`, fuera del
código: copiar/adaptar [examples/instance.kubernetes.json](../examples/instance.kubernetes.json)
y verificar explícitamente el contexto antes de un deploy real.

## Primer flujo real posterior

1. Crear namespace con labels/pod security obtenidos del `render` y preparar
   Secrets por el mecanismo autorizado si la app los necesita.
2. CI externo construye/pushea la imagen y entrega un digest inmutable; esta
   sesión no publica nada. Escribir el digest real en una spec personal. El repo
   sigue siendo metadata; Oracle sólo descarga la imagen.
3. `python3 -m imageharbor --config personal/instance.json render personal/app.json`
   permite revisión offline. `render` no incluye la metadata release y **no es un
   sustituto de deploy**: los recursos aplicados a mano no forman un release
   inventariado. Usar sólo la CLI/MCP para la aplicación completa.
4. Autorizar y ejecutar deploy. Verificar `status`, PVC Bound, `observed_image`,
   Pods Running/readiness y límites. El API ack no significa app sana.
5. Con rutas aún deshabilitadas, probar funcionalmente desde un port-forward
   autorizado y enlazado a loopback:
   `kubectl --kubeconfig personal/kubeconfig --context CONTEXTO_EXPLICITO -n ih-personal-APP port-forward --address 127.0.0.1 service/app 18080:8080`.
   Adjustar APP/puerto al spec. Esto requiere permisos del operador de port-forward,
   deliberadamente ausentes en el role del controller.
6. Si corresponde, habilitar rutas privadas con source CIDRs y Secret TLS,
   probar hostnames/path y X-Forwarded-Prefix; probar un cliente no permitido.
   No dar por verificados assets/OAuth/cookies sin prueba funcional de la app.
7. Probar update de imagen, rollback de spec y remove; verificar que el UID/datos
   del PVC siguen. Respaldar antes de probar cualquier borrado de datos. Nunca
   borrar namespace para simular remove.
8. Correr reconcile --watch en el host con un solo state_dir escritor y luego
   integrar un harness real sólo con el contrato de capacidades revisado.

La identidad de namespace aplica al instance ID: cambiar ese ID crea otra
identidad, no migra datos. Mover la configuración/state entre hosts sin coordinar
los escritores no es un mecanismo de failover.

## CI de imágenes fuera de Oracle

La aplicación, en su propio repo opcional, mantiene Dockerfile y pipeline. Ejemplo
conceptual que el owner ejecutará únicamente después de autorizar credenciales y
publicación:

```sh
docker buildx build --platform linux/amd64,linux/arm64 \
  --tag docker.io/ORGANIZACION/APP:git-COMMIT_SHA --push .
```

Guardar el digest emitido por el build en el artefacto del pipeline. El control
plane consume `docker.io/ORGANIZACION/APP@sha256:DIGEST_REAL`. No crea registry,
repo, credenciales CI, robots Docker Hub ni grants en OCI. El workflow incluido en
este proyecto sólo corre tests del control plane.
