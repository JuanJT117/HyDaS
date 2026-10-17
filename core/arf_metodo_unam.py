import os
import numpy as np
import pandas as pd
import geopandas as gpd
from shapely.geometry import Point
from shapely import wkt
from scipy.interpolate import Rbf
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import io
import base64
import traceback
import warnings
import gc  # Recolector de basura para optimización de RAM

warnings.filterwarnings("ignore", category=RuntimeWarning)

class MotorUNAM:
    """
    [MÓDULO AISLADO: ARF UNAM - TORMENTA CENTRADA (TOPOLÓGICO HPC)]
    Kriging de Ultra-Alta resolución optimizado para entornos de 32GB RAM+.
    Genera exportaciones nativas para QGIS (ASCII Grid).
    """

    @staticmethod
    def _calcular_area_pixel(x, y):
        mean_lat = np.mean(y)
        dx_km = (x[1] - x[0]) * 111.32 * np.cos(np.radians(mean_lat))
        dy_km = (y[1] - y[0]) * 111.32
        return abs(dx_km * dy_km)

    @staticmethod
    def ejecutar_analisis_tormenta_centrada(df_maestro: pd.DataFrame, df_coords: pd.DataFrame, cuenca_wkt: str) -> dict:
        try:
            print("🌪️ [TLÁLOC HPC] Iniciando Fase 6: Tormenta Centrada Ultra-HD (Top 6 en Memoria)")
            
            estaciones = [col for col in df_maestro.columns if col != 'LMA_Base']
            
            # --- 1. PROXY VOLUMÉTRICO: Encontrar el TOP 6 ---
            df_proxy = df_maestro[estaciones].mean(axis=1)
            top_6_fechas = df_proxy.nlargest(6).index
            
            # --- 2. CONFIGURACIÓN DEL GRID DE ALTA RESOLUCIÓN ---
            x_coords, y_coords, claves_validas = [], [], []
            for est in estaciones:
                if est in df_coords.index:
                    x_coords.append(df_coords.loc[est, 'LONGITUD'])
                    y_coords.append(df_coords.loc[est, 'LATITUD'])
                    claves_validas.append(est)
                    
            x, y = np.array(x_coords), np.array(y_coords)
            
            minx, maxx = x.min(), x.max()
            miny, maxy = y.min(), y.max()
            margen_x = (maxx - minx) * 0.10
            margen_y = (maxy - miny) * 0.10
            
            # 🚀 MALLA ULTRA HD: 1500 x 1500 = 2.25 Millones de pixeles
            resolucion = 1500 
            xi = np.linspace(minx - margen_x, maxx + margen_x, resolucion)
            yi = np.linspace(miny - margen_y, maxy + margen_y, resolucion)
            XI, YI = np.meshgrid(xi, yi)
            
            pixel_area_km2 = MotorUNAM._calcular_area_pixel(xi, yi)
            
            curvas_tormentas = []
            mapas_tormentas = []
            
            # --- 3. RECONSTRUCCIÓN FORENSE CONTINUA (TOP 6) ---
            coords_dict = {est: (df_coords.loc[est, 'LONGITUD'], df_coords.loc[est, 'LATITUD']) for est in estaciones if est in df_coords.index}
            for rank, fecha in enumerate(top_6_fechas):
                print(f"   > Procesando Super-Celda {rank+1}/6: {fecha.strftime('%Y-%m-%d')}...")
                z = []
                x = []
                y = []
                for sta in estaciones:
                    val = df_maestro.at[fecha, sta]
                    if pd.notnull(val) and sta in coords_dict:
                        z.append(float(val))
                        x.append(coords_dict[sta][0])
                        y.append(coords_dict[sta][1])
                
                # =================================================================
                # SANACIÓN TOPOLÓGICA ZERO-TRUST PARA INTERPOLACIÓN RBF
                # =================================================================
                x_arr = np.array(x)
                y_arr = np.array(y)
                z_arr = np.array(z)
                
                # Máscara lógica estricta para bloquear NaNs e Infinitos en los tres ejes
                mascara_validos = ~np.isnan(z_arr) & ~np.isinf(z_arr) & ~np.isnan(x_arr) & ~np.isnan(y_arr)
                
                x_limpio = x_arr[mascara_validos]
                y_limpio = y_arr[mascara_validos]
                z_limpio = z_arr[mascara_validos]

                # Rbf requiere un mínimo de puntos matemáticos para no colapsar la matriz
                if len(z_limpio) < 4:
                    print(f"   [ADVERTENCIA] Tormenta del {fecha.date()} ignorada (Datos válidos insuficientes: {len(z_limpio)}).")
                    continue
                
                # Invocación purificada
                rbf = Rbf(x_limpio, y_limpio, z_limpio, function='linear')
                ZI = rbf(XI, YI)
                ZI = np.clip(ZI, 0, None)
                
                z_max_absoluto = ZI.max()
                if z_max_absoluto <= 0: continue
                
                # 🚀 ALMACENAMIENTO EN MEMORIA RAM (ASCII GRID)
                Z_export = np.flipud(ZI)
                cellsize_x = (maxx + margen_x - (minx - margen_x)) / resolucion
                
                asc_buffer = io.StringIO()
                asc_buffer.write(f"ncols         {resolucion}\n")
                asc_buffer.write(f"nrows         {resolucion}\n")
                asc_buffer.write(f"xllcorner     {minx - margen_x}\n")
                asc_buffer.write(f"yllcorner     {miny - margen_y}\n")
                asc_buffer.write(f"cellsize      {cellsize_x}\n")
                asc_buffer.write(f"NODATA_value  -9999\n")
                np.savetxt(asc_buffer, Z_export, fmt='%.2f', delimiter=' ')
                
                asc_string = asc_buffer.getvalue()
                asc_buffer.close()
                filename_sugerido = f"UNAM_SuperCelda_{rank+1}_{fecha.strftime('%Y%m%d')}.asc"

                # DESCENSO TOPOLÓGICO CONTINUO
                umbrales_pct = np.linspace(0.999, 0.01, 100)
                areas_km2, fra_valores = [], []
                
                for pct in umbrales_pct:
                    umbral_lluvia = z_max_absoluto * pct
                    mask_isoyeta = ZI >= umbral_lluvia
                    
                    area_actual = mask_isoyeta.sum() * pixel_area_km2
                    lluvia_media_areal = ZI[mask_isoyeta].mean()
                    fra_actual = lluvia_media_areal / z_max_absoluto
                    
                    if area_actual > 0:
                        areas_km2.append(area_actual)
                        fra_valores.append(fra_actual)
                        
                curvas_tormentas.append({
                    "fecha": fecha.strftime('%Y-%m-%d'),
                    "rank": rank + 1,
                    "p_max": z_max_absoluto,
                    "areas": np.array(areas_km2),
                    "fras": np.array(fra_valores)
                })
                
                # RENDERIZADO DEL MAPA
                fig_m, ax_m = plt.subplots(figsize=(6, 4))
                fig_m.patch.set_facecolor('#0a0a0a'); ax_m.set_facecolor('#0a0a0a')
                
                contour = ax_m.contourf(XI, YI, ZI, levels=25, cmap='turbo', alpha=0.85)
                ax_m.contour(XI, YI, ZI, levels=15, colors='black', linewidths=0.5, alpha=0.5)
                
                cbar = fig_m.colorbar(contour, ax=ax_m, fraction=0.046, pad=0.04)
                cbar.set_label('Precipitación (mm)', color='white')
                cbar.ax.yaxis.set_tick_params(color='white')
                plt.setp(plt.getp(cbar.ax.axes, 'yticklabels'), color='white')
                
                if cuenca_wkt:
                    gdf_cuenca = gpd.GeoDataFrame(index=[0], geometry=[wkt.loads(cuenca_wkt)], crs="EPSG:4326")
                    gdf_cuenca.plot(ax=ax_m, facecolor='none', edgecolor='#00ff41', linewidth=2, zorder=6)
                    c_minx, c_miny, c_maxx, c_maxy = gdf_cuenca.total_bounds
                    c_margen_x = (c_maxx - c_minx) * 0.5
                    c_margen_y = (c_maxy - c_miny) * 0.5
                    ax_m.set_xlim(c_minx - c_margen_x, c_maxx + c_margen_x)
                    ax_m.set_ylim(c_miny - c_margen_y, c_maxy + c_margen_y)
                    
                ax_m.scatter(x, y, c='white', edgecolor='black', s=15, alpha=0.5, zorder=5)
                
                idx_y, idx_x = np.unravel_index(ZI.argmax(), ZI.shape)
                ax_m.scatter(XI[idx_y, idx_x], YI[idx_y, idx_x], c='#ff0044', marker='*', s=200, edgecolor='white', zorder=10)
                
                ax_m.set_title(f"Super-Celda #{rank+1} | {fecha.strftime('%Y-%m-%d')} | $P_{{max}}$: {z_max_absoluto:.1f} mm", color='white', fontsize=10)
                ax_m.axis('off')
                plt.tight_layout(); buf_m = io.BytesIO(); plt.savefig(buf_m, format='png', dpi=120); plt.close(fig_m)
                
                # AGREGAMOS LOS DATOS EN MEMORIA AL DICCIONARIO
                mapas_tormentas.append({
                    "id": f"T{rank+1}: {fecha.strftime('%Y-%m-%d')}",
                    "b64": base64.b64encode(buf_m.getvalue()).decode('utf-8'),
                    "asc_data": asc_string,
                    "filename": filename_sugerido
                })

                # 🧹 LIMPIEZA DE RAM ESTRICTA (HPC)
                del rbf, ZI, mask_isoyeta, Z_export
                gc.collect()

            # --- 4. CONSTRUCCIÓN DE LA CURVA ENVOLVENTE (MÁXIMA SEGURIDAD) ---
            area_maxima_global = max([c['areas'].max() for c in curvas_tormentas])
            area_minima_global = min([c['areas'].min() for c in curvas_tormentas])
            
            areas_maestras = np.logspace(np.log10(max(1.0, area_minima_global)), np.log10(area_maxima_global), 100)
            
            matriz_fras_interpolados = []
            curvas_datos_tabulares = {f"T{i+1}": [] for i in range(len(curvas_tormentas))}
            
            for i, curva in enumerate(curvas_tormentas):
                idx_sort = np.argsort(curva['areas'])
                fra_interp = np.interp(areas_maestras, curva['areas'][idx_sort], curva['fras'][idx_sort])
                matriz_fras_interpolados.append(fra_interp)
                curvas_datos_tabulares[f"T{i+1}"] = fra_interp
                
            matriz_fras_np = np.array(matriz_fras_interpolados)
            fra_envolvente_crudo = np.max(matriz_fras_np, axis=0)
            fra_envolvente = np.clip(fra_envolvente_crudo, 0.4, 1.0)

            # RENDERIZADO DEL GRÁFICO (Top 6)
            fig_d, ax_d = plt.subplots(figsize=(9, 5))
            fig_d.patch.set_facecolor('#0a0a0a'); ax_d.set_facecolor('#0a0a0a')
            
            colores_tormentas = ['#1c75fa', '#9900ff', '#ffaa00', '#00ff41', '#ff00ff', '#00ffff']
            for i, curva in enumerate(curvas_tormentas):
                ax_d.plot(curva['areas'], curva['fras'], color=colores_tormentas[i], alpha=0.5, linewidth=1.2, label=f"T{i+1}: {curva['fecha']}")
                
            ax_d.plot(areas_maestras, fra_envolvente, color='#ff0044', linewidth=3, label='ENVOLVENTE DE DISEÑO')
            
            # Área de Cuenca Exacta (Proyección Global Cilíndrica Equivalente de Lambert EPSG:6933)
            area_cuenca = None
            if cuenca_wkt:
                try:
                    area_cuenca = gpd.GeoSeries([wkt.loads(cuenca_wkt)], crs="EPSG:4326").to_crs("EPSG:6933").area.iloc[0] / 1e6
                    ax_d.axvline(x=area_cuenca, color='#00ff41', linestyle='--', linewidth=2, label=f"Área Cuenca ({area_cuenca:.1f} km²)")
                except Exception as ex_area:
                    print(f"Error área cuenca: {ex_area}")

            ax_d.set_xscale('log')
            ax_d.set_ylim(0.4, 1.05)
            ax_d.set_title("Curvas Área-Profundidad (Tormenta Centrada) - Método UNAM", color='white', pad=10)
            ax_d.set_xlabel("Área de Cobertura de la Tormenta (km²) - Escala Log", color='grey')
            ax_d.set_ylabel("Factor de Reducción Areal (FRA)", color='grey')
            ax_d.tick_params(colors='grey')
            for spine in ax_d.spines.values(): spine.set_color('#333333')
            ax_d.legend(facecolor='#111111', edgecolor='#333333', labelcolor='white')
            ax_d.grid(True, which='both', linestyle=':', color='#333333', alpha=0.5)
            plt.tight_layout(); buf_d = io.BytesIO(); plt.savefig(buf_d, format='png', dpi=120); plt.close(fig_d)

            # --- 5. PREPARACIÓN DE LA TABLA MASIVA ---
            tabla_masiva = []
            fra_diseno_oficial = 1.00 
            
            if area_cuenca:
                fra_diseno_oficial = np.interp(area_cuenca, areas_maestras, fra_envolvente)
                fra_diseno_oficial = float(np.clip(fra_diseno_oficial, 0.4, 1.0))

            for i in range(len(areas_maestras)):
                fila = {"area": round(areas_maestras[i], 1), "fra_env": round(fra_envolvente[i], 4)}
                for j in range(len(curvas_tormentas)):
                    fila[f"t{j+1}"] = round(curvas_datos_tabulares[f"T{j+1}"][i], 4)
                tabla_masiva.append(fila)

            del XI, YI
            gc.collect()

            return {
                "exito": True,
                "mapas": mapas_tormentas,
                "plot_dad": base64.b64encode(buf_d.getvalue()).decode('utf-8'),
                "tabla_masiva": tabla_masiva,
                "area_cuenca": round(area_cuenca, 1) if area_cuenca else 0.0,
                "area_dominio": round(len(xi) * len(yi) * pixel_area_km2, 1),
                "fra_oficial": round(fra_diseno_oficial, 4)
            }

        except Exception as e:
            traceback.print_exc()
            return {"exito": False, "error": str(e)}
