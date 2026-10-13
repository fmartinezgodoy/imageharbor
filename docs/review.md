# Segunda revisión del primer corte

2026-10-08. Revisión de código y contratos con regresiones locales, en el mismo
directorio. Sin instalar herramientas, iniciar Docker, contactar Oracle ni
publicar. Los hallazgos siguientes se corrigieron en el alcance autorizado.

| Hallazgo | Corrección | Evidencia |
| --- | --- | --- |
| El alcance de tareas dependía de un nombre de app mutable y diagnose podía reconciliar. | Identidad de instancia/namespace/release persistida, app_id de sólo lectura, request copiado y diagnose sólo lectura. | Tareas obsoletas no llaman al harness; mutación de request no cambia el alcance; diagnose rechaza escritura. |
| Una cancelación durante la espera del lock podía permitir escrituras; un heartbeat tardío podía revivir un lease. | Checks dentro del lock y antes de cada mutación; heartbeat/finish rechazan leases vencidos. | Cancelación esperando lock y entre Service/Deployment; heartbeat vencido y resultado tardío. |
| La metadata podía sobrescribir una versión más nueva tras releerla. | Comparación contra UID/resourceVersion originalmente leídos y escritura CAS. | Dos lectores: el segundo save obsoleto falla. |
| Labels solos permitían seguir una identidad de namespace/PVC recreada; un PVC faltante podía recrearse vacío. | UID de namespace/PVC persistidos; rechazo de reemplazos, ausencias registradas y ownerReferences de PVC. | Namespace reemplazado, PVC ausente/reemplazado, rollback con volumen histórico ausente. |
| Borrado de datos podía ignorar Pods/Deployments sin labels; faltaba intención durable por volumen. | Revisa todos en el namespace, registra delete_requested antes de DELETE, conserva pendientes y confirma desaparición. | Guard de recursos sin labels, fallo parcial entre volúmenes y finalizer simulado con reintento. |
| Cleanup podía barrer recursos extra con labels; logs no comprobaban Deployment propio. | Nombres/roles de runtime fijos, namespace validado y verificación de propiedad antes de logs. | Recursos desconocidos bloquean cleanup y Deployment ajeno no llega al backend de logs. |
| Una app corrupta bloqueaba consultas ajenas; desactivar rutas podía atascarse por un PVC inválido. | Errores por app, status acotado y cierre de routers previo a reconciliar datos. | Metadata corrupta aislada; router eliminado aun con PVC faltante. |
| Inputs MCP anidados e identificadores JSON-RPC inválidos podían alcanzar acciones. | Validación recursiva de schemas y del handshake/id antes de despachar. | Tipos/campos inválidos y mensajes de inicialización no habilitan acciones. |
| Reusar estado para otra configuración mezclaba cola/control plane; configuración mínima y URLs malformadas fallaban fuera del contrato. | Vínculo de estado y checks del worker; default state_dir válido y errores de URL sanitizados; canonical scheme consistente con TLS/HTTP. | Reuso entre instancias rechazado; configuración mínima y URLs inválidas cubiertas. |

Remove y reconciliación de tombstones establecen NetworkPolicy deny-all antes
del cleanup y vuelven a revisar el ledger de datos antes de borrar runtime. Si la
retención no se puede garantizar, se informa el fallo y se conserva el runtime.
Esto no demuestra que el CNI aplique la policy en un cluster real.

## Cobertura y límites de las garantías

Se agregaron 27 tests en [test_review.py](../tests/test_review.py), además de los
39 existentes. La suite completa y los comandos ejecutados están en
[validation.md](validation.md). Son pruebas de control plane, persistencia y
contratos. Las dos pruebas con kubectl real siguen usando una API simulada en
loopback: no validan Kubernetes real, admission, storage, GC, proxy ni seguridad
de red. Los finalizers y fallos parciales de las regresiones también son fakes.

La unicidad de rutas usa LIST + validación antes de registrar la intención. El
lock local cubre un escritor por instancia con el mismo directorio de estado;
dos controllers independientes podrían competir entre esas llamadas. No hay
admission ni lock distribuido: esa concurrencia sigue fuera del alcance soportado.
Rollback crea una nueva revisión de configuración y falla si los datos requeridos
faltan; no restaura contenido ni revierte migraciones. No hay backup físico probado.

Cancelación es cooperativa, puede terminar una llamada API ya iniciada y no
revierte cambios aplicados. Los objetos Python no aíslan código hostil. El
control plane, el administrador del cluster y el destino del kubeconfig deben
ser confiables. El vínculo de estado registra ruta/contexto, no la identidad
criptográfica del servidor Kubernetes. Metadata antigua sin UIDs se vincula a
recursos propios existentes durante una operación autorizada; no reconstruye su
historia de propiedad. Tareas antiguas sin scope verificado fallan.

## MCP y trabajo pendiente

En esta segunda revisión la entrega sólo implementaba stdio local con autenticación del proceso/usuario y
ACL del sistema. No tiene URL, túnel seguro configurado ni integración verificada
con ChatGPT. La documentación actual admite HTTPS o Secure MCP Tunnel, incluido
stdio detrás del túnel; configurar y autenticar uno requiere el siguiente corte.
[Conexión oficial](https://developers.openai.com/plugins/deploy/connect-chatgpt).

El harness real continúa explícitamente no configurado. Quedan pendientes
aislamiento y RPC autenticado de capacidades, transporte remoto, validación en un
cluster nuevo autorizado, bootstrap/Helm, PVC físicos, DNS/TLS/paths, NetworkPolicy
y grants efectivos. No se concedieron credenciales/grants ni se contactaron
servicios existentes. Se puede seguir trabajando en código y fakes sin esas
autorizaciones; las verificaciones de infraestructura requieren una autorización
posterior concreta.

La continuación agregó HTTP privado y stdio restringido; estado actual y límites
en [remote-plan.md](remote-plan.md). SSH remoto, OAuth y ChatGPT siguen sin probar.
