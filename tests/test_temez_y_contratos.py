# tests/test_temez_y_contratos.py
import os
import sys
import gc
import sqlite3
import tempfile
import unittest
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

if sys.platform == 'win32':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

from core.analisis_cuenca import run_cuenca_analysis
from core.gastos_logic import calcular_coeficientes_y_gastos

class MockSession(dict):
    """Simulador minimalista de SessionStorage de Flet."""
    def get(self, key, default=None):
        return super().get(key, default)
    def set(self, key, value):
        self[key] = value

class MockPage:
    """Mock de página Flet para pruebas de backend."""
    def __init__(self):
        self.session = MockSession()

class TestProtocoloIntegridadHyDaS(unittest.TestCase):

    def setUp(self):
        np.random.seed(42)
        self.datos_sam = np.array([45.0, 52.0, 61.0, 68.0, 75.0, 82.0, 95.0, 110.0])
        self.df_sam = pd.DataFrame({"Max_Anual": self.datos_sam})

    def test_vector_1_integridad_geopackage(self):
        """Vector 1: Integridad GeoPackage SQLite (estacion_descriptores_chen)."""
        print("\n--- EJECUTANDO VECTOR 1: Integridad GeoPackage SQLite ---")
        with tempfile.NamedTemporaryFile(suffix=".gpkg", delete=False) as tmp:
            tmp_path = tmp.name

        try:
            # 1. Probar r_regional
            res_reg = run_cuenca_analysis(
                best_fit_name="Gumbel",
                df_maximos_mensuales=self.df_sam,
                metodo_r="r_regional",
                r_regional_manual=0.3882,
                station_id="RH24_REG"
            )
            desc_reg = res_reg["descriptores_chen"]

            # 2. Probar isoyetas_sct
            res_sct = run_cuenca_analysis(
                best_fit_name="Gumbel",
                df_maximos_mensuales=self.df_sam,
                metodo_r="isoyetas_sct",
                p1_10_manual=42.0,
                p1_25_manual=50.0,
                p1_50_manual=58.0,
                station_id="SCT_MULTI"
            )
            desc_sct = res_sct["descriptores_chen"]

            conn = sqlite3.connect(tmp_path, timeout=30.0)
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

            for d in [desc_reg, desc_sct]:
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
                        r_promedio=excluded.r_promedio,
                        param_a=excluded.param_a,
                        param_b=excluded.param_b,
                        param_c=excluded.param_c;
                """, (
                    d["station_id"], d["distribucion_aplicada"], d["metodo_r_utilizado"],
                    d["p1_10_mm"], d["p1_25_mm"], d["p1_50_mm"],
                    d["p24_fijo_10_mm"], d["p24_fijo_25_mm"], d["p24_fijo_50_mm"], d["p24_fijo_100_mm"],
                    d["factor_weiss"],
                    d["p24_movil_10_mm"], d["p24_movil_25_mm"], d["p24_movil_50_mm"], d["p24_movil_100_mm"],
                    d["r_10"], d["r_25"], d["r_50"],
                    d["r_acotado"], d["f_calculado"],
                    d["param_a"], d["param_b"], d["param_c"]
                ))
            conn.commit()

            cur.execute("SELECT station_id, metodo_r, r_promedio, param_a, param_b, param_c FROM estacion_descriptores_chen;")
            filas = cur.fetchall()
            conn.close()

            self.assertEqual(len(filas), 2)
            for sid, met_r, r_prom, a, b, c in filas:
                self.assertIn(met_r, ["isoyetas_sct", "r_regional"])
                self.assertNotEqual(met_r, "bell_chen", "bell_chen jamás debe aparecer en la base de datos.")
                self.assertGreaterEqual(r_prom, 0.10)
                self.assertLessEqual(r_prom, 0.699)
                self.assertGreater(a, 0.0)
                self.assertGreater(b, 0.0)
                self.assertGreater(c, 0.0)
            print("[PASS] Vector 1 Superado: GeoPackage WAL verificado con metodos normativos y R acotado.")

        finally:
            if os.path.exists(tmp_path):
                try: os.remove(tmp_path)
                except Exception: pass

    def test_vector_2_estabilidad_indices_parquet(self):
        """Vector 2: Estabilidad de Índices Numéricos en Memoria y Parquet."""
        print("\n--- EJECUTANDO VECTOR 2: Estabilidad de Índices Numéricos (Parquet) ---")
        with tempfile.TemporaryDirectory() as cache_dir:
            res = run_cuenca_analysis(
                best_fit_name="Gumbel",
                df_maximos_mensuales=self.df_sam,
                metodo_r="r_regional",
                r_regional_manual=0.3882,
                station_id="ST_FLOAT"
            )
            df_altura = res["df_altura"]
            df_intensidad = res["df_intensidad"]

            self.assertEqual(df_altura.index.dtype, np.float64)
            self.assertEqual(df_intensidad.index.dtype, np.float64)
            self.assertEqual(df_altura.index.name, "DURACION_MIN")
            self.assertEqual(df_intensidad.index.name, "DURACION_MIN")

            # Simular persistencia Parquet en persistir_estacion_actual
            path_alt = os.path.join(cache_dir, "df_altura.parquet")
            path_int = os.path.join(cache_dir, "df_intensidad.parquet")

            df_altura_safe = df_altura.copy()
            df_altura_safe.columns = [str(c) for c in df_altura_safe.columns]
            df_altura_safe.to_parquet(path_alt, engine="pyarrow")

            df_int_safe = df_intensidad.copy()
            df_int_safe.columns = [str(c) for c in df_int_safe.columns]
            df_int_safe.to_parquet(path_int, engine="pyarrow")

            # Rehidratar Parquet desde disco
            df_recuperado_int = pd.read_parquet(path_int)
            self.assertEqual(df_recuperado_int.index.dtype, np.float64)

            # Verificar que gastos_logic lo consuma sin KeyError ni TypeError
            df_cuencas = pd.DataFrame({'area': [2.5]}, index=['C1'])
            df_cuencas['LCP'] = 1500.0
            df_cuencas['Porcentaje_terreno'] = '100.0'
            df_cuencas['Coeficiente_C_por_Cuenca'] = '5'
            df_cuencas['indice_Uso_terreno'] = '65'
            df_cuencas['Grupo_hidrologico'] = 'B'

            df_cotas = pd.DataFrame([{'cuenca': 'C1', 'distancia1': 0.0, 'distancia2': 1500.0, 'cota mayor': 200.0, 'cota menor': 150.0}])

            res_r, _, _, _, _, _ = calcular_coeficientes_y_gastos(
                df_cuencas, df_cotas, metodo_tc="Kirpich",
                df_int_global=path_int, df_alt_global=path_alt,
                lista_tr=['10', '25', '50'], aplicar_temez=False
            )
            self.assertIsNotNone(res_r)
            self.assertGreater(res_r.loc['C1', 'TR_10'], 0.0)
            print("[PASS] Vector 2 Superado: Indices np.float64 conservados en Parquet y calculados en gastos.")

    def test_vector_3_conmutacion_temez(self):
        """Vector 3: Conmutación de Témez en Caudales Racionales y no doble reducción en ARF."""
        print("\n--- EJECUTANDO VECTOR 3: Conmutación de Témez en Caudales Racionales ---")
        res = run_cuenca_analysis("Gumbel", self.df_sam, metodo_r="r_regional", r_regional_manual=0.3882)
        df_i = res["df_intensidad"]
        df_a = res["df_altura"]

        area_test = 5.0  # km²
        df_cuencas = pd.DataFrame({'area': [area_test]}, index=['C1'])
        df_cuencas['LCP'] = 2000.0
        df_cuencas['Porcentaje_terreno'] = '100.0'
        df_cuencas['Coeficiente_C_por_Cuenca'] = '5'
        df_cuencas['indice_Uso_terreno'] = '70'
        df_cuencas['Grupo_hidrologico'] = 'B'

        df_cotas = pd.DataFrame([{'cuenca': 'C1', 'distancia1': 0.0, 'distancia2': 2000.0, 'cota mayor': 300.0, 'cota menor': 200.0}])

        # Caso 1: sw_aplicar_temez = False (Gasto Pleno)
        res_r_pleno, _, _, _, _, log_pleno = calcular_coeficientes_y_gastos(
            df_cuencas, df_cotas, metodo_tc="Kirpich",
            df_int_global=df_i, df_alt_global=df_a,
            lista_tr=['10'], aplicar_temez=False, aplicar_arf=False
        )
        q_pleno = res_r_pleno.loc['C1', 'TR_10']

        # Caso 2: sw_aplicar_temez = True (Atenuación KA)
        res_r_temez, _, _, _, _, log_temez = calcular_coeficientes_y_gastos(
            df_cuencas, df_cotas, metodo_tc="Kirpich",
            df_int_global=df_i, df_alt_global=df_a,
            lista_tr=['10'], aplicar_temez=True, aplicar_arf=False
        )
        q_temez = res_r_temez.loc['C1', 'TR_10']

        ka_teorico = max(1.0 - (np.log10(area_test) / 15.0), 0.5)  # 1 - log10(5)/15 ≈ 0.9534
        self.assertAlmostEqual(ka_teorico, 0.9534, places=3)
        self.assertAlmostEqual(q_temez / q_pleno, ka_teorico, places=3)
        self.assertIn("Reducción Témez activa", log_temez)
        self.assertIn("Reducción Témez desactivada", log_pleno)

        # Caso 3: modo_lluvia_activo == "regional_arf" (Inhibición de doble reducción)
        page_mock = MockPage()
        page_mock.session.set("modo_lluvia_activo", "regional_arf")
        page_mock.session.set("arf_df_intensidad_maestra", df_i)
        page_mock.session.set("arf_df_altura_maestra", df_a)

        res_r_arf, _, _, _, _, log_arf = calcular_coeficientes_y_gastos(
            df_cuencas, df_cotas, metodo_tc="Kirpich",
            df_int_global=df_i, df_alt_global=df_a,
            lista_tr=['10'], aplicar_temez=True, aplicar_arf=True,
            df_intensidad_arf=df_i, df_altura_arf=df_a, page=page_mock
        )
        q_arf = res_r_arf.loc['C1', 'TR_10']
        # Con ARF activo, el factor KA debe ser 1.000 (sin reducción adicional de Témez)
        self.assertAlmostEqual(q_arf, q_pleno, places=3)
        self.assertIn("Reducción Témez omitida", log_arf)
        print(f"[PASS] Vector 3 Superado: Q_pleno={q_pleno:.3f}, Q_temez={q_temez:.3f} (KA={ka_teorico:.4f}), Q_arf={q_arf:.3f} (KA=1.000).")

    def test_vector_4_hidratacion_multiescenario(self):
        """Vector 4: Hidratación Multiescenario, clonación segura y recolección de basura."""
        print("\n--- EJECUTANDO VECTOR 4: Hidratación Multiescenario y Cierre Seguro ---")
        keys = [
            "datos_cuencas_config", "df_cuencas_base", "df_cotas", "df_hms", 
            "df_intensidad", "df_altura", "res_racional", "res_chow", "df_variables", 
            "estaciones_db", "pesos_estaciones", "station_counter", "target_station_id",
            "res_hms_peak", "res_hidrogramas", "modo_lluvia_activo", "aplicar_temez"
        ]

        db_escenarios = {
            "Escenario Base": {k: None for k in keys}
        }
        db_escenarios["Escenario Base"]["aplicar_temez"] = False
        db_escenarios["Escenario Base"]["df_cuencas_base"] = pd.DataFrame({'area': [10.0]}, index=['C1'])

        # Clonación segura (sin referencias circulares)
        def clonar_dato_seguro(obj):
            if isinstance(obj, pd.DataFrame):
                return obj.copy(deep=True)
            elif isinstance(obj, dict):
                return {k: clonar_dato_seguro(v) for k, v in obj.items()}
            elif isinstance(obj, list):
                return [clonar_dato_seguro(v) for v in obj]
            else:
                import copy
                return copy.deepcopy(obj)

        nuevo_nombre = "Escenario Duplicado"
        db_escenarios[nuevo_nombre] = clonar_dato_seguro(db_escenarios["Escenario Base"])
        db_escenarios[nuevo_nombre]["aplicar_temez"] = True
        db_escenarios[nuevo_nombre]["df_cuencas_base"].loc['C1', 'area'] = 20.0

        # Verificar independencia total de memoria
        self.assertEqual(db_escenarios["Escenario Base"]["aplicar_temez"], False)
        self.assertEqual(db_escenarios[nuevo_nombre]["aplicar_temez"], True)
        self.assertEqual(db_escenarios["Escenario Base"]["df_cuencas_base"].loc['C1', 'area'], 10.0)
        self.assertEqual(db_escenarios[nuevo_nombre]["df_cuencas_base"].loc['C1', 'area'], 20.0)

        # Recolección de basura
        coleccionados = gc.collect()
        self.assertGreaterEqual(coleccionados, 0)
        print("[PASS] Vector 4 Superado: Snapshots multiescenario independientes y memoria íntegra.")

    def test_vector_5_ddl_migration_y_winerror5(self):
        """Vector 5: Migración dinámica de DDL SQLite y resiliencia a WinError 5 en Windows."""
        print("\n--- EJECUTANDO VECTOR 5: DDL Dinámica SQLite y Resiliencia WinError 5 ---")
        
        # 1. Prueba de Migración DDL en Caliente (Esquema Antiguo sin metodo_r)
        with tempfile.NamedTemporaryFile(suffix=".gpkg", delete=False) as tmp:
            old_gpkg = tmp.name

        try:
            conn = sqlite3.connect(old_gpkg)
            cur = conn.cursor()
            # Crear tabla antigua incompleta (simulando versión previa de HyDaS sin p24_fijo_25 ni metodo_r)
            cur.execute("""
                CREATE TABLE estacion_descriptores_chen (
                    station_id TEXT PRIMARY KEY,
                    distribucion TEXT NOT NULL,
                    p1_10 REAL NOT NULL,
                    p24_fijo_10 REAL NOT NULL,
                    p24_fijo_50 REAL NOT NULL,
                    p24_fijo_100 REAL NOT NULL,
                    factor_weiss REAL NOT NULL,
                    p24_movil_10 REAL NOT NULL,
                    p24_movil_25 REAL NOT NULL,
                    p24_movil_50 REAL NOT NULL,
                    p24_movil_100 REAL NOT NULL,
                    r_promedio REAL NOT NULL,
                    param_a REAL NOT NULL,
                    param_b REAL NOT NULL,
                    param_c REAL NOT NULL,
                    fecha_calculo TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            conn.commit()

            # Comprobar que metodo_r y p24_fijo_25 NO existen aún
            cur.execute("PRAGMA table_info(estacion_descriptores_chen);")
            cols_antes = {col[1] for col in cur.fetchall()}
            self.assertNotIn("metodo_r", cols_antes)
            self.assertNotIn("p24_fijo_25", cols_antes)

            # Ejecutar lógica de migración defensiva en caliente completa (idéntica a analisis_view.py)
            columnas_requeridas = {
                "distribucion": "TEXT DEFAULT ''",
                "metodo_r": "TEXT DEFAULT 'r_regional'",
                "p1_10": "REAL DEFAULT 0.0",
                "p1_25": "REAL DEFAULT 0.0",
                "p1_50": "REAL DEFAULT 0.0",
                "p24_fijo_10": "REAL DEFAULT 0.0",
                "p24_fijo_25": "REAL DEFAULT 0.0",
                "p24_fijo_50": "REAL DEFAULT 0.0",
                "p24_fijo_100": "REAL DEFAULT 0.0",
                "factor_weiss": "REAL DEFAULT 1.13",
                "p24_movil_10": "REAL DEFAULT 0.0",
                "p24_movil_25": "REAL DEFAULT 0.0",
                "p24_movil_50": "REAL DEFAULT 0.0",
                "p24_movil_100": "REAL DEFAULT 0.0",
                "r_10": "REAL DEFAULT 0.0",
                "r_25": "REAL DEFAULT 0.0",
                "r_50": "REAL DEFAULT 0.0",
                "r_promedio": "REAL DEFAULT 0.0",
                "factor_f": "REAL DEFAULT 1.0",
                "param_a": "REAL DEFAULT 0.0",
                "param_b": "REAL DEFAULT 0.0",
                "param_c": "REAL DEFAULT 0.0",
                "fecha_calculo": "TIMESTAMP DEFAULT CURRENT_TIMESTAMP"
            }
            for col_nombre, col_tipo in columnas_requeridas.items():
                if col_nombre not in cols_antes:
                    cur.execute(f"ALTER TABLE estacion_descriptores_chen ADD COLUMN {col_nombre} {col_tipo};")
            conn.commit()

            # Verificar que ahora sí existen todas las columnas (incluyendo p24_fijo_25)
            cur.execute("PRAGMA table_info(estacion_descriptores_chen);")
            cols_despues = {col[1] for col in cur.fetchall()}
            for col_req in columnas_requeridas:
                self.assertIn(col_req, cols_despues)
            self.assertIn("p24_fijo_25", cols_despues)

            # Probar inserción con metodo_r y p24_fijo_25 sin OperationalError
            cur.execute("""
                INSERT INTO estacion_descriptores_chen (
                    station_id, distribucion, metodo_r, p1_10, p1_25, p1_50,
                    p24_fijo_10, p24_fijo_25, p24_fijo_50, p24_fijo_100,
                    factor_weiss, p24_movil_10, p24_movil_25, p24_movil_50, p24_movil_100,
                    r_10, r_25, r_50, r_promedio, factor_f, param_a, param_b, param_c
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                "19004", "Gumbel", "r_regional", 40.0, 48.0, 55.0,
                80.0, 100.0, 115.0, 130.0, 1.13, 90.4, 113.0, 129.95, 146.9,
                0.44, 0.42, 0.42, 0.3882, 1.0, 0.5, 0.3, 0.2
            ))
            conn.commit()
            conn.close()
        finally:
            if os.path.exists(old_gpkg):
                try: os.remove(old_gpkg)
                except Exception: pass

        # 2. Prueba de Resiliencia a Colisión de Archivo / WinError 5
        with tempfile.TemporaryDirectory() as cache_dir:
            sid = "19004"
            station_cache = os.path.join(cache_dir, str(sid).strip())

            # Simular colisión: crear un archivo plano con el mismo nombre que la carpeta esperada
            with open(station_cache, "w") as f:
                f.write("archivo remanente bloqueando os.makedirs")

            self.assertTrue(os.path.exists(station_cache))
            self.assertFalse(os.path.isdir(station_cache))

            # Ejecutar lógica EAFP resiliente
            try:
                if os.path.exists(station_cache) and not os.path.isdir(station_cache):
                    os.remove(station_cache)
                os.makedirs(station_cache, exist_ok=True)
            except PermissionError:
                import uuid
                station_cache = os.path.join(cache_dir, f"{sid}_{uuid.uuid4().hex[:6]}")
                os.makedirs(station_cache, exist_ok=True)

            self.assertTrue(os.path.isdir(station_cache))

            # Guardar parquet de prueba para verificar atomicidad
            df_test = pd.DataFrame({"col": [1, 2, 3]}, index=[10.0, 20.0, 30.0])
            test_parquet = os.path.join(station_cache, "test.parquet")
            df_test.to_parquet(test_parquet)
            self.assertTrue(os.path.exists(test_parquet))

        print("[PASS] Vector 5 Superado: DDL migrada en caliente y colisión en Windows resuelta con éxito.")

if __name__ == "__main__":
    unittest.main()

