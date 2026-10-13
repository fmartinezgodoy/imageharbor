# Validación actual

Validación local del primer corte, repetible con Python 3.11+. Configuración e
inventarios de instancias quedan fuera de la fuente pública, en personal/.
Los resultados siguientes corresponden a control plane y transportes locales;
no se presentan como validación de infraestructura desplegada.

## Comandos ejecutados

```sh
python3 -m unittest discover -q
python3 -m unittest tests.test_remote -v
python3 -m examples.demo
python3 -m compileall -q imageharbor tests examples
sh -n bootstrap/local.sh
python3 -m imageharbor ssh-plan --alias HOST_ALIAS \
  --remote-root /srv/imageharbor --allow-app hello
```

Verificación previa a publicación, 2026-10-13, Python 3.13.7:
**86 tests, OK, sin skips** (8,805 s en la ejecución observada). Incluye los
66 de la revisión anterior y 20 nuevos de transporte/política/plan SSH. Los 20
nuevos pasaron también por separado. El demo HTTP en subprocess real se ejecutó
como parte de la suite; el demo original también se repitió y pasó. Compilación
Python y sintaxis shell: OK. El comando ssh-plan devolvió executed=false y nunca
conectó a un host. La suite crea y cierra exclusivamente listeners temporales en
127.0.0.1. El sandbox negó bind en su perfil normal: se usó la excepción revisada
para verificarlos, no se contaron los skips iniciales como cobertura.

El demo HTTP se puede repetir explícitamente con `python3 -m examples.remote_demo`.
Su resultado comprobado: revisiones [1,2,3], async succeeded con fake, remove
conservando UID del PVC, transporte loopback real, autenticación efímera y ningún
borrado de datos anunciado. Oracle y ChatGPT no fueron contactados. El proceso
hijo y su estado fake temporal se eliminan al finalizar.

## Cobertura real

- Modelo y flujo image-first: registry/digest/inputs, deploy/update/rollback,
  persistencia, idempotencia, intención durable ante fallos parciales y drift.
- Rutas host/path, límites de segmento, strip/preserve, alias/canonical,
  conflictos entre apps/instancias y routers ajenos. Defaults sin rutas.
- Propiedad y datos: namespace/PVC UID, labels/roles/nombres, comparación de
  metadata original, PVC ausente/reemplazado, ownerReferences, tombstones,
  retención, delete_requested, fallo parcial y finalizer simulado/reintento.
  Pod/Deployment sin labels bloquean datos; rollback no crea un volumen perdido.
- LIST/WATCH/relist 410: dos tests usan kubectl real contra API simulada en
  loopback. GET/LIST/POST/PUT/WATCH/DELETE con precondiciones se transportan por
  HTTP real. Paginación/contexto explícito también tienen contratos con runner fake.
- MCP stdio y subprocess CLI; schema recursivo, handshake, inputs inválidos,
  bounded tools, política de logs y errores sin eco de valores sensibles.
- SQLite y harness: claim exclusivo, scope persistido, diagnose sólo lectura,
  cancel queued/running, espera del lock y entre mutaciones, lease vencido,
  respuestas tardías y tareas legacy sin scope. Adapter no configurado falla y
  fake no puede operar contra backend real.
- Nuevo HTTP: proceso/cliente HTTP reales en loopback, bearer en cada request
  soportada, TTL, handshake/session independiente, expiración por inactividad y
  vida máxima, DELETE, límites de sesiones y rate, socket inactivo cerrado,
  Host/Origin/forwarding/headers duplicados y JSON/body/media/protocolo inválidos.
- Política remota: catálogo y llamada directa, default read sin escrituras,
  inventario sin apps ajenas, task_status/result/cancel sin datos ni efectos fuera
  de app autorizada; permiso adicional de datos conserva guards del platform.
- Pipe: consume/cierra token; archivos y tokens cortos/oversized se rechazan sin
  leer archivos de credenciales. Token/request sensible no aparece en logs ni
  estado fake. Plan SSH puro validado, sin subprocess/red y con rechazo de inyección.

## Lo que estas pruebas no demuestran

**No hubo cluster real.** La API de prueba y el backend fake no validan admission,
scheduler, CRDs/Helm, almacenamiento/GC/finalizers físicos, defaults completos,
proxy/paths/TLS, NetworkPolicy ni grants efectivos. TCP readiness fake tampoco
prueba salud de una imagen. El bootstrap sólo tiene sintaxis revisada, no ejecución.

**No se probó ImageHarbor remoto ni conexión con ChatGPT.** No se validó el
comando del producto por SSH, forward, OAuth, Secure MCP Tunnel ni
interoperabilidad con un cliente MCP externo. HTTP es un perfil privado limitado con bearer propio,
no la implementación OAuth MCP de un endpoint público. No se simula una
integración funcional de harness LLM/Oracle.

No se promete detección universal de secretos en env/logs, resistencia completa
a DoS ni aislamiento contra procesos/admins comprometidos del host. Cancelación
puede terminar una llamada API ya iniciada; no revierte cambios. Un lock local
no resuelve carreras entre controllers independientes ni hace transaccional la
unicidad de rutas. Ver [review.md](review.md) y [remote-plan.md](remote-plan.md).

## Requisitos y próximos pasos

Los tests de control plane sólo necesitan Python 3.11+. Las dos pruebas con
kubectl requieren su cliente y loopback disponible; se saltan explícitamente si
falta alguno. No confundir un resultado con skips con una ejecución completa.
El demo HTTP abre únicamente listeners temporales en loopback y los cierra al
finalizar. No se necesita Docker para estos checks.

Para un cluster local nuevo, el bootstrap necesita Docker, k3d, kubectl y Helm.
Presupuestar cientos de MB o más de 1 GB de imágenes más VM/datos y autorizar la
infraestructura antes de iniciarla. Los checks no generan credenciales/grants
persistentes ni modifican servicios existentes. Inventarios, acceso remoto y
detalles de sesiones de operadores se conservan fuera del repositorio público.

Antes de desplegar, verificar arquitectura/digests, puertos/subredes, capacidad,
persistencia, backups y compatibilidad con infraestructura existente. La prueba
real del cluster y del transporte remoto sigue pendiente. Fuentes primarias de
decisiones: README, bootstrap/oracle-plan.md y docs/remote-plan.md. Plan concreto:
[remote-plan.md](remote-plan.md#próximos-pasos-concretos).
