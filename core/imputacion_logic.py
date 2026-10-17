import os
import pandas as pd
import numpy as np
from sklearn.linear_model import LinearRegression
from geopy.distance import great_circle 
import pmdarima as pm
import folium 
import traceback
import warnings
import shapefile # <--- REQUISITO: pip install pyshp
import threading
import datetime
import geopandas as gpd
from shapely.geometry import Point, MultiPoint
from shapely.ops import voronoi_diagram

# Ignorar advertencias de modelos para limpieza de consola
warnings.filterwarnings("ignore")

# Señal de interrupción global para el módulo de imputación
señal_abortar_imputacion = threading.Event()

#-------------------------------------------------------------------------------------------------------------------------------------------------------
# 1. GESTIÓN DE ARCHIVOS Y MAPA
#-------------------------------------------------------------------------------------------------------------------------------------------------------

def leer_estaciones(folder_path):
    print(f"--- Escaneando carpeta: {folder_path} ---")
    local_station_files = {}
    try:
        archivos = [f for f in os.listdir(folder_path) if f.endswith('.txt')]
    except Exception as e:
        print(f"Error al listar archivos: {e}")
        return {}

    for nombre_archivo in archivos:
        try:
            path = os.path.join(folder_path, nombre_archivo)
            lat, lon, alt = None, None, None 
            
            with open(path, 'r', encoding='utf-8', errors='ignore') as f:
                for _ in range(20):
                    linea = f.readline()
                    if not linea: break
                    
                    if 'LATITUD' in linea:
                        lat = float(linea.split(':')[1].strip().split(' ')[0])
                    elif 'LONGITUD' in linea:
                        lon = float(linea.split(':')[1].strip().split(' ')[0])
                    elif 'ALTITUD' in linea: 
                        try:
                            alt = float(linea.split(':')[1].strip().split(' ')[0])
                        except:
                            alt = 0.0

                    if lat is not None and lon is not None and alt is not None: break
            
            if alt is None: alt = 0.0

            if lat is not None and lon is not None:
                station_id = nombre_archivo.split('.')[0]
                local_station_files[station_id] = {'file': nombre_archivo, 'lat': lat, 'lon': lon, 'alt': alt, 'path': path}
        except Exception as e:
            print(f"Error leyendo cabecera de {nombre_archivo}: {e}")
    return local_station_files

def generar_mapa_html(station_files, output_dir="."):
    if not station_files: return None
    try:
        lats = [v['lat'] for v in station_files.values()]
        lons = [v['lon'] for v in station_files.values()]
        center = [np.mean(lats), np.mean(lons)]
        m = folium.Map(location=center, zoom_start=8, tiles='CartoDB dark_matter')
        for sid, info in station_files.items():
            folium.Marker([info['lat'], info['lon']], popup=f"Est: {sid}", tooltip=sid, icon=folium.Icon(color="green", icon="info-sign")).add_to(m)
        path = os.path.join(output_dir, "mapa_estaciones.html")
        m.save(path)
        return os.path.abspath(path)
    except Exception as e:
        print(f"Error mapa: {e}")
        return None

#-------------------------------------------------------------------------------------------------------------------------------------------------------
# 2. UTILERÍAS GIS (SHAPEFILE)
#-------------------------------------------------------------------------------------------------------------------------------------------------------

def generar_poligonos_thiessen(station_files, output_path):
    """
    Genera un Shapefile de Polígonos de Thiessen (Voronoi) a partir de las estaciones detectadas.
    """
    try:
        points = []
        ids = []
        for sid, info in station_files.items():
            if info.get('lon') is not None and info.get('lat') is not None:
                points.append(Point(info['lon'], info['lat']))
                ids.append(sid)
                
        if len(points) < 3:
            raise ValueError("Se requieren al menos 3 estaciones para triangular polígonos de Thiessen.")
            
        mp = MultiPoint(points)
        regions = voronoi_diagram(mp)
        
        polygons = []
        station_ids = []
        
        # Mapeo espacial exacto: buscar el punto generador más cercano al centroide de cada polígono
        for poly in regions.geoms:
            poly_center = poly.centroid
            # Encontrar el punto más cercano (que debe ser el generador del diagrama)
            min_dist = float('inf')
            closest_id = None
            for pt, sid in zip(points, ids):
                d = pt.distance(poly_center)
                if d < min_dist:
                    min_dist = d
                    closest_id = sid
            
            polygons.append(poly)
            station_ids.append(closest_id)
            
        gdf = gpd.GeoDataFrame({'ID_EST': station_ids}, geometry=polygons, crs="EPSG:4326")
        
        base_path = os.path.splitext(output_path)[0]
        gdf.to_file(f"{base_path}.shp", driver="ESRI Shapefile", encoding='utf-8')
        return f"{base_path}.shp"
        
    except Exception as e:
        print(f"Error generando Thiessen: {e}")
        import traceback
        traceback.print_exc()
        raise e

#-------------------------------------------------------------------------------------------------------------------------------------------------------
# 3. PARSEO Y RANGO GLOBAL
#-------------------------------------------------------------------------------------------------------------------------------------------------------

def parse_station_data(file_path):
    data = []
    try:
        with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
            lines = f.readlines()
            parsing = False
            for line in lines:
                if 'FECHA' in line and 'PRECIP' in line: parsing = True; continue
                if parsing:
                    parts = line.strip().split()
                    if len(parts) >= 2:
                        try:
                            fecha = parts[0]
                            val = parts[1]
                            precip = float(val) if val != 'NULO' else np.nan
                            data.append([fecha, precip])
                        except: continue
        df = pd.DataFrame(data, columns=['FECHA', 'PRECIP'])
        df['FECHA'] = pd.to_datetime(df['FECHA'], format='%Y-%m-%d', errors='coerce')
        df = df.dropna(subset=['FECHA']).set_index('FECHA').sort_index()
        df = df[~df.index.duplicated(keep='first')]
        return df
    except Exception as e:
        print(f"Error parseando {file_path}: {e}")
        return pd.DataFrame(columns=['FECHA', 'PRECIP'])

def obtener_rango_global_fechas(station_files):
    if not station_files: return None
    
    fechas_min = []
    fechas_max = []
    
    # Optimización Crítica: Escaneo secuencial sin instanciar DataFrames (Evita colapso OOM)
    for info in station_files.values():
        try:
            with open(info['path'], 'r', encoding='utf-8', errors='ignore') as f:
                parsing = False
                local_min = "9999-99-99"
                local_max = "0000-00-00"
                found = False
                for line in f:
                    if not parsing:
                        if 'FECHA' in line and 'PRECIP' in line: 
                            parsing = True
                        continue
                    
                    line_strip = line.strip()
                    if not line_strip: continue
                    
                    parts = line_strip.split()
                    if len(parts) >= 1:
                        d = parts[0]
                        # Validación robusta de formato YYYY-MM-DD
                        if len(d) == 10 and d[4] == '-' and d[7] == '-':
                            if d < local_min: local_min = d
                            if d > local_max: local_max = d
                            found = True
                
                if found:
                    fechas_min.append(local_min)
                    fechas_max.append(local_max)
        except Exception:
            pass
            
    if not fechas_min: return None
    
    todas_min = pd.to_datetime(fechas_min, format='%Y-%m-%d', errors='coerce')
    todas_max = pd.to_datetime(fechas_max, format='%Y-%m-%d', errors='coerce')
    
    real_min = todas_min.min()
    real_max = todas_max.max()
    
    if pd.isna(real_min) or pd.isna(real_max): return None
    
    return pd.date_range(real_min, real_max, freq='D')

#-------------------------------------------------------------------------------------------------------------------------------------------------------
# 4. UTILERÍAS DE CÁLCULO
#-------------------------------------------------------------------------------------------------------------------------------------------------------

def calculate_distance(lat1, lon1, lat2, lon2):
    return great_circle((lat1, lon1), (lat2, lon2)).km

def _filtrar_ruido_intermedio(df_target, df_neighbors, original_mask):
    try:
        temp_df = df_neighbors.copy()
        temp_df['Año'] = temp_df.index.year
        temp_df['Semana'] = temp_df.index.isocalendar().week
        cols_vecinos = [c for c in temp_df.columns if c not in ['Año', 'Semana']]
        
        if not cols_vecinos: return df_target, 0 
            
        maximos_semanales = temp_df.groupby(['Año', 'Semana'])[cols_vecinos].max().max(axis=1)
        idx_to_group = pd.DataFrame({'Año': df_target.index.year, 'Semana': df_target.index.isocalendar().week}, index=df_target.index)
        map_dict = maximos_semanales.to_dict()
        
        def get_limit(r): return map_dict.get((r['Año'], r['Semana']), 9999) 
        limites = idx_to_group.apply(get_limit, axis=1)
        
        imputed_mask = ~original_mask & df_target['PRECIP'].notna()
        suspicious = imputed_mask & (df_target['PRECIP'] > (limites * 1.2 + 5)) 
        count_removed = suspicious.sum()
        
        if count_removed > 0:
            df_target.loc[suspicious, 'PRECIP'] = np.nan
            
        return df_target, count_removed
    except Exception as e:
        return df_target, 0

#-------------------------------------------------------------------------------------------------------------------------------------------------------
# 5. NÚCLEO DE IMPUTACIÓN
#-------------------------------------------------------------------------------------------------------------------------------------------------------

def impute_target_station(target_id, station_files, radius_km, global_range, progress_callback=None, log_callback=None):
    log = []
    
    # Envoltorio EAFP para logs en tiempo real y memoria
    def _log(msg):
        log.append(msg)
        if log_callback: 
            log_callback(msg)
            
    def _prog(pct, msg):
        if progress_callback:
            progress_callback(pct, msg)

    señal_abortar_imputacion.clear() # <-- Limpiar señal al iniciar
    try:
        _log(f"Radio de Búsqueda seleccionado: {radius_km} km")

        info_target = station_files[target_id]
        _prog(None, f"Cargando objetivo: {target_id}...")
        
        df_target = parse_station_data(info_target['path'])
        df_target = df_target.reindex(global_range)
        df_target.index.name = 'FECHA'
        
        df_target['PRECIP_ORIGINAL'] = df_target['PRECIP'].copy()
        original_data_mask = df_target['PRECIP'].notna()
        
        initial_nans = df_target['PRECIP'].isna().sum()
        _log(f"Huecos totales a rellenar: {initial_nans}")

        _prog(None, f"Cargando vecinos (Radio {radius_km}km)...")
        
        lat_t, lon_t = info_target['lat'], info_target['lon']
        neighbors_all = []
        
        for sid, info in station_files.items():
            if sid == target_id: continue
            dist = calculate_distance(lat_t, lon_t, info['lat'], info['lon'])
            
            if dist <= radius_km:
                df_nb = parse_station_data(info['path'])
                df_nb = df_nb.reindex(global_range)
                
                if not df_nb['PRECIP'].dropna().empty:
                    col_name = f"PRECIP_{sid}"
                    neighbors_all.append({
                        'id': sid,
                        'dist': dist,
                        'col_name': col_name,
                        'data': df_nb['PRECIP'].rename(col_name),
                        'weight': 1/(dist**2 if dist>0 else 0.001)
                    })

        _log(f"Vecinos detectados (<{radius_km}km): {len(neighbors_all)}")
        
        if len(neighbors_all) < 30:
            _log("\n⚠️ ALERTA: Menos de 30 vecinos detectados en este radio.")
            _log("   -> Se recomienda descartar este resultado y AUMENTAR EL RADIO de búsqueda para mejorar la precisión matemática.\n")
        
        if neighbors_all:
            df_neighbors = pd.concat([n['data'] for n in neighbors_all], axis=1)
        else:
            df_neighbors = pd.DataFrame(index=global_range)

        _prog(0.2, None)

        # FASE 1: IDW (VECTORIZADO)
        missing_indices = df_target[df_target['PRECIP'].isna()].index
        
        if not df_neighbors.empty and len(missing_indices) > 0:
            _prog(None, "Fase 1: IDW (Req. >= 5 vecinos)...")
            
            if señal_abortar_imputacion.is_set(): return None, "🛑 PROCESO ABORTADO DURANTE FASE IDW."
            
            # --- Vectorización de cruce espacial ---
            df_missing_neighbors = df_neighbors.loc[missing_indices]
            weights = np.array([nb['weight'] for nb in neighbors_all])
            
            data_mat = df_missing_neighbors.values
            valid_mask = ~np.isnan(data_mat)
            valid_counts = valid_mask.sum(axis=1)
            
            to_impute_mask = valid_counts >= 5
            
            data_mat_zeroed = np.nan_to_num(data_mat, 0)
            weights_mat = np.tile(weights, (len(missing_indices), 1))
            weights_valid = weights_mat * valid_mask
            
            num = (data_mat_zeroed * weights_valid).sum(axis=1)
            den = weights_valid.sum(axis=1)
            
            safe_den = np.where(den > 0, den, 1)
            idw_result = np.round(num / safe_den, 2)
            
            imputed_dates = missing_indices[to_impute_mask]
            df_target.loc[imputed_dates, 'PRECIP'] = idw_result[to_impute_mask]
            
            count_idw = to_impute_mask.sum()
            skipped_idw = (~to_impute_mask).sum()
            
            _log(f"Rellenados IDW: {count_idw} (Omitidos: {skipped_idw})")
            
            df_target, rm = _filtrar_ruido_intermedio(df_target, df_neighbors, original_data_mask)
            if rm > 0: _log(f"-> Filtro IDW: Se eliminaron {rm} datos ruidosos.")

        _prog(0.4, "Seleccionando estaciones Élite...")
        
        elite_neighbors = []
        
        if not df_neighbors.empty:
            correlations = df_neighbors.corrwith(df_target['PRECIP'])
            for nb in neighbors_all:
                r_val = correlations.get(nb['col_name'], 0)
                if nb['dist'] <= radius_km and r_val >= 0.6:
                    nb['corr'] = r_val
                    elite_neighbors.append(nb)
            
            if not elite_neighbors:
                _log("⚠️ Relajando criterio a r>0.4...")
                for nb in neighbors_all:
                    r_val = correlations.get(nb['col_name'], 0)
                    if nb['dist'] <= radius_km and r_val >= 0.4:
                        nb['corr'] = r_val
                        elite_neighbors.append(nb)

            elite_neighbors.sort(key=lambda x: x['corr'], reverse=True)
            
        elite_cols = [n['col_name'] for n in elite_neighbors]
        _log(f"Estaciones Élite finales: {len(elite_neighbors)}")

        # FASE 2: MLR (SEMI-VECTORIZADO)
        missing_indices = df_target[df_target['PRECIP'].isna()].index
        
        if len(missing_indices) > 0 and len(elite_neighbors) > 0:
            _prog(None, "Fase 2: MLR...")
            
            if señal_abortar_imputacion.is_set(): return None, "🛑 PROCESO ABORTADO ANTES DE MLR."
            
            df_train = df_neighbors[elite_cols].copy()
            df_train['TARGET'] = df_target['PRECIP']
            df_train = df_train.dropna()
            
            if len(df_train) >= 14: 
                X = df_train.drop(columns=['TARGET'])
                y = df_train['TARGET']
                
                try:
                    model = LinearRegression().fit(X, y)
                    count_mlr = 0
                    
                    # Filtramos fechas válidas antes de iterar para no usar loc miles de veces
                    df_missing_elite = df_neighbors.loc[missing_indices, elite_cols]
                    valid_counts = df_missing_elite.count(axis=1)
                    valid_dates = df_missing_elite[valid_counts >= 5].index
                    
                    for date in valid_dates:
                        if señal_abortar_imputacion.is_set(): return None, "🛑 PROCESO ABORTADO DURANTE PREDICCIÓN MLR."
                        if count_mlr >= 7: break
                        
                        X_input = df_missing_elite.loc[[date]].fillna(0)
                        pred = max(model.predict(X_input)[0], 0)
                        df_target.at[date, 'PRECIP'] = round(pred, 2)
                        count_mlr += 1
                        
                    _log(f"Rellenados MLR: {count_mlr}")
                    df_target, rm = _filtrar_ruido_intermedio(df_target, df_neighbors, original_data_mask)
                    if rm > 0: _log(f"-> Filtro MLR: {rm} eliminados.")
                    
                except Exception as ex:
                    _log(f"Fallo en MLR: {ex}")
            else:
                _log(f"MLR omitido: Insuficientes datos ({len(df_train)}).")

        _prog(0.7, None)

        # FASE 3: SARIMAX
        missing_indices = df_target[df_target['PRECIP'].isna()].index
        
        if len(missing_indices) > 0:
            
            if señal_abortar_imputacion.is_set(): return None, "🛑 PROCESO ABORTADO ANTES DE SARIMAX."
            
            _prog(None, "Fase 3: SARIMAX/Interpolación...")
            
            try:
                exog_data = None
                if len(elite_neighbors) > 0:
                    exog_data = df_neighbors[elite_cols].fillna(0)
                
                y_train_temp = df_target['PRECIP'].interpolate(method='linear', limit_direction='both').fillna(0)
                
                if len(missing_indices) < 2000000:
                    model = pm.auto_arima(
                        y_train_temp, X=exog_data,
                        start_p=1, start_q=1, max_p=2, max_q=2,
                        seasonal=False, stepwise=True,
                        error_action='ignore', suppress_warnings=True
                    )
                    fitted_vals = model.predict_in_sample(X=exog_data)
                    if not isinstance(fitted_vals, pd.Series):
                        fitted_vals = pd.Series(fitted_vals, index=global_range)
                        
                    for date in missing_indices:
                        val = fitted_vals.loc[date]
                        df_target.at[date, 'PRECIP'] = round(max(val, 0), 2)
                    
                    _log(f"Rellenados SARIMAX: {len(missing_indices)}")
                else:
                    raise Exception(f"Demasiados huecos ({len(missing_indices)}).")
                    
            except Exception as ex_arima:
                _log(f"Fallback a Interpolación.")
                before_int = df_target['PRECIP'].isna().sum()
                df_target['PRECIP'] = df_target['PRECIP'].interpolate(method='time', limit_direction='both')
                filled_int = before_int - df_target['PRECIP'].isna().sum()
                _log(f"Rellenados Interpolación: {filled_int}")

        df_target['PRECIP'] = df_target['PRECIP'].round(2)
        final_nans = df_target['PRECIP'].isna().sum()
        
        if final_nans > 0:
            _log(f"⚠️ Quedaron {final_nans} datos vacíos.")
        else:
            _log("✅ Serie completada.")

        _prog(None, "Consolidando...")
        
        df_final = df_target.join(df_neighbors, how='left')
        cols_base = ['PRECIP', 'PRECIP_ORIGINAL']
        cols_final = cols_base + elite_cols + [c for c in df_final.columns if c not in cols_base and c not in elite_cols]
        df_final = df_final[cols_final]
        
        _prog(1.0, "Finalizado.")
        return df_final.reset_index(), "\n".join(log)

    except Exception as e:
        return None, f"Error crítico: {e}\n{traceback.format_exc()}"

def save_target_csv(df, target_id, output_folder):
    if not os.path.exists(output_folder): os.makedirs(output_folder)
    filename = f"{target_id}_imputado.csv"
    path = os.path.join(output_folder, filename)
    df.to_csv(path, index=False)
    return path

def save_imputation_log(log_text, target_id, output_folder):
    """Guarda el log de resultados en un archivo .txt como evidencia de Calidad (QA)."""
    try:
        log_path = os.path.join(output_folder, f"QA_Log_Estacion_{target_id}.txt")
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        with open(log_path, 'w', encoding='utf-8') as f:
            f.write("=================================================================\n")
            f.write(f" REPORTE DE CONTROL DE CALIDAD (QA) - IMPUTACIÓN MATEMÁTICA\n")
            f.write("=================================================================\n")
            f.write(f" Estación Objetivo : {target_id}\n")
            f.write(f" Fecha de Proceso  : {timestamp}\n")
            f.write("=================================================================\n\n")
            f.write(log_text)
            f.write("\n\n=================================================================\n")
            f.write(" FIN DEL REPORTE\n")
            
        return log_path
    except Exception as e:
        print(f"Error al guardar el log QA: {e}")
        return None

def resolver_ruta_hds(ruta_base: str) -> str:
    """Resuelve la ruta definitiva del archivo .hds GeoPackage/SQLite activo."""
    if not ruta_base:
        return ""
    ruta_str = str(ruta_base)
    if ruta_str.endswith('.hds') or ruta_str.endswith('.gpkg') or ruta_str.endswith('.sqlite'):
        return ruta_str
    if ruta_str.endswith('.tar.xz') or ruta_str.endswith('.xz'):
        return ruta_str.replace('.tar.xz', '.hds').replace('.xz', '.hds')
    return ruta_str + ".hds"

def guardar_en_sqlite_hds(df: pd.DataFrame, target_id: str, ruta_base: str) -> bool:
    """
    Guarda el DataFrame imputado directamente en el GeoPackage (.hds / .gpkg).
    Realiza un Upsert (Borra la estación si ya existe y la reescribe) para evitar duplicados.
    """
    if df is None or df.empty or not ruta_base: return False
    
    try:
        ruta_hds = resolver_ruta_hds(ruta_base)

        # 2. Preparar el DataFrame
        df_sql = df.copy()
        if df_sql.index.name == 'FECHA':
            df_sql.reset_index(inplace=True)
        
        df_sql['clave_estacion'] = str(target_id)
        
        # 3. Conexión y Upsert (Protegido por WAL y busy_timeout)
        import sqlite3
        conn = sqlite3.connect(ruta_hds, timeout=10.0)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout = 10000;")
        
        # Guardamos la estación en su propia tabla
        nombre_tabla = f"serie_imputada_{target_id}"
        df_sql.to_sql(nombre_tabla, conn, if_exists='replace', index=False)
        
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        print(f"⚠️ Error guardando {target_id} en SQLite: {e}")
        import traceback; traceback.print_exc()
        return False

def guardar_log_en_sqlite_hds(log_text: str, target_id: str, ruta_base: str) -> bool:
    """Guarda el log de calidad (QA) de una estación en SQLite para persistencia entre sesiones."""
    if not log_text or not ruta_base: return False
    try:
        ruta_hds = resolver_ruta_hds(ruta_base)
            
        import sqlite3
        conn = sqlite3.connect(ruta_hds, timeout=10.0)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout = 10000;")
        conn.execute("CREATE TABLE IF NOT EXISTS qa_logs (station_id TEXT PRIMARY KEY, log_text TEXT, fecha TEXT)")
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn.execute("INSERT OR REPLACE INTO qa_logs (station_id, log_text, fecha) VALUES (?, ?, ?)", (str(target_id), log_text, timestamp))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        print(f"⚠️ Error guardando log QA de {target_id} en SQLite: {e}")
        return False

def obtener_log_de_sqlite_hds(target_id: str, ruta_base: str) -> str:
    """Recupera el log de calidad (QA) de una estación almacenado en SQLite."""
    if not ruta_base: return ""
    try:
        ruta_hds = resolver_ruta_hds(ruta_base)
            
        if not os.path.exists(ruta_hds): return ""
        import sqlite3
        conn = sqlite3.connect(ruta_hds, timeout=10.0)
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='qa_logs'")
        if not cursor.fetchone():
            conn.close()
            return ""
        cursor.execute("SELECT log_text FROM qa_logs WHERE station_id = ?", (str(target_id),))
        row = cursor.fetchone()
        conn.close()
        return row[0] if row else ""
    except Exception:
        return ""

def eliminar_estacion_de_sqlite_hds(target_id: str, ruta_base: str) -> bool:
    """Elimina la tabla de serie imputada y el log QA de una estación en SQLite."""
    if not target_id or not ruta_base: return False
    try:
        ruta_hds = resolver_ruta_hds(ruta_base)
            
        if not os.path.exists(ruta_hds): return False
        import sqlite3
        conn = sqlite3.connect(ruta_hds, timeout=10.0)
        conn.execute("PRAGMA busy_timeout = 10000;")
        conn.execute(f"DROP TABLE IF EXISTS serie_imputada_{target_id}")
        
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='qa_logs'")
        if cursor.fetchone():
            conn.execute("DELETE FROM qa_logs WHERE station_id = ?", (str(target_id),))
            
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        print(f"⚠️ Error eliminando {target_id} de SQLite: {e}")
        return False