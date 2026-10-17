import json
import geopandas as gpd
import pandas as pd
import numpy as np
from shapely.geometry import Polygon, LineString, MultiPolygon
from shapely.ops import polygonize
from scipy.spatial import Voronoi
import fiona
import os
import traceback
import gc

# --- [PARCHE DEVSECOPS: FIJAR RUTAS PROJ PARA FIONA] ---
# Corrige "PROJ: Cannot find proj.db" en entornos Conda / PyInstaller
_fiona_proj = os.path.join(os.path.dirname(fiona.__file__), "proj_data")
if os.path.exists(_fiona_proj):
    os.environ["PROJ_LIB"] = _fiona_proj
    os.environ["PROJ_DATA"] = _fiona_proj
# --------------------------------------------------------

from core.proyecciones_mx import estandarizar_a_wgs84_utm, EPSG_A_ZONA, ZONAS_UTM_MEXICO

# Definición del CRS oficial del sistema (UTM Zona 14N por defecto, parametrizable)
DEFAULT_CRS = "EPSG:32614"

class MotorEspacial:
    
    @staticmethod
    def generar_plantilla(schema_class, tipo_geometria: str, crs=DEFAULT_CRS) -> gpd.GeoDataFrame:
        """Genera un GeoDataFrame vacío con el tipado estricto del esquema Pydantic."""
        def get_dtype(annotation):
            if annotation is int: return 'Int64'
            if annotation is float: return 'float64'
            if annotation is str: return 'object'
            return 'object'
            
        esquema = schema_class.model_fields
        columnas = {nombre: pd.Series(dtype=get_dtype(field.annotation)) 
                    for nombre, field in esquema.items()}
        
        df = pd.DataFrame(columnas)
        # Inyección segura de geometría vacía
        gdf = gpd.GeoDataFrame(df, geometry=pd.Series(dtype='geometry'), crs=crs)
        return gdf

    @staticmethod
    def ingerir_cuencas_zero_trust(ruta_shp: str, epsg_utm: int = None) -> dict:
        """
        [ADUANA ESPACIAL] Procesa un shapefile que puede contener una o múltiples subcuencas.
        Estandariza obligatoriamente a WGS 84 / UTM en las zonas 11N a 16N de México.
        Retorna la geometría agrupada (Convex Hull), metadatos de proyección y suma de áreas.
        """
        try:
            # 1. Lectura y Validación (Zero-Trust)
            gdf = gpd.read_file(ruta_shp)
            if gdf.empty:
                raise ValueError("El archivo Shapefile proporcionado está vacío o corrupto.")

            # 2. Normalización Vectorizada y Reproyección a WGS84 UTM México (11N a 16N)
            zona_forzada = EPSG_A_ZONA.get(epsg_utm) if epsg_utm else None
            gdf, meta_crs = estandarizar_a_wgs84_utm(
                gdf, zona_forzada=zona_forzada, nombre_capa=os.path.basename(ruta_shp)
            )

            # 3. Cálculo Vectorizado de Áreas Individuales y Totales
            gdf['area_km2'] = gdf.geometry.area / 1e6
            area_total = float(gdf['area_km2'].sum())
            num_subcuencas = len(gdf)

            # 4. Geometría Envolvente para el Buffer de Extracción (Convex Hull)
            geometria_maestra = gdf.unary_union.convex_hull

            print(f"|> [MOTOR ESPACIAL] Ingesta exitosa: {num_subcuencas} subcuenca(s) estandarizadas a {meta_crs['nombre_crs']}. Área Total: {area_total:.2f} km²")

            return {
                "exito": True,
                "gdf_cuencas": gdf,
                "area_total_km2": area_total,
                "num_subcuencas": num_subcuencas,
                "wkt_envolvente": geometria_maestra.wkt,
                "crs_info": meta_crs
            }

        except Exception as e:
            print(f"[CRÍTICO] Fallo en la Aduana Espacial al leer SHP.")
            traceback.print_exc()
            return {"exito": False, "error": str(e)}
            
        finally:
            # Directriz 1: Liberación de memoria C++ de GEOS/GDAL
            gc.collect()

    @staticmethod
    def calcular_area_cuenca_async(ruta_shapefile: str, epsg_utm: int, page, buffer_km: int = 100, on_success=None, on_error=None) -> None:
        """
        Motor espacial asíncrono para el cálculo del área de la cuenca.
        """
        import threading
        import traceback
        import gc
        
        def _task():
            try:
                # 1. Ingesta Defensiva y Zero-Trust (Multicuenca via Aduana Espacial)
                res_ingesta = MotorEspacial.ingerir_cuencas_zero_trust(ruta_shapefile, epsg_utm)
                if not res_ingesta["exito"]:
                    raise ValueError(res_ingesta.get("error", "Error en ingesta multicuenca."))
                    
                gdf = res_ingesta["gdf_cuencas"]
                area_total = res_ingesta["area_total_km2"]
                wkt_envolvente = res_ingesta["wkt_envolvente"]
                
                print(f"|> [NÚCLEO FÍSICO] Área computada: {area_total:.4f} km² ({res_ingesta['num_subcuencas']} subcuencas)")
                
                # Generación de Buffer desde el polígono envolvente (Convex Hull)
                from shapely import wkt
                geom_env = wkt.loads(wkt_envolvente)
                buffer_metrico_geom = geom_env.buffer(buffer_km * 1000)
                buffer_gs_utm = gpd.GeoSeries([buffer_metrico_geom], crs=f"EPSG:{epsg_utm}")
                buffer_gs_geo = buffer_gs_utm.to_crs("EPSG:4326")
                buffer_geom_geo = buffer_gs_geo.iloc[0]

                # Reproyección a WGS84 para UI
                cuenca_geo = gdf.to_crs("EPSG:4326")
                
                cuenca_geojson = cuenca_geo.geometry.unary_union.__geo_interface__
                buffer_geojson = buffer_geom_geo.__geo_interface__
                bbox = list(buffer_gs_geo.total_bounds)
                
                # 5. Persistencia en la sesión de la UI si page está disponible
                if page is not None and hasattr(page, "session"):
                    page.session.set('area_cuenca_km2', round(area_total, 2))
                    page.session.set('gdf_multicuenca', gdf)
                    page.session.set('wkt_envolvente_busqueda', wkt_envolvente)
                    page.session.set('cuenca_geojson', cuenca_geojson)
                    page.session.set('buffer_geojson', buffer_geojson)
                    page.session.set('cuenca_bbox', bbox)
                    page.session.set('arf_activado', area_total > 25.0)
                    page.session.set('es_multicuenca_arf', area_total > 25.0)
                    page.session.set('buffer_geom', buffer_geom_geo)
                    page.session.set('buffer_geom_shapely', buffer_geom_geo)
                
                if on_success:
                    if page is not None and hasattr(page, "run_thread"):
                        page.run_thread(on_success)
                    else:
                        on_success()
    
            except Exception as e:
                print(f"[CRÍTICO] Fallo en motor espacial (Cálculo de Área).")
                traceback.print_exc()
                
                if page is not None and hasattr(page, "snack_bar"):
                    try:
                        import flet as ft
                        page.snack_bar = ft.SnackBar(
                            content=ft.Text(f"ERROR: Cálculo de Área falló. Detalle: {str(e)}", color="#ffffff"),
                            bgcolor="#cc0000",
                            open=True
                        )
                        page.update()
                    except Exception:
                        pass
                if on_error:
                    if page is not None and hasattr(page, "run_thread"):
                        page.run_thread(on_error)
                    else:
                        on_error()
                
            finally:
                if 'gdf' in locals(): del gdf
                if 'cuenca_geo' in locals(): del cuenca_geo
                gc.collect()
    
        # Disparo asíncrono para liberar el hilo principal (GIL)
        threading.Thread(target=_task, daemon=True).start()

    @staticmethod
    def exportar_plantilla_shp(schema_class, tipo_geometria: str, ruta_salida: str, crs=DEFAULT_CRS):
        """
        [PARCHE DEVSECOPS] Exporta un Shapefile vacío forzando estrictamente 
        el tipo de geometría en el Header binario del archivo .shp.
        Evita el fallback a LineString de GDAL.
        """
        esquema_pydantic = schema_class.model_fields
        
        # 1. Mapeo de tipos de Python a tipos compatibles con C/Fiona
        propiedades_fiona = {}
        for nombre, field in esquema_pydantic.items():
            tipo_str = str(field.annotation).lower()
            if 'int' in tipo_str: 
                fiona_type = 'int'
            elif 'float' in tipo_str: 
                fiona_type = 'float'
            else: 
                fiona_type = 'str'
            propiedades_fiona[nombre] = fiona_type

        # 2. Definición inquebrantable del Esquema
        schema_fiona = {
            'geometry': tipo_geometria, # 'Polygon' o 'LineString' forzado
            'properties': propiedades_fiona
        }
        
        # 3. Creación atómica del archivo usando Fiona en modo escritura ('w')
        # Al no escribir ningún registro (pass), se crea un archivo perfecto y vacío.
        try:
            with fiona.open(ruta_salida, 'w', driver='ESRI Shapefile', crs=crs, schema=schema_fiona) as c:
                pass 
        except Exception as e:
            import traceback
            traceback.print_exc()
            raise RuntimeError(f"Fallo al forzar el header del Shapefile en Fiona: {str(e)}")

    @staticmethod
    def generar_thiessen(df_estaciones: pd.DataFrame, gdf_cuenca: gpd.GeoDataFrame, crs=DEFAULT_CRS) -> gpd.GeoDataFrame:
        """
        Calcula Polígonos de Thiessen (Voronoi) vectorizados y los recorta con la cuenca.
        df_estaciones DEBE contener ['Clave', 'X_UTM', 'Y_UTM', 'Prec_Base']
        """
        try:
            # 1. Extraer coordenadas
            coords = df_estaciones[['X_UTM', 'Y_UTM']].values
            
            # 2. Truco SciPy: Añadir "Puntos Fantasma" muy lejanos para cerrar los polígonos periféricos
            radio_expansion = 200000 # 200km
            center = coords.mean(axis=0)
            puntos_fantasma = np.array([
                center + [radio_expansion, 0], center + [-radio_expansion, 0],
                center + [0, radio_expansion], center + [0, -radio_expansion]
            ])
            puntos_totales = np.vstack([coords, puntos_fantasma])
            
            # 3. Álgebra de Voronoi (C++ Backend)
            vor = Voronoi(puntos_totales)
            
            # 4. Reconstrucción Topológica Shapely
            lines = [LineString(vor.vertices[line]) for line in vor.ridge_vertices if -1 not in line]
            poligonos = list(polygonize(lines))
            
            # 5. Mapeo Espacial Punto a Polígono
            gdf_voronoi = gpd.GeoDataFrame(geometry=poligonos, crs=crs)
            gdf_puntos = gpd.GeoDataFrame(df_estaciones, geometry=gpd.points_from_xy(df_estaciones.X_UTM, df_estaciones.Y_UTM), crs=crs)
            
            # Spatial Join: ¿Qué estación quedó dentro de qué polígono?
            gdf_unido = gpd.sjoin(gdf_voronoi, gdf_puntos, how="inner", predicate="intersects")
            
            # 6. Intersección con la Cuenca Limitante (Recorte)
            gdf_final = gpd.overlay(gdf_unido, gdf_cuenca, how='intersection')
            
            # 7. Cálculo de Áreas Automático (En Metros Cuadrados -> km2)
            gdf_final['Area_km2'] = gdf_final.geometry.area / 1e6
            area_total = gdf_final['Area_km2'].sum()
            gdf_final['Peso_Relativo'] = gdf_final['Area_km2'] / area_total
            
            return gdf_final
        except Exception as e:
            traceback.print_exc()
            raise RuntimeError(f"Fallo en álgebra espacial Voronoi: {str(e)}")

    @staticmethod
    def ingestar_capa_externa(ruta_archivo: str, schema_class, capa_nombre: str = None) -> gpd.GeoDataFrame:
        """
        DevSecOps: Aduana de validación. Verifica que el SHP/GPKG de QGIS cumpla el contrato.
        """
        try:
            # EAFP: Intentar cargar (GDAL maneja el binario)
            gdf = gpd.read_file(ruta_archivo, layer=capa_nombre)
        except Exception as e:
            raise RuntimeError(f"Archivo corrupto o ilegible por GDAL: {str(e)}")
            
        # Validación de Columnas
        columnas_requeridas = set(schema_class.model_fields.keys())
        columnas_actuales = set(gdf.columns)
        
        faltantes = columnas_requeridas - columnas_actuales
        if faltantes:
            raise ValueError(f"Fallo de Ingesta: Faltan las siguientes columnas obligatorias: {faltantes}")
            
        # Forzar CRS a UTM 14N si viene en WGS84 (Tolerancia Cero a EPSG:4326)
        if gdf.crs != DEFAULT_CRS:
            gdf = gdf.to_crs(DEFAULT_CRS)
            
        return gdf
