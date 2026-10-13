# Fuente pública y configuración privada

El repositorio distribuye código, pruebas deterministas, ejemplos neutros y
bootstrap para revisión. No distribuye datos de instancias ni autenticación.

- personal/, .local/, kubeconfigs, .env, claves y archivos de credenciales se
  excluyen. Mantener allí inventarios, specs reales, resultados de inspección y
  estado del operador; no agregar esas rutas con git add -f.
- Los ejemplos usan dominios example.test, contexto placeholder y referencias,
  nunca valores secretos. Los tokens del demo se generan en RAM y no se guardan.
- CI sólo ejecuta tests/compilación con permisos contents:read; no construye ni
  publica imágenes ni despliega, y checkout no persiste credenciales.
- Antes de publicar cambios, revisar todos los archivos y commits seleccionados,
  incluidos ocultos, metadatos de autor y objetos históricos. .gitignore no elimina
  un archivo que ya está tracked ni un secreto de commits anteriores.

El primer conjunto público se preparó desde una allowlist explícita, con revisión
textual y scans de referencias privadas/credenciales. Inventarios y respaldos de
la preparación quedaron fuera del historial. Esto describe el proceso de esta
publicación; no convierte la lint del producto en detección universal de secretos.
