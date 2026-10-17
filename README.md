# HyDaS - Documentación Técnica y Guía de Despliegue

> 🧭 **Centro de Control del Proyecto:**  
> Toda la arquitectura, especificaciones técnicas, protocolos de seguridad, suite de pruebas automatizadas (21 tests) y mapa esquemático de procesos se encuentran centralizados en el **[Centro de Control](file:///c:/Users/USER/Documents/proyectos/conda/CONAGUA-Completando_Datos/Version11.2/centro_de_control/INDICE_DOCUMENTACION.md)** (`centro_de_control/`).

## Resumen de Módulos

El proyecto está estructurado mediante arquitectura limpia (Separación de Preocupaciones) dividiendo el código en los siguientes módulos principales:

1. **`main.py` (App Shell & UI)**: 
   El punto de entrada estricto del programa construido sobre el framework `Flet` (Flutter para Python). Maneja el enrutamiento visual, el contenedor general y la recolección agresiva de memoria (destrucción explícita de referencias) para garantizar que el consumo de RAM sea mínimo incluso en proyectos gigantes.

2. **`core/` (Lógica Pura y Algoritmos)**:
   Contiene el *backend* del sistema. Todo el procesamiento pesado y matemático vive aquí, separado de la interfaz de usuario:
   - **`descarga_logic.py`**: Motor de web scraping y geocruce para descargar masivamente y procesar datos crudos desde los servidores de CONAGUA.
   - **`imputacion_logic.py`**: Módulo matemático intensivo diseñado para la interpolación de datos faltantes mediante IDW (Inverse Distance Weighting), regresiones lineales (MLR) y series de tiempo con SARIMAX.
   - **`gastos_logic.py`**: Motor de hidrología superficial. Calcula gastos pico, genera hietogramas de diseño (SCS, bloques alternos) e hidrogramas sintéticos empleando métodos como Racional, Chow y modelos unitarios (HEC-HMS). Utiliza multi-hilos (`ThreadPoolExecutor`) para procesar el modo distribuido concurrentemente.
   - **`climatologia_logic.py` / `lluvias_logic.py`**: Utilidades para el análisis de lluvias diarias y anuales.
   - **`hidrologia_mx.py`**: Catálogo estático de tablas y constantes hidrológicas mexicanas (valores N, coeficientes C y curvas adimensionales SCS).

3. **`infrastructure/` (Capa de Persistencia e I/O)**:
   - **`project_manager.py`**: Motor seguro de base de datos comprimida `.hds`. Transforma y comprime los datos de Pandas (vía formato `.parquet`) a un archivo maestro `.tar.xz` usando el algoritmo LZMA. Implementa una **transacción atómica con Doble Validación de Integridad** (escribe en temporal, lee para verificar que no haya corrupción, y luego sobreescribe usando comandos del Kernel OS) garantizando pérdida nula de datos.

4. **`ui/` (Componentes Front-End)**: 
   - `components.py`: Botones y contenedores reusables.
   - `views/`: Las pantallas individuales inyectables (Extracción, Imputación, Análisis, Gastos, Climatología).

---

## Instrucciones de Compilación y Empaquetado

### 1. Configuración del Entorno (Anaconda)
Para garantizar una compilación estable, debes clonar un entorno aislado en Anaconda con todas las dependencias y versiones exactas, para evitar colisiones:

1. Abre **Anaconda Prompt** o **PowerShell Prompt** desde Anaconda Navigator (asegúrate de iniciarlo como Administrador si es posible).
2. Navega a la carpeta raíz del proyecto (donde se encuentra `assets/environment.yml`):
   ```powershell
   cd "C:\Ruta\A\Tu\Proyecto\HyDaS"
   ```
3. Crea el entorno leyendo el archivo de configuración `environment.yml` proporcionado:
   ```powershell
   conda env create -f assets/environment.yml
   ```
4. Activa el entorno recién creado:
   ```powershell
   conda activate flet_env
   ```

> **Nota:** Si actualizas dependencias manualmente en el futuro, no olvides respaldarlas ejecutando: `conda env export > assets/environment.yml`. (Asegúrate de que la codificación del archivo se guarde correctamente).

### 2. Generación del Ejecutable (.exe)
Con el entorno activado, usaremos `flet pack` (que internamente llama a PyInstaller) para empaquetar todo el código de Python, más el runtime nativo de Flutter, en un solo ejecutable *standalone*.

Es vital incluir todas las librerías matemáticas ocultas (hidden imports) para que PyInstaller no las omita durante la compilación. Ejecuta el siguiente comando completo en tu terminal:

```powershell
flet pack main.py --name "HyDaS_v11" --add-data "assets;assets" --hidden-import "pandas" --hidden-import "numpy" --hidden-import "scipy" --hidden-import "scipy.spatial" --hidden-import "scipy.interpolate" --hidden-import "scipy.stats" --hidden-import "statsmodels" --hidden-import "sklearn" --hidden-import "pmdarima" --hidden-import "matplotlib" --hidden-import "matplotlib.backends.backend_agg" --hidden-import "matplotlib.pyplot" --hidden-import "seaborn" --hidden-import "folium" --hidden-import "geopy" --hidden-import "shapefile" --hidden-import "shapely" --hidden-import "tabulate" --hidden-import "requests" --hidden-import "jinja2" --hidden-import "openpyxl" --hidden-import "pyarrow" --icon "assets/icon.ico"
```

Este proceso tomará algunos minutos (el archivo puede pesar ~300-500 MB). Al finalizar, se creará una carpeta llamada `dist/` en la cual estará alojado tu archivo **`HyDaS_v11.exe`**.

### 3. Creación del Instalador Oficial (NSIS Setup)
Para distribuir la aplicación a usuarios finales sin complicaciones, el proyecto cuenta con un script de automatización (`crear_instalador.py`) que genera un instalador tradicional (setup) usando NSIS. Este instalador agregará tu software a "Archivos de Programa", creará accesos directos en el escritorio e incluirá un desinstalador oficial de Windows.

1. Asegúrate de tener instalado el motor **NSIS** (Nullsoft Scriptable Install System) en tu computadora. Puedes descargarlo e instalarlo gratis. La ruta típica será `C:\Program Files (x86)\NSIS\makensis.exe`.
2. Una vez que tu ejecutable esté listo dentro de la carpeta `dist/`, lanza el constructor automático:
   ```powershell
   python crear_instalador.py
   ```
3. Si el proceso es exitoso, verás un mensaje de validación y se generará un archivo llamado **`HyDaS_11.exe`** en la raíz del proyecto. Este es el instalador final que puedes subir a las Releases de GitHub o enviarle a tus usuarios.
