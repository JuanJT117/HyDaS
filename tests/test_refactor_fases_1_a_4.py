# tests/test_refactor_fases_1_a_4.py
import os
import sys

try:
    import pyproj
    _proj_dir = pyproj.datadir.get_data_dir()
    if _proj_dir and os.path.exists(_proj_dir):
        os.environ["PROJ_LIB"] = _proj_dir
        os.environ["PROJ_DATA"] = _proj_dir
except Exception:
    _conda_proj = os.path.join(sys.prefix, "Library", "share", "proj")
    if os.path.exists(_conda_proj):
        os.environ["PROJ_LIB"] = _conda_proj
        os.environ["PROJ_DATA"] = _conda_proj

import unittest
import tempfile
import numpy as np
import pandas as pd
import geopandas as gpd
from shapely.geometry import Polygon, Point

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from infrastructure.project_manager import project_manager_instance, GeoProjectManager
from core.imputacion_logic import (
    resolver_ruta_hds,
    guardar_en_sqlite_hds,
    guardar_log_en_sqlite_hds,
    obtener_log_de_sqlite_hds,
    eliminar_estacion_de_sqlite_hds
)
from core.gastos_logic import calcular_lluvias_escenario
from core.topologia_acf_logic import AnalisisTopologicoCuenca
from core.climatologia_logic import exportar_reporte_climatico_latex, calcular_normales_climatologicas

class TestRefactorFases1a4(unittest.TestCase):
    """
    Batería de validación formal para las Fases 1, 2, 3 y 4 del
    Plan Director de Refactorización y Actualización Arquitectónica.
    """

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="hydas_refactor_test_")

    def tearDown(self):
        project_manager_instance.close_project()
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    # ---------------------------------------------------------
    # FASE 1: SERIALIZACIÓN RECURSIVA & CAPAS GPKG & .BAK BACKUP
    # ---------------------------------------------------------
    def test_fase1_serializacion_recursiva_dataframes_anidados(self):
        """Verifica que DataFrames anidados a cualquier profundidad no se conviertan en <NonSerializable: DataFrame>."""
        hds_path = os.path.join(self.temp_dir, "test_nested_session.hds")
        
        # Simular estructura anidada compleja (escenarios de gastos y fases de ARF)
        df_sub1 = pd.DataFrame({"Tr_10": [50.5, 60.2], "Tr_25": [75.1, 88.9]}, index=[10.0, 20.0])
        df_sub2 = pd.DataFrame({"Precip": [12.0, 34.5, 56.1]}, index=pd.date_range("2020-01-01", periods=3))
        series_test = pd.Series([1.5, 2.5, 3.5], name="factor_k")

        nested_session = {
            "nombre_proyecto": "Proyecto Anidado Test",
            "db_escenarios": {
                "Escenario_Base": {
                    "df_intensidad": df_sub1,
                    "metadata": {"version": 1102, "activo": True},
                    "series_aux": series_test
                },
                "Escenario_Alterno": {
                    "df_altura": df_sub2,
                    "coeficientes": np.array([0.15, 0.25, 0.35], dtype=np.float64)
                }
            },
            "numpy_int": np.int64(42),
            "numpy_float": np.float64(3.14159)
        }

        # Guardar proyecto
        exito_save = project_manager_instance.save_project(hds_path, nested_session)
        self.assertTrue(exito_save, "El guardado recursivo falló.")

        # Cargar proyecto
        recovered = project_manager_instance.load_project(hds_path)
        self.assertIn("db_escenarios", recovered)
        
        rec_esc = recovered["db_escenarios"]
        rec_df1 = rec_esc["Escenario_Base"]["df_intensidad"]
        
        # Comprobar que NO es un string <NonSerializable: DataFrame>
        self.assertIsInstance(rec_df1, pd.DataFrame, "El DataFrame anidado se corrompió o convirtió en string.")
        self.assertEqual(list(rec_df1.columns), ["Tr_10", "Tr_25"])
        self.assertAlmostEqual(rec_df1.loc[10.0, "Tr_10"], 50.5, places=3)

        rec_series = rec_esc["Escenario_Base"]["series_aux"]
        self.assertTrue(isinstance(rec_series, (pd.Series, pd.DataFrame)))

        rec_df2 = rec_esc["Escenario_Alterno"]["df_altura"]
        self.assertIsInstance(rec_df2, pd.DataFrame)
        self.assertEqual(len(rec_df2), 3)

        # Comprobar que se generó el respaldo .bak en el segundo guardado
        project_manager_instance.save_project(hds_path, nested_session)
        bak_path = hds_path + ".bak"
        self.assertTrue(os.path.exists(bak_path), "No se generó el archivo de respaldo .bak")

    def test_fase1_capas_espaciales_geopackage(self):
        """Verifica el almacenamiento, listado y recarga de capas OGC en el GeoPackage (.hds)."""
        hds_path = os.path.join(self.temp_dir, "test_spatial.hds")
        project_manager_instance.inicializar_proyecto(hds_path, "Proyecto SIG Test")

        # Crear GeoDataFrame de prueba (polígono de cuenca)
        poly = Polygon([(0, 0), (10, 0), (10, 10), (0, 10), (0, 0)])
        gdf = gpd.GeoDataFrame({"id_cuenca": [1], "nombre": ["Cuenca Norte"]}, geometry=[poly], crs="EPSG:32614")

        # Guardar capa
        project_manager_instance.guardar_capa_espacial("shp_cuenca_limite", gdf)

        # Listar capas
        capas = project_manager_instance.listar_capas_espaciales()
        self.assertIn("shp_cuenca_limite", capas)

        # Cargar capa
        gdf_loaded = project_manager_instance.cargar_capa_espacial("shp_cuenca_limite")
        self.assertIsNotNone(gdf_loaded)
        self.assertEqual(len(gdf_loaded), 1)
        self.assertEqual(gdf_loaded.iloc[0]["nombre"], "Cuenca Norte")
        self.assertEqual(gdf_loaded.geometry.iloc[0].area, 100.0)

    # ---------------------------------------------------------
    # FASE 2: SINGLE SOURCE OF TRUTH (PERSISTENCIA DIRECTA EN .HDS)
    # ---------------------------------------------------------
    def test_fase2_persistencia_unificada_sqlite_imputacion(self):
        """Verifica que las tablas de series imputadas y logs QA se escriban en el .hds sin colisiones ni sufijos huérfanos."""
        hds_path = os.path.join(self.temp_dir, "proyecto_unificado.hds")
        project_manager_instance.inicializar_proyecto(hds_path, "Proyecto Unificado")

        df_imput = pd.DataFrame({
            "FECHA": pd.date_range("2021-01-01", periods=5),
            "PRECIP": [0.0, 15.2, 32.1, 0.0, 5.4],
            "FLAG": [0, 1, 0, 0, 1]
        })

        # Guardar serie imputada y log QA
        ok_serie = guardar_en_sqlite_hds(df_imput, "19050", hds_path)
        self.assertTrue(ok_serie, "Error al guardar serie imputada en SQLite.")

        ok_log = guardar_log_en_sqlite_hds("LOG QA: 5 registros procesados con éxito.", "19050", hds_path)
        self.assertTrue(ok_log, "Error al guardar log QA en SQLite.")

        # Recuperar log QA
        log_recuperado = obtener_log_de_sqlite_hds("19050", hds_path)
        self.assertIn("5 registros procesados", log_recuperado)

        # Eliminar estación y verificar
        ok_del = eliminar_estacion_de_sqlite_hds("19050", hds_path)
        self.assertTrue(ok_del)
        self.assertEqual(obtener_log_de_sqlite_hds("19050", hds_path), "")

    # ---------------------------------------------------------
    # FASE 3: AUDITORÍA Y DETECCIÓN DE CRASH
    # ---------------------------------------------------------
    def test_fase3_auditoria_y_crash_recovery(self):
        """Verifica el registro de trazas en hidro_audit_log y el flag de sesión activa."""
        hds_path = os.path.join(self.temp_dir, "audit_test.hds")
        project_manager_instance.inicializar_proyecto(hds_path, "Proyecto Auditoría")

        # Registrar eventos
        project_manager_instance.registrar_auditoria("MODULO_IMPUTACION", "IMPUTAR_ESTACION", "Estación 19050 completada")
        project_manager_instance.registrar_auditoria("MODULO_GASTOS", "CALCULAR_GASTO", "Método Racional ejecutado")

        logs = project_manager_instance.obtener_auditoria()
        self.assertGreaterEqual(len(logs), 2)
        acciones = [l["accion"] for l in logs]
        self.assertIn("IMPUTAR_ESTACION", acciones)
        self.assertIn("CALCULAR_GASTO", acciones)

        # Probar flag de sesión activa
        project_manager_instance.set_session_activa(True)
        self.assertTrue(project_manager_instance.verificar_recuperacion_sesion())

        project_manager_instance.set_session_activa(False)
        self.assertFalse(project_manager_instance.verificar_recuperacion_sesion())

    # ---------------------------------------------------------
    # FASE 4: DESACOPLAMIENTO, CONTRATOS Y ESTABILIDAD
    # ---------------------------------------------------------
    def test_fase4_desacoplamiento_gastos_puro_sin_flet(self):
        """Verifica que calcular_lluvias_escenario opere con session_dict puro sin dependencias de Flet."""
        df_i_mock = pd.DataFrame({"10": [25.0, 15.0]}, index=[60.0, 120.0])
        df_h_mock = pd.DataFrame({"10": [25.0, 30.0]}, index=[60.0, 120.0])

        session_mock = {
            "modo_lluvia_activo": "regional_arf",
            "arf_df_intensidad_maestra": df_i_mock,
            "arf_df_altura_maestra": df_h_mock,
            "arf_df_hietograma_maestro": None
        }

        exito, msj = calcular_lluvias_escenario(session_dict=session_mock)
        self.assertTrue(exito)
        self.assertIn("df_intensidad", session_mock)
        self.assertIn("df_altura", session_mock)
        self.assertEqual(session_mock["df_intensidad"].loc[60.0, "10"], 25.0)

    def test_fase4_topologia_acf_logic_morfometria(self):
        """Verifica que core/topologia_acf_logic calcule métricas morfométricas válidas."""
        poly = Polygon([(0, 0), (2000, 0), (2000, 2000), (0, 2000), (0, 0)]) # 4 km²
        gdf_cuenca = gpd.GeoDataFrame({"id": [1]}, geometry=[poly], crs="EPSG:32614")

        self.assertTrue(AnalisisTopologicoCuenca.validar_geometrias_cuenca(gdf_cuenca))
        stats = AnalisisTopologicoCuenca.calcular_estadisticas_morfometricas(gdf_cuenca)
        self.assertAlmostEqual(stats["area_km2"], 4.0, places=2)
        self.assertAlmostEqual(stats["perimetro_km"], 8.0, places=2)
        self.assertGreater(stats["factor_compacidad_gravelius"], 0.0)

if __name__ == "__main__":
    unittest.main()
