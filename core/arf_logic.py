import os
import sqlite3
import pandas as pd
import numpy as np
import geopandas as gpd
from scipy.interpolate import Rbf
from concurrent.futures import ThreadPoolExecutor
import traceback
import warnings
warnings.filterwarnings("ignore")
import gc
import io
import base64
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import seaborn as sns

class MotorFiltradoARF:
    
    @staticmethod
    def filtrar_datos(df: pd.DataFrame, usar_c1=True, usar_c2=True, usar_c3=True):
        """
        [FILTRO ESTADÍSTICO ESTRICTO - MÓDULO 3]
        Evalúa y purga anomalías de la serie temporal basándose en vecinos élite.
        """
        if df is None or df.empty: return pd.DataFrame() 
        try:
            df_proc = df.copy()
            cols_precip_n = [col for col in df_proc.columns if (col.startswith('PRECIP_') or col.startswith('N_')) and col not in ['PRECIP_imputado', 'PRECIP_original']]
            
            if usar_c3:
                if 'Año' not in df_proc.columns: df_proc['Año'] = df_proc.index.year
                if 'Semana' not in df_proc.columns: df_proc['Semana'] = df_proc.index.isocalendar().week
                if cols_precip_n:
                    maximos_por_año_semana = df_proc.groupby(['Año', 'Semana'])[cols_precip_n].max().max(axis=1)
                else:
                    maximos_por_año_semana = pd.Series(dtype=float)

            mask_eliminar = pd.Series(False, index=df_proc.index)
            
            if usar_c1:
                if cols_precip_n:
                    condicion_1 = df_proc['PRECIP_original'].isnull() & df_proc[cols_precip_n].isnull().all(axis=1)
                    mask_eliminar = mask_eliminar | condicion_1
                else:
                    mask_eliminar = mask_eliminar | df_proc['PRECIP_original'].isnull()

            if usar_c2 and cols_precip_n:
                condicion_2 = (df_proc['PRECIP_original'].isnull()) & (df_proc[cols_precip_n].notnull().sum(axis=1) < 5)
                mask_eliminar = mask_eliminar | condicion_2

            if usar_c3:
                def get_max(row): return maximos_por_año_semana.get((row['Año'], row['Semana']), 0)
                map_max = df_proc.apply(get_max, axis=1)
                condicion_3 = df_proc['PRECIP_original'].isnull() & (df_proc['PRECIP_imputado'] > map_max)
                mask_eliminar = mask_eliminar | condicion_3

            return df_proc[~mask_eliminar]
        except Exception as e:
            traceback.print_exc()
            return None

    @staticmethod
    def calcular_continuidad_temporal(df_filtrado: pd.DataFrame, clave_estacion: str) -> dict:
        """
        Analiza lagunas (Gaps) y rachas continuas en la serie filtrada.
        """
        try:
            if df_filtrado is None or df_filtrado.empty: return None
            
            df = df_filtrado.sort_index()
            fechas_validas = df[df['PRECIP_imputado'].notnull()].index
            if fechas_validas.empty: return None

            fecha_inicio = fechas_validas.min()
            fecha_fin = fechas_validas.max()
            dias_totales = (fecha_fin - fecha_inicio).days + 1
            dias_efectivos = len(fechas_validas)
            porcentaje = round((dias_efectivos / dias_totales) * 100, 2) if dias_totales > 0 else 0

            # Vectorización binaria para auditoría de brechas hídricas
            rango_completo = pd.date_range(start=fecha_inicio, end=fecha_fin, freq='D')
            serie_binaria = pd.Series(0, index=rango_completo)
            serie_binaria.loc[fechas_validas] = 1

            es_nulo = (serie_binaria == 0)
            es_valido = (serie_binaria == 1)

            max_gap_dias = es_nulo.groupby((~es_nulo).cumsum()).sum().max()
            max_seq_dias = es_valido.groupby((~es_valido).cumsum()).sum().max()

            return {
                "Clave": clave_estacion,
                "Inicio": fecha_inicio.strftime('%Y-%m-%d'),
                "Fin": fecha_fin.strftime('%Y-%m-%d'),
                "Dias_Totales": dias_totales,
                "Dias_Efectivos": dias_efectivos,
                "Integridad_%": porcentaje,
                "Max_Gap_Dias": int(max_gap_dias) if pd.notnull(max_gap_dias) else 0,
                "Mayor_Racha_Dias": int(max_seq_dias) if pd.notnull(max_seq_dias) else 0
            }
        except Exception as e:
            traceback.print_exc()
            return None

    @staticmethod
    def procesar_gpkg_arf(ruta_base: str, claves_objetivo: list, callback_progreso=None) -> list:
        """
        [MOTOR RELACIONAL ARF]
        Lee las series imputadas EXCLUSIVAMENTE desde el .gpkg, aplica la purga 
        obligatoria (C1, C2, C3) y guarda el resultado filtrado.
        """
        if not ruta_base or not claves_objetivo: return []

        if ruta_base.endswith('.tar.xz') or ruta_base.endswith('.xz'):
            ruta_hds = ruta_base.replace('.tar.xz', '.gpkg').replace('.xz', '.gpkg')
        elif not ruta_base.endswith('.gpkg') and not ruta_base.endswith('.sqlite'):
            ruta_hds = ruta_base + "_ARF.gpkg"
        else:
            ruta_hds = ruta_base
            
        print(f"📦 [TLÁLOC] Leyendo datos para Módulo 4 desde BD Relacional: {ruta_hds}")
        
        resultados_continuidad = []
        total = len(claves_objetivo)
        
        import sqlite3
        conn = sqlite3.connect(ruta_hds)
        conn.execute("PRAGMA journal_mode=WAL;")
        
        try:
            for idx, clave in enumerate(claves_objetivo):
                try:
                    query = f"SELECT * FROM serie_imputada_{clave}"
                    df_estacion = pd.read_sql_query(query, conn, parse_dates=['FECHA'])
                except pd.io.sql.DatabaseError:
                    # Ignorar si no existe la tabla o la estación
                    continue
                    
                if df_estacion.empty: continue
                
                df_estacion.set_index('FECHA', inplace=True)
                
                # Formatear columnas para el motor de filtrado (compatibilidad con Módulo 3/4)
                mapa_nombres = {}
                if 'PRECIP' in df_estacion.columns: mapa_nombres['PRECIP'] = 'PRECIP_imputado'
                if 'PRECIP_ORIGINAL' in df_estacion.columns: mapa_nombres['PRECIP_ORIGINAL'] = 'PRECIP_original'
                df_estacion.rename(columns=mapa_nombres, inplace=True)
                
                if 'PRECIP_original' not in df_estacion.columns and 'PRECIP_imputado' in df_estacion.columns:
                    df_estacion['PRECIP_original'] = df_estacion['PRECIP_imputado']
                
                # Purga inmutable obligatoria
                df_filtrado = MotorFiltradoARF.filtrar_datos(df_estacion, usar_c1=True, usar_c2=True, usar_c3=True)
                
                if df_filtrado is not None and not df_filtrado.empty:
                    df_filtrado.reset_index(inplace=True)
                    df_filtrado['clave_estacion'] = clave
                    
                    # Upsert (Replace para evitar duplicados y colisiones)
                    nombre_tabla_out = f"serie_filtrada_arf_{clave}"
                    df_filtrado.to_sql(nombre_tabla_out, conn, if_exists='replace', index=False)
                    
                    df_filtrado.set_index('FECHA', inplace=True)
                    stats = MotorFiltradoARF.calcular_continuidad_temporal(df_filtrado, clave)
                    if stats: resultados_continuidad.append(stats)
                
                if callback_progreso: callback_progreso((idx + 1) / total)
                    
            import gc; gc.collect()
            
        except Exception as e:
            import traceback; traceback.print_exc()
            raise RuntimeError(str(e))
        finally:
            conn.close()

        return sorted(resultados_continuidad, key=lambda x: x['Integridad_%'], reverse=True)

class MotorCalculoARF:
    """
    Orquestador Matemático para los 3 métodos de Reducción Areal.
    Maneja HPC (High Performance Computing) para operaciones espaciotemporales.
    """
    
    @staticmethod
    def _calculate_empirical_arf(area_km2: float, duracion_hr: float) -> dict:
        """
        Método 1: Empírico (Fórmulas Estándar U.S. Weather Bureau).
        O(1) - Cálculo directo.
        """
        # TODO: Implementar fórmulas empíricas (ej. Fruhling, Horton, NERC)
        # Retorna el factor ARF estático.
        return {"metodo": "Empirico", "arf": 0.85, "area": area_km2}

    @staticmethod
    def _calculate_frequency_arf(df_lma: pd.DataFrame, df_puntuales: pd.DataFrame) -> dict:
        """
        Método 2: Análisis de Frecuencias (Área Fija).
        Requiere reciclaje del Módulo 3 para extrapolar hasta Tr=10,000.
        """
        # TODO: 
        # 1. Extraer Máximos Anuales de LMA y Puntuales.
        # 2. Ajustar funciones de distribución.
        # 3. Calcular ARF = LMA_Tr / Puntual_Tr
        return {"metodo": "Frecuencias", "curva_tr": {}}

    @staticmethod
    def _calculate_unam_severiano_arf(matriz_espaciotemporal, cuenca_geom) -> dict:
        """
        Método 3: I.I. UNAM (Simultaneidad - Tesis Severiano).
        Requiere MotorTensorial (GPU/Sparse) para evaluar tormenta centrada.
        """
        # TODO:
        # 1. Identificar eventos convectivos simultáneos.
        # 2. Construir isoyetas dinámicas (Kriging).
        # 3. Extraer gradientes y comparar contra precipitación absoluta.
        return {"metodo": "UNAM", "curva_decaimiento": {}}

    @staticmethod
    def ejecutar_pipeline_completo(ruta_datos_limpios: str, area_cuenca: float, cuenca_geom):
        """
        El director de orquesta. Llama a los 3 métodos de forma segura.
        """
        resultados = {}
        try:
            # 1. Ejecutar Empírico
            resultados["empirico"] = MotorCalculoARF._calculate_empirical_arf(area_cuenca, 24.0)
            
            # 2. (Simulación de Invocación) Generar GRID y LMA
            # lma = MotorTensorial.calcular_lluvia_areal_dia(...)
            
            # 3. Ejecutar Frecuencias
            # resultados["frecuencias"] = MotorCalculoARF._calculate_frequency_arf(...)
            
            # 4. Ejecutar UNAM
            # resultados["unam"] = MotorCalculoARF._calculate_unam_severiano_arf(...)
            
            return True, resultados
        except Exception as e:
            traceback.print_exc()
            return False, str(e)

class GestorMatricesARF:
    """
    Constructor del Marco Común de Datos (Fase 2 - Bento Box V2.0).
    Implementa evaluación de densidad OMM, cálculo espacial de LMA y 
    renderizado de isoyetas para eventos extremos.
    """

    @staticmethod
    def _evaluar_densidad_omm(area_km2, num_estaciones):
        if num_estaciones == 0 or area_km2 <= 0: return "Crítico", "#cc0000", 0
        densidad = area_km2 / num_estaciones
        if densidad <= 250: return "Excelente (OMM)", "#00ff41", densidad
        elif densidad <= 900: return "Aceptable (OMM)", "#ffdd00", densidad
        else: return "Deficiente", "#ff0044", densidad

    @staticmethod
    def _generar_isoyeta_base64(fecha_str, df_dia, gdf_cuenca, df_coords):
        """Genera un mapa de isoyetas (Kriging/RBF) para un día específico con escala de color."""
        try:
            # 1. Aumentamos el tamaño y resolución del lienzo (Protagonismo)
            fig, ax = plt.subplots(figsize=(10, 6))
            fig.patch.set_facecolor('#0a0a0a')
            ax.set_facecolor('#0a0a0a')
            
            lluvia_coords = df_coords.join(df_dia, how='inner').dropna()
            lluvia_coords.columns = ['LAT', 'LON', 'PRECIP']
            
            estacion_epicentro = "N/A"
            valor_maximo = 0.0
            
            if len(lluvia_coords) < 3:
                ax.text(0.5, 0.5, "Estaciones insuficientes para interpolar", color='grey', ha='center')
            else:
                # --- 🚀 PROYECCIÓN MÉTRICA ABSOLUTA (EPSG:6933) ---
                gdf_pts = gpd.GeoDataFrame(
                    lluvia_coords, 
                    geometry=gpd.points_from_xy(lluvia_coords['LON'], lluvia_coords['LAT']),
                    crs="EPSG:4326"
                ).to_crs("EPSG:6933")
                
                x = gdf_pts.geometry.x.values
                y = gdf_pts.geometry.y.values
                z = lluvia_coords['PRECIP'].values
                claves = lluvia_coords.index.values 
                
                # Proyectar la cuenca al mismo sistema métrico
                gdf_cuenca_metric = gdf_cuenca.to_crs("EPSG:6933")
                
                min_x, max_x = x.min(), x.max()
                min_y, max_y = y.min(), y.max()
                
                margen_x = (max_x - min_x) * 0.10
                margen_y = (max_y - min_y) * 0.10
                
                xi = np.linspace(min_x - margen_x, max_x + margen_x, 100)
                yi = np.linspace(min_y - margen_y, max_y + margen_y, 100)
                XI, YI = np.meshgrid(xi, yi)
                
                rbf = Rbf(x, y, z, function='linear')
                ZI = rbf(XI, YI)
                ZI = np.clip(ZI, 0, None)
                
                # Dibujar Isoyetas
                contour = ax.contourf(XI, YI, ZI, levels=15, cmap='turbo', alpha=0.8)
                
                # --- NUEVO: BARRA DE COLOR (SCALE) ---
                cbar = fig.colorbar(contour, ax=ax, fraction=0.046, pad=0.04)
                cbar.set_label('Precipitación (mm)', color='white', weight='bold')
                cbar.ax.yaxis.set_tick_params(color='white')
                plt.setp(plt.getp(cbar.ax.axes, 'yticklabels'), color='white')

                gdf_cuenca_metric.plot(ax=ax, facecolor='none', edgecolor='#00ff41', linewidth=2)
                ax.scatter(x, y, c='white', edgecolor='black', s=30, zorder=5)
                
                # Rastrear el Epicentro Real
                idx_max = np.argmax(z)
                estacion_epicentro = str(claves[idx_max])
                valor_maximo = z[idx_max]
                
                ax.scatter(x[idx_max], y[idx_max], c='#ff0044', marker='*', s=250, zorder=10, edgecolor='white')
                
            ax.set_title(f"Campo de Precipitación Regional | Evento: {fecha_str}", color='white', fontsize=12)
            ax.axis('off')
            
            plt.tight_layout()
            buf = io.BytesIO()
            plt.savefig(buf, format='png', dpi=150) # DPI elevado para máxima nitidez
            plt.close(fig)
            
            return base64.b64encode(buf.getvalue()).decode('utf-8'), estacion_epicentro, valor_maximo
        except Exception as e:
            plt.close('all')
            return "", "N/A", 0.0

    @staticmethod
    def construir_matriz_dinamica(ruta_base: str, stats_continuidad: list, area_cuenca_km2: float, cuenca_geom_wkt: str, umbral_lluvia: float = 0.5, coordenadas_extra: dict = None):
        if not ruta_base or not os.path.exists(ruta_base) or not stats_continuidad:
            return False, "Faltan datos de inicialización o archivo inválido."

        # LÓGICA DE RESOLUCIÓN DE RUTA:
        if ruta_base.endswith('.tar.xz') or ruta_base.endswith('.xz'):
            ruta_hds = ruta_base.replace('.tar.xz', '.gpkg').replace('.xz', '.gpkg')
        elif not ruta_base.endswith('.gpkg') and not ruta_base.endswith('.sqlite'):
            ruta_hds = ruta_base + "_ARF.gpkg"
        else:
            ruta_hds = ruta_base

        if not os.path.exists(ruta_hds):
            return False, f"La base de datos relacional no existe: {ruta_hds}"

        try:
            # 1. Criba de Estaciones Vivas
            claves_validas = [s["Clave"] for s in stats_continuidad if s.get("Integridad_%", 0) > 15.0]
            if not claves_validas: return False, "Ninguna estación superó el umbral mínimo (15%)."

            print(f"🌊 [TLÁLOC] Construyendo Matriz LMA Espacial con {len(claves_validas)} estaciones...")
            conn = sqlite3.connect(ruta_hds)
            # 2. Extracción (cada estación tiene su propia tabla serie_filtrada_arf_{clave})
            dfs = []
            for clave in claves_validas:
                try:
                    df_est = pd.read_sql_query(f"SELECT FECHA, PRECIP_imputado FROM serie_filtrada_arf_{clave}", conn, parse_dates=['FECHA'])
                    df_est.set_index('FECHA', inplace=True)
                    df_est.rename(columns={'PRECIP_imputado': clave}, inplace=True)
                    dfs.append(df_est)
                except pd.io.sql.DatabaseError:
                    continue # Tabla no existe, ignorar
            
            # Extraer Coordenadas para Interpolación (Fallback Múltiple)
            df_coords = pd.DataFrame()
            try:
                placeholders = ','.join(['?'] * len(claves_validas))
                try:
                    q_coords = f"SELECT Clave as clave_estacion, Latitud as LATITUD, Longitud as LONGITUD FROM estaciones_encontradas WHERE Clave IN ({placeholders})"
                    df_coords = pd.read_sql_query(q_coords, conn, params=claves_validas)
                except:
                    q_coords = f"SELECT clave_estacion, LATITUD, LONGITUD FROM estaciones_activas WHERE clave_estacion IN ({placeholders})"
                    df_coords = pd.read_sql_query(q_coords, conn, params=claves_validas)
                df_coords.set_index('clave_estacion', inplace=True)
            except Exception as e:
                print(f"⚠️ [TLÁLOC] Error al extraer coordenadas de la BD: {e}")
                
            # INYECCIÓN DE COORDENADAS DESDE MEMORIA (Parche anti-ausencia de BD)
            if df_coords.empty and coordenadas_extra:
                filas = []
                for clave in claves_validas:
                    if clave in coordenadas_extra:
                        filas.append({'clave_estacion': clave, 'LATITUD': coordenadas_extra[clave].get('LATITUD', 0), 'LONGITUD': coordenadas_extra[clave].get('LONGITUD', 0)})
                if filas:
                    df_coords = pd.DataFrame(filas).set_index('clave_estacion')
                    print(f"🗺️ Coordenadas inyectadas desde memoria RAM ({len(filas)} estaciones).")
            
            conn.close()

            if not dfs: return False, "No se encontraron datos filtrados en la BD."

            # 3. Pivotar y Alinear Calendario
            df_pivot = pd.concat(dfs, axis=1)
            fecha_min, fecha_max = df_pivot.index.min(), df_pivot.index.max()
            rango_completo = pd.date_range(start=fecha_min, end=fecha_max, freq='D')
            df_maestro = df_pivot.reindex(rango_completo)

            # --- 🛡️ FILTRO ESTRICTO DE DATA SCIENCE: RESOLUCIÓN ESPACIAL MÍNIMA ---
            print("🛡️ [TLÁLOC] Aplicando Filtro Espacial (Min. 30 estaciones activas por día)...")
            # Contamos cuántas estaciones tienen datos válidos (no nulos) en cada día
            estaciones_activas_por_dia = df_maestro[claves_validas].notnull().sum(axis=1)
            
            # Máscara booleana: True donde hay menos de 30 estaciones aportando datos
            mascara_dias_invalidos = estaciones_activas_por_dia < 30
            dias_purgados = mascara_dias_invalidos.sum()
            
            # Ejecutamos la anulación implacable: convertimos en NaN todos los registros de esos días
            df_maestro.loc[mascara_dias_invalidos, claves_validas] = np.nan
            print(f"🛑 [TLÁLOC] Cortafuegos activado: Se purgaron {dias_purgados} días por falta de densidad pluviométrica (< 30 estaciones).")

            # 4. Filtro HPC (Lloviznas)
            for col in claves_validas:
                # Solo aplicar a los datos que sobrevivieron al cortafuegos
                df_maestro.loc[df_maestro[col] <= umbral_lluvia, col] = 0.0

            # 5. Cálculo de LMA (Lluvia Media Areal)
            # Calculamos la media espacial. Los días purgados darán NaN automáticamente.
            df_maestro['LMA_Base'] = df_maestro[claves_validas].mean(axis=1, skipna=True)
            
            # Seguro adicional: forzar explícitamente la nulidad de la LMA en días inválidos
            df_maestro.loc[mascara_dias_invalidos, 'LMA_Base'] = np.nan
            
            # 6. Cosecha AMS (Máximos Anuales de la LMA con Simultaneidad)
            df_ams = df_maestro[['LMA_Base']].groupby(df_maestro.index.year).max()
            df_ams.rename(columns={'LMA_Base': 'LMA_Max_Anual'}, inplace=True)
            
            # Rastrear las fechas exactas en las que ocurrieron esos máximos
            fechas_ams = []
            for year in df_ams.index:
                serie_anio = df_maestro[df_maestro.index.year == year]['LMA_Base']
                if not serie_anio.dropna().empty:
                    idx_max = serie_anio.idxmax()
                    if pd.notnull(idx_max): fechas_ams.append(idx_max)

            # 6. Auditoría OMM y KPIs
            dias_totales = len(df_maestro)
            dias_lluviosos = (df_maestro['LMA_Base'] > 0).sum()
            pct_secos = 100.0 * (dias_totales - dias_lluviosos) / dias_totales if dias_totales > 0 else 0
            lma_max = df_maestro['LMA_Base'].max()
            
            eval_omm, color_omm, val_densidad = GestorMatricesARF._evaluar_densidad_omm(area_cuenca_km2, len(claves_validas))

            # 7. GRÁFICOS BASE (Cinemático de Serie Completa)
            def plot_serie(df):
                fig, ax = plt.subplots(figsize=(14, 3.5))
                fig.patch.set_facecolor('#0a0a0a'); ax.set_facecolor('#0a0a0a')
                ax.plot(df.index, df['LMA_Base'], color='#1c75fa', linewidth=0.6, alpha=0.6, label='LMA Diaria')
                rolling = df['LMA_Base'].rolling(window=30, min_periods=1).mean()
                ax.plot(df.index, rolling, color='#ff00ff', linewidth=1.5, label='Media Móvil (30d)')
                ax.set_title("Evolución Cinemática Histórica (Precipitación Media Areal)", color='white', fontsize=10)
                ax.tick_params(colors='grey', labelsize=8)
                for spine in ax.spines.values(): spine.set_color('#333333')
                ax.legend(facecolor='#111111', edgecolor='#333333', labelcolor='white', fontsize=8)
                fig.tight_layout(); buf = io.BytesIO(); plt.savefig(buf, format='png', dpi=120); plt.close(fig)
                return base64.b64encode(buf.getvalue()).decode('utf-8')
                
            def plot_ams_barras(df_ams):
                fig, ax = plt.subplots(figsize=(6, 3.5))
                fig.patch.set_facecolor('#0a0a0a'); ax.set_facecolor('#0a0a0a')
                ax.bar(df_ams.index, df_ams['LMA_Max_Anual'], color='#00ff41', alpha=0.8, edgecolor='black')
                ax.set_title("Serie AMS (Tormentas Areales Máximas por Año)", color='white', fontsize=10)
                ax.tick_params(colors='grey', labelsize=8)
                for spine in ax.spines.values(): spine.set_color('#333333')
                fig.tight_layout(); buf = io.BytesIO(); plt.savefig(buf, format='png', dpi=120); plt.close(fig)
                return base64.b64encode(buf.getvalue()).decode('utf-8')

            # 8. MOTOR DE MAPAS ISOYETAS Y ENVOLVENTE (Eventos Extremos)
            diccionario_mapas_ams = {}
            if not df_coords.empty and cuenca_geom_wkt:
                from shapely import wkt
                gdf_cuenca = gpd.GeoDataFrame(index=[0], geometry=[wkt.loads(cuenca_geom_wkt)], crs="EPSG:4326")
                print("🗺️ [TLÁLOC] Generando Isoyetas Dinámicas Regionales con Escala...")
                
                lma_por_epicentro = {} # Diccionario para atrapar los máximos
                
                for fecha in fechas_ams: 
                    fecha_s = fecha.strftime('%Y-%m-%d')
                    df_dia = df_maestro.loc[fecha, claves_validas]
                    
                    b64_map, est_max, val_max = GestorMatricesARF._generar_isoyeta_base64(fecha_s, df_dia, gdf_cuenca, df_coords)
                    
                    if b64_map:
                        diccionario_mapas_ams[str(fecha.year)] = {
                            "fecha": fecha_s, 
                            "b64": b64_map,
                            "estacion": est_max,
                            "valor": float(val_max)
                        }
                        
                        # --- LÓGICA DE SUSCEPTIBILIDAD AREAL ---
                        lma_actual = df_ams.loc[fecha.year, 'LMA_Max_Anual']
                        
                        # CONDICIONANTE 1: Solo se registra si hay un epicentro y el LMA es real (>0)
                        if est_max != "N/A" and pd.notnull(lma_actual) and lma_actual > 0:
                            if lma_actual > lma_por_epicentro.get(est_max, 0):
                                lma_por_epicentro[est_max] = lma_actual

                # --- KRIGING DEL MAPA COMPUESTO FINAL (ENVOLVENTE) ---
                # CONDICIONANTE 2 (ANTI-SESGO): Purgar cualquier dato nulo o 0 que haya logrado filtrarse.
                lma_por_epicentro = {k: v for k, v in lma_por_epicentro.items() if v > 0}
                
                # Solo se interpola si logramos recolectar al menos 3 epicentros distintos en la historia
                if len(lma_por_epicentro) >= 3:
                    print(f"🌟 [TLÁLOC] Generando Mapa Compuesto omitiendo estaciones sin LMA (Usando {len(lma_por_epicentro)} epicentros)...")
                    
                    df_sintetico = pd.Series(lma_por_epicentro, name='PRECIP')
                    
                    # Al enviar df_sintetico, el inner join interno de _generar_isoyeta_base64 
                    # OMITIRÁ FÍSICAMENTE de la interpolación a cualquier estación que no esté aquí.
                    b64_envolvente, est_dom, val_dom = GestorMatricesARF._generar_isoyeta_base64(
                        "ENVOLVENTE DE SUSCEPTIBILIDAD AREAL (LMA)", 
                        df_sintetico, 
                        gdf_cuenca, 
                        df_coords
                    )
                    
                    if b64_envolvente:
                        diccionario_mapas_ams["9999"] = {
                            "fecha": "COMPUESTO HISTÓRICO",
                            "b64": b64_envolvente,
                            "estacion": f"{len(lma_por_epicentro)} Epicentros Validados",
                            "valor": float(val_dom)
                        }

            # --- SQLITE PERSISTENCE (RAM OPTIMIZATION) ---
            print("💾 [TLÁLOC] Transfiriendo matriz maestra a SQLite para liberar RAM...")
            b64_plot_serie = plot_serie(df_maestro)
            try:
                conn_sqlite = sqlite3.connect(ruta_hds)
                df_maestro.to_sql("arf_matriz_maestra", conn_sqlite, if_exists="replace", index=True, index_label="Fecha")
                conn_sqlite.close()
                del df_maestro
                gc.collect()
            except Exception as e:
                print(f"❌ Error al guardar en SQLite: {e}")

            # Preparar tabla AMS exportable
            tabla_ams = [{"anio": y, "lma": round(v, 2)} for y, v in df_ams['LMA_Max_Anual'].items()]

            paquete_datos = {
                "df_ams": df_ams,
                "claves_activas": claves_validas,
                "fecha_inicio": fecha_min.strftime('%Y-%m-%d'), "fecha_fin": fecha_max.strftime('%Y-%m-%d'),
                "pct_secos": pct_secos, "lma_maxima": float(lma_max) if pd.notnull(lma_max) else 0.0,
                "eval_omm": eval_omm, "color_omm": color_omm, "densidad": round(val_densidad, 1),
                "tabla_ams": tabla_ams,
                "plot_serie": b64_plot_serie,
                "plot_ams": plot_ams_barras(df_ams),
                "mapas_isoyetas": diccionario_mapas_ams
            }
            return True, paquete_datos

        except Exception as e:
            import traceback; traceback.print_exc()
            return False, f"Fallo en construcción de Matriz Espacial: {str(e)}"