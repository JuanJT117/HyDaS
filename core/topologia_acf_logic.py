# core/topologia_acf_logic.py
"""
Módulo de Lógica Topológica y Análisis Espacial de Cuencas (HyDaS v11.2).
Proporciona utilidades para análisis de conectividad hidrológica,
autocorrelación espacial y morfometría de red hídrica.
"""

import numpy as np
import pandas as pd
import geopandas as gpd

class AnalisisTopologicoCuenca:
    """
    Herramientas de cálculo para topología de redes de drenaje y métricas de cuenca.
    """
    
    @staticmethod
    def validar_geometrias_cuenca(gdf: gpd.GeoDataFrame) -> bool:
        """Verifica que las geometrías de la cuenca sean válidas y cerradas."""
        if gdf is None or gdf.empty:
            return False
        return bool(gdf.geometry.is_valid.all())

    @staticmethod
    def calcular_estadisticas_morfometricas(gdf_cuenca: gpd.GeoDataFrame, gdf_cauces: gpd.GeoDataFrame = None) -> dict:
        """
        Calcula métricas morfométricas básicas: área, perímetro, factor de forma y densidad de drenaje.
        """
        if gdf_cuenca is None or gdf_cuenca.empty:
            return {}
            
        area_m2 = gdf_cuenca.geometry.area.sum()
        area_km2 = area_m2 / 1e6
        perimetro_m = gdf_cuenca.geometry.length.sum()
        perimetro_km = perimetro_m / 1000.0
        
        factor_forma = (4 * np.pi * area_m2) / (perimetro_m ** 2) if perimetro_m > 0 else 0.0
        
        longitud_cauces_km = 0.0
        if gdf_cauces is not None and not gdf_cauces.empty:
            longitud_cauces_km = gdf_cauces.geometry.length.sum() / 1000.0
            
        densidad_drenaje = (longitud_cauces_km / area_km2) if area_km2 > 0 else 0.0
        
        return {
            "area_km2": float(round(area_km2, 4)),
            "perimetro_km": float(round(perimetro_km, 4)),
            "factor_compacidad_gravelius": float(round(0.28 * perimetro_km / np.sqrt(area_km2), 4)) if area_km2 > 0 else 0.0,
            "coeficiente_forma": float(round(factor_forma, 4)),
            "densidad_drenaje_km_km2": float(round(densidad_drenaje, 4))
        }
