# Archi_Tools

![Licencia MIT](https://img.shields.io/badge/licencia-MIT-green)
![pyRevit 7+](https://img.shields.io/badge/pyRevit-7.0.0%2B-blue)
![Revit 2024 | 2025](https://img.shields.io/badge/Revit-2024%20%7C%202025-lightgrey)

Archi_Tools es una extensión de [pyRevit](https://github.com/pyrevitlabs/pyRevit) creada por Archiplan. Agrega una pestaña a la cinta de Revit con herramientas de modelado, presentación, auditoría y gestión de schedules, y un botón para mantenerse al día con las versiones publicadas en este repositorio.

## Herramientas

| Panel | Herramienta | Qué hace |
|---|---|---|
| Actualizar | **Actualizar** | Busca en GitHub una versión nueva, te muestra los cambios y la instala solo si tú confirmas. |
| ScheduleBoard | **Schedule Board** | Todos los schedules del proyecto en un mismo lienzo, cada uno con su tabla real: compararlos, encontrar los vacíos o desactualizados, dar un mismo campo a varios a la vez o corregir un valor en muchas filas. |
| Auditoría | **Warnings Analyzer** | Dashboard interactivo para entender y resolver los warnings del modelo: filtros, explicación de cada tipo, corrección automática de duplicados, tendencia diaria y exportación a CSV. |
| Elements | **Floor ⇄ Topo** | Convierte floors en toposolids y viceversa, conservando contorno, huecos, nivel, offset, forma y parámetros. |
| Elements | **Geometry Slope** · **Geometry Slope Multi** · **Unir Losas** | Pendientes en floors y toposolids desde una arista o desde varias, y unión de losas con pendientes distintas. |
| Elements | **Topo Level** | Empata niveles de perímetro entre toposolids y floors. |
| Elements | **Trace Section** | Calca la silueta de un toposolid o floor en el plano donde lo corta la vista activa. |
| Elements | **Floor Silhouette** | Ajusta un pavimento restando el perímetro de los muros. |
| Elements | **Party Wall** | Crea muros prefabricados escalonados (postes y paneles) que se adaptan al terreno. |
| Geometry | **Path Array** | Clona y alinea elementos a lo largo de un recorrido de líneas o curvas. |
| Rooms | **Center A/R** · **Center A/R TAG** · **Rotate A/R TAG** | Centran Rooms/Areas y sus etiquetas, y alinean las etiquetas con un muro o contorno. |
| Rooms | **Assign Colors** · **Color Scheme Manager** | Asignan colores a un Color Fill Scheme y limpian sus valores fantasma, todo en una ventana. |
| Presentation | **Crop a Ventana** · **Link Display** · **Link VG** | Encuadre de la crop region y gestión en lote de los Display Settings de los Revit Links. |
| Exportación | **PRO-Batch Export** | Exporta láminas a DWG y PDF en lote, con nombres de archivo paramétricos y perfiles reutilizables. |
| Accesibilidad | **Ruta Accesible** | Crea regiones de relleno para rutas accesibles con radios de giro. |
| Relax | **Relax** | Un juego rápido estilo Arkanoid para el descanso. |

Cada botón explica su uso en el tooltip de la cinta (pasa el cursor por encima).

## Requisitos

- pyRevit 7.0.0 o superior. Con versiones anteriores no uses el botón de recargar pyRevit: ScheduleBoard registra sus rutas al iniciar y la recarga puede cerrar Revit.
- Revit 2024 o 2025.
- Git no hace falta instalarlo: pyRevit incluye lo necesario.

Si aún no tienes pyRevit, descarga el instalador desde su página oficial de versiones: <https://github.com/pyrevitlabs/pyRevit/releases>.

## Instalación

Con pyRevit instalado, abre una terminal y ejecuta una sola línea:

```
pyrevit extend ui Archiplan https://github.com/E-RC/ARCH_Tools.git --branch=main
```

Luego reinicia Revit (o recarga pyRevit). La pestaña **Archi_Tools** aparece en la cinta.

Si ya tienes una copia de Archi_Tools instalada (por ejemplo, copiada a mano), bórrala primero: el comando falla con "Destination path already exists" si la carpeta de destino ya existe.

## Actualización

Usa el botón **Actualizar** de la pestaña Archi_Tools. Consulta GitHub y, si hay una versión nueva, te muestra los cambios y te pregunta antes de instalar: nunca actualiza sin tu confirmación. Al terminar puede pedirte recargar pyRevit o reiniciar Revit para aplicar los cambios.

Las versiones siguen [versionado semántico](https://semver.org/lang/es/) y se publican como etiquetas (`v1.0.0`, `v1.1.0`, …) en la sección *Releases* del repositorio.

## ScheduleBoard y escritura en el modelo

ScheduleBoard puede modificar el modelo desde el tablero. La escritura viene activa. Para dejar un equipo en solo lectura, crea un archivo vacío llamado `writes-disabled` en `%LOCALAPPDATA%\ScheduleBoard`; para volver a activarla, bórralo. No hace falta reiniciar Revit.

## Datos y privacidad

- La configuración de las herramientas se guarda en tu equipo, en `%APPDATA%\Archiplan` y `%LOCALAPPDATA%\ScheduleBoard`. No se envía a ninguna parte.
- La extensión solo se conecta a internet en el botón **Actualizar** (consulta este repositorio en GitHub) y, en **Warnings Analyzer**, para cargar una tipografía desde Google Fonts.
- ScheduleBoard se comunica con Revit solo dentro de tu equipo, a través del servidor local de pyRevit (`localhost`).

## Estructura del repositorio

```
Archi_Tools.tab/   botones de la cinta, organizados por panel
lib/               código compartido (ScheduleBoard, actualizador, utilidades)
web/dist/          interfaz compilada de ScheduleBoard
startup.py         registro de ScheduleBoard al iniciar pyRevit
extension.json     nombre, versión y metadatos de la extensión
```

## Soporte y contribuciones

Este repositorio lo mantiene Archiplan y por ahora no recibe contribuciones externas. Úsalo bajo los términos de la licencia, sin garantía de ningún tipo.

## Desinstalar

Borra la carpeta de la extensión (la que contiene `extension.json`), o ejecuta en una terminal:

```
pyrevit extensions delete Archiplan
```

Luego reinicia Revit.

## Licencia

MIT. Ver el archivo [LICENSE](LICENSE). © 2026 Archiplan S.A.
