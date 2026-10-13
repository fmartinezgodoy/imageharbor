# ImageHarbor — primera versión experimental

Plataforma personal image-first: una especificación JSON describe una aplicación y
la CLI/MCP registra su intención, crea recursos Kubernetes y permite observar y
reconciliar su estado. Python 3.11+ y biblioteca estándar; no hay dependencias de
runtime ni builds en Oracle. El repositorio es metadata opcional. Licencia MIT.

El backend por defecto es una simulación persistida. El adaptador Kubernetes
usa `kubectl` real, con contexto/kubeconfig explícitos; sus contratos se prueban
contra una API temporal en localhost. El runtime Kubernetes, el almacenamiento
físico y el proxy todavía requieren validación en un cluster real autorizado.
El código no instala infraestructura ni modifica servicios existentes al iniciar.

## Probar ahora, sin Docker ni instalaciones

Desde la raíz de una copia local del repositorio:

```sh
python3 -m unittest discover -v
python3 -m examples.demo

python3 -m imageharbor deploy examples/hello.json
python3 -m imageharbor update examples/hello-v2.json
python3 -m imageharbor rollback hello --revision 1
python3 -m imageharbor status hello
python3 -m imageharbor logs hello --tail 30
python3 -m imageharbor inventory
python3 -m imageharbor remove hello
```

El demo usa un directorio temporal propio. La CLI usa `.local/fake/` y conserva
estado entre procesos. `ready: true` con `backend: fake` expresa readiness
**simulada**. Los ejemplos no contienen credenciales y `whoami` es solamente una
app de demostración: puede devolver información del entorno en su endpoint API;
no le inyectes secretos.

`deploy` reintentado con la misma especificación es idempotente. Para cambiar una
app activa usá `update` con una especificación completa, no un parche.
`deploy/update/rollback` confirman intención y operaciones API; no esperan que el
contenedor arranque. `status.ready`, `observed`, `observed_image` y `drift` muestran
el progreso real. Un fallo parcial deja metadata recuperable; ejecutá
`reconcile` después de resolver la causa.

## Modelo de aplicación

Ver [examples/hello.json](examples/hello.json). Campos:

| Campo | Comportamiento |
| --- | --- |
| `id`, `purpose` | Identidad estable y propósito. Namespace `ih-<instancia>-<id>`. |
| `image` | Docker Hub, tag explícito o digest SHA-256. Rechaza `latest` y tags flotantes conocidos. Instancias reales exigen digest por defecto. |
| `port` | Puerto HTTP interno 1024–65535; un Service ClusterIP por aplicación. |
| `repo` | URL HTTPS opcional, sin credenciales; nunca se clona ni construye. |
| `env` | Configuración no secreta. Valores string. Nombres sensibles y URLs con credenciales se rechazan. |
| `secrets` | Referencias `{env, name, key}` a Secrets preexistentes en el namespace de la app. No acepta sus valores ni lee/crea Secrets. |
| `image_pull_secrets` | Nombres de Secrets de registry preexistentes en ese namespace. |
| `volumes` | Hasta 8 PVC propios, `{name, mount_path, size_gi}`. 1–20 GiB solicitados; sin hostPath ni adopción de PVC ajenos. |
| `routes` | Hasta 16 combinaciones hostname/path hacia el mismo Service. `host` exacto o `subdomain` relativo al dominio base. |
| `path_support` | Consentimiento de compatibilidad de la app con un prefijo de path; necesario fuera de `/`. |
| `canonical_url` | URL fija del origen/ruta preferidos; debe coincidir con una ruta declarada. |
| `tls_secret` | Secret TLS local al namespace, necesario para habilitar rutas HTTPS. |
| `resources` | CPU 10–2000 millicores y memoria 32–2048 MiB; requests=limits. |
| `uid` | Usuario/grupo no root, por defecto 10001. La imagen debe ser compatible. |
| `egress_cidrs` | Destinos de salida explícitos; sin ellos sólo DNS del cluster. |

No hay comandos, args, YAML arbitrario, exec, builds, host mounts ni permisos
privilegiados en la especificación. Rootfs de sólo lectura, `/tmp` efímero de
64 MiB, capabilities vacías y token de ServiceAccount sin montar. Un volumen no
puede usar el nombre `tmp` ni cubrir `/tmp`; mounts superpuestos se rechazan.

La lint de `env` detecta errores comunes, no puede reconocer cualquier secreto
disfrazado como configuración. Es responsabilidad del operador usar referencias
para **todo** valor sensible. Metadata e historial son ConfigMaps, no una caja de
secretos. Los Secrets se preparan fuera de esta herramienta mediante un flujo
autorizado. Para preparar un namespace antes de cargar referencias se pueden
renderizar los manifiestos y revisar el Namespace; no hay provisioning automático
de credenciales.

## Hostnames, paths y origen fijo

Dominio base configurable por instancia (`base_domain`). `subdomain: hello`
produce `hello.<base_domain>`; `host` permite un dominio completo. No administra
DNS. Un cambio del dominio base afecta especificaciones nuevas; las revisiones
guardadas contienen hosts ya resueltos y no se reescriben silenciosamente.

`/foo` coincide con `/foo` y `/foo/...`, no con `/foobar`. `strip` elimina el
prefijo antes de enviar al backend; `preserve` mantiene el path. Se rechazan
wildcards, traversal, escapes, query strings y prefijos superpuestos para un
mismo host, incluso entre aplicaciones/instancias. No se resuelve un conflicto
con prioridades implícitas. `/` ocupa todo ese host. Routers ajenos ambiguos
producen un rechazo conservador. El bootstrap deshabilita los providers Ingress
estándar/Gateway; es para un cluster personal dedicado.

Quitar un prefijo no adapta una app automáticamente: redirects absolutos,
assets, WebSockets, cookies, OAuth callbacks y URLs de webhooks deben admitir un
base path. Apps que requieren origen fijo suelen funcionar mejor en un
subdominio con `/`. Configurá su propia variable de URL pública con
`canonical_url`. Los alias siguen llegando al mismo Service: esta versión **no
fuerza redirects canónicos** ni reescribe cookies/HTML. La URL canónica no abre
una ruta y no configura la app por sí sola. Traefik aporta `X-Forwarded-Prefix`
al usar StripPrefix; la aplicación debe aprovecharlo si corresponde.
[Documentación de StripPrefix](https://doc.traefik.io/traefik/reference/routing-configuration/http/middlewares/stripprefix/).

## Inventario y reconciliación

`ConfigMap/imageharbor-release` conserva spec deseada, estado `active/removed`,
revisión, últimas 20 revisiones y ledger de volúmenes. No depende de una tabla
manual de servicios. Labels de instancia/app/rol rastrean la propiedad en cada
recurso; UIDs y resourceVersions protegen las escrituras y borrados. Recursos
ajenos nunca se adoptan. El inventario expone propósito, imagen deseada/observada,
repo, rutas, recursos, readiness, PVC y drift; reporta propietarios sin metadata
registrada sin borrar sus recursos. Metadata corrupta se reporta por aplicación;
no impide consultar el estado de otra app válida.

```sh
python3 -m imageharbor reconcile
python3 -m imageharbor reconcile --watch --interval 5
```

El controlador hace LIST paginado por tipo, obtiene su resourceVersion y abre
WATCH acotados de Namespace, ConfigMap, Deployment, Service, Pod, PVC,
NetworkPolicy, IngressRoute y Middleware. Al terminar cada ronda reconcilia;
`410 Gone` fuerza relist. Reconexión/errores vuelven a una ronda LIST. La ronda
incluye resync aun sin eventos. No es una foto atómica entre tipos. Una demora
normal de observación es aproximadamente el intervalo de watch. Borrados y
desviaciones de recursos gestionados se reparan desde la intención. El controlador
debe estar corriendo para auto-reparar drift; las operaciones también reconcilian.
[Semántica LIST/WATCH](https://kubernetes.io/docs/reference/using-api/api-concepts/).

Un solo control plane escritor por instancia, compartiendo `state_dir`: lock
local entre CLI, MCP, worker y reconciliador; SQLite serializa claims de tareas;
Kubernetes create/replace usa concurrencia optimista y DELETE tiene precondiciones.
La metadata se compara contra la versión originalmente leída. `state_dir` queda
vinculado a instancia, backend y ubicación/contexto del kubeconfig; reutilizarlo
para otra configuración falla. Ese vínculo no detecta cambios internos de un
kubeconfig confiable: el operador debe mantener estable su destino.
No coordina escritores repartidos en varias máquinas. Los conflictos de rutas
entre instancias se detectan, pero instancias con escritores concurrentes no
tienen un lock distribuido: operá una sola instancia en este primer corte.

## Runtime, volúmenes e imágenes tienen ciclos distintos

`remove` guarda un tombstone y elimina Deployment, Service y rutas/middlewares
gestionados. Conserva namespace, metadata, NetworkPolicy, Secrets ajenos y PVC.
La policy pasa a deny-all para proteger Pods durante su terminación. El namespace no
se borra: borrarlo en Kubernetes también borraría sus PVC. Un volumen quitado de
la spec queda retenido. Re-deploy reutiliza el PVC de la misma identidad. Rollback
crea una revisión nueva de la spec anterior; **no revierte datos ni migraciones**.
PVC no tiene ownerReference hacia el Deployment. No se soporta resize de volumen.
El ledger guarda el UID del PVC: si un volumen registrado falta o fue reemplazado,
deploy/reconcile/rollback fallan sin crear datos vacíos ni adoptar el reemplazo.
OwnerReferences inesperados también bloquean operaciones de retención.

Para solicitar un borrado de datos, primero quitar la app y esperar que no queden
Deployment ni Pods. Opt-in del operador en configuración personal:
`allow_volume_delete: true`, luego:

```sh
python3 -m imageharbor --config personal/instance.json volumes-delete hello \
  --volumes state --confirm 'DELETE DATA hello'
```

Esto elimina sólo PVC del ledger de esa app. UID/resourceVersion impiden borrar
otro recurso si cambió entre lectura y borrado. Puede haber finalizers pendientes.
La intención de borrado se persiste antes de llamar a la API; los reintentos
requieren la misma elección explícita. Sólo al confirmar la desaparición se marca
`deleted`, habilitando una creación posterior vacía. Cualquier Pod o Deployment
en el namespace, incluso sin labels de la app, bloquea el borrado de datos.
El resultado del borrado físico depende de la reclaim policy del StorageClass/PV;
con `local-path` normalmente es `Delete`. Verificá backups y cleanup del backend
por separado. La herramienta no borra imágenes de Docker Hub ni ejecuta GC de
containerd; eliminar un Deployment no elimina sus imágenes. No se implementan
backup/restore ni snapshots de datos en esta versión.

## MCP y tareas para un agente

```sh
python3 -m imageharbor --config examples/instance.fake.json mcp
```

MCP stdio con JSON-RPC por línea: initialize, notifications/initialized,
tools/list, tools/call y ping. Operaciones directas acotadas: inventory, status,
deploy, update, rollback, logs, remove y volumes_delete. Este modo stdio no abre
listener; el adaptador HTTP opt-in se describe abajo. Sin shell/root ni API de Secrets. Ver
[docs/agent-and-security.md](docs/agent-and-security.md) para autenticación,
contrato y límites del harness.

Ahora también hay un perfil HTTP privado opt-in: loopback literal, bearer
efímero mediante pipe, sesiones acotadas y allowlist por app/operación. Default
restringido de sólo lectura; borrado de datos requiere permiso adicional. Stdio
admite la misma política con `--allow-app hello --access read`.

```sh
# Listener temporal y procesos reales; plataforma fake; sin token persistido:
python3 -m examples.remote_demo
# Sólo muestra un comando propuesto, no conecta:
python3 -m imageharbor ssh-plan --alias HOST_ALIAS \
  --remote-root /srv/imageharbor --allow-app hello
```

**No está conectado a ChatGPT.** El bearer privado no implementa OAuth MCP. La
conexión HTTPS/OAuth o Secure MCP Tunnel, incluyendo stdio detrás de un túnel
configurado, sigue pendiente. No se creó URL pública, túnel ni credencial
persistente. Diseño, ejecución y límites: [docs/remote-plan.md](docs/remote-plan.md).
[Conexión oficial](https://developers.openai.com/plugins/deploy/connect-chatgpt).

Interfaz asíncrona `submit / task_status / result / cancel`:

```sh
python3 -m imageharbor submit hello --kind diagnose --idempotency-key demo-001
# Usar el task_id que devuelve submit:
python3 -m imageharbor task-status TASK_UUID
python3 -m imageharbor worker --adapter fake --once
python3 -m imageharbor result TASK_UUID
python3 -m imageharbor cancel TASK_UUID
```

Cada tarea está limitada a `diagnose` o `reconcile` para **una** app. SQLite guarda
estado y resultado, idempotencia, claim, heartbeat y cancelación cooperativa.
El alcance persiste instancia y UIDs de namespace/release; una tarea antigua no
puede operar sobre una app recreada. Diagnose sólo tiene lectura. Cancelación y
lease se comprueban al adquirir el lock y antes de cada mutación; una llamada API
ya iniciada puede completar. Las tareas antiguas sin alcance verificado fallan.
El worker fake sólo opera sobre backend fake. El worker predeterminado usa
`UnconfiguredHarness` y devuelve `failed / harness_not_configured`: **no hay un
harness LLM/Oracle conectado**. El protocolo/adaptador y sus tests quedan listos
para integrarlo con un proceso aislado y capacidades limitadas.

## Kubernetes local u Oracle, con autorización posterior

[bootstrap/local.sh](bootstrap/local.sh) crea un cluster k3d **nuevo**, un nodo
K3s `v1.35.9+k3s1`, Traefik `3.7.14` con chart `41.6.1`, sólo ClusterIP y API
en loopback. No cambia el kubeconfig predeterminado. Requiere Docker activo,
k3d, kubectl y Helm. No fue ejecutado y puede descargar varios cientos de MB o
más de 1 GB. Revisá ese consumo de disco antes de ejecutarlo.

```sh
# Sólo después de autorizar creación de infraestructura local:
sh bootstrap/local.sh --create-local
```

Para un flujo real posterior, pinneá la imagen por digest desde el artefacto de
CI, ajustá la spec para las restricciones de runtime y usá `personal/local.json`.
Al principio mantené rutas deshabilitadas y comprobá el backend por port-forward
a `127.0.0.1`. No se publican puertos de aplicaciones en el host. Los pasos y
verificaciones pendientes están en [bootstrap/oracle-plan.md](bootstrap/oracle-plan.md).

Traefik fue elegido por su mantenimiento vigente y CRDs que expresan host/path y
middleware por ruta. La release verificada el 2026-10-08 fue
[v3.7.14](https://github.com/traefik/traefik/releases/tag/v3.7.14), y el chart
[v41.6.1](https://github.com/traefik/traefik-helm-chart/releases/tag/v41.6.1).
Kubernetes anunció el retiro de Ingress NGINX para marzo de 2026, por lo que no
se seleccionó ese controlador. [Aviso oficial](https://kubernetes.io/blog/2026/01/29/ingress-nginx-statement/).

## Límites concretos

CLI/MCP, sin dashboard web. Una réplica por app y estrategia Recreate: updates
pueden interrumpir servicio y no hay HA. TCP readiness verifica el puerto, no la
salud funcional de la app. Sin multi-tenancy, autoscaling, canary, build service,
TLS automático, DNS automático, métricas de uso, resize, backup ni GC de imágenes.
Local-path solicita capacidad, pero no garantiza una cuota física por volumen;
hay que vigilar disco del nodo. Tags son mutables, por eso la configuración real
exige digest. CI externo debe producir imagen Linux para la arquitectura Oracle
(por ejemplo ARM64 para Ampere), idealmente multiarch. Elegí los digests correctos
y mantené el toolchain de bootstrap actualizado antes de instalar.

Configuración personal, inventarios privados, estado y kubeconfigs están
excluidos de la fuente pública. No se suministran credenciales/grants activos.
Resultados de validación y pendientes: [docs/validation.md](docs/validation.md).
Hallazgos y correcciones de la segunda revisión: [docs/review.md](docs/review.md).
