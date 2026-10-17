# tests/test_chen_integration.py
import os
import sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# Configurar stdout a UTF-8 para compatibilidad en consolas Windows
if sys.platform == 'win32':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

import numpy as np
import pandas as pd
from core.analisis_cuenca import run_cuenca_analysis, ExtractorCuantilesExtremos

def test_doble_metodo_chen():
    print("=== INICIANDO BATERIA DE PRUEBAS DE NO-REGRESION (R-REGIONAL / SCT) ===")
    
    # 1. Muestra sintetica de 40 anos
    np.random.seed(42)
    datos_sinteticos = np.random.gumbel(loc=60.0, scale=20.0, size=40)
    df_m = pd.DataFrame({"Max_Anual": datos_sinteticos})

    # 2. Test Metodo 1: Factor R Regional Directo (Campos-Aranda / SICT)
    print("-> Test 1: Metodo Factor R Regional Directo (Campos-Aranda / SICT)...")
    res_reg = run_cuenca_analysis(
        best_fit_name="Gumbel",
        df_maximos_mensuales=df_m,
        metodo_r="r_regional",
        r_regional_manual=0.3882,
        station_id="TEST_REGIONAL"
    )
    desc_r = res_reg["descriptores_chen"]
    assert desc_r["p1_10_mm"] > 0, "Falla: P1_10 no generado analiticamente"
    assert 0.10 <= desc_r["r_acotado"] <= 0.699, "Falla: R acotado fuera de rango fisico"
    assert desc_r["f_calculado"] > 1.0, "Falla: F no monotono"
    assert res_reg["df_intensidad"].index.name == "DURACION_MIN", "Falla: index.name debe ser DURACION_MIN"
    assert res_reg["df_intensidad"].index.dtype == np.float64, "Falla: index.dtype debe ser float64"
    assert res_reg["df_intensidad"].loc[5.0, 100] > res_reg["df_intensidad"].loc[60.0, 100], "Falla: dI/dt no decreciente"
    print("[PASS] Test 1 (Factor R Regional) Aprobado con exito.")

    # 3. Test Metodo 2: Isoyetas SCT Multi-Tr
    print("-> Test 2: Metodo Isoyetas SCT (Entrada Manual)...")
    res_sct = run_cuenca_analysis(
        best_fit_name="Gumbel",
        df_maximos_mensuales=df_m,
        metodo_r="isoyetas_sct",
        p1_10_manual=45.0,
        p1_25_manual=54.0,
        p1_50_manual=62.0,
        station_id="TEST_SCT"
    )
    desc_s = res_sct["descriptores_chen"]
    assert 0.10 <= desc_s["r_acotado"] <= 0.699, "Falla: R fuera de rango fisico"
    assert res_sct["df_altura"].index.name == "DURACION_MIN", "Falla: index.name debe ser DURACION_MIN"
    assert res_sct["df_altura"].index.dtype == np.float64, "Falla: index.dtype debe ser float64"
    assert res_sct["df_altura"].loc[60.0, 10] < res_sct["df_altura"].loc[1440.0, 10], "Falla: dH/dt no creciente"
    print("[PASS] Test 2 (Isoyetas SCT) Aprobado con exito.")

    print("=== TODAS LAS PRUEBAS DE DOMINIO FUERON SUPERADAS CON EXITO ===")

if __name__ == "__main__":
    test_doble_metodo_chen()
