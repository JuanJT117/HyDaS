# tests/test_proyecciones_y_excepciones.py
"""
Batería de Pruebas Automatizadas para el Módulo de Proyecciones UTM de México
y la Taxonomía de Excepciones Hidrológicas (HyDaS v11.2).
"""

import os
import sys
import unittest
import warnings
import geopandas as gpd
from shapely.geometry import Polygon, Point

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from core.proyecciones_mx import (
    ZONAS_UTM_MEXICO,
    calcular_zona_utm_desde_longitud,
    obtener_info_zona_utm,
    detectar_zona_utm_geometria,
    estandarizar_a_wgs84_utm
)
from core.exceptions import (
    HyDaSBaseException,
    ClimaDataIncompleteError,
    NonMonotonicIDRError,
    SpatialCRSMismatchWarning,
    WinFileLockTimeoutError
)

class TestProyeccionesYExcepciones(unittest.TestCase):

    def test_calculo_zonas_utm_longitudes_mexico(self):
        """Verifica la asignación correcta de las 6 zonas UTM de México (11N a 16N)."""
        # Baja California (Ensenada / Tijuana) -> Zona 11N
        self.assertEqual(calcular_zona_utm_desde_longitud(-116.5), 11)
        # Sonora / BCS (Hermosillo / La Paz) -> Zona 12N
        self.assertEqual(calcular_zona_utm_desde_longitud(-110.9), 12)
        # Chihuahua / Durango / Sinaloa Sur -> Zona 13N
        self.assertEqual(calcular_zona_utm_desde_longitud(-106.1), 13)
        # CDMX / Monterrey / Guadalajara Centro -> Zona 14N
        self.assertEqual(calcular_zona_utm_desde_longitud(-99.13), 14)
        # Veracruz / Chiapas / Tabasco -> Zona 15N
        self.assertEqual(calcular_zona_utm_desde_longitud(-93.1), 15)
        # Península de Yucatán (Mérida / Cancún) -> Zona 16N
        self.assertEqual(calcular_zona_utm_desde_longitud(-86.8), 16)

    def test_catalogo_epsg_wgs84_utm(self):
        """Valida que los códigos EPSG correspondan al estándar oficial EPSG:32611 a 32616."""
        self.assertEqual(ZONAS_UTM_MEXICO[11]["epsg"], 32611)
        self.assertEqual(ZONAS_UTM_MEXICO[12]["epsg"], 32612)
        self.assertEqual(ZONAS_UTM_MEXICO[13]["epsg"], 32613)
        self.assertEqual(ZONAS_UTM_MEXICO[14]["epsg"], 32614)
        self.assertEqual(ZONAS_UTM_MEXICO[15]["epsg"], 32615)
        self.assertEqual(ZONAS_UTM_MEXICO[16]["epsg"], 32616)

    def test_estandarizacion_automatica_geometria_yucatan(self):
        """Prueba que un polígono en Cancún (Zona 16N) sea detectado y reproyectado automáticamente a EPSG:32616."""
        # Polígono sintético en grados decimales (Cancún / Quintana Roo)
        poly_cancun = Polygon([
            (-86.90, 21.15),
            (-86.80, 21.15),
            (-86.80, 21.20),
            (-86.90, 21.20),
            (-86.90, 21.15)
        ])
        gdf_cancun = gpd.GeoDataFrame({"id": [1], "nombre": ["Cuenca_Cancun"]}, geometry=[poly_cancun], crs="EPSG:4326")

        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            gdf_utm, meta = estandarizar_a_wgs84_utm(gdf_cancun, nombre_capa="cuenca_cancun")
            
            # Verificar reproyección exitosa a Zona 16N
            self.assertEqual(meta["epsg"], 32616)
            self.assertEqual(meta["zona_utm"], 16)
            self.assertEqual(gdf_utm.crs.to_epsg(), 32616)
            self.assertTrue(meta["reproyectado"])
            # Verificar que el área en km² calculada sobre la proyección planar sea positiva
            area_km2 = gdf_utm.geometry.area.sum() / 1e6
            self.assertGreater(area_km2, 10.0) # ~55 km²
            # Verificar emisión de advertencia de normalización
            self.assertTrue(any(issubclass(item.category, SpatialCRSMismatchWarning) for item in w))

    def test_estandarizacion_zona_forzada(self):
        """Verifica que el usuario pueda forzar una zona específica si el estudio lo requiere."""
        poly_centro = Polygon([
            (-99.20, 19.40),
            (-99.10, 19.40),
            (-99.10, 19.50),
            (-99.20, 19.50),
            (-99.20, 19.40)
        ])
        gdf = gpd.GeoDataFrame({"id": [1]}, geometry=[poly_centro], crs="EPSG:4326")
        
        # Forzar zona 13N aunque esté en 14N
        gdf_utm, meta = estandarizar_a_wgs84_utm(gdf, zona_forzada=13)
        self.assertEqual(meta["epsg"], 32613)
        self.assertEqual(meta["zona_utm"], 13)
        self.assertEqual(gdf_utm.crs.to_epsg(), 32613)

    def test_taxonomia_excepciones_dominio(self):
        """Verifica el formato estructurado de las excepciones hidrológicas."""
        err_clima = ClimaDataIncompleteError(anios_disponibles=8, anios_minimos=20, estacion_id="CNA-19045")
        self.assertIn("ERR_CLIMA_DATA_SHORT", str(err_clima))
        self.assertIn("CNA-19045", str(err_clima))
        self.assertTrue(len(err_clima.sugerencia) > 0)

        err_lock = WinFileLockTimeoutError("proyecto.hds", timeout_ms=5000)
        self.assertIn("ERR_WIN_FILE_LOCK", str(err_lock))

if __name__ == '__main__':
    unittest.main()
