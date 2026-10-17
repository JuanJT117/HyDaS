import numpy as np
import pandas as pd
import scipy.stats as st
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

# --- IMPORTACIÓN NATIVA DEL MÓDULO 3 ---
# Asegura la ruta correcta dependiendo de tu estructura. 
# Asumimos que analisis_cuenca.py está accesible en el mismo nivel o en el core.
from core.analisis_cuenca import run_cuenca_analysis

warnings.filterwarnings("ignore", category=RuntimeWarning)

class MotorFrecuencias:
    """
    [MÓDULO AISLADO: ARF FRECUENCIAS - ÁREA FIJA ESPACIAL]
    Implementa el cálculo del Factor de Reducción Areal estricto a 24 horas.
    Ejecuta Kriging sobre los valores de diseño (Tr) para el denominador.
    Soporta Modos Homogéneo (Regional) y Heterogéneo (Microclima).
    """
    
    # Mapeo de distribuciones Scipy hacia los nombres exactos de tu `analisis_cuenca.py`
    MAPEO_DISTRIBUCIONES = {
        'Gumbel': st.gumbel_r,
        'Pearson III': st.pearson3,
        'Log Normal': st.lognorm,
        'Gamma': st.gamma,
        'Normal': st.norm,
        'General Valores Extremos': st.genextreme
    }

    @staticmethod
    def _determinar_mejor_modelo(serie_ams: pd.Series) -> str:
        """
        Encuentra el Best-Fit mediante la Suma de Errores Cuadráticos (SSE)
        basado en la probabilidad empírica de Weibull, y retorna el nombre 
        exacto requerido por analisis_cuenca.py.
        """
        y = serie_ams.dropna().sort_values().values
        if len(y) < 5: return 'Gumbel' # Fallback de seguridad
            
        n = len(y)
        prob_empirica = np.arange(1, n + 1) / (n + 1)
        
        mejor_nombre = 'Gumbel'
        mejor_sse = np.inf

        for nombre_mod, distribucion in MotorFrecuencias.MAPEO_DISTRIBUCIONES.items():
            try:
                params = distribucion.fit(y)
                cdf_teorica = distribucion.cdf(y, *params)
                sse = np.sum((cdf_teorica - prob_empirica)**2)
                
                if sse < mejor_sse:
                    mejor_sse = sse
                    mejor_nombre = nombre_mod
            except Exception:
                continue
                
        return mejor_nombre

    @staticmethod
    def _extraer_lamina_24h(best_fit_name: str, serie_ams: pd.Series, tr_lista: list) -> dict:
        """
        Envía la serie a analisis_cuenca.py, extrae la matriz resultante,
        y rescata EXCLUSIVAMENTE los valores de lluvia para D = 24h (1440 min).
        """
        # Formatear al estándar que exige analisis_cuenca.py
        df_in = pd.DataFrame({'Max_Anual': serie_ams.dropna().values})
        
        # Invocar motor central
        res = run_cuenca_analysis(best_fit_name, df_in)
        df_altura = res.get('df_altura')
        
        if df_altura is None or df_altura.empty:
            raise ValueError(f"Fallo en analisis_cuenca al procesar {best_fit_name}.")
            
        # Extraer exclusivamente la fila de 24 horas (1440 min)
        if 'TR (AÑOS)' in df_altura.columns:
            fila_24h = df_altura[df_altura['TR (AÑOS)'] == 1440]
        else:
            # Fallback en caso de que el índice sea la duración
            fila_24h = df_altura.loc[[1440]]

        if fila_24h.empty:
            raise ValueError("No se encontró la duración de 1440 min en las Curvas de Altura.")
            
        laminas = {}
        for tr in tr_lista:
            # Si el Tr no fue procesado por analisis_cuenca, usamos interpolación o default
            if tr in fila_24h.columns:
                laminas[tr] = float(fila_24h[tr].values[0])
            else:
                laminas[tr] = np.nan
                
        return laminas

    @staticmethod
    def ejecutar_analisis_espacial(df_maestro: pd.DataFrame, df_ams_lma: pd.DataFrame, df_coords: pd.DataFrame, cuenca_wkt: str, modo: str = 'homogeneo') -> dict:
        """
        Paso 1: Determina Best-Fit de LMA.
        Paso 2: Acopla a estaciones (Forzado si es Homogéneo, Libre si es Heterogéneo).
        Paso 3: Kriging de lluvias puntuales de 24h para cada Tr.
        Paso 4: Aplica Seguro de Monotonicidad.
        """
        try:
            print(f"🌪️ [TLÁLOC] Iniciando Frecuencias en Modo: {modo.upper()}")
            tr_lista = [2, 5, 10, 20, 50, 100, 500, 1000, 10000]
            estaciones = [col for col in df_maestro.columns if col != 'LMA_Base']
            gdf_cuenca = gpd.GeoDataFrame(index=[0], geometry=[wkt.loads(cuenca_wkt)], crs="EPSG:4326")
            
            # --- PASO 1: NUMERADOR (Ajuste de la LMA Histórica a 24h) ---
            dist_lma_name = MotorFrecuencias._determinar_mejor_modelo(df_ams_lma['LMA_Max_Anual'])
            laminas_lma = MotorFrecuencias._extraer_lamina_24h(dist_lma_name, df_ams_lma['LMA_Max_Anual'], tr_lista)
            
            distribuciones_ganadoras = {"LMA Areal (Cuenca)": dist_lma_name}
            
            # --- PASO 2: DENOMINADOR (Ajuste de Estaciones Individuales) ---
            laminas_estaciones = {est: {} for est in estaciones}
            df_ams_estaciones = df_maestro.groupby(df_maestro.index.year).max()
            
            for est in estaciones:
                if modo == 'homogeneo':
                    # REGLA 1: Forzamiento Regional
                    dist_est_name = dist_lma_name 
                else:
                    # REGLA 2: Best-Fit Heterogéneo
                    dist_est_name = MotorFrecuencias._determinar_mejor_modelo(df_ams_estaciones[est])
                    
                distribuciones_ganadoras[est] = dist_est_name
                laminas_estaciones[est] = MotorFrecuencias._extraer_lamina_24h(dist_est_name, df_ams_estaciones[est], tr_lista)

            # --- PASO 3: KRIGING PROBABILÍSTICO POR Tr (ESPACIO MÉTRICO) ---
            laminas_kriging = {}
            
            # Extraer solo las coordenadas válidas
            df_cv = df_coords.loc[[est for est in estaciones if est in df_coords.index]]
            claves_validas = df_cv.index.tolist()
            
            # 🚀 PROYECCIÓN MÉTRICA (EPSG:6933)
            gdf_pts = gpd.GeoDataFrame(
                df_cv, geometry=gpd.points_from_xy(df_cv['LONGITUD'], df_cv['LATITUD']), crs="EPSG:4326"
            ).to_crs("EPSG:6933")
            
            x = gdf_pts.geometry.x.values
            y = gdf_pts.geometry.y.values
            
            # Proyectar Cuenca y preparar Máscara Geométrica
            gdf_cuenca_metric = gdf_cuenca.to_crs("EPSG:6933")
            minx, miny, maxx, maxy = gdf_cuenca_metric.total_bounds
            
            # El Bounding Box ahora está en Metros
            xi = np.linspace(minx, maxx, 100)
            yi = np.linspace(miny, maxy, 100)
            XI, YI = np.meshgrid(xi, yi)
            mask = np.zeros_like(XI, dtype=bool)
            poly = gdf_cuenca_metric.geometry[0]
            
            for i in range(XI.shape[0]):
                for j in range(XI.shape[1]):
                    if poly.contains(Point(XI[i,j], YI[i,j])): mask[i,j] = True

            diccionario_mapas_tr = {}

            for tr in tr_lista:
                z = np.array([laminas_estaciones[est][tr] for est in claves_validas])
                
                # Interpolación Radial
                rbf = Rbf(x, y, z, function='linear')
                ZI = rbf(XI, YI)
                ZI = np.clip(ZI, 0, None)
                
                # Promedio Matemático de la Superficie Estricta a la Cuenca
                if np.any(mask):
                    promedio_areal = ZI[mask].mean()
                else:
                    promedio_areal = ZI.mean() # Fallback si falla polígono
                
                laminas_kriging[tr] = promedio_areal

                # GUARDAR MAPA PARA CADA Tr Y HACER ZOOM A LA CUENCA
                fig_k, ax_k = plt.subplots(figsize=(6, 4))
                fig_k.patch.set_facecolor('#0a0a0a'); ax_k.set_facecolor('#0a0a0a')
                
                contour = ax_k.contourf(XI, YI, ZI, levels=15, cmap='turbo', alpha=0.8)
                cbar = fig_k.colorbar(contour, ax=ax_k, fraction=0.046, pad=0.04)
                cbar.set_label(f'Precipitación Tr={tr} (mm)', color='white')
                cbar.ax.yaxis.set_tick_params(color='white')
                plt.setp(plt.getp(cbar.ax.axes, 'yticklabels'), color='white')
                
                gdf_cuenca_metric.plot(ax=ax_k, facecolor='none', edgecolor='#00ff41', linewidth=2)
                ax_k.scatter(x, y, c='white', edgecolor='black', s=20)
                
                # ZOOM ESTRICTO A LA CUENCA CON UN MARGEN DEL 15% (El Secreto)
                margen_x = (maxx - minx) * 0.15
                margen_y = (maxy - miny) * 0.15
                ax_k.set_xlim(minx - margen_x, maxx + margen_x)
                ax_k.set_ylim(miny - margen_y, maxy + margen_y)
                
                ax_k.set_title(f"Riesgo Espacial (Tr {tr} Años) | {modo.capitalize()}", color='white', fontsize=10)
                ax_k.axis('off')
                
                plt.tight_layout()
                buf_k = io.BytesIO()
                plt.savefig(buf_k, format='png', dpi=120)
                plt.close(fig_k)
                
                diccionario_mapas_tr[str(tr)] = base64.b64encode(buf_k.getvalue()).decode('utf-8')

            # --- PASO 4: CÁLCULO DEL FRA Y SEGURO DE MONOTONICIDAD ---
            tabla_resultados = []
            fra_crudo_plot = []
            fra_seguro_plot = []
            lma_plot = [laminas_lma[tr] for tr in tr_lista]
            krig_plot = [laminas_kriging[tr] for tr in tr_lista]
            
            ultimo_fra_valido = 1.0

            for i, tr in enumerate(tr_lista):
                lma_v = laminas_lma[tr]
                krig_v = laminas_kriging[tr]
                
                fra_crudo = lma_v / krig_v if krig_v > 0 else 1.0
                fra_crudo_plot.append(fra_crudo)
                
                # SEGURO: Truncamiento físico
                fra_seguro = float(np.clip(fra_crudo, 0.4, 1.0))
                
                # SEGURO: Monotonicidad (No puede llover proporcionalmente más en área grande en Tr extremos)
                if i > 0 and fra_seguro > ultimo_fra_valido:
                    fra_seguro = ultimo_fra_valido 
                
                ultimo_fra_valido = fra_seguro
                fra_seguro_plot.append(fra_seguro)
                
                tabla_resultados.append({
                    "tr": tr, "lma": round(lma_v, 2), "kriging": round(krig_v, 2),
                    "fra": round(fra_seguro, 4)
                })

            # --- PASO 5: RENDERIZACIÓN VECTORIAL BENTO ---
            # Gráfico Lluvia vs Tr
            fig1, ax1 = plt.subplots(figsize=(8, 4))
            fig1.patch.set_facecolor('#0a0a0a'); ax1.set_facecolor('#0a0a0a')
            ax1.plot(tr_lista, krig_plot, 'o-', color='#ffdd00', label='Kriging Regional (Denom.)', linewidth=2)
            ax1.plot(tr_lista, lma_plot, 's-', color='#1c75fa', label='LMA 24h (Num.)', linewidth=2)
            ax1.fill_between(tr_lista, lma_plot, krig_plot, color='#ffdd00', alpha=0.1)
            ax1.set_xscale('log'); ax1.set_title("Comportamiento Areal vs Regional Extremo", color='white')
            ax1.set_xlabel("Periodo de Retorno Tr (Años)", color='grey'); ax1.set_ylabel("Precipitación 24h (mm)", color='grey')
            ax1.tick_params(colors='grey'); ax1.legend(facecolor='#111111', edgecolor='#333333', labelcolor='white')
            ax1.grid(True, which='both', linestyle=':', color='#333333', alpha=0.5)
            plt.tight_layout(); buf1 = io.BytesIO(); plt.savefig(buf1, format='png', dpi=120); plt.close(fig1)

            # Gráfico ARF vs Tr
            fig2, ax2 = plt.subplots(figsize=(8, 4))
            fig2.patch.set_facecolor('#0a0a0a'); ax2.set_facecolor('#0a0a0a')
            ax2.plot(tr_lista, fra_crudo_plot, '--', color='grey', alpha=0.5, label='FRA Crudo (Sin Filtro)')
            ax2.plot(tr_lista, fra_seguro_plot, 'o-', color='#00ff41', linewidth=2.5, label='FRA Oficial (Monótono)')
            ax2.set_xscale('log'); ax2.set_ylim(0.35, 1.05); ax2.set_title("Atenuación del FRA (Área Fija)", color='white')
            ax2.set_xlabel("Periodo de Retorno Tr (Años)", color='grey'); ax2.set_ylabel("Factor de Reducción", color='grey')
            ax2.tick_params(colors='grey'); ax2.legend(facecolor='#111111', edgecolor='#333333', labelcolor='white')
            ax2.grid(True, which='both', linestyle=':', color='#333333', alpha=0.5)
            plt.tight_layout(); buf2 = io.BytesIO(); plt.savefig(buf2, format='png', dpi=120); plt.close(fig2)

            return {
                "exito": True, "distribuciones": distribuciones_ganadoras, "tabla": tabla_resultados,
                "plot_lluvia": base64.b64encode(buf1.getvalue()).decode('utf-8'),
                "plot_arf": base64.b64encode(buf2.getvalue()).decode('utf-8'),
                "mapas_tr": diccionario_mapas_tr
            }

        except Exception as e:
            traceback.print_exc()
            return {"exito": False, "error": str(e)}