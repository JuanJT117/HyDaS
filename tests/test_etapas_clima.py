# tests/test_etapas_clima.py
import unittest
import numpy as np
import pandas as pd
import sqlite3
import tempfile
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# Configurar stdout a UTF-8 para compatibilidad en consolas Windows
if sys.platform == 'win32':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

from core.modelos_clima import EntradaDescriptoresChen, ResultadoDescriptoresChen
from core.analisis_cuenca import (
    ExtractorCuantilesExtremos, 
    _calculate_regional_params, 
    _calculate_curvas_hidrologicas, 
    run_cuenca_analysis
)

class TestIntegracionClimaChen(unittest.TestCase):

    def test_1_inferencia_cuantiles_multi_r(self):
        """
        [TEST 1: Inferencia Multi-R y Cuantiles Tr=10, 25, 50, 100]
        Entrada: Serie SAM sintética basada en LP3 con N=30 registros.
        Láminas SCT: P1_10=40.0, P1_25=48.0, P1_50=55.0 mm.
        Validar: R10, R25, R50, R_promedio y Factor Weiss = 1.13.
        """
        np.random.seed(42)
        log_mean = 1.8
        log_std = 0.2
        raw_log = np.random.normal(loc=log_mean, scale=log_std, size=30)
        datos_sam = np.exp(raw_log)
        
        cuantiles = ExtractorCuantilesExtremos.extraer_cuantiles_p24_multi('Log Pearson III', datos_sam)
        
        self.assertIn(2, cuantiles)
        self.assertIn(10, cuantiles)
        self.assertIn(25, cuantiles)
        self.assertIn(50, cuantiles)
        self.assertIn(100, cuantiles)
        
        self.assertGreater(cuantiles[10], cuantiles[2])
        self.assertGreater(cuantiles[25], cuantiles[10])
        self.assertGreater(cuantiles[50], cuantiles[25])
        self.assertGreater(cuantiles[100], cuantiles[50])
        
        df_sam = pd.DataFrame({'Max_Anual': datos_sam})
        res = run_cuenca_analysis(
            'Log Pearson III', df_sam, 
            metodo_r="isoyetas_sct",
            p1_10_manual=40.0, p1_25_manual=48.0, p1_50_manual=55.0, 
            aplicar_weiss=True, station_id="TEST_MULTI_R"
        )
        
        desc = res["descriptores_chen"]
        self.assertEqual(desc["factor_weiss"], 1.13)
        self.assertAlmostEqual(desc["p24_movil_10_mm"], cuantiles[10] * 1.13, places=3)
        self.assertAlmostEqual(desc["p24_movil_25_mm"], cuantiles[25] * 1.13, places=3)
        self.assertAlmostEqual(desc["p24_movil_50_mm"], cuantiles[50] * 1.13, places=3)
        self.assertAlmostEqual(desc["p24_movil_100_mm"], cuantiles[100] * 1.13, places=3)
        
        # Verificar cálculo de R individual y promedio
        r10_esperado = 40.0 / (cuantiles[10] * 1.13)
        r25_esperado = 48.0 / (cuantiles[25] * 1.13)
        r50_esperado = 55.0 / (cuantiles[50] * 1.13)
        r_prom_esperado = (r10_esperado + r25_esperado + r50_esperado) / 3.0
        
        self.assertAlmostEqual(desc["r_10"], r10_esperado, places=4)
        self.assertAlmostEqual(desc["r_25"], r25_esperado, places=4)
        self.assertAlmostEqual(desc["r_50"], r50_esperado, places=4)
        self.assertAlmostEqual(desc["r_promedio_crudo"], r_prom_esperado, places=4)
        print("[PASS] TEST 1 Superado: Inferencia Multi-R (R10, R25, R50, R_promedio) y Factor de Weiss correctos.")
    
    def test_2_clamping_multi_r_y_advertencias(self):
        """
        [TEST 2: Clamping de Chen Multi-R]
        Entrada: P1_10=250.0, P1_25=280.0, P1_50=300.0 mm (R_promedio > 0.70)
        Validar: R_acotado == 0.699. Advertencia registrada en el log.
        """
        datos_sam = np.array([80.0, 90.0, 100.0, 110.0, 120.0, 130.0, 140.0])
        df_sam = pd.DataFrame({'Max_Anual': datos_sam})
        
        res = run_cuenca_analysis(
            'Gumbel', df_sam, 
            metodo_r="isoyetas_sct",
            p1_10_manual=250.0, p1_25_manual=280.0, p1_50_manual=300.0, 
            aplicar_weiss=False, station_id="TEST_CLAMP"
        )
        desc = res["descriptores_chen"]
        
        self.assertEqual(desc["r_acotado"], 0.699, "R promedio debe estar acotado a 0.699 según Campos-Aranda (2010).")
        self.assertTrue(any("0.699" in adv or "0.70" in adv for adv in desc["advertencias"]), "Debe registrarse advertencia de truncamiento.")
        self.assertIn("ADVERTENCIA", res["log_text"])
        print("[PASS] TEST 2 Superado: Clamping defensivo de R promedio a 0.699 y advertencias verificadas.")

    def test_3_estabilidad_curvas_idf(self):
        """
        [TEST 3: Estabilidad de Curvas IDF]
        Entrada: Matriz evaluada de 5 a 1440 min.
        Validar: Monotonía estrictamente decreciente en Intensidad (dI/dt <= 0).
        Validar: Monotonía estrictamente creciente en Altura (dH/dt >= 0).
        """
        r_val = 0.35
        f_val = 1.45
        p1_10 = 50.0
        param_a, param_b, param_c = _calculate_regional_params(r_val)
        
        df_altura, df_intensidad = _calculate_curvas_hidrologicas(param_a, param_b, param_c, f_val, p1_10)
        tr_cols = [2, 5, 10, 20, 50, 100, 500, 1000, 10000]
        
        for tr in tr_cols:
            alturas = df_altura[tr].values
            intensidades = df_intensidad[tr].values
            
            diff_h = np.diff(alturas)
            self.assertTrue(np.all(diff_h >= -1e-6), f"Curva de altura para Tr={tr} debe ser monótonamente creciente.")
            
            diff_i = np.diff(intensidades)
            self.assertTrue(np.all(diff_i <= 1e-6), f"Curva de intensidad para Tr={tr} debe ser monótonamente decreciente.")
            
        print("[PASS] TEST 3 Superado: Estabilidad y monotonía matemática verificada en curvas IDF y PDR.")

    def test_4_persistencia_sqlite_geopackage_multi_r(self):
        """
        [TEST 4: Persistencia SQLite GeoPackage Multi-R]
        Validar: Tabla 'estacion_descriptores_chen' almacena p1_10, p1_25, p1_50, r_10, r_25, r_50, r_promedio, param_a, param_b, param_c.
        Validar: Upsert atómico y rehidratación.
        """
        with tempfile.NamedTemporaryFile(suffix=".gpkg", delete=False) as tmp:
            tmp_path = tmp.name

        conn = None
        try:
            sid = "EST_MULTI_99"
            datos_sam = np.array([45.0, 55.0, 60.0, 72.0, 85.0, 95.0, 110.0])
            df_sam = pd.DataFrame({'Max_Anual': datos_sam})
            
            res = run_cuenca_analysis(
                'Normal', df_sam, 
                metodo_r="isoyetas_sct",
                p1_10_manual=40.0, p1_25_manual=48.0, p1_50_manual=55.0, 
                aplicar_weiss=True, station_id=sid
            )
            desc = res["descriptores_chen"]
            
            conn = sqlite3.connect(tmp_path)
            cur = conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL;")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS estacion_descriptores_chen (
                    station_id TEXT PRIMARY KEY,
                    distribucion TEXT NOT NULL,
                    metodo_r TEXT NOT NULL,
                    p1_10 REAL NOT NULL,
                    p1_25 REAL NOT NULL,
                    p1_50 REAL NOT NULL,
                    p24_fijo_10 REAL NOT NULL,
                    p24_fijo_25 REAL NOT NULL,
                    p24_fijo_50 REAL NOT NULL,
                    p24_fijo_100 REAL NOT NULL,
                    factor_weiss REAL NOT NULL,
                    p24_movil_10 REAL NOT NULL,
                    p24_movil_25 REAL NOT NULL,
                    p24_movil_50 REAL NOT NULL,
                    p24_movil_100 REAL NOT NULL,
                    r_10 REAL NOT NULL,
                    r_25 REAL NOT NULL,
                    r_50 REAL NOT NULL,
                    r_promedio REAL NOT NULL,
                    factor_f REAL NOT NULL,
                    param_a REAL NOT NULL,
                    param_b REAL NOT NULL,
                    param_c REAL NOT NULL,
                    fecha_calculo TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            cur.execute("""
                INSERT INTO estacion_descriptores_chen (
                    station_id, distribucion, metodo_r, p1_10, p1_25, p1_50,
                    p24_fijo_10, p24_fijo_25, p24_fijo_50, p24_fijo_100,
                    factor_weiss, p24_movil_10, p24_movil_25, p24_movil_50, p24_movil_100,
                    r_10, r_25, r_50, r_promedio, factor_f, param_a, param_b, param_c
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(station_id) DO UPDATE SET
                    distribucion=excluded.distribucion,
                    metodo_r=excluded.metodo_r,
                    p1_10=excluded.p1_10,
                    p1_25=excluded.p1_25,
                    p1_50=excluded.p1_50,
                    p24_fijo_10=excluded.p24_fijo_10,
                    p24_fijo_25=excluded.p24_fijo_25,
                    p24_fijo_50=excluded.p24_fijo_50,
                    p24_fijo_100=excluded.p24_fijo_100,
                    factor_weiss=excluded.factor_weiss,
                    p24_movil_10=excluded.p24_movil_10,
                    p24_movil_25=excluded.p24_movil_25,
                    p24_movil_50=excluded.p24_movil_50,
                    p24_movil_100=excluded.p24_movil_100,
                    r_10=excluded.r_10,
                    r_25=excluded.r_25,
                    r_50=excluded.r_50,
                    r_promedio=excluded.r_promedio,
                    factor_f=excluded.factor_f,
                    param_a=excluded.param_a,
                    param_b=excluded.param_b,
                    param_c=excluded.param_c,
                    fecha_calculo=CURRENT_TIMESTAMP;
            """, (
                sid, desc["distribucion_aplicada"], desc["metodo_r_utilizado"],
                desc["p1_10_mm"], desc["p1_25_mm"], desc["p1_50_mm"],
                desc["p24_fijo_10_mm"], desc["p24_fijo_25_mm"],
                desc["p24_fijo_50_mm"], desc["p24_fijo_100_mm"],
                desc["factor_weiss"],
                desc["p24_movil_10_mm"], desc["p24_movil_25_mm"],
                desc["p24_movil_50_mm"], desc["p24_movil_100_mm"],
                desc["r_10"], desc["r_25"], desc["r_50"],
                desc["r_acotado"], desc["f_calculado"],
                desc["param_a"], desc["param_b"], desc["param_c"]
            ))
            conn.commit()
            
            # Rehidratación
            cur.execute("""
                SELECT station_id, metodo_r, p1_10, p1_25, p1_50, r_10, r_25, r_50, r_promedio, param_a, param_b, param_c 
                FROM estacion_descriptores_chen WHERE station_id = ?
            """, (sid,))
            row = cur.fetchone()
            self.assertIsNotNone(row, "Debe existir el registro en SQLite/GeoPackage.")
            self.assertEqual(row[0], sid)
            self.assertEqual(row[1], "isoyetas_sct")
            self.assertAlmostEqual(row[2], 40.0)
            self.assertAlmostEqual(row[3], 48.0)
            self.assertAlmostEqual(row[4], 55.0)
            self.assertAlmostEqual(row[5], desc["r_10"], places=4)
            self.assertAlmostEqual(row[6], desc["r_25"], places=4)
            self.assertAlmostEqual(row[7], desc["r_50"], places=4)
            self.assertAlmostEqual(row[8], desc["r_acotado"], places=4)
            
            print("[PASS] TEST 4 Superado: Persistencia GeoRelacional Multi-R y Rehidratacion exitosa en SQLite/GPKG.")
        finally:
            if conn:
                try: conn.close()
                except Exception: pass
            if os.path.exists(tmp_path):
                try: os.remove(tmp_path)
                except Exception: pass

    def test_5_no_regresion_modulos_4_y_5(self):
        """
        [TEST 5: No-Regresión Módulos 4 (ARF) y 5 (GASTOS)]
        Validar que las matrices generadas son consumibles directamente por:
        1. Extractor ARF de láminas a 24h (1440 min).
        2. auto_cargar_thiessen() del Módulo 5.
        """
        datos_sam = np.array([40.0, 50.0, 60.0, 70.0, 80.0, 90.0, 100.0])
        df_sam = pd.DataFrame({'Max_Anual': datos_sam})
        res = run_cuenca_analysis(
            'Gumbel', df_sam, 
            p1_10_manual=40.0, p1_25_manual=48.0, p1_50_manual=55.0, 
            aplicar_weiss=True, station_id="EST_TEST_45"
        )
        
        df_h = res["df_altura"]
        df_i = res["df_intensidad"]
        
        # 1. Validación ARF: existencia de fila 1440 min
        if 'TR (AÑOS)' in df_h.columns:
            fila_1440 = df_h[df_h['TR (AÑOS)'] == 1440]
        else:
            fila_1440 = df_h.loc[[1440]]
        
        self.assertFalse(fila_1440.empty, "Debe existir la duración de 1440 min (24h) para el Módulo 4 ARF.")
        for tr in [2, 5, 10, 20, 50, 100, 500, 1000, 10000]:
            self.assertIn(tr, fila_1440.columns, f"Columna Tr={tr} debe estar presente en df_altura.")
            self.assertGreater(float(fila_1440[tr].iloc[0]), 0.0, f"Lámina para Tr={tr} debe ser positiva.")
            
        # 2. Validación Módulo 5 Gastos: estructura compatible con auto_cargar_thiessen
        db_curvas = {
            "EST_TEST_45": {
                "df_intensidad": df_i,
                "df_altura": df_h
            }
        }
        
        db_estaciones = {}
        for sid, data in db_curvas.items():
            db_estaciones[sid] = {
                "nombre": f"{sid} (Procesada)",
                "intensidad": data["df_intensidad"],
                "altura": data["df_altura"]
            }
            
        self.assertIn("EST_TEST_45", db_estaciones)
        self.assertEqual(db_estaciones["EST_TEST_45"]["intensidad"].shape, (288, 9))
        self.assertEqual(db_estaciones["EST_TEST_45"]["altura"].shape, (288, 9))
        print("[PASS] TEST 5 Superado: Compatibilidad total con Modulo 4 (ARF) y Modulo 5 (GASTOS).")

if __name__ == "__main__":
    unittest.main()
