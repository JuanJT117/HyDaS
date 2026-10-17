# Políticas de Seguridad, Resiliencia y Estándares DevSecOps (HyDaS v11.2)

> **Documento:** Políticas de Seguridad, Gestión de Memoria y Resiliencia en Windows  
> **Sistema:** HyDaS (Hydrological Data Acquisition & Simulation System)  
> **Versión:** 11.2  

---

## 1. Políticas de DevSecOps y Aislamiento de Entorno

### 1.1. Gestión Aislada de Dependencias en Conda
Para evitar colisiones entre bibliotecas C compiladas (GDAL, GEOS, PROJ, NumPy, SciPy) y la versión global de Python del sistema operativo:
- **Entorno Oficial:** La ejecución debe realizarse estrictamente dentro del entorno Conda creado desde [`assets/environment.yml`](file:///c:/Users/USER/Documents/proyectos/conda/CONAGUA-Completando_Datos/Version11.2/assets/environment.yml) (`flet_env` o `Hydrosistem`).
- **Verificación de Integridad:** Se prohíbe la instalación de paquetes con `pip install` que sobreescriban librerías binarias instaladas por `conda-forge`. Todas las dependencias críticas geoespaciales (`rasterio`, `geopandas`, `shapely`, `pyproj`) deben resolverse prioritariamente mediante el canal `conda-forge`.

### 1.2. Inyección Dinámica de Variables de Entorno Geoespaciales
En Windows, las herramientas geoespaciales fallan frecuentemente con el error:  
`ERROR 1: PROJ: Cannot find proj.db`.  
Para erradicar este problema en modo desarrollo y en ejecutable empaquetado (`PyInstaller`), tanto en [`main.py`](file:///c:/Users/USER/Documents/proyectos/conda/CONAGUA-Completando_Datos/Version11.2/main.py#L64-L72) como en [`infrastructure/project_manager.py`](file:///c:/Users/USER/Documents/proyectos/conda/CONAGUA-Completando_Datos/Version11.2/infrastructure/project_manager.py#L18-L30) se ejecuta al inicio el siguiente protocolo de inyección:

```python
import os
import sys

# Detección y resolución prioritaria de la base de datos de proyecciones
try:
    import pyproj
    _proj_dir = pyproj.datadir.get_data_dir()
    if _proj_dir and os.path.exists(_proj_dir):
        os.environ["PROJ_LIB"] = _proj_dir
        os.environ["PROJ_DATA"] = _proj_dir
except Exception:
    _conda_proj_win = os.path.join(sys.prefix, "Library", "share", "proj")
    _conda_proj_unix = os.path.join(sys.prefix, "share", "proj")
    if os.path.exists(os.path.join(_conda_proj_win, "proj.db")):
        os.environ["PROJ_LIB"] = _conda_proj_win
        os.environ["PROJ_DATA"] = _conda_proj_win
    elif os.path.exists(os.path.join(_conda_proj_unix, "proj.db")):
        os.environ["PROJ_LIB"] = _conda_proj_unix
        os.environ["PROJ_DATA"] = _conda_proj_unix
```

### 1.3. Soporte Híbrido: Código Fuente vs. PyInstaller Frozen
El acceso a recursos estáticos (shapefiles, imágenes, templates LaTeX) utiliza la función de resolución:
```python
if getattr(sys, 'frozen', False):
    BASE_DIR = sys._MEIPASS
else:
    BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
```
Esto previene que la aplicación busque carpetas relativas inexistentes al ser ejecutada desde un instalador en `Program Files`.

---

## 2. Gestión de Memoria y Prevención de Out-Of-Memory (OOM)

Los cálculos hidrológicos masivos, la interpolación geoespacial y la generación de cientos de gráficas pueden saturar rápidamente la memoria RAM si no se gestionan explícitamente.

### 2.1. Política de Destrucción de Figuras Matplotlib
- **Backend Headless Estricto:** En todos los módulos de lógica (`gastos_logic.py`, `arf_logic.py`, `analisis_cuenca.py`), se fuerza:
  ```python
  import matplotlib
  matplotlib.use('Agg') # Evita la carga de GUIs de escritorio (Tkinter/Qt) y fugas de GDI en Windows
  ```
- **Cierre Forzado de Lienzos:** Tras convertir cualquier gráfico a imagen Base64 para mostrarlo en Flet, es **obligatorio** invocar:
  ```python
  fig.clf()
  plt.close(fig)
  plt.close('all')
  del fig
  ```

### 2.2. Recolección de Basura Agresiva en Navegación (App Shell)
Al cambiar de una pestaña a otra en el riel de navegación de [`main.py`](file:///c:/Users/USER/Documents/proyectos/conda/CONAGUA-Completando_Datos/Version11.2/main.py#L384-L388):
1. Se desasocian los controles visuales anteriores.
2. Se ejecuta de forma síncrona `gc.collect()`.
3. Se introduce una pausa de 300 ms (`time.sleep(0.3)`) para permitir que el motor de Flutter destruya los elementos de la escena gráfica en el árbol de renderizado.

### 2.3. Filtrado de Controles UI en Guardados en Background
Para evitar serializar widgets de Flet (lo que provocaría referencias circulares y fugas de memoria masivas):
```python
def _extract_serializable_session():
    session_dict = {}
    BLACKLIST_KEYS = ["catalogo_conagua", "df_estaciones_nacionales", "db_climatologica_masiva"]
    for k in page.session.get_keys():
        val = page.session.get(k)
        # Se descartan callables, Controles Flet y módulos de interfaz
        if callable(val) or isinstance(val, ft.Control) or "flet" in type(val).__module__:
            continue
        if k in BLACKLIST_KEYS:
            continue
        session_dict[k] = val
    return session_dict
```

---

## 3. Resiliencia contra Bloqueos de Windows (WinError 5 / WinError 32)

### 3.1. Naturaleza del Problema en Windows
En sistemas Windows, los motores antivirus (Windows Defender) y el indexador de búsqueda (`SearchIndexer.exe`) abren de forma intermitente manejadores de archivo sobre bases de datos SQLite (`.hds`, `-wal`, `-shm`). Si la aplicación intenta renombrar, eliminar o escribir de forma exclusiva sobre el archivo en ese microsegundo, el sistema operativo lanza:  
`[WinError 5] Acceso Denegado` o `[WinError 32] El proceso no tiene acceso al archivo porque está siendo utilizado por otro proceso`.

### 3.2. Medidas de Mitigación Implementadas
1. **Bloqueo Reentrante en Memoria (`_io_lock`):** Todas las operaciones que escriben en disco están envueltas en un candado `threading.RLock()`:
   ```python
   with self._io_lock:
       # Operación segura de I/O
   ```
2. **Tiempo de Espera en SQLite (`busy_timeout`):** Se fijó en 10,000 ms (`PRAGMA busy_timeout = 10000;`). Esto instruye al driver SQLite de C a reintentar automáticamente la adquisición del bloqueo durante 10 segundos antes de fallar.
3. **Modo WAL (Write-Ahead Logging):** Separa los lectores de los escritores. Múltiples hilos pueden consultar datos climatológicos sin competir con el hilo que está guardando un escenario de lluvia.
4. **Migración en Caliente de Tablas (DDL Adaptativo):** En lugar de borrar y recrear tablas SQLite para añadir columnas (acción que gatilla bloqueos de archivo), se ejecuta `ALTER TABLE` dentro de bloques `try/except` para mutar el esquema de forma transparente.

---

## 4. Blindaje contra Cierre Forzado del Sistema Operativo ("Guillotina OS")

### 4.1. El Riesgo
Si un usuario hace clic en la "X" superior de la ventana de Windows mientras el hilo en background de autoguardado está escribiendo en la base de datos o transfiriendo tensores Parquet, la base de datos `.hds` puede quedar a medio escribir y corromperse irrecuperablemente.

### 4.2. Protocolo de Intercepción
En [`main.py`](file:///c:/Users/USER/Documents/proyectos/conda/CONAGUA-Completando_Datos/Version11.2/main.py#L116-L132) se implementa el mecanismo de defensa:
```python
# 1. Indicamos a Windows que la app no debe cerrarse automáticamente
page.window.prevent_close = True

def _interceptor_cierre_os(e):
    if e.data == "close":
        # 2. Si el sistema está navegando, guardando o en cálculo crítico, IGNORAMOS la orden
        if is_navigating[0]:
            return 
        
        # 3. Si hay un proyecto abierto, desplegamos diálogo modal de salida segura
        funcion_cierre_seguro = page.session.get("trigger_exit_dialog")
        if funcion_cierre_seguro and page.session.get("current_project_path"):
            funcion_cierre_seguro(None)
        else:
            # Si estamos en la pantalla inicial de bienvenida, cerramos la ventana
            page.window.destroy()

page.window.on_event = _interceptor_cierre_os
```

### 4.3. Protocolo de Cierre Seguro
Cuando el usuario decide cerrar:
1. Se activa una pantalla visual de bloqueo: `"GUARDANDO Y CERRANDO... Escribiendo persistencia final"`.
2. Se invocan las rutinas de persistencia en disco de forma síncrona.
3. Se invoca [`project_manager_instance.close_project()`](file:///c:/Users/USER/Documents/proyectos/conda/CONAGUA-Completando_Datos/Version11.2/infrastructure/project_manager.py#L308-L323):
   - Cambia el flag `session_activa = False` en `hidro_proyecto`.
   - Ejecuta el checkpoint del log WAL hacia la base de datos principal.
   - Dispone el engine SQLAlchemy (`self.engine.dispose()`).
   - Elimina directorios temporales de caché creados durante la sesión.
4. Solo tras completar los pasos anteriores, se despacha `page.window.destroy()`.

---

## 5. Políticas de Serialización Segura

### 5.1. Prohibición Terminante de `pickle`
Se prohíbe el uso de la biblioteca estándar `pickle` para almacenar el estado de proyectos en disco por dos razones:
1. **Seguridad (CWE-502 - Deserialización de Datos No Confiables):** Un archivo `.hds` malicioso podría ejecutar código arbitrario en la máquina del hidrólogo al deserializar clases Python no autenticadas.
2. **Fragilidad de Código:** Si una clase se renombra o cambia de paquete durante una actualización de HyDaS, `pickle` genera errores de `AttributeError` impidiendo abrir proyectos creados en versiones previas.

### 5.2. Estándar de Almacenamiento: Parquet + Base64
- **Formato Tabular:** Los DataFrames de pandas se serializan mediante **Apache Parquet (`pyarrow`)**.
  - Conserva estrictamente los tipos nativos de columnas e índices (`float64`, `datetime64`, `int32`).
  - Aplica compresión columnar de alto desempeño (Snappy).
- **Tipos Escalares:** Los escalares de C de NumPy (`np.int64`, `np.float64`) se normalizan a tipos primitivos mediante la clase custom `NumpyEncoder`.

---

## 6. Política de Respaldos `.bak` y Detección de Caídas (Crash Recovery)

### 6.1. Respaldo Automático Preventivo
Antes de modificar cualquier archivo `.hds` existente en disco, [`save_project`](file:///c:/Users/USER/Documents/proyectos/conda/CONAGUA-Completando_Datos/Version11.2/infrastructure/project_manager.py#L406-L411) genera una copia idéntica con extensión `.bak`:
```python
bak_path = path + ".bak"
if os.path.exists(path):
    try:
        shutil.copy2(path, bak_path)
    except Exception as e:
        print(f"[WARN] No se pudo crear respaldo .bak: {e}", file=sys.stderr)
```
Si ocurriese un corte de energía durante la escritura, el usuario o el soporte técnico puede recuperar la versión íntegra previa simplemente renombrando el archivo `.bak`.

### 6.2. Detección de Caídas Anómalas
En la tabla `hidro_proyecto`, la columna `session_activa` actúa como un centinela:
- Al abrir el proyecto se establece en `True`.
- Al cerrar el proyecto limpiamente se establece en `False`.
- Si el usuario sufre un apagón o reinicio forzado del equipo, en el siguiente arranque [`verificar_recuperacion_sesion()`](file:///c:/Users/USER/Documents/proyectos/conda/CONAGUA-Completando_Datos/Version11.2/infrastructure/project_manager.py#L383) detecta `session_activa == True` y notifica al usuario que el proyecto no fue cerrado de forma estándar, validando la integridad del log WAL y sugiriendo restaurar el `.bak` si se detecta inconsistencia.

---

## 7. Trazabilidad y Registro de Auditoría

Cada operación relevante dentro de la aplicación es registrada en la tabla `hidro_audit_log` con:
- Marca temporal UTC (`datetime.now(timezone.utc)`).
- Nombre del módulo (`EXTRACCION`, `IMPUTACION`, `ANALISIS`, `ARF`, `GASTOS`, `AUTOSAVE`, etc.).
- Tipo de acción (`GUARDAR`, `CHECKPOINT`, `CALCULO_CHEN`, `EXPORTAR_REPORTE`).
- Detalle cuantitativo (por ejemplo: *"Se imputaron 15 estaciones con método IDW"* o *"Cálculo de IDF para Tr=10, 25, 50"*).
