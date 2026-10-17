# core/arf_metodo_diseno.py
import numpy as np
import pandas as pd
import geopandas as gpd
from shapely import wkt
from pykrige.ok import OrdinaryKriging
from scipy.interpolate import interp1d
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import io
import base64
import traceback
import gc
import os
import threading
import warnings

warnings.filterwarnings("ignore", category=RuntimeWarning)

class MotorDiseno:
    """
    [MÓDULO 7: GENERADOR DE TORMENTAS DE DISEÑO]
    Consolida la generación de Hietogramas (Bloques Alternos) y 
    la proyección de Matrices Areales (Hadamard product).
    """

    @staticmethod
    def _estrategia_arf(d_min: float, area_km2: float, fuente_arf: str, payload_arf: dict) -> float:
        """Extrae el ARF para una duración específica (Para Bloques Alternos)"""
        try:
            d_hr = d_min / 60.0
            if fuente_arf == "empirico":
                d_025 = d_hr ** 0.25
                area_mi2 = area_km2 / 2.58999
                arf = 1 - np.exp(-1.1 * d_025) + np.exp(-1.1 * d_025 - 0.015 * np.sqrt(area_mi2))
                return float(np.clip(arf, 0.4, 1.0))
            
            elif fuente_arf in ["frecuencias", "unam"] and payload_arf:
                x_duraciones = np.array(payload_arf.get("duraciones_hr", []))
                y_arfs = np.array(payload_arf.get("arfs_calculados", []))
                if len(x_duraciones) > 0 and len(y_arfs) > 0:
                    f_interp = interp1d(x_duraciones, y_arfs, kind='linear', fill_value=(y_arfs[0], y_arfs[-1]), bounds_error=False)
                    return float(np.clip(f_interp(d_hr), 0.4, 1.0))
            return 1.0
        except Exception:
            return 1.0

    @staticmethod
    def construir_bloques_alternos(curva_idf, tr: int, duracion_total_min: int, dt_min: int, 
                                   area_km2: float, fuente_arf: str, payload_arf: dict):
        """Física del Hietograma con forzado de monotonicidad"""
        tiempos_min = np.arange(dt_min, duracion_total_min + dt_min, dt_min)
        i_puntual = np.array([curva_idf(tr, d) for d in tiempos_min])
        p_acum_puntual = i_puntual * (tiempos_min / 60.0)
        
        arfs = np.array([MotorDiseno._estrategia_arf(d, area_km2, fuente_arf, payload_arf) for d in tiempos_min])
        p_acum_areal = np.maximum.accumulate(p_acum_puntual * arfs)
        p_inc_areal = np.insert(np.diff(p_acum_areal), 0, p_acum_areal[0])
        
        p_sorted = np.sort(p_inc_areal)[::-1]
        bloques = np.zeros_like(p_sorted)
        medio = len(bloques) // 2
        bloques[medio] = p_sorted[0]
        
        izq, der = medio - 1, medio + 1
        for i in range(1, len(p_sorted)):
            if i % 2 != 0 and der < len(bloques):
                bloques[der] = p_sorted[i]
                der += 1
            elif izq >= 0:
                bloques[izq] = p_sorted[i]
                izq -= 1
        return tiempos_min, bloques

    @staticmethod
    def generar_matrices_multitr(df_maestro: pd.DataFrame, df_coords: pd.DataFrame, cuenca_wkt: str, dict_fra_tr: dict) -> dict:
        """Motor HPC para proyección Espacial con Kriging y ARF Dinámico"""
        try:
            if df_maestro is None or df_coords is None or not cuenca_wkt:
                return {"exito": False, "error": "Contratos espaciales maestros ausentes."}
            
            gdf_cuenca = gpd.GeoDataFrame(index=[0], geometry=[wkt.loads(cuenca_wkt)], crs="EPSG:4326")
            bounds = gdf_cuenca.total_bounds
            grid_res = max((bounds[2] - bounds[0]) / 150, (bounds[3] - bounds[1]) / 150)
            grid_x = np.arange(bounds[0], bounds[2], grid_res)
            grid_y = np.arange(bounds[1], bounds[3], grid_res)

            estaciones = [c for c in df_maestro.columns if c in df_coords.index]
            trs = [2, 5, 10, 20, 50, 100, 500, 1000, 10000]
            mapas_b64 = {}

            for tr in trs:
                z_values, coords_validas = [], []
                for est in estaciones:
                    serie = df_maestro[est].dropna().values
                    if len(serie) > 5:
                        mean, std = np.mean(serie), np.std(serie)
                        y_tr = -np.log(-np.log(1 - 1/tr))
                        val_tr = mean + ((y_tr - 0.5772) * (np.sqrt(6) / np.pi)) * std
                        z_values.append(val_tr)
                        lon_col = 'LONGITUD' if 'LONGITUD' in df_coords.columns else ('Lon' if 'Lon' in df_coords.columns else 'lon')
                        lat_col = 'LATITUD' if 'LATITUD' in df_coords.columns else ('Lat' if 'Lat' in df_coords.columns else 'lat')
                        coords_validas.append((df_coords.loc[est, lon_col], df_coords.loc[est, lat_col]))

                if len(z_values) < 3: continue
                coords_validas = np.array(coords_validas)
                
                ok = OrdinaryKriging(coords_validas[:, 0], coords_validas[:, 1], np.array(z_values), variogram_model='spherical', verbose=False)
                z_grid, _ = ok.execute('grid', grid_x, grid_y)
                
                fra_tr = dict_fra_tr.get(tr, 1.0)
                matriz_areal = z_grid * fra_tr

                fig, ax = plt.subplots(figsize=(6, 5), dpi=100)
                fig.patch.set_facecolor('#111111'); ax.set_facecolor('#111111')
                X, Y = np.meshgrid(grid_x, grid_y)
                c = ax.contourf(X, Y, matriz_areal, levels=15, cmap='viridis', alpha=0.85)
                gdf_cuenca.boundary.plot(ax=ax, color='#00ff41', linewidth=1.5)
                plt.colorbar(c, ax=ax, label=f"Lluvia Areal (mm) [FRA: {fra_tr:.3f}]")
                ax.set_title(f"Tormenta Tr={tr} Años", color='white', weight='bold')
                ax.tick_params(colors='grey')
                
                plt.tight_layout(); buf = io.BytesIO(); plt.savefig(buf, format='png', dpi=90); plt.close(fig)
                mapas_b64[f"Tr_{tr}"] = base64.b64encode(buf.getvalue()).decode('utf-8')

            gc.collect()
            return {"exito": True, "mapas": mapas_b64}
        except Exception as e:
            traceback.print_exc()
            return {"exito": False, "error": str(e)}

    @staticmethod
    def generar_y_exportar_tormenta_asincrono(page, parametros: dict):
        """
        [CONCURRENCIA EAFP]
        Lanza la ejecución pesada en hilo demonio y exporta a disco/sesión.
        """
        def tarea_hpc():
            try:
                area_km2 = parametros["area_km2"]
                dt_min = parametros["dt_min"]
                duracion_total = parametros["duracion_total"]
                trs = parametros.get("periodos_retorno", [2, 10, 50, 100, 500])
                fuente_arf = parametros.get("fuente_arf", "empirico")
                payload_arf = parametros.get("payload_arf", {})
                ruta_export = parametros.get("ruta_export")
                
                curva_idf = parametros.get("curva_idf")
                if not curva_idf:
                    curva_idf = lambda tr, d: (40.0 * (tr ** 0.2)) / ((d / 60.0 + 0.5) ** 0.6)

                diccionario_series = {}
                tiempos = None

                for tr in trs:
                    t_min, p_inc = MotorDiseno.construir_bloques_alternos(
                        curva_idf, tr, duracion_total, dt_min, area_km2, fuente_arf, payload_arf
                    )
                    if tiempos is None:
                        tiempos = t_min
                    diccionario_series[f"Tr_{tr}_años"] = p_inc

                resultados_df = pd.DataFrame(diccionario_series, index=tiempos)
                resultados_df.index.name = "Tiempo_min"

                if ruta_export and os.path.exists(ruta_export):
                    archivo_csv = os.path.join(ruta_export, "Tormenta_Diseno_HyDaS.csv")
                    resultados_df.to_csv(archivo_csv)

                    fig, ax = plt.subplots(figsize=(10, 5), dpi=100)
                    fig.patch.set_facecolor('#111111')
                    ax.set_facecolor('#1a1a1a')

                    colores = ['#00ff41', '#00d8ff', '#ffdd00', '#ff8800', '#ff0044']
                    for idx, col in enumerate(resultados_df.columns):
                        color = colores[idx % len(colores)]
                        ax.plot(resultados_df.index, resultados_df[col], marker='o', label=col, color=color, linewidth=2)

                    ax.set_title(f"Hietograma de Diseño - Bloques Alternos (Área: {area_km2} km²)", color='white', weight='bold')
                    ax.set_xlabel("Tiempo (min)", color='white')
                    ax.set_ylabel("Precipitación Incremental (mm)", color='white')
                    ax.tick_params(colors='white')
                    ax.legend(facecolor='#111111', edgecolor='#333333', labelcolor='white')
                    ax.grid(True, linestyle='--', alpha=0.3, color='#444444')

                    archivo_png = os.path.join(ruta_export, "Tormenta_Diseno_HyDaS.png")
                    plt.tight_layout()
                    plt.savefig(archivo_png, facecolor=fig.get_facecolor())
                    plt.close(fig)

                page.session.set("tormenta_diseno_df", int(0) if resultados_df.empty else resultados_df)
                gc.collect()

                def notificar_exito():
                    page.open(
                        ft.SnackBar(
                            content=ft.Text("✅ Hietograma y Tormenta de Diseño calculados y guardados.", color="white"),
                            bgcolor="#00ff41"
                        )
                    )
                    page.update()

                page.run_thread(notificar_exito)

            except Exception as e:
                traceback.print_exc()
                err_msg = str(e)
                def notificar_error():
                    page.open(
                        ft.SnackBar(
                            content=ft.Text(f"❌ Error en cálculo de Hietograma: {err_msg}", color="white"),
                            bgcolor="#cc0000"
                        )
                    )
                    page.update()
                page.run_thread(notificar_error)

        hilo = threading.Thread(target=tarea_hpc, daemon=True)
        hilo.start()
