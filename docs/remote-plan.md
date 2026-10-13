# MCP privado, SSH y conexión futura de ChatGPT

Implementado el 2026-10-08 sobre el stack existente Python/stdlib. El transporte
HTTP fue probado en loopback con procesos reales y plataforma fake. No se probó el transporte SSH remoto del producto, TLS, OAuth ni conexión
desde ChatGPT. La evidencia pública excluye los inventarios de instancias.

## Ejecutar un flujo local reproducible

Desde la raíz del proyecto:

```sh
python3 -m examples.remote_demo
python3 -m unittest discover -v
```

El demo abre un listener temporal en 127.0.0.1 con puerto efímero, genera un token
sólo en RAM y lo entrega al subprocess mediante un pipe heredado. Hace handshake,
deploy/update/rollback, submit/result con worker fake y remove conservando el UID
del PVC. Termina el proceso y elimina únicamente su estado fake temporal, incluso
si falla. No toca credenciales personales ni configura una conexión remota.

Stdio restringido, sin listener:

```sh
python3 -m imageharbor --config examples/instance.fake.json mcp \
  --allow-app hello --access read
```

`mcp` sin allowlist conserva el modo administrativo local del primer corte y
requiere confianza en el usuario/proceso que lo inicia. Para SSH o un túnel usar
siempre el modo restringido. `ssh-plan` sólo imprime un plan, no ejecuta SSH:

```sh
python3 -m imageharbor ssh-plan --alias HOST_ALIAS \
  --remote-root /srv/imageharbor --allow-app hello --access read
```

El path remoto es una propuesta para revisar, no una instalación existente.

## Perfil HTTP privado implementado

`mcp-http --allow-app hello --token-fd FD --port 8765` requiere que un supervisor
entregue el token por un pipe heredado, cierre el extremo escritor y pase ese FD
al proceso. No admite token en argv, variable de entorno, archivo ni config. Ver
[remote_demo.py](../examples/remote_demo.py) como referencia ejecutable del ciclo.
No copiar tokens a comandos curl/history. El caller debe generar al menos 256 bits
aleatorios; el servidor valida formato/longitud, no puede medir entropía real.

| Control | Comportamiento |
| --- | --- |
| Bind | Literal 127.0.0.1; no flag de interfaz pública ni modo reverse proxy. |
| Autenticación | Bearer efímero comparado en tiempo constante en requests soportadas; TTL 900 s, configurable 60–3600 s. |
| Sesión | ID aleatorio en RAM; 16 máximo; 300 s inactividad y 900 s vida máxima. La sesión nunca sustituye el token. |
| HTTP | `/mcp`, POST JSON y DELETE de sesión; GET responde 405 sin SSE. |
| Protocolo | 2025-11-25 o 2025-06-18; MCP-Protocol-Version obligatorio tras initialize. Notifications aceptadas responden 202. |
| Input/output | Body 256 KiB; respuesta 1 MiB; sólo UTF-8 JSON, sin batches, chunked request, NaN ni Expect. Headers críticos duplicados se rechazan. |
| Navegador/proxy | Host debe ser 127.0.0.1:puerto-del-servidor; cualquier Origin, Forwarded o X-Forwarded-* se rechaza. Sin CORS. |
| Recursos | Un handler serial; backlog 4; inactividad del socket 3 s; bucket global 1 request/s y burst 20. |
| Telemetría | Sin access logs ni eco de headers/body/token; errores internos genéricos. Cache-Control no-store. |

El proceso deja de aceptar el token al vencerlo; sigue escuchando hasta que el
supervisor lo termine. Reiniciar para rotar y perder todas las sesiones. No hay
rotación en caliente ni reanudación SSE. No se promete borrar físicamente de RAM
las copias de strings/bytes del runtime. El servidor stdlib serial es un perfil
experimental para un operador y clientes privados confiables, no un edge público
ni una defensa completa ante DoS de un proceso hostil del mismo host. El timeout
es por inactividad, no una fecha límite total contra slowloris.

## Permisos por operación y aplicación

La misma política inmutable se usa en HTTP y stdio restringido. Exige entre 1 y
32 app IDs exactos y comprueba cada invocación, incluso si el caller evita el
catálogo. Inventory sólo consulta apps permitidas; no devuelve errores ni nombres
de propietarios ajenos. task_status/result/cancel validan la app registrada en la
tarea antes de devolver datos o cancelar.

| Permiso | Tools |
| --- | --- |
| read (default restringido) | inventory, status, task_status, result |
| operate | Los anteriores más deploy, update, rollback, logs, remove, submit, cancel |
| operate + --allow-data-delete | Agrega volumes_delete; siguen exigiéndose opt-in de instancia, remove, nombres, confirmación y guards de datos. |

Read no puede crear una tarea reconcile: submit entero requiere operate. Logs
siguen requiriendo log_safe_apps. La política no convierte el controller en un
sistema multiusuario ni a Python en un sandbox. El token corresponde a un único
operador/política; quien lo posee tiene todas esas capacidades. El worker/harness
conserva alcance persistido y cancelación cooperativa del primer corte.

Una desconexión HTTP no revierte ni necesariamente cancela una operación mutante;
consultar estado antes de reintentar. El límite de respuesta también puede
alcanzarse después de aplicar cambios. No hay journal HTTP de request IDs o
exactly-once; submit conserva su idempotency_key. No activar retries ciegos en el
cliente para update/rollback/remove.

## SSH posterior, sin exposición pública

La autenticación SSH puede reutilizar credenciales existentes después de una
inspección y autorización específicas. `ssh-plan` genera un comando stdio fijo con
PTY/agent/X11/forwards/multiplexing deshabilitados, BatchMode, host key estricta,
sin actualizarlas, sin password interactivo ni ProxyCommand/ProxyJump. El comando
remoto sólo inicia ImageHarbor con config fija y allowlist; no acepta shell libre.
No se ejecutó. El alias y path se validan para evitar inyección y traversal.

Antes de ejecutarlo revisar la configuración SSH: Includes/Match exec podrían
introducir ejecución local aun con ese argv. Revisar host key ya confiable y usar
cuenta restringida con directorio aislado; no sudo/root ni copiar claves. Los
flags son defensa del cliente, no restringen por sí solos una cuenta SSH que ya
tiene acceso general. No se crearon usuarios, authorized_keys o grants.
[Manual SSH](https://man.openbsd.org/ssh),
[configuración SSH](https://man.openbsd.org/ssh_config).

Alternativa para un cliente HTTP controlado: forward local explícito hacia el
listener remoto loopback. Mantener igual puerto local/remoto para el Host exacto,
o configurar deliberadamente el Host remoto en el cliente. Sin -g, 0.0.0.0 ni
forward remoto. ClearAllForwardings=yes del plan stdio no se reutiliza para esa
alternativa porque también eliminaría el forward deseado. No hay launcher de
forward implementado ni prueba SSH. El token privado requiere entrega efímera
segura aparte; no ponerlo en argumentos remotos ni archivos permanentes.

## ChatGPT: conexión aún pendiente

HTTPS o Secure MCP Tunnel son las rutas oficiales; un túnel configurado puede
alcanzar stdio o HTTP privado. El proyecto no incluye una conexión activa ni una
prueba desde ChatGPT. Stdio/SSH/loopback no crean esa conexión automáticamente.
[Conexión oficial](https://developers.openai.com/plugins/deploy/connect-chatgpt).

Evaluar primero Secure MCP Tunnel sobre stdio restringido: necesita tunnel-client,
tunnel_id, runtime API key, permisos y asociación del contexto autorizado. Nada
se instaló/configuró. El permiso de túnel y las capacidades de ImageHarbor son
controles distintos.
[Secure MCP Tunnel](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels).

El bearer privado implementado **no cumple el contrato OAuth MCP** ni se presenta
como autenticación directamente compatible con ChatGPT. Si se elige HTTPS,
integrar un proveedor OAuth 2.1 existente con protected-resource metadata,
discovery, PKCE S256, resource/audience y scopes; no construir un issuer propio
ni publicar este listener HTTP detrás de un proxy genérico. Esa integración y
sus grants necesitan autorización específica y prueba real.
[Autenticación oficial](https://developers.openai.com/plugins/build/auth),
[transporte MCP](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports),
[autorización MCP](https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization).

## Próximos pasos concretos

1. Elegir el host Oracle y autorizar una inspección SSH de sólo lectura: OS,
   arquitectura, disco, puertos y servicios existentes; revisar host key y cuenta.
   El nombre de un alias no identifica por sí solo la instancia adecuada.
2. Revisar un directorio remoto aislado y el plan de coexistencia; elegir acceso
   privado SSH o Secure MCP Tunnel. Iniciar/configurar ese proceso sólo después
   de aprobar su comando, permisos y entrega de autenticación.
3. Autorizar un cluster nuevo de prueba. Docker accesible, Helm y k3d son requisitos
   del bootstrap local. Presupuestar cientos de MB o más de 1 GB en imágenes,
   además del disco de VM/datos; no iniciar ni descargar automáticamente.
4. Verificar admission, PVC físicos, proxy/paths/TLS y NetworkPolicy en cluster;
   luego revisar grants mínimos y bootstrap de Oracle. No migrar ni detener
   servicios existentes como parte de este corte.

Se puede continuar con código/fakes sin conectar Oracle. La evidencia y límites
están en [validation.md](validation.md). Inventarios de instancias, alias y
reportes privados deben permanecer en personal/, fuera de la fuente pública.
