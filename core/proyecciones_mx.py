# core/proyecciones_mx.py
"""
Módulo de Normalización y Gestión de Zonas UTM de México (HyDaS v11.2).
Estandariza todas las capas vectoriales y cálculos de cuenca al datum oficial
WGS 84 / UTM en las zonas 11N, 12N, 13N, 14N, 15N y 16N para eliminar distorsiones
de longitud, área o posición geográfica.
"""

import warnings
import geopandas as gpd
import pandas as pd
from shapely.geometry import Point
from typing import Optional, Tuple, Dict, Any

from core.exceptions import SpatialCRSMismatchWarning, InvalidGeometryError

# Catálogo canónico de Zonas UTM de la República Mexicana (WGS 84 Hemisferio Norte)
ZONAS_UTM_MEXICO = {
    11: {
        "epsg": 32611,
        "nombre": "WGS 84 / UTM Zona 11N",
        "lon_min": -120.0,
        "lon_max": -114.0,
        "cobertura": "Baja California (Tijuana, Mexicali, Ensenada)"
    },
    12: {
        "epsg": 32612,
        "nombre": "WGS 84 / UTM Zona 12N",
        "lon_min": -114.0,
        "lon_max": -108.0,
        "cobertura": "Sonora, Baja California Sur, Norte de Sinaloa"
    },
    13: {
        "epsg": 32613,
        "nombre": "WGS 84 / UTM Zona 13N",
        "lon_min": -108.0,
        "lon_max": -102.0,
        "cobertura": "Chihuahua, Durango, Sinaloa Sur, Nayarit, Poniente de Jalisco"
    },
    14: {
        "epsg": 32614,
        "nombre": "WGS 84 / UTM Zona 14N",
        "lon_min": -102.0,
        "lon_max": -96.0,
        "cobertura": "Centro y Noreste (Coahuila, NL, SLP, Zacatecas, Bajío, CDMX, EdoMex, Puebla, Oaxaca Pte)"
    },
    15: {
        "epsg": 32615,
        "nombre": "WGS 84 / UTM Zona 15N",
        "lon_min": -96.0,
        "lon_max": -90.0,
        "cobertura": "Golfo y Sureste (Veracruz, Tabasco, Chiapas, Oaxaca Ote, Campeche Poniente)"
    },
    16: {
        "epsg": 32616,
        "nombre": "WGS 84 / UTM Zona 16N",
        "lon_min": -90.0,
        "lon_max": -84.0,
        "cobertura": "Península de Yucatán (Campeche Oriente, Yucatán, Quintana Roo)"
    }
}

EPSG_A_ZONA = {info["epsg"]: zona for zona, info in ZONAS_UTM_MEXICO.items()}

def calcular_zona_utm_desde_longitud(longitud: float) -> int:
    """
    Calcula analíticamente la zona UTM (11 a 16) correspondiente a una longitud en grados decimales.
    Fórmula estándar: int(floor((lon + 180) / 6)) + 1 acotada a los meridianos de México.
    """
    zona_calculada = int((longitud + 180.0) // 6.0) + 1
    # Clamping al territorio continental y marítimo de México (Zonas 11 a 16)
    if zona_calculada < 11:
        return 11
    if zona_calculada > 16:
        return 16
    return zona_calculada

def obtener_info_zona_utm(zona: int) -> Dict[str, Any]:
    """Retorna los metadatos de la zona UTM indicada (11 a 16)."""
    if zona not in ZONAS_UTM_MEXICO:
        raise ValueError(f"Zona UTM {zona} fuera de los límites de México (válidas: 11 a 16).")
    return ZONAS_UTM_MEXICO[zona]

def detectar_zona_utm_geometria(gdf: gpd.GeoDataFrame) -> Dict[str, Any]:
    """
    Determina la zona UTM óptima para un GeoDataFrame calculando el centroide
    de sus geometrías en coordenadas geográficas WGS84 (EPSG:4326).
    """
    if gdf is None or gdf.empty:
        # Por defecto Zona 14N (Centro de México)
        return ZONAS_UTM_MEXICO[14]

    # Trabajar con copia ligera para no alterar el original
    gdf_temp = gdf.copy()
    
    # Sanar geometrías inválidas antes del centroide
    gdf_temp.geometry = gdf_temp.geometry.apply(
        lambda geom: geom.buffer(0) if geom is not None and not geom.is_valid else geom
    )

    # Si no tiene CRS o ya está en geográficas
    if gdf_temp.crs is None:
        # Heurística: si las coordenadas están en rango [-120, -80] es geográfico
        bounds = gdf_temp.total_bounds # (minx, miny, maxx, maxy)
        if -125.0 <= bounds[0] <= -80.0:
            gdf_temp.crs = "EPSG:4326"
        else:
            # Asumir UTM 14N por defecto
            return ZONAS_UTM_MEXICO[14]

    # Convertir a WGS 84 geográfico para calcular longitud real en grados
    if gdf_temp.crs.to_epsg() != 4326:
        gdf_wgs = gdf_temp.to_crs(epsg=4326)
    else:
        gdf_wgs = gdf_temp

    centroide_global = gdf_wgs.unary_union.centroid
    lon_centro = float(centroide_global.x)
    lat_centro = float(centroide_global.y)
    
    zona_optima = calcular_zona_utm_desde_longitud(lon_centro)
    info = ZONAS_UTM_MEXICO[zona_optima].copy()
    info["centroide_lon"] = lon_centro
    info["centroide_lat"] = lat_centro
    return info

def estandarizar_a_wgs84_utm(
    gdf: gpd.GeoDataFrame,
    zona_forzada: Optional[int] = None,
    nombre_capa: str = "capa_vectorial"
) -> Tuple[gpd.GeoDataFrame, Dict[str, Any]]:
    """
    Estandariza obligatoriamente cualquier capa GeoDataFrame a WGS 84 / UTM (Zonas 11N a 16N).
    - Corrige geometrías corruptas con buffer(0).
    - Identifica el CRS original.
    - Reproyecta a la zona UTM detectada automáticamente (o a la forzada por el usuario).
    - Emite una advertencia formal `SpatialCRSMismatchWarning` si hubo transformación.
    """
    if gdf is None or gdf.empty:
        raise ValueError("El GeoDataFrame proporcionado está vacío o es nulo.")

    gdf_out = gdf.copy()
    
    # 1. Sanación topológica vectorizada
    gdf_out.geometry = gdf_out.geometry.apply(
        lambda geom: geom.buffer(0) if geom is not None and not geom.is_valid else geom
    )

    # 2. Determinar zona destino
    crs_original_str = str(gdf.crs) if gdf.crs else "DESCONOCIDO"
    
    if zona_forzada is not None:
        if zona_forzada not in ZONAS_UTM_MEXICO:
            raise ValueError(f"Zona forzada {zona_forzada} inválida para México (11 a 16).")
        info_zona = ZONAS_UTM_MEXICO[zona_forzada]
    else:
        info_zona = detectar_zona_utm_geometria(gdf_out)

    epsg_destino = info_zona["epsg"]
    
    # 3. Reproyección
    reproyectado = False
    if gdf_out.crs is None:
        # Asignar WGS84 si está en grados o asumir el destino
        bounds = gdf_out.total_bounds
        if -125.0 <= bounds[0] <= -80.0:
            gdf_out.set_crs(epsg=4326, inplace=True)
            gdf_out = gdf_out.to_crs(epsg=epsg_destino)
            reproyectado = True
        else:
            gdf_out.set_crs(epsg=epsg_destino, inplace=True)
    else:
        epsg_actual = gdf_out.crs.to_epsg()
        if epsg_actual != epsg_destino:
            gdf_out = gdf_out.to_crs(epsg=epsg_destino)
            reproyectado = True
            warnings.warn(
                SpatialCRSMismatchWarning(
                    crs_origen=crs_original_str,
                    crs_destino=info_zona["nombre"],
                    capa=nombre_capa
                )
            )

    meta = {
        "zona_utm": info_zona if isinstance(info_zona, int) else [k for k, v in ZONAS_UTM_MEXICO.items() if v["epsg"] == epsg_destino][0],
        "epsg": epsg_destino,
        "nombre_crs": info_zona["nombre"],
        "crs_original": crs_original_str,
        "reproyectado": reproyectado
    }
    
    return gdf_out, meta
