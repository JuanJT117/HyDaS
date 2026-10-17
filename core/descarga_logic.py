import sys
import os
import time
import random
import pandas as pd
import requests
import urllib3
import re
import tarfile
import io
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
import shapefile 
from shapely.geometry import shape, Point 
import shutil
import geopandas as gpd
from core import generador_pdf

# --- 1. RESOLUCIÓN ABSOLUTA DE RUTAS (DevSecOps & PyInstaller) ---
# Si se ejecuta como .exe (PyInstaller), sys.frozen es True y los assets están en sys._MEIPASS
if getattr(sys, 'frozen', False):
    BASE_DIR = sys._MEIPASS
else:
    # Como este archivo está en core/, subimos un nivel para llegar a la raíz del proyecto
    BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

# --- 2. CATÁLOGOS Y AGRUPACIONES ---
CATALOGO_ESTADOS_CONAGUA = {
    "Aguascalientes": "ags", "Baja California": "bc", "Baja California Sur": "bcs",
    "Campeche": "camp", "Coahuila": "coah", "Colima": "col", "Chiapas": "chis",
    "Chihuahua": "chih", "Ciudad de México": "df", "Durango": "dgo",
    "Guanajuato": "gto", "Guerrero": "gro", "Hidalgo": "hgo", "Jalisco": "jal",
    "Estado de México": "mex", "Michoacán": "mich", "Morelos": "mor",
    "Nayarit": "nay", "Nuevo León": "nl", "Oaxaca": "oax", "Puebla": "pue",
    "Querétaro": "qro", "Quintana Roo": "qroo", "San Luis Potosí": "slp",
    "Sinaloa": "sin", "Sonora": "son", "Tabasco": "tab", "Tamaulipas": "tamps",
    "Tlaxcala": "tlax", "Veracruz": "ver", "Yucatán": "yuc", "Zacatecas": "zac"
}

MAPA_IDS_CONAGUA = {
    "ags": 1, "bc": 2, "bcs": 3, "camp": 4, "coah": 5, "col": 6, "chis": 7,
    "chih": 8, "df": 9, "dgo": 10, "gto": 11, "gro": 12, "hgo": 13, "jal": 14,
    "mex": 15, "mich": 16, "mor": 17, "nay": 18, "nl": 19, "oax": 20, "pue": 21,
    "qro": 22, "qroo": 23, "slp": 24, "sin": 25, "son": 26, "tab": 27, "tamps": 28,
    "tlax": 29, "ver": 30, "yuc": 31, "zac": 32
}

# --- 3. VARIABLES GEOESPACIALES GLOBALES ---
BBOX_MEXICO = {"min_x": -118.5, "max_x": -86.5, "min_y": 14.5, "max_y": 33.0}

# Candado para evitar corrupción al escribir en el archivo comprimido .tar.xz
escritura_lock = threading.Lock()
# Señal de interrupción de emergencia
señal_abortar = threading.Event()

def obtener_clave_estado(nombre_shp):
    """
    Normalizador heurístico (Fuzzy Matcher) para resolver conflictos entre 
    nombres INEGI (Shapefiles en Mayúsculas/Sufijos) y el catálogo CONAGUA.
    """
    if not nombre_shp: return ""
    t = str(nombre_shp).lower().replace('á','a').replace('é','e').replace('í','i').replace('ó','o').replace('ú','u').strip()
    
    # 1. Resolver discrepancias oficiales absolutas (INEGI vs SMN)
    if "mexico" in t and "ciudad" not in t and "df" not in t: return "mex"
    if "ciudad de mexico" in t or "distrito federal" in t or t == "cdmx": return "df"
    if "coahuila" in t: return "coah"
    if "michoacan" in t: return "mich"
    if "veracruz" in t: return "ver"
    if "queretaro" in t: return "qro"
    
    # 2. Búsqueda flexible blindada contra "Canibalismo de subcadenas"
    # Ordenamos el catálogo por longitud de mayor a menor.
    # Así "Baja California Sur" se evalúa estrictamente antes que "Baja California".
    for nombre_cat, clave in sorted(CATALOGO_ESTADOS_CONAGUA.items(), key=lambda x: len(x[0]), reverse=True):
        nom_limpio = nombre_cat.lower().replace('á','a').replace('é','e').replace('í','i').replace('ó','o').replace('ú','u')
        if nom_limpio in t or t in nom_limpio:
            return clave
            
    return ""

class GestorExtraccion:
    @staticmethod
    def filtrar_estaciones_por_buffer(buffer_geom, df_estaciones: pd.DataFrame, wkt_envolvente: str = None, buffer_km: float = 100.0) -> list:
        try:
            if df_estaciones is None or df_estaciones.empty: return []
            
            df_limpio = df_estaciones.dropna(subset=['lat', 'lon']).copy()
            
            # --- LIMPIEZA AGRESIVA CON REGEX ---
            df_limpio['lat'] = df_limpio['lat'].astype(str).str.replace(',', '.')
            df_limpio['lon'] = df_limpio['lon'].astype(str).str.replace(',', '.')
            
            # Extrae SÓLO el número, ignorando letras (N, W) u otros caracteres corruptos
            df_limpio['lat'] = pd.to_numeric(df_limpio['lat'].str.extract(r'(-?\d+(?:\.\d+)?)')[0], errors='coerce')
            df_limpio['lon'] = pd.to_numeric(df_limpio['lon'].str.extract(r'(-?\d+(?:\.\d+)?)')[0], errors='coerce')
            df_limpio = df_limpio.dropna(subset=['lat', 'lon'])
            
            if df_limpio.empty: return []

            # --- INNOVACIÓN ZERO-TRUST & MULTICUENCA: Núcleo Envolvente ---
            from shapely import wkt
            wkt_env = wkt_envolvente or (buffer_geom if isinstance(buffer_geom, str) else None)
            if wkt_env is not None:
                geom_env = wkt.loads(wkt_env) if isinstance(wkt_env, str) else wkt_env
                buffer_gs = gpd.GeoSeries([geom_env.buffer(buffer_km * 1000)], crs="EPSG:32614").to_crs("EPSG:4326")
                buffer_geom_final = buffer_gs.iloc[0]
            else:
                buffer_geom_final = buffer_geom
                
            if buffer_geom_final is None: return []

            # Adaptación Métrica o Geográfica
            max_lon_abs = df_limpio['lon'].abs().max()
            if max_lon_abs > 180:
                estaciones_gdf = gpd.GeoDataFrame(df_limpio, geometry=gpd.points_from_xy(df_limpio['lon'], df_limpio['lat']), crs='EPSG:32614').to_crs('EPSG:4326')
            else:
                df_limpio['lon_real'] = df_limpio['lon'].apply(lambda x: x if x < 0 else -x)
                df_limpio['lat_real'] = df_limpio['lat'].apply(lambda y: y if y > 0 else -y)
                estaciones_gdf = gpd.GeoDataFrame(df_limpio, geometry=gpd.points_from_xy(df_limpio['lon_real'], df_limpio['lat_real']), crs='EPSG:4326')

            # INTERSECCIÓN INQUEBRANTABLE
            buffer_gs = gpd.GeoSeries([buffer_geom_final], crs="EPSG:4326")
            mask = estaciones_gdf.geometry.intersects(buffer_gs.iloc[0])
            estaciones_objetivo = estaciones_gdf[mask]
            
            claves = estaciones_objetivo['clave'].tolist()
            print(f"[OK] [TLÁLOC] Motor Relacional completado. Estaciones detectadas: {len(claves)}")
            return claves
            
        except Exception as e:
            traceback.print_exc()
            raise RuntimeError(f'Fallo en motor georrelacional: {str(e)}')

# --- 3. MOTOR ESPACIAL (Pyshp y Shapely) ---
def cargar_poligonos(modo):
    """Lee los Shapefiles desde la carpeta assets según el modo seleccionado."""
    if modo == "RESPALDO MASIVO":
        return []
        
    nombre_archivo = "estados.shp" if modo == "POR ESTADO" else "cuencas.shp"
    archivo_shp = os.path.join(BASE_DIR, "assets", nombre_archivo)
    
    if not os.path.exists(archivo_shp):
        raise FileNotFoundError(f"No se encontró el archivo. Ruta buscada: {archivo_shp}")
        
    sf = shapefile.Reader(archivo_shp)
    poligonos = []
    
    for sr in sf.shapeRecords():
        atr = sr.record.as_dict()
        nombre = atr.get('ESTADO') or atr.get('NOM_CUENCA') or "Desconocido"
        id_pol = atr.get('ID_ESTADO') or atr.get('ID_CUENCA') or "0"
        
        geom_real = shape(sr.shape.__geo_interface__)
        # Simplificación EXTREMA para Flet (0.05 grados = ~5.5 km). 
        # Preserve topology=False borra cientos de islas microscópicas que Flet no necesita.
        geom_ligera = geom_real.simplify(0.05, preserve_topology=False)
        
        poligonos.append({
            "id": str(id_pol),
            "nombre": nombre,
            "geometria": geom_ligera.__geo_interface__, 
            "shapely_obj": geom_real 
        })
        
    return poligonos

def detectar_clic_poligono(poligonos, lon, lat):
    """Verifica si la coordenada cae dentro de algún polígono con Shapely."""
    punto_clic = Point(lon, lat)
    for pol in poligonos:
        if pol["shapely_obj"].contains(punto_clic):
            return pol
    return None

# --- 4. MOTOR DE WEB SCRAPING Y ETL ---
def obtener_archivos_directorio(url_estado):
    """Hace scraping del índice web de CONAGUA para obtener los nombres de los .txt."""
    try:
        response = requests.get(url_estado, timeout=30, verify=False) 
        response.raise_for_status()
        archivos = re.findall(r'href="(\d+\.txt)"', response.text)
        return list(set(archivos)) 
    except Exception as e:
        return []

def extraer_metadata(texto_crudo):
    """Extrae coordenadas y datos base usando búsqueda insensible a mayúsculas."""
    metadata = {"lat": None, "lon": None, "estado": "Desconocido", "nombre": "Desconocido"}
    
    # Escaneamos un poco más de líneas (20) por si acaso
    cabecera = "\n".join(texto_crudo.splitlines()[:20])
    
    # CORRECCIÓN: re.IGNORECASE permite encontrar 'Latitud', 'LATITUD' o 'latitud'
    match_lat = re.search(r'LATITUD\s*:\s*(-?\d+\.?\d*)', cabecera, re.IGNORECASE)
    match_lon = re.search(r'LONGITUD\s*:\s*(-?\d+\.?\d*)', cabecera, re.IGNORECASE)
    match_est = re.search(r'ESTADO\s*:\s*(.*)', cabecera, re.IGNORECASE)
    match_nom = re.search(r'NOMBRE\s*:\s*(.*)', cabecera, re.IGNORECASE)
    
    if match_lat: metadata["lat"] = float(match_lat.group(1))
    if match_lon: metadata["lon"] = float(match_lon.group(1))
    if match_est: metadata["estado"] = match_est.group(1).strip().replace('°', '')
    if match_nom: metadata["nombre"] = match_nom.group(1).strip()
    
    return metadata

def descargar_y_procesar_estacion(url_archivo, clave_estado, poligonos_cuencas):
    """Hilo individual de descarga, limpieza y geocruce."""
    if señal_abortar.is_set():
        return False, None, None
        
    # Agregamos reintentos para evitar pérdida de datos por rate limit de CONAGUA
    for intento in range(4):
        try:
            # REGLA ZERO-TRUST: Simular latencia humana y usar Headers legítimos
            time.sleep(random.uniform(0.6, 1.5))
            headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) HyDaS/11'}
            
            r = requests.get(url_archivo, headers=headers, timeout=30, verify=False)
            
            # Filtro 404 (Si no existe, ignorar silenciosamente y salir rápido)
            if r.status_code == 404:
                return False, None, None
            
            # Si hay error temporal (500, 502, 503, 504), forzar reintento
            if r.status_code >= 500:
                time.sleep(1 + intento)
                continue
                
            r.raise_for_status()
            texto = r.text
            
            # Filtro de archivos basura
            if len(texto) < 500 or "<html" in texto.lower():
                return False, None, None
                
            nombre_archivo = url_archivo.split('/')[-1]
            
            # CORRECCIÓN: Limpiamos el prefijo 'dia' para que nuestra BD tenga el ID puro
            clave_estacion = nombre_archivo.replace('.txt', '').replace('dia', '')
            
            meta = extraer_metadata(texto)
            meta["clave"] = clave_estacion
            meta["estado_origen"] = clave_estado.upper()
            meta["cuenca_id"] = "No Asignada"
            meta["cuenca_nombre"] = "No Asignada"
            
            # Geocruce
            if meta["lat"] and meta["lon"] and poligonos_cuencas:
                lon_real = meta["lon"] if meta["lon"] < 0 else -meta["lon"]
                pol_detectado = detectar_clic_poligono(poligonos_cuencas, lon_real, meta["lat"])
                if pol_detectado:
                    meta["cuenca_id"] = pol_detectado["id"]
                    meta["cuenca_nombre"] = pol_detectado["nombre"]
            
            contenido_bytes = texto.encode('utf-8')
            return True, meta, contenido_bytes
        except requests.exceptions.RequestException:
            # Errores de Timeout o conexión se reintentan
            time.sleep(1 + intento * 1.5)
            
    # Si falla 4 veces, lo ignoramos para no frenar el enjambre
    return False, None, None

# =======================================================
# 4.5 MOTOR DE INDEXACIÓN EN MEMORIA (In-Memory)
# =======================================================
def indexar_base_datos_tar(ruta_tar, poligonos_cuencas, callback_log, callback_progreso):
    """Escanea la cabecera de cada archivo en el tar.xz y reconstruye el DataFrame."""
    if not os.path.exists(ruta_tar): return None
        
    registros = []
    try:
        with tarfile.open(ruta_tar, "r:xz") as tar:
            miembros = [m for m in tar.getmembers() if m.isfile() and m.name.endswith('.txt')]
            total = len(miembros)
            
            for i, miembro in enumerate(miembros):
                if señal_abortar.is_set(): return None
                    
                partes = miembro.name.split('/')
                if len(partes) != 2: continue
                
                f = tar.extractfile(miembro)
                if not f: continue
                
                # REGLA ZERO-TRUST: Leer solo cabecera (1000 bytes) para no colapsar la RAM pero asegurar que alcance LATITUD
                cabecera = f.read(1000).decode('utf-8', errors='ignore')
                meta = extraer_metadata(cabecera)
                meta["clave"] = partes[1].replace('.txt', '').replace('dia', '')
                meta["estado_origen"] = partes[0].upper()
                meta["cuenca_id"], meta["cuenca_nombre"] = "No Asignada", "No Asignada"
                
                # Geocruce en tiempo real 
                if meta["lat"] and meta["lon"] and poligonos_cuencas:
                    lon_real = meta["lon"] if meta["lon"] < 0 else -meta["lon"]
                    pol_detectado = detectar_clic_poligono(poligonos_cuencas, lon_real, meta["lat"])
                    if pol_detectado:
                        meta["cuenca_id"], meta["cuenca_nombre"] = pol_detectado["id"], pol_detectado["nombre"]
                
                registros.append(meta)
                if i % 50 == 0 or i == total - 1: callback_progreso((i + 1) / total)

        df = pd.DataFrame(registros)
        columnas = ["lat", "lon", "estado", "nombre", "clave", "estado_origen", "cuenca_id", "cuenca_nombre"]
        for col in columnas:
            if col not in df.columns: df[col] = "Desconocido"
        return df[columnas]
    except Exception as e:
        callback_log(f"> [ERROR CRÍTICO] Fallo de indexación LZMA: {e}")
        return None

# --- 5. ORQUESTADOR PRINCIPAL ---
def procesar_descarga(modo, elementos, ruta_tar_target, df_catalogo, callback_log, callback_progreso, callback_mapa=None, carpeta_previa=None):
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    señal_abortar.clear() 

    try:
        if modo == "RESPALDO MASIVO":
            callback_log(f"> ---------------------------------------")
            callback_log(f"> INICIANDO PROTOCOLO DE RESPALDO MASIVO (LZMA)")
            
            ruta_base = os.path.dirname(ruta_tar_target)
            os.makedirs(ruta_base, exist_ok=True)
            
            poligonos_cuencas = []
            try: 
                poligonos_cuencas = cargar_poligonos("POR CUENCA")
                callback_log(f"> Capa de Cuencas cargada para cruce espacial.")
            except: pass
            
            todas_las_urls = []
            callback_log(f"> 🔍 Cargando Lista estaciones publicas de CONAGUA...")
            ruta_txt_estaciones = os.path.join(BASE_DIR, "assets", "Claves-Estaciones-CONAGUA.txt")
            if not os.path.exists(ruta_txt_estaciones):
                callback_log(f"> [CRÍTICO] No se encontró el catálogo {ruta_txt_estaciones}.")
                return None, None
                
            try:
                # 1. ESCUDO PANDAS: Forzamos la lectura como cadena de texto pura (dtype=str)
                try: df_estaciones = pd.read_csv(ruta_txt_estaciones, sep='\t', encoding='utf-8', dtype=str)
                except UnicodeDecodeError: df_estaciones = pd.read_csv(ruta_txt_estaciones, sep='\t', encoding='latin1', dtype=str)
                
                df_estaciones.columns = [str(c).strip() for c in df_estaciones.columns]
                for _, row in df_estaciones.iterrows():
                    clave_estado = str(row['Clave estado']).strip().lower()
                    
                    # 2. ESCUDO DE INTEGRIDAD: Forzamos los 5 dígitos rellenando con ceros a la izquierda.
                    # Esto repara las URLs de los estados 01 al 09 (ej. dia1001.txt -> dia01001.txt)
                    clave_estacion = str(row['Clave']).split('.')[0].strip().zfill(5)
                    
                    if clave_estado in CATALOGO_ESTADOS_CONAGUA.values():
                        url = f"https://smn.conagua.gob.mx/tools/RESOURCES/Normales_Climatologicas/Diarios/{clave_estado}/dia{clave_estacion}.txt"
                        todas_las_urls.append((url, clave_estado))
            except Exception as e:
                callback_log(f"> [ERROR CRÍTICO] Fallo al parsear la Lista de estaciones: {e}")
                return None, None
            
            total_urls = len(todas_las_urls)
            callback_log(f"> 📍 Lista validada: {total_urls} estaciones a descargar.")
            
            progreso_estados = {}
            for clave in CATALOGO_ESTADOS_CONAGUA.values():
                total_est = sum(1 for u, c in todas_las_urls if c == clave)
                progreso_estados[clave.upper()] = {"procesados": 0, "total": total_est if total_est > 0 else 1}
            
            estaciones_procesadas, exitosos, registros_catalogo = 0, 0, []
            
            with tarfile.open(ruta_tar_target, "w:xz") as tar:
                with ThreadPoolExecutor(max_workers=15) as executor:
                    futuros_map = {executor.submit(descargar_y_procesar_estacion, url, clave, poligonos_cuencas): clave for url, clave in todas_las_urls}
                    
                    for futuro in as_completed(futuros_map):
                        if señal_abortar.is_set():
                            callback_log("> [🛑] ABORTANDO EJECUCIÓN...")
                            break 
                        
                        clave_estado = futuros_map[futuro]
                        estaciones_procesadas += 1
                        progreso_estados[clave_estado.upper()]["procesados"] += 1
                        
                        exito, meta, datos_bytes = futuro.result()
                        if exito:
                            exitosos += 1
                            registros_catalogo.append(meta)
                            with escritura_lock:
                                info = tarfile.TarInfo(name=f"{meta['estado_origen']}/{meta['clave']}.txt")
                                info.size = len(datos_bytes)
                                tar.addfile(info, io.BytesIO(datos_bytes))
                        
                        if estaciones_procesadas % 150 == 0 or estaciones_procesadas == total_urls:
                            callback_progreso(estaciones_procesadas / total_urls)
                            if callback_mapa:
                                porcentajes = {k: min(v["procesados"]/v["total"], 1.0) for k, v in progreso_estados.items()}
                                callback_mapa(porcentajes)
            
            if not señal_abortar.is_set():
                callback_log(f"> Generando catálogo relacional (In-Memory)...")
                df_cat = pd.DataFrame(registros_catalogo)
                columnas = ["lat", "lon", "estado", "nombre", "clave", "estado_origen", "cuenca_id", "cuenca_nombre"]
                for col in columnas:
                    if col not in df_cat.columns: df_cat[col] = "Desconocido"
                df_cat = df_cat[columnas]
                callback_log(f"> ✅ RESPALDO COMPLETADO. Estaciones: {exitosos}")
                return ruta_base, df_cat
                
            return None, None

        elif modo in ["POR ESTADO", "POR CUENCA", "CUENCA OBJETIVO"]:
            callback_log(f"> ---------------------------------------")
            callback_log(f"> INICIANDO MOTOR DE EXTRACCIÓN LOCAL ({modo})")
            
            if df_catalogo is None or df_catalogo.empty:
                callback_log("> [ERROR] No hay un Índice HDS activo en memoria. Vincula una BD primero.")
                return None, None
                
            if not os.path.exists(ruta_tar_target):
                callback_log(f"> [ERROR] No se encontró el archivo de base de datos en: {ruta_tar_target}")
                return None, None
                
            if modo == "POR ESTADO":
                claves_seleccionadas = []
                for e in elementos:
                    clave = obtener_clave_estado(e)
                    if clave: claves_seleccionadas.append(clave.upper())
                    else: callback_log(f"> [AVISO] Zona no reconocida o fuera de México: {e}")
                
                df_filtro = df_catalogo[df_catalogo['estado_origen'].isin(claves_seleccionadas)]
                callback_log("> --- RESUMEN DE SELECCIÓN ---")
                conteo = df_filtro['estado_origen'].value_counts()
                for est_clave, cant in conteo.items():
                    nom_est = next((k for k, v in CATALOGO_ESTADOS_CONAGUA.items() if v.upper() == est_clave), est_clave)
                    callback_log(f"> 📍 {nom_est}: {cant} estaciones localizadas")
            elif modo == "POR CUENCA": 
                df_filtro = df_catalogo[df_catalogo['cuenca_nombre'].isin(elementos)]
                callback_log("> --- RESUMEN DE SELECCIÓN ---")
                conteo = df_filtro['cuenca_nombre'].value_counts()
                for cue_nom, cant in conteo.items():
                    callback_log(f"> 🌊 Cuenca [{cue_nom}]: {cant} estaciones localizadas")
            elif modo == "CUENCA OBJETIVO":
                claves_str = [str(c) for c in elementos]
                df_filtro = df_catalogo[df_catalogo['clave'].astype(str).isin(claves_str)]
                callback_log("> --- RESUMEN DE SELECCIÓN ---")
                callback_log(f"> 🎯 Cuenca Objetivo: {len(df_filtro)} estaciones dentro del Buffer.")
                
            total_archivos = len(df_filtro)
            if total_archivos == 0:
                callback_log("> [ERROR] No se encontraron estaciones para la zona seleccionada.")
                return None, None
                
            callback_log(f"> Total a extraer de la BD: {total_archivos} archivos.")
            
            ruta_base = os.path.dirname(ruta_tar_target)
            carpeta_salida = os.path.join(ruta_base, "Tlaloc_Extraccion_Activa")
            os.makedirs(carpeta_salida, exist_ok=True)
            
            # --- PROTECCIÓN CONTRA DIRECTORIOS SUCIOS ---
            # Purgamos los archivos viejos (ej. Tamaulipas) para que no se sumen a la nueva extracción
            try:
                for f in os.listdir(carpeta_salida):
                    if f.endswith(".txt"): os.remove(os.path.join(carpeta_salida, f))
            except Exception:
                pass
            # ---------------------------------------------
            
            if carpeta_previa and os.path.exists(carpeta_previa) and os.path.abspath(carpeta_previa) != os.path.abspath(carpeta_salida):
                callback_log("> 📚 Sincronizando historial pluvial de la sesión actual...")
                try:
                    for f in os.listdir(carpeta_previa):
                        if f.endswith(".txt"): shutil.copy(os.path.join(carpeta_previa, f), os.path.join(carpeta_salida, f))
                except Exception as ex:
                    callback_log(f"> [AVISO] Ocurrió un conflicto menor al fusionar archivos previos: {ex}")
            
            nombres_a_extraer = [f"{row['clave']}.txt" for _, row in df_filtro.iterrows()]
            extraidos, faltantes = 0, 0
            
            with tarfile.open(ruta_tar_target, "r:xz") as tar:
                miembros_tar = {os.path.basename(m.name): m for m in tar.getmembers() if m.isfile()}
                
                for i, nombre_archivo in enumerate(nombres_a_extraer):
                    if señal_abortar.is_set():
                        callback_log("> [🛑] ABORTANDO EXTRACCIÓN...")
                        break
                        
                    if nombre_archivo in miembros_tar:
                        f_in = tar.extractfile(miembros_tar[nombre_archivo])
                        if f_in:
                            with open(os.path.join(carpeta_salida, nombre_archivo), 'wb') as f_out:
                                f_out.write(f_in.read())
                            extraidos += 1
                    else:
                        faltantes += 1 
                    
                    if i % 20 == 0 or i == total_archivos - 1: callback_progreso((i + 1) / total_archivos)
            
            callback_log(f"> ---------------------------------------")
            callback_log(f"> ✅ EXTRACCIÓN COMPLETADA.")
            callback_log(f"> Archivos desempacados y listos: {extraidos}")
            if faltantes > 0: callback_log(f"> [AVISO] {faltantes} estaciones de la BD no estaban en el archivo.")
            
            return carpeta_salida, None
        return None, None
    except Exception as e:
        callback_log(f"> [ERROR CRÍTICO]: {str(e)}")
        import traceback; traceback.print_exc()
        return None, None
    
# =======================================================
# 6. MÓDULO DE INSPECCIÓN (SONDA EN VIVO)
# =======================================================

def obtener_catalogo_visor(modo, elementos, df_catalogo):
    """Filtra la BD en memoria y devuelve una lista de estaciones para el visor."""
    if df_catalogo is None or df_catalogo.empty or not elementos: 
        return []
        
    if modo == "POR ESTADO":
        claves = []
        for e in elementos:
            clave = obtener_clave_estado(e)
            if clave: claves.append(clave.upper())
        df_filtro = df_catalogo[df_catalogo['estado_origen'].isin(claves)]
    else:
        df_filtro = df_catalogo[df_catalogo['cuenca_nombre'].isin(elementos)]
        
    return df_filtro[['nombre', 'clave', 'estado_origen']].to_dict('records')

def inspeccionar_estacion_aislada(clave, estado_origen, ruta_tar):
    """Lee 5 líneas del disco local y 5 líneas de CONAGUA y las compara."""
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    
    # 1. LECTURA LOCAL (Disco Duro)
    local_lines = []
    try:
        with tarfile.open(ruta_tar, "r:xz") as tar:
            nombre_archivo = f"{clave}.txt"
            miembro = next((m for m in tar.getmembers() if os.path.basename(m.name) == nombre_archivo), None)
            if miembro:
                f = tar.extractfile(miembro)
                if f:
                    # Leemos y nos quedamos solo con las últimas 5 líneas que no estén vacías
                    lineas = [l.decode('utf-8').strip() for l in f.readlines() if l.strip()]
                    local_lines = lineas[-5:] if lineas else ["Archivo vacío."]
            else:
                local_lines = ["No encontrada en BD Local."]
    except Exception as e:
        local_lines = [f"Error al leer local: {e}"]

    # 2. LECTURA SERVIDOR (Scraping Vivo)
    server_lines = []
    try:
        url = f"https://smn.conagua.gob.mx/tools/RESOURCES/Normales_Climatologicas/Diarios/{estado_origen.lower()}/dia{clave}.txt"
        r = requests.get(url, timeout=5, verify=False)
        
        if r.status_code == 200:
            lineas_srv = [l.strip() for l in r.text.splitlines() if l.strip()]
            server_lines = lineas_srv[-5:] if lineas_srv else ["Archivo vacío en servidor."]
        else:
            server_lines = [f"Error 404: No existe en servidor."]
    except Exception as e:
        server_lines = [f"Error de conexión: {e}"]

    return local_lines, server_lines

# =======================================================
# 7. MÓDULO DE AUDITORÍA PROFUNDA Y REPORTE (DEEP SCAN)
# =======================================================

def auditar_base_datos_profunda(ruta_tar, ruta_salida, callback_log, callback_progreso):
    """Escaneo físico riguroso con extracción de KPIs Avanzados para el Dashboard DQA."""
    
    if not os.path.exists(ruta_tar):
        return False, "Error: No se encontró el archivo .tar.xz.", {}

    # NUEVO DWH TEMPORAL: Estructura ampliada para KPIs de Alta Dirección
    stats = {clave.upper(): {
                "sanas": 0, "corruptas": 0, "años_inicio": [], "años_fin": [], 
                "total_dias": 0, "nulos": 0, "outliers": 0, "max_gap": 0, "nom": nom
             } for nom, clave in CATALOGO_ESTADOS_CONAGUA.items()}
    
    nacional_sanas, nacional_corruptas = 0, 0
    callback_log("> 🔎 INICIANDO MOTOR DE ESCANEO DE BAJO NIVEL (DQA KPI Extraction)...")
    señal_abortar.clear()

    try:
        # FASE 1: EXTRACCIÓN DE KPIS EN MEMORIA
        with tarfile.open(ruta_tar, "r:xz") as tar:
            miembros = tar.getmembers()
            total_archivos = len(miembros)
            
            for i, miembro in enumerate(miembros):
                if señal_abortar.is_set(): return False, "🛑 AUDITORÍA ABORTADA.", {}
                if i % 50 == 0: callback_progreso(i / total_archivos)
                    
                if not miembro.isfile() or not miembro.name.endswith(".txt"): continue
                    
                partes = miembro.name.split('/')
                if len(partes) != 2: continue
                estado_clave = partes[0].upper()
                if estado_clave not in stats: continue
                
                if miembro.size < 300:
                    stats[estado_clave]["corruptas"] += 1; nacional_corruptas += 1; continue
                    
                f = tar.extractfile(miembro)
                if not f:
                    stats[estado_clave]["corruptas"] += 1; nacional_corruptas += 1; continue
                
                try:
                    contenido = f.read().decode('utf-8', errors='ignore').splitlines()
                    lineas_validas = [l for l in contenido if re.match(r'^\d{4}-\d{2}-\d{2}', l)]
                    
                    if not lineas_validas or len(lineas_validas) < 30:
                        stats[estado_clave]["corruptas"] += 1; nacional_corruptas += 1; continue
                        
                    fechas, valores = [], []
                    for linea in lineas_validas:
                        pts = linea.split()
                        if len(pts) >= 2:
                            fechas.append(pts[0])
                            v_str = pts[1].lower()
                            if v_str == 'nulo' or v_str == '-99.9':
                                stats[estado_clave]["nulos"] += 1
                            else:
                                try:
                                    v_float = float(v_str)
                                    if v_float > 300.0: # Umbral heurístico de Huracán/Error de Sensor
                                        stats[estado_clave]["outliers"] += 1
                                except ValueError:
                                    stats[estado_clave]["nulos"] += 1
                    
                    stats[estado_clave]["total_dias"] += len(lineas_validas)
                    
                    # Cálculo de Brecha Temporal (Gap Máximo)
                    años_unicos = sorted(list(set([int(f.split('-')[0]) for f in fechas])))
                    if len(años_unicos) > 1:
                        gaps = [años_unicos[j] - años_unicos[j-1] for j in range(1, len(años_unicos))]
                        max_g = max(gaps) - 1
                        if max_g > stats[estado_clave]["max_gap"]: stats[estado_clave]["max_gap"] = max_g
                        
                    stats[estado_clave]["años_inicio"].append(años_unicos[0])
                    stats[estado_clave]["años_fin"].append(años_unicos[-1])
                    stats[estado_clave]["sanas"] += 1; nacional_sanas += 1
                    
                except Exception:
                    stats[estado_clave]["corruptas"] += 1; nacional_corruptas += 1

        callback_progreso(1.0)
        callback_log(f"> ✅ KPIs Calculados. Sanas: {nacional_sanas} | Corruptas: {nacional_corruptas}")
        
        # FASE 2: DELEGACIÓN AL MOTOR PDF
        callback_log("> 📊 Renderizando Dashboard DQA Vectorial (FPDF2)...")
        # --- Arquitectura Hexagonal: El Core delega la presentación visual al Adaptador PDF ---
        ruta_pdf = generador_pdf.generar_dashboard_dqa(stats, ruta_salida, nacional_sanas, nacional_corruptas)
        
        return True, f"Dashboard Generado en: {ruta_pdf}", stats
        
    except Exception as e:
        import traceback; traceback.print_exc()
        return False, f"Error Crítico DQA: {str(e)}", {}
