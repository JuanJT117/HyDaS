import os
import numpy as np
import pandas as pd
import geopandas as gpd
from shapely import wkt
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import io
import base64
import traceback
import warnings

# Importamos los motores previos para orquestar el recálculo maestro
from core.arf_metodo_empirico import MotorEmpirico
from core.arf_metodo_frecuencias import MotorFrecuencias
from core.arf_metodo_unam import MotorUNAM

warnings.filterwarnings("ignore", category=RuntimeWarning)

class MotorComparativa:
    """
    [MÓDULO 7: DICTAMEN FINAL Y DATA SCIENCE]
    Ensambla todos los métodos, calcula el cono de incertidumbre epistémica,
    evalúa el Delta Volumétrico en Hm3 y emite un dictamen determinista.
    """

    @staticmethod
    def ejecutar_dictamen_maestro(df_maestro: pd.DataFrame, df_coords: pd.DataFrame, cuenca_wkt: str, p_24h_mm: float) -> dict:
        try:
            print("⚖️ [TLÁLOC] Iniciando Auditoría y Dictamen Final (Fase 7)...")
            tr_lista = [2, 5, 10, 20, 50, 100, 500, 1000, 10000]
            
            # 1. CÁLCULO DE ÁREA EXACTA (EPSG:6933)
            gdf_cuenca = gpd.GeoDataFrame(index=[0], geometry=[wkt.loads(cuenca_wkt)], crs="EPSG:4326")
            area_km2 = gdf_cuenca.to_crs("EPSG:6933").area.iloc[0] / 1e6
            is_microcuenca = area_km2 < 25.0

            # 2. EXTRACCIÓN DE VECTORES DE TODOS LOS MODELOS
            # A) Empíricos (Constantes en Tr)
            emp_uswb = MotorEmpirico._calcular_uswb(area_km2, 24.0)
            emp_temez = MotorEmpirico._calcular_temez(area_km2)
            
            # Reconstrucción del AMS para Frecuencias y UNAM
            df_ams_lma = df_maestro[['LMA_Base']].groupby(df_maestro.index.year).max()
            df_ams_lma.rename(columns={'LMA_Base': 'LMA_Max_Anual'}, inplace=True)

            # B) Frecuencias (Homogéneo y Heterogéneo)
            res_hom = MotorFrecuencias.ejecutar_analisis_espacial(df_maestro, df_ams_lma, df_coords, cuenca_wkt, 'homogeneo')
            res_het = MotorFrecuencias.ejecutar_analisis_espacial(df_maestro, df_ams_lma, df_coords, cuenca_wkt, 'heterogeneo')
            
            dic_hom = {f['tr']: f['fra'] for f in res_hom['tabla']} if res_hom['exito'] else {t: 1.0 for t in tr_lista}
            dic_het = {f['tr']: f['fra'] for f in res_het['tabla']} if res_het['exito'] else {t: 1.0 for t in tr_lista}

            # C) Método UNAM (HPC) - Ejecutado en silencio sin exportar a QGIS
            res_unam = MotorUNAM.ejecutar_analisis_tormenta_centrada(df_maestro, df_coords, cuenca_wkt)
            fra_unam_base = res_unam['fra_oficial'] if res_unam['exito'] else 1.0

            # 3. CONSTRUCCIÓN DE LA MATRIZ DE CONSENSO Y ENSAMBLAJE
            datos_matriz = []
            fra_ensemble_list = []
            fra_max_list, fra_min_list = [], []

            for tr in tr_lista:
                if is_microcuenca:
                    v_uswb, v_temez, v_hom, v_het, v_unam = 1.0, 1.0, 1.0, 1.0, 1.0
                    v_ensemble = 1.0
                else:
                    v_uswb = emp_uswb
                    v_temez = emp_temez
                    v_hom = dic_hom.get(tr, 1.0)
                    v_het = dic_het.get(tr, 1.0)
                    
                    # La UNAM por definición es PMP (Tormenta Centrada). Asumimos que su valor rige en Tr altos.
                    v_unam = fra_unam_base
                    
                    # --- EL ALGORITMO DE DATA SCIENCE: Fusión Asintótica Ponderada ---
                    # En Tr=2 (Log=0.3), manda el Homogéneo. En Tr=10000 (Log=4.0), manda la UNAM.
                    peso_unam = (np.log10(tr) - np.log10(2)) / (np.log10(10000) - np.log10(2))
                    peso_hom = 1.0 - peso_unam
                    
                    v_ensemble = (v_hom * peso_hom) + (v_unam * peso_unam)
                    v_ensemble = float(np.clip(v_ensemble, 0.4, 1.0))

                todos_vals = [v_uswb, v_temez, v_hom, v_het, v_unam]
                f_max, f_min = max(todos_vals), min(todos_vals)
                
                fra_max_list.append(f_max)
                fra_min_list.append(f_min)
                fra_ensemble_list.append(v_ensemble)
                
                datos_matriz.append({
                    "Tr": tr, "USWB (TP-29)": round(v_uswb, 3), "Témez": round(v_temez, 3),
                    "Frec. Homogéneo": round(v_hom, 3), "Frec. Heterogéneo": round(v_het, 3),
                    "UNAM (T. Centrada)": round(v_unam, 3), "ENSEMBLE (Consenso)": round(v_ensemble, 3)
                })

            df_resultados = pd.DataFrame(datos_matriz)

            # 4. TRADUCCIÓN VOLUMÉTRICA DE RIESGO (Hm3)
            # Para el Tr = 10,000 años
            peor_escenario_fra = fra_max_list[-1]
            mejor_escenario_fra = fra_min_list[-1]
            spread = peor_escenario_fra - mejor_escenario_fra
            
            vol_max_hm3 = (area_km2 * (p_24h_mm * peor_escenario_fra)) / 1000.0
            vol_min_hm3 = (area_km2 * (p_24h_mm * mejor_escenario_fra)) / 1000.0
            delta_vol_hm3 = vol_max_hm3 - vol_min_hm3

            # 5. MOTOR DE RECOMENDACIÓN (IA DETERMINISTA)
            dictamen = ""
            if is_microcuenca:
                dictamen = f"**DICTAMEN TÉCNICO:** La cuenca en estudio tiene un área de {area_km2:.2f} km², lo cual la clasifica como Microcuenca (< 25 km²). La literatura internacional (USACE, HEC-HMS) y la normativa dictan que **la atenuación espacial es nula**. Se impone el uso del FRA = 1.00 para todas las duraciones y periodos de retorno."
            elif spread > 0.15:
                dictamen = f"**DICTAMEN TÉCNICO:** Se detectó una **Alta Varianza Espacial (Spread = {spread:.2f})** entre los modelos. Esto indica topografía escarpada o microclimas agresivos. Se recomienda rechazar los métodos empíricos e implementar la **Curva de Consenso (Ensemble)**, la cual absorbe la física de la Tormenta Centrada (UNAM) y ancla un volumen de seguridad de {vol_max_hm3:.2f} Hm³."
            else:
                dictamen = f"**DICTAMEN TÉCNICO:** Los modelos muestran una convergencia altamente estable (Spread = {spread:.2f}). Para la precipitación ingresada de {p_24h_mm} mm en 24h, el impacto volumétrico es moderado. Se recomienda aplicar la **Frecuencia Homogénea** para Tr < 100 años, y el **Método UNAM** para la revisión del vertedor de demasías (Tr 10,000), o bien utilizar la Curva de Consenso para todo el rango."

            # 6. RENDERIZADO DEL "RING DE COMBATE" (Gráfico)
            fig, ax = plt.subplots(figsize=(10, 5))
            fig.patch.set_facecolor('#0a0a0a'); ax.set_facecolor('#0a0a0a')
            
            # Cono de Incertidumbre
            ax.fill_between(tr_lista, fra_min_list, fra_max_list, color='#333333', alpha=0.5, label='Cono de Incertidumbre Epistémica')
            
            # Curvas Base
            ax.plot(tr_lista, df_resultados['USWB (TP-29)'], '--', color='#1c75fa', alpha=0.6, label='USWB (TP-29)')
            ax.plot(tr_lista, df_resultados['Témez'], '--', color='#9900ff', alpha=0.6, label='Témez (España)')
            ax.plot(tr_lista, df_resultados['Frec. Homogéneo'], '-', color='#00ff41', alpha=0.8, label='Frec. Homogénea (Regional)')
            ax.plot(tr_lista, df_resultados['Frec. Heterogéneo'], '-', color='#ffaa00', alpha=0.8, label='Frec. Heterogénea (Microclima)')
            ax.plot(tr_lista, df_resultados['UNAM (T. Centrada)'], '-', color='#00ffff', alpha=0.8, label='UNAM (Topológica)')
            
            # Estrella del Show: El Ensemble
            ax.plot(tr_lista, df_resultados['ENSEMBLE (Consenso)'], '-', color='#ff0044', linewidth=4, label='FRA DE CONSENSO (Ensemble)')
            
            ax.set_xscale('log'); ax.set_ylim(0.35, 1.05)
            ax.set_title(f"Comparativa Final de Modelos ARF | Área: {area_km2:.1f} km²", color='white', pad=10)
            ax.set_xlabel("Periodo de Retorno Tr (Años) - Escala Log", color='grey')
            ax.set_ylabel("Factor de Reducción Areal (FRA)", color='grey')
            ax.tick_params(colors='grey')
            for spine in ax.spines.values(): spine.set_color('#333333')
            ax.legend(facecolor='#111111', edgecolor='#333333', labelcolor='white', loc='lower left', fontsize=8)
            ax.grid(True, which='both', linestyle=':', color='#333333', alpha=0.5)
            plt.tight_layout(); buf = io.BytesIO(); plt.savefig(buf, format='png', dpi=120); plt.close(fig)

            return {
                "exito": True,
                "df_resultados": df_resultados,
                "plot_b64": base64.b64encode(buf.getvalue()).decode('utf-8'),
                "area_km2": round(area_km2, 2),
                "fra_max": round(peor_escenario_fra, 3),
                "fra_min": round(mejor_escenario_fra, 3),
                "delta_hm3": round(delta_vol_hm3, 2),
                "dictamen": dictamen
            }

        except Exception as e:
            traceback.print_exc()
            return {"exito": False, "error": str(e)}
