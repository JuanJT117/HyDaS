# core/analisis_cuenca.py
import os
import sys

# Asegurar carga de librerías nativas (libpng, freetype) en entornos Conda en Windows
if sys.platform == 'win32':
    for base_dir in [sys.prefix, sys.exec_prefix]:
        for sub in [('Library', 'bin'), ('Library', 'usr', 'bin'), ('Library', 'mingw-w64', 'bin'), ('bin',), ('Scripts',)]:
            p = os.path.join(base_dir, *sub)
            if os.path.isdir(p):
                os.environ['PATH'] = p + os.pathsep + os.environ.get('PATH', '')
                if hasattr(os, 'add_dll_directory'):
                    try: os.add_dll_directory(p)
                    except Exception: pass

import pandas as pd
import numpy as np
import scipy.stats as stats
from scipy.stats import gamma, lognorm, norm, gumbel_r, genextreme
from scipy.optimize import brentq
import math
import io
import base64
import traceback
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from typing import Dict, Tuple, List, Optional, Any

from core.modelos_clima import EntradaDescriptoresChen, ResultadoDescriptoresChen

def _figure_to_base64(fig) -> str:
    """Convierte una figura Matplotlib a base64 optimizado."""
    buf = io.BytesIO()
    fig.savefig(buf, format='png', dpi=100, bbox_inches='tight')
    plt.close(fig)
    buf.seek(0)
    return base64.b64encode(buf.getvalue()).decode('utf-8')

class ExtractorCuantilesExtremos:
    """Motor polimórfico para cálculo de cuantiles p2=0.50, p10=0.90, p25=0.96, p50=0.98 y p100=0.99."""
    
    @staticmethod
    def _calcular_k_kite(Tr: float, skew: float) -> float:
        """Factor de frecuencia mediante transformación Wilson-Hilferty / Kite."""
        prob = 1.0 / Tr
        w = np.sqrt(np.log(1.0 / (prob ** 2)))
        num = 2.515517 + (0.802853 * w) + (0.010328 * (w ** 2))
        den = 1.0 + (1.432788 * w) + (0.189269 * (w ** 2)) + (0.001308 * (w ** 3))
        z = w - (num / den)
        
        if abs(skew) < 0.001:
            return float(z)
        
        k = skew / 6.0
        A = (z ** 2) - 1.0
        B = (1.0 / 3.0) * ((z ** 3) - (6.0 * z))
        kt = z + (A * k) + (B * (k ** 2)) - (A * (k ** 3)) + (z * (k ** 4)) + ((1.0 / 3.0) * (k ** 5))
        return float(kt)

    @classmethod
    def extraer_cuantiles_p24_multi(cls, best_fit_name: str, max_annual_data: np.ndarray) -> Dict[int, float]:
        """
        Extrae los cuantiles teóricos fijos P24 para Tr in [2, 10, 25, 50, 100].
        Retorna diccionario: {2: p2, 10: p10, 25: p25, 50: p50, 100: p100}.
        """
        datos = np.asarray(max_annual_data[~np.isnan(max_annual_data)], dtype=np.float64)
        if len(datos) < 5:
            raise ValueError(f"Muestra insuficiente ({len(datos)} registros) para inferencia de extremos.")

        nombre_clean = best_fit_name.strip().lower()
        trs = [2, 10, 25, 50, 100]
        probs = [1.0 - (1.0 / tr) for tr in trs]
        cuantiles = {}

        # 1. Log-Pearson III
        if any(k in nombre_clean for k in ['log pearson', 'log-pearson', 'lp3']):
            y = np.log10(datos)
            mean_y, std_y, skew_y = np.mean(y), np.std(y, ddof=1), float(pd.Series(y).skew())
            for tr in trs:
                k_val = cls._calcular_k_kite(float(tr), skew_y)
                cuantiles[tr] = float(10.0 ** (mean_y + (k_val * std_y)))

        # 2. Log-Normal
        elif any(k in nombre_clean for k in ['log normal', 'log-normal', 'ln']):
            w = np.log(datos)
            mean_w, std_w = np.mean(w), np.std(w, ddof=1)
            for tr, p in zip(trs, probs):
                z = norm.ppf(p)
                cuantiles[tr] = float(np.exp(mean_w + z * std_w))

        # 3. Gumbel 2 Poblaciones
        elif any(k in nombre_clean for k in ['2 poblaciones', '2-poblaciones', 'dos poblaciones']):
            mid = len(datos) // 2
            p1 = gumbel_r.fit(datos[:mid])
            p2 = gumbel_r.fit(datos[mid:])
            def cdf_mix(x):
                return 0.5 * gumbel_r.cdf(x, *p1) + 0.5 * gumbel_r.cdf(x, *p2)
            lim_inf, lim_sup = np.min(datos) * 0.5, np.max(datos) * 5.0
            for tr, p in zip(trs, probs):
                cuantiles[tr] = float(brentq(lambda x: cdf_mix(x) - p, lim_inf, lim_sup))

        # 4. Gumbel Simple
        elif 'gumbel' in nombre_clean:
            std_x = np.std(datos, ddof=1)
            alpha = (np.sqrt(6.0) / np.pi) * std_x
            mu = np.mean(datos) - (0.5772156649 * alpha)
            for tr, p in zip(trs, probs):
                cuantiles[tr] = float(mu - alpha * np.log(-np.log(p)))

        # 5. Pearson Tipo III
        elif 'pearson' in nombre_clean:
            mean_x, std_x = np.mean(datos), np.std(datos, ddof=1)
            skew_x = float(pd.Series(datos).skew())
            if abs(skew_x) < 0.001: skew_x = 0.001
            beta = (2.0 / skew_x) ** 2
            alfa = std_x / np.sqrt(beta)
            gamma_param = mean_x - (alfa * beta)
            for tr, p in zip(trs, probs):
                cuantiles[tr] = float(gamma.ppf(p, a=beta, loc=gamma_param, scale=alfa))

        # 6. Exponencial
        elif 'exp' in nombre_clean:
            mean_x, std_x = np.mean(datos), np.std(datos, ddof=1)
            x0 = mean_x - std_x
            for tr, p in zip(trs, probs):
                cuantiles[tr] = float(x0 - std_x * np.log(1.0 - p))

        # 7. Normal
        elif 'normal' in nombre_clean:
            mean_x, std_x = np.mean(datos), np.std(datos, ddof=1)
            for tr, p in zip(trs, probs):
                z = norm.ppf(p)
                cuantiles[tr] = float(mean_x + z * std_x)

        # 8. Gamma
        elif 'gamma' in nombre_clean:
            mean_x, std_x = np.mean(datos), np.std(datos, ddof=1)
            a_param = (mean_x / std_x) ** 2
            scale_param = (std_x ** 2) / mean_x
            for tr, p in zip(trs, probs):
                cuantiles[tr] = float(gamma.ppf(p, a=a_param, scale=scale_param))

        # 9. GEV
        elif any(k in nombre_clean for k in ['valores extremos', 'gev']):
            params = genextreme.fit(datos)
            for tr, p in zip(trs, probs):
                cuantiles[tr] = float(genextreme.ppf(p, *params))

        else:
            raise NotImplementedError(f"Distribución '{best_fit_name}' no soportada por el motor de cuantiles.")

        return cuantiles

def _calculate_regional_params(r: float) -> Tuple[float, float, float]:
    """Polinomios oficiales de 4º grado (Campos-Aranda, 1990/2010 / SCT)."""
    r_val = float(np.clip(r, 0.10, 0.699))
    a = -2.297536 + (100.0389 * r_val) - (432.5438 * (r_val**2)) + (1256.228 * (r_val**3)) - (1028.902 * (r_val**4))
    b = -9.845761 + (96.94864 * r_val) - (341.4349 * (r_val**2)) + (757.9172 * (r_val**3)) - (598.7461 * (r_val**4))
    c = -0.06498345 + (5.069294 * r_val) - (16.08111 * (r_val**2)) + (29.0960 * (r_val**3)) - (20.06288 * (r_val**4))
    return float(a), float(b), float(c)

def _calculate_curvas_hidrologicas(
    p_r_a: float, p_r_b: float, p_r_c: float, p_r_f: float, p1_10: float
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """
    Construye las matrices de Altura (mm) e Intensidad (mm/h) según Chen (1983).
    Garantiza índices numéricos en minutos (np.float64) y nombres de columna estándar.
    """
    tr_cols = [2, 5, 10, 20, 50, 100, 500, 1000, 10000]
    duraciones = np.arange(5, 1445, 5, dtype=np.float64)
    
    t_grid = duraciones[:, np.newaxis]
    tr_grid = np.array(tr_cols, dtype=np.float64)[np.newaxis, :]
    
    term_frec = np.log10((10.0 ** (2.0 - p_r_f)) * (tr_grid ** (p_r_f - 1.0)))
    intensidades = (p_r_a * p1_10 * term_frec) / ((t_grid + p_r_b) ** p_r_c)
    intensidades = np.maximum(intensidades, 0.0)
    
    alturas = (intensidades * t_grid) / 60.0
    
    df_altura = pd.DataFrame(alturas, index=duraciones, columns=tr_cols)
    df_altura.index.name = 'DURACION_MIN'
    
    df_intensidad = pd.DataFrame(intensidades, index=duraciones, columns=tr_cols)
    df_intensidad.index.name = 'DURACION_MIN'
    
    return df_altura, df_intensidad

def _generate_cuenca_plots(df_altura_in, df_intensidad_in, ylim_altura, ylim_intensidad, station_id=""):
    """Genera los 4 gráficos hidrológicos en base64."""
    df_altura = df_altura_in.copy()
    df_intensidad = df_intensidad_in.copy()

    if 'DURACION_MIN' not in df_altura.columns and 'TR (AÑOS)' not in df_altura.columns:
        df_altura.reset_index(inplace=True)
    col_x_h = 'DURACION_MIN' if 'DURACION_MIN' in df_altura.columns else ('TR (AÑOS)' if 'TR (AÑOS)' in df_altura.columns else df_altura.columns[0])

    if 'DURACION_MIN' not in df_intensidad.columns and 'TR (AÑOS)' not in df_intensidad.columns:
        df_intensidad.reset_index(inplace=True)
    col_x_i = 'DURACION_MIN' if 'DURACION_MIN' in df_intensidad.columns else ('TR (AÑOS)' if 'TR (AÑOS)' in df_intensidad.columns else df_intensidad.columns[0])

    columnas_precipitacion = [2, 5, 10, 20, 50, 100, 500, 1000, 10000]
    columnas_precipitacion_2 = [100, 500, 1000, 10000]

    cols_data = [c for c in df_altura.columns if c not in ['TR (AÑOS)', 'DURACION_MIN']]
    max_h_data = df_altura[cols_data].max().max() if cols_data else 100
    
    cols_data_int = [c for c in df_intensidad.columns if c not in ['TR (AÑOS)', 'DURACION_MIN']]
    max_i_data = df_intensidad[cols_data_int].max().max() if cols_data_int else 100

    final_ylim_h = ylim_altura if (ylim_altura and ylim_altura > 0) else (max_h_data * 1.1 if max_h_data > 0 else 100)
    final_ylim_i = ylim_intensidad if (ylim_intensidad and ylim_intensidad > 0) else (max_i_data * 1.1 if max_i_data > 0 else 100)

    # 1. Altura Completa
    fig1, ax1 = plt.subplots(figsize=(10, 5))
    for col in columnas_precipitacion:
        if col in df_altura.columns:
            ax1.plot(df_altura[col_x_h], df_altura[col], label=f'TR = {col} años', alpha=0.8)
    ax1.set_xlabel('DURACIÓN (MIN)')
    ax1.set_ylabel('ALTURA (mm)')
    ax1.set_title(f'CURVAS DE ALTURA - {station_id}' if station_id else 'CURVAS DE ALTURA')
    ax1.set_ylim(0, final_ylim_h)
    ax1.legend(loc='upper right', fontsize='small')
    ax1.grid(True, linestyle='--', alpha=0.7)
    plt.tight_layout()
    p1 = _figure_to_base64(fig1)

    # 2. Altura Zoom 300min
    df_f2 = df_altura[(df_altura[col_x_h] >= 0) & (df_altura[col_x_h] <= 300)]
    fig2, ax2 = plt.subplots(figsize=(10, 5))
    for col in columnas_precipitacion_2:
        if col in df_f2.columns:
            ax2.plot(df_f2[col_x_h], df_f2[col], label=f'TR = {col} años', alpha=0.8)
    ax2.set_xlabel('DURACIÓN (MIN)')
    ax2.set_ylabel('ALTURA (mm)')
    ax2.set_title(f'CURVAS DE ALTURA (0-300min) - {station_id}' if station_id else 'CURVAS DE ALTURA (0-300min)')
    ax2.set_ylim(0, final_ylim_h)
    ax2.set_xlim(0, 300)
    ax2.legend(loc='upper right', fontsize='small')
    ax2.grid(True, linestyle='--', alpha=0.7)
    plt.tight_layout()
    p2 = _figure_to_base64(fig2)

    # 3. Intensidad Completa
    fig3, ax3 = plt.subplots(figsize=(10, 5))
    for col in columnas_precipitacion:
        if col in df_intensidad.columns:
            ax3.plot(df_intensidad[col_x_i], df_intensidad[col], label=f'TR = {col} años', alpha=0.8)
    ax3.set_xlabel('DURACIÓN (MIN)')
    ax3.set_ylabel('INTENSIDAD (mm/hr)')
    ax3.set_title(f'CURVAS DE INTENSIDAD - {station_id}' if station_id else 'CURVAS DE INTENSIDAD')
    ax3.set_ylim(0, final_ylim_i)
    ax3.legend(loc='upper right', fontsize='small')
    ax3.grid(True, linestyle='--', alpha=0.7)
    plt.tight_layout()
    p3 = _figure_to_base64(fig3)

    # 4. Intensidad Zoom 300min
    df_f3 = df_intensidad[(df_intensidad[col_x_i] >= 0) & (df_intensidad[col_x_i] <= 300)]
    fig4, ax4 = plt.subplots(figsize=(10, 5))
    for col in columnas_precipitacion_2:
        if col in df_f3.columns:
            ax4.plot(df_f3[col_x_i], df_f3[col], label=f'TR = {col} años', alpha=0.8)
    ax4.set_xlabel('DURACIÓN (MIN)')
    ax4.set_ylabel('INTENSIDAD (mm/hr)')
    ax4.set_title(f'CURVAS DE INTENSIDAD (0-300min) - {station_id}' if station_id else 'CURVAS DE INTENSIDAD (0-300min)')
    ax4.set_ylim(0, final_ylim_i)
    ax4.set_xlim(0, 300)
    ax4.legend(loc='upper right', fontsize='small')
    ax4.grid(True, linestyle='--', alpha=0.7)
    plt.tight_layout()
    p4 = _figure_to_base64(fig4)

    return p1, p2, p3, p4

def run_cuenca_analysis(
    best_fit_name: str,
    df_maximos_mensuales: pd.DataFrame,
    metodo_r: str = "r_regional",
    r_regional_manual: float = 0.3882,
    p1_10_manual: Optional[float] = None,
    p1_25_manual: Optional[float] = None,
    p1_50_manual: Optional[float] = None,
    ylim_altura: Optional[float] = None,
    ylim_intensidad: Optional[float] = None,
    station_id: str = "",
    aplicar_weiss: bool = True
) -> Dict[str, Any]:
    """
    Motor matemático de desagregación de tormentas (Chen / Campos-Aranda).
    Bifurcación exclusiva: 'isoyetas_sct' (Láminas P1) vs 'r_regional' (Factor R Directo).
    """
    log_list = []
    advertencias = []

    # 1. Extracción de Máximos Anuales
    col_target = 'Max_Anual' if 'Max_Anual' in df_maximos_mensuales.columns else df_maximos_mensuales.columns[0]
    datos_max = df_maximos_mensuales[col_target].dropna().values

    # 2. Extracción de Cuantiles P24 Fijos (Tr = 2, 10, 25, 50, 100)
    cuantiles_fijos = ExtractorCuantilesExtremos.extraer_cuantiles_p24_multi(best_fit_name, datos_max)
    p24_fijo_2 = cuantiles_fijos[2]
    p24_fijo_10 = cuantiles_fijos[10]
    p24_fijo_25 = cuantiles_fijos[25]
    p24_fijo_50 = cuantiles_fijos[50]
    p24_fijo_100 = cuantiles_fijos[100]

    # 3. Factor de Corrección por Intervalo Fijo de Observación (Weiss / OMM)
    factor_w = 1.13 if aplicar_weiss else 1.0
    p24_movil_10 = p24_fijo_10 * factor_w
    p24_movil_25 = p24_fijo_25 * factor_w
    p24_movil_50 = p24_fijo_50 * factor_w
    p24_movil_100 = p24_fijo_100 * factor_w

    # 4. Determinación Determinista del Cociente R
    if metodo_r == "r_regional":
        log_list.append("Método de Calibración: FACTOR R REGIONAL DIRECTO (Campos-Aranda / SICT)")
        r_prom_crudo = float(r_regional_manual)
        r_acotado = float(np.clip(r_prom_crudo, 0.10, 0.699))
        
        # Deducción analítica de P1_10 si no se ingresó explícitamente
        if p1_10_manual and p1_10_manual > 0:
            p1_10 = float(p1_10_manual)
        else:
            p1_10 = r_acotado * p24_movil_10
            
        p1_25 = p1_10 * 1.18
        p1_50 = p1_10 * 1.32
        r_10 = r_acotado
        r_25 = r_acotado
        r_50 = r_acotado
    else:
        log_list.append("Método de Calibración: ISOYETAS SCT (Multi-Tr)")
        p1_10 = float(p1_10_manual) if (p1_10_manual and p1_10_manual > 0) else 40.0
        p1_25 = float(p1_25_manual) if (p1_25_manual and p1_25_manual > 0) else (p1_10 * 1.18)
        p1_50 = float(p1_50_manual) if (p1_50_manual and p1_50_manual > 0) else (p1_10 * 1.32)
        
        r_10 = p1_10 / p24_movil_10
        r_25 = p1_25 / p24_movil_25
        r_50 = p1_50 / p24_movil_50
        r_prom_crudo = (r_10 + r_25 + r_50) / 3.0
        r_acotado = float(np.clip(r_prom_crudo, 0.10, 0.699))

    if r_prom_crudo >= 0.70:
        advertencias.append(f"R promedio ({r_prom_crudo:.4f}) >= 0.70. Acotado a 0.699 (Límite matemático de Chen).")
    elif r_prom_crudo < 0.10:
        advertencias.append(f"R promedio ({r_prom_crudo:.4f}) < 0.10. Acotado a 0.100 mínimo.")

    # 5. Cociente de Frecuencia F y Parámetros Chen (a, b, c)
    f_calc = p24_movil_100 / p24_movil_10
    param_a, param_b, param_c = _calculate_regional_params(r_acotado)

    # 6. Generación de Matrices Hidrológicas
    df_altura, df_intensidad = _calculate_curvas_hidrologicas(param_a, param_b, param_c, f_calc, p1_10)

    # 7. Renderizado de Gráficos en Base64
    plot_1_b64, plot_2_b64, plot_3_b64, plot_4_b64 = _generate_cuenca_plots(
        df_altura, df_intensidad, ylim_altura, ylim_intensidad, station_id
    )

    # 8. Reporte y Esquema Pydantic
    log_list.append(f"=== REPORTE DE CALIBRACIÓN DE TORMENTAS - ESTACIÓN {station_id} ===")
    log_list.append(f"Modelo de Distribución Óptimo: {best_fit_name}")
    log_list.append(f"Factor de Weiss Aplicado: {factor_w:.2f}")
    log_list.append(f"Láminas 60 min (P1): Tr10={p1_10:.2f} mm | Tr25={p1_25:.2f} mm | Tr50={p1_50:.2f} mm")
    log_list.append(f"Láminas 24h Móviles: Tr10={p24_movil_10:.2f} mm | Tr100={p24_movil_100:.2f} mm")
    log_list.append(f"Cocientes de Duración: R10={r_10:.4f} | R25={r_25:.4f} | R50={r_50:.4f}")
    log_list.append(f"R Aplicado: {r_acotado:.4f} | Factor F: {f_calc:.4f}")
    log_list.append(f"Parámetros Chen: a={param_a:.4f}, b={param_b:.4f}, c={param_c:.4f}")
    for adv in advertencias:
        log_list.append(f"ADVERTENCIA: {adv}")

    resultado_contrato = ResultadoDescriptoresChen(
        station_id=station_id,
        distribucion_aplicada=best_fit_name,
        metodo_r_utilizado=metodo_r,
        p1_10_mm=p1_10,
        p1_25_mm=p1_25,
        p1_50_mm=p1_50,
        p24_fijo_10_mm=p24_fijo_10,
        p24_fijo_25_mm=p24_fijo_25,
        p24_fijo_50_mm=p24_fijo_50,
        p24_fijo_100_mm=p24_fijo_100,
        factor_weiss=factor_w,
        p24_movil_10_mm=p24_movil_10,
        p24_movil_25_mm=p24_movil_25,
        p24_movil_50_mm=p24_movil_50,
        p24_movil_100_mm=p24_movil_100,
        r_10=r_10,
        r_25=r_25,
        r_50=r_50,
        r_promedio_crudo=r_prom_crudo,
        r_acotado=r_acotado,
        f_calculado=f_calc,
        param_a=param_a,
        param_b=param_b,
        param_c=param_c,
        advertencias=advertencias
    )

    df_parametros = pd.DataFrame([
        {"Parámetro": "Método de Calibración", "Valor": "Isoyetas SCT" if metodo_r == "isoyetas_sct" else "Factor R Regional Directo"},
        {"Parámetro": "Modelo de Distribución", "Valor": best_fit_name},
        {"Parámetro": "P1_10 (60 min)", "Valor": f"{p1_10:.3f} mm"},
        {"Parámetro": "Factor Weiss (OMM)", "Valor": f"{factor_w:.2f}"},
        {"Parámetro": "P24_10 (Móvil)", "Valor": f"{p24_movil_10:.3f} mm"},
        {"Parámetro": "R Aplicado", "Valor": f"{r_acotado:.4f}"},
        {"Parámetro": "Factor F (P100/P10)", "Valor": f"{f_calc:.4f}"},
        {"Parámetro": "Chen a", "Valor": f"{param_a:.4f}"},
        {"Parámetro": "Chen b", "Valor": f"{param_b:.4f}"},
        {"Parámetro": "Chen c", "Valor": f"{param_c:.4f}"}
    ])

    return {
        "log_text": "\n".join(log_list),
        "df_altura": df_altura,
        "df_intensidad": df_intensidad,
        "df_parametros": df_parametros,
        "descriptores_chen": resultado_contrato.model_dump(),
        "plot_1_b64": plot_1_b64,
        "plot_2_b64": plot_2_b64,
        "plot_3_b64": plot_3_b64,
        "plot_4_b64": plot_4_b64
    }