# Recuperación y selección de revisores

## Activación

Claves de nivel raíz en `factory.toml`:

```toml
recovery_enabled = true
review_mode = "observe" # all, observe, selective
```

`observe` registra qué omitiría la política y ejecuta los cuatro revisores. `selective`
omite únicamente UI/UX en notas Markdown internas de `brain/learnings` y
`brain/conventions`, sin señales visuales. No se omiten seguridad ni código.
APIs, correos, dependencias, documentación pública, pantallas y rutas desconocidas
mantienen las cuatro revisiones. Esta primera política es deliberadamente estrecha:
no presupone que una dependencia o un endpoint carezca de impacto en la experiencia.
El Aprobador puede pedir `additional_reviewers: ["ui", "ux"]`.

## Qué se conserva

Cada etapa y cada revisión completada genera un paquete inmutable con `state.json`,
`code.bundle`, capturas PNG y manifiesto SHA-256. El bundle guarda el árbol de trabajo
(incluidos nuevos archivos no ignorados) con un índice temporal: no cambia el índice
ni publica una rama incompleta. Los archivos ignorados, los directorios de ejecución
y los archivos nuevos con nombres de credenciales conocidos se excluyen o bloquean.
No se debe versionar secretos. Un paquete contiene historia Git y se trata como código
privado del repositorio; la retención remota del ejemplo es 14 días.

Si falla guardar el paquete, se conserva el worktree local. Los checkpoints locales
son atómicos; el remoto es duradero sólo cuando GitHub devuelve su ID de artefacto.
Un cierre abrupto puede perder lo producido después de la última subida completada.
No se recupera la sesión interna del modelo: se reconstruye con código, especificación,
feedback y resultados. El contador de iteraciones no se reinicia al reanudar.

## Reanudar

Local:

```bash
factory resume <run_id>
factory resume <run_id> --checkpoint /ruta/al/paquete
```

GitHub Actions: copiar/actualizar `.github/workflows/ai-factory.yml` y el workflow de
reintentos. El paso `actions/github-script` suministra las credenciales temporales al
publicador `@actions/artifact@2.3.2`; se eliminan del entorno que reciben los agentes y
los comandos de pruebas. `actions: read` permite consultar y descargar artefactos.
Si `GH_TOKEN` es un PAT, necesita también lectura de Actions; el permiso del workflow
no amplía los permisos de un PAT.

El reintento automático pasa `issue`, `resume_run_id` y `checkpoint_artifact`.
Para una cancelación abrupta, seleccionar manualmente el último artefacto
`factory-checkpoint-<run_id>-<generation>` de la ejecución fallida/cancelada y usar esos
inputs en **Run workflow**. No se usa un comentario como prueba de autenticidad:
se valida repositorio, workflow, rama base, evento, estado de la ejecución, identidad de
tarea, ID de ejecución, manifiesto, checksums y bundle. Un artefacto de PR/fork o de una
ejecución terminada exitosamente se rechaza. Un SHA-256 comprueba integridad, no autoría;
la procedencia se establece con la API de GitHub y los permisos del workflow.

La etiqueta de espera sólo se retira después de que GitHub acepte el dispatch. Si
la pausa nueva no tiene artefacto remoto se mantiene en espera: no se reinicia desde
cero silenciosamente. Mensajes que mencionan límite mensual/semanal suspenden los
reintentos automáticos aunque también incluyan una hora de reinicio de cinco horas.
Una hora prevista no es una confirmación de que haya cuota.

## Validez y publicación

En un runner nuevo se repiten setup y pruebas. Las revisiones se reutilizan sólo si
la base, requisitos, configuración y código de la fábrica siguen siendo los mismos.
Cambiar los requisitos o la base invalida especificación y revisiones; el código se
conserva como punto de partida. Cada nueva iteración del Builder invalida las revisiones.
Se comprueban rutas protegidas antes de ejecutar las verificaciones del cambio restaurado.

Una publicación iniciada tiene un resultado potencialmente incierto: no se repite
automáticamente. Verificar commit remoto, PR y despliegue y resolver manualmente. Esto
impide duplicar efectos externos sin fingir una garantía de ejecución exactamente una
vez. Las ejecuciones finalizadas tampoco se pueden reanudar desde su estado local.
La aprobación humana de rutas núcleo permanece obligatoria.

## Piloto y métricas

Mantener los modelos y `review_mode = "observe"` durante al menos 20 tareas comparables.
Los estados/comentarios incluyen `attempt`, `next_stage`, `reused_stages`,
`review_selection`, `usage_by_agent`, `usage_calls` y costo USD equivalente acumulado
entre intentos. Los valores corresponden al motor Claude Code cuando éste los reporta;
no son cargos de API ni una medición de cuota de suscripción. Las llamadas interrumpidas
sin evento final de uso pueden no tener costo registrado.

Comparar consumo por tarea aceptada (sumando intentos), reanudaciones exitosas, regresiones
y hallazgos de UI/UX en tareas que la política habría omitido. No sumar costos por intento
si cada estado ya incluye el acumulado: usar el último estado por `run_id`. No atribuir
un porcentaje causal a una muestra con mezcla de tareas distinta. Activar `selective`
cuando la observación no muestre hallazgos relevantes perdidos. El objetivo previo de
15% es una hipótesis del piloto, no una promesa.

Rollback: `review_mode = "all"`. Se puede desactivar `recovery_enabled` para nuevas tareas;
conservar artefactos de tareas pausadas hasta resolverlas. Los checkpoints antiguos
sin manifiesto no se convierten automáticamente en evidencia válida.

## Orden de despliegue

1. Fusionar y publicar AI Factory con estos cambios y sus pruebas.
2. Actualizar el workflow y `factory.toml` de cada aplicación (Forja tiene un PR separado).
3. Probar una tarea pequeña con fallo simulado de cuota y verificar la restauración real
   entre runners. Las pruebas locales usan modelos simulados, no consumen suscripción.
4. Iniciar el piloto de observación. No ejecutar tareas reales sólo para verificar una
   subida de código: las pruebas de integración deben usar el cliente simulado.
