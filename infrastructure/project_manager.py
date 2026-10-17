import os
import sys
import traceback
import sqlite3
import json
import io
import base64
import threading
import pandas as pd
import numpy as np
import uuid
import tempfile
import zipfile
import shutil
import atexit
from datetime import datetime, timezone

# --- CONFIGURACIÓN DEVSECOPS: PROJ_LIB / PROJ_DATA PARA GDAL Y PYOGRIO ---
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

import geopandas as gpd
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker, scoped_session
from core.models import Base, Proyecto, SessionState, AuditLog

# --- CODIFICADOR CUSTOM PARA NUMPY/PANDAS E IGNORAR TIPOS NO SERIALIZABLES ---
class NumpyEncoder(json.JSONEncoder):
    """Garantiza que los tipos numéricos de C/Numpy se serialicen a JSON nativo sin romper el script."""
    def default(self, obj):
        if isinstance(obj, (np.integer,)): return int(obj)
        if isinstance(obj, (np.floating,)): return float(obj)
        if isinstance(obj, (np.bool_,)): return bool(obj)
        if isinstance(obj, np.ndarray): return obj.tolist()
        try:
            return super(NumpyEncoder, self).default(obj)
        except TypeError:
            return f"<NonSerializable: {type(obj).__name__}>"

class GeoProjectManager:
    """
    Adaptador de Persistencia basado en GeoPackage/SQLite.
    Patrón Singleton por Proyecto cargado con soporte transaccional,
    empaquetado recursivo de estructuras anidadas y gestión de capas OGC.
    """
    EXTENSION = ".hds"

    def __init__(self):
        self.db_path = None
        self.engine = None
        self.SessionLocal = None
        self._cache_dir = None
        self._io_lock = threading.RLock()
        
        # --- SHUTDOWN HOOK ---
        atexit.register(self.close_project)

    def _enforce_extension(self, path: str) -> str:
        if not path.endswith(self.EXTENSION):
            return path + self.EXTENSION
        return path

    def _get_absolute_path(self, path: str) -> str:
        """DevSecOps: Resuelve rutas seguras para PyInstaller y Runtime normal."""
        try:
            base_path = sys._MEIPASS
        except AttributeError:
            base_path = os.path.abspath(".")
        return os.path.join(base_path, path)

    # --- SERIALIZACIÓN RECURSIVA UNIVERSAL (FASE 1) ---

    @staticmethod
    def pack_tree(node):
        """
        Serializa recursivamente cualquier estructura de datos en memoria (diccionarios anidados,
        listas, tuplas, DataFrames, Series, ndarrays y punteros a caché en disco)
        en un árbol serializable en JSON con incrustación nativa de DataFrames en Parquet (base64).
        Elimina permanentemente el problema de <NonSerializable: DataFrame> y los punteros
        huérfanos a %TEMP%.
        """
        if node is None:
            return None
            
        # Caso 1: DataFrame o Series de Pandas
        if isinstance(node, (pd.DataFrame, pd.Series)):
            is_series = isinstance(node, pd.Series)
            df_safe = pd.DataFrame(node) if is_series else node.copy(deep=True)
            df_safe.columns = df_safe.columns.astype(str)
            if df_safe.index.dtype == object:
                df_safe.index = df_safe.index.astype(str)
            for col in df_safe.columns:
                if df_safe[col].dtype == object:
                    try:
                        df_safe[col] = pd.to_numeric(df_safe[col])
                    except Exception:
                        df_safe[col] = df_safe[col].astype(str)
            buf = io.BytesIO()
            df_safe.to_parquet(buf, engine="pyarrow", index=True)
            return {
                "__hds_node__": "dataframe_parquet",
                "is_series": is_series,
                "payload_b64": base64.b64encode(buf.getvalue()).decode("ascii")
            }

        # Caso 2: Punteros de archivo en disco (ej. %TEMP% de IDF o Climatología)
        if isinstance(node, dict) and node.get("type") in ["df", "parquet"] and "path" in node:
            disk_path = node["path"]
            if disk_path and os.path.exists(disk_path):
                try:
                    df_loaded = pd.read_parquet(disk_path)
                    return GeoProjectManager.pack_tree(df_loaded)
                except Exception:
                    with open(disk_path, "rb") as f:
                        return {
                            "__hds_node__": "raw_file",
                            "type": node.get("type"),
                            "payload_b64": base64.b64encode(f.read()).decode("ascii")
                        }
            return {
                "__hds_node__": "missing_cache",
                "orig_type": node.get("type")
            }

        # Caso 3: Backup de carpeta
        if isinstance(node, dict) and node.get("__type__") == "folder_backup":
            return node

        # Caso 4: Diccionario anidado
        if isinstance(node, dict):
            if "__hds_node__" in node:
                return node
            packed_dict = {}
            for k, v in node.items():
                packed_dict[str(k)] = GeoProjectManager.pack_tree(v)
            return packed_dict

        # Caso 5: Listas y tuplas
        if isinstance(node, (list, tuple)):
            return [GeoProjectManager.pack_tree(item) for item in node]

        # Caso 6: Numpy arrays y tipos escalares
        if isinstance(node, np.ndarray):
            return {
                "__hds_node__": "ndarray",
                "dtype": str(node.dtype),
                "payload": node.tolist()
            }
        if isinstance(node, (np.integer,)):
            return int(node)
        if isinstance(node, (np.floating,)):
            return float(node)
        if isinstance(node, (np.bool_,)):
            return bool(node)

        # Tipos nativos JSON
        return node

    @staticmethod
    def unpack_tree(node, cache_dir=None):
        """
        Reconstruye recursivamente las estructuras de datos nativas (DataFrames, Series,
        ndarrays, diccionarios) a partir del árbol serializado.
        """
        if node is None:
            return None

        if isinstance(node, dict):
            tag = node.get("__hds_node__")
            if tag == "dataframe_parquet":
                payload_bytes = base64.b64decode(node["payload_b64"])
                buf = io.BytesIO(payload_bytes)
                df = pd.read_parquet(buf, engine="pyarrow")
                if node.get("is_series", False):
                    return df.iloc[:, 0]
                return df
            elif tag == "ndarray":
                return np.array(node["payload"], dtype=node.get("dtype"))
            elif tag == "raw_file":
                if cache_dir:
                    ext = ".parquet" if node.get("type") in ["df", "parquet"] else ".bin"
                    temp_path = os.path.join(cache_dir, f"cache_{uuid.uuid4().hex[:8]}{ext}")
                    with open(temp_path, "wb") as f:
                        f.write(base64.b64decode(node["payload_b64"]))
                    return {"type": node.get("type"), "path": temp_path}
                return None
            elif tag == "missing_cache":
                return None
            else:
                return {k: GeoProjectManager.unpack_tree(v, cache_dir=cache_dir) for k, v in node.items()}

        if isinstance(node, list):
            return [GeoProjectManager.unpack_tree(item, cache_dir=cache_dir) for item in node]

        return node

    # --- GESTIÓN DE CICLO DE VIDA DE PROYECTO ---

    def inicializar_proyecto(self, ruta_archivo: str, nombre_proyecto: str = "Proyecto Nuevo", force_recreate_if_invalid: bool = False):
        """
        Filosofía EAFP: Intenta crear y conectar. Si falla, propaga el error con auditoría.
        El archivo de salida usará extensión .hidro (o .hds), pero formato .gpkg.
        """
        with self._io_lock:
            ruta_archivo = self._enforce_extension(ruta_archivo)
            self.db_path = self._get_absolute_path(ruta_archivo)
            
            try:
                # 0. Mitigación contra Flet Picker y Legacy files
                if os.path.exists(self.db_path):
                    if os.path.getsize(self.db_path) == 0:
                        os.remove(self.db_path)
                    else:
                        conn = None
                        try:
                            conn = sqlite3.connect(self.db_path)
                            conn.execute("PRAGMA journal_mode;")
                        except sqlite3.DatabaseError:
                            if conn:
                                conn.close()
                                conn = None
                            if force_recreate_if_invalid:
                                os.remove(self.db_path)
                            else:
                                raise RuntimeError("❌ Proyecto Incompatible: Este archivo es de una versión anterior de HyDaS (v10 o menor) o el archivo está corrupto. La nueva versión requiere bases de datos GeoPackage.")
                        finally:
                            if conn:
                                conn.close()

                # 1. Bootstrap OGC GeoPackage si es nuevo
                if not os.path.exists(self.db_path):
                    try:
                        gdf_empty = gpd.GeoDataFrame({'geometry': []}, crs="EPSG:32614")
                        gdf_empty.to_file(self.db_path, driver="GPKG", layer="init_layer")
                    except Exception:
                        try:
                            gdf_empty = gpd.GeoDataFrame({'geometry': []})
                            gdf_empty.to_file(self.db_path, driver="GPKG", layer="init_layer")
                        except Exception:
                            conn = sqlite3.connect(self.db_path)
                            conn.execute("PRAGMA journal_mode=WAL;")
                            conn.close()

                # 2. Configuración SQLAlchemy con mitigación de Thread-Locking
                sqlite_url = f"sqlite:///{self.db_path}"
                self.engine = create_engine(
                    sqlite_url, 
                    connect_args={"check_same_thread": False},
                    pool_pre_ping=True
                )
                
                # 3. Forzar Modo WAL y busy_timeout para concurrencia masiva
                with self.engine.connect() as conn:
                    conn.execute(text("PRAGMA journal_mode=WAL;"))
                    conn.execute(text("PRAGMA synchronous=NORMAL;"))
                    conn.execute(text("PRAGMA busy_timeout = 10000;"))
                    # Conservar PRAGMA user_version para cumplimiento OGC GeoPackage (versión del esquema app se almacena en Proyecto.version_esquema)

                # 4. Construir esquema relacional de HidroSistem
                Base.metadata.create_all(bind=self.engine)
                
                # 5. Fábrica de sesiones
                self.SessionLocal = scoped_session(sessionmaker(autocommit=False, autoflush=False, bind=self.engine))
                
                # 6. Registrar Metadatos Iniciales si es nuevo
                db = self.SessionLocal()
                try:
                    if db.query(Proyecto).count() == 0:
                        nuevo_proyecto = Proyecto(nombre=nombre_proyecto)
                        db.add(nuevo_proyecto)
                        db.commit()
                except Exception:
                    db.rollback()
                    raise
                finally:
                    db.close()

            except Exception as e:
                print("[CRÍTICO] Fallo al inicializar GeoPackage.", file=sys.stderr)
                traceback.print_exc()
                raise RuntimeError(f"Corrupción en inicialización I/O: {str(e)}")

    def get_session(self):
        """Generador para Inyección de Dependencias. Manejo estricto de memoria."""
        if not self.SessionLocal:
            raise ValueError("Proyecto no inicializado. Llama a inicializar_proyecto primero.")
        db = self.SessionLocal()
        try:
            yield db
        finally:
            db.close()
            import gc
            gc.collect()

    def conectar_proyecto(self, ruta_archivo: str, is_new: bool = False):
        """Conecta a un proyecto existente sin sobreescribir los metadatos base."""
        self.inicializar_proyecto(ruta_archivo, force_recreate_if_invalid=is_new)

    def close_project(self):
        """Cierre seguro del motor. Fuerza la limpieza atómica del -wal y -shm."""
        with self._io_lock:
            if self.engine:
                try:
                    self.set_session_activa(False)
                except Exception:
                    pass
                self.engine.dispose()
                self.engine = None
            self.SessionLocal = None
            self.db_path = None
            if self._cache_dir:
                shutil.rmtree(self._cache_dir, ignore_errors=True)
                self._cache_dir = None

    # --- AUDITORÍA Y TRAZABILIDAD (FASE 3) ---

    def registrar_auditoria(self, modulo: str, accion: str, detalle: str = "", usuario: str = "HidroSistem User"):
        """Registra un evento de auditoría en la tabla hidro_audit_log de forma segura."""
        if not self.SessionLocal:
            return
        with self._io_lock:
            db = self.SessionLocal()
            try:
                entry = AuditLog(
                    modulo=modulo,
                    accion=accion,
                    detalle=detalle,
                    usuario=usuario,
                    timestamp=datetime.now(timezone.utc)
                )
                db.add(entry)
                db.commit()
            except Exception as e:
                db.rollback()
                print(f"[AUDIT ERROR] No se pudo registrar auditoría: {e}", file=sys.stderr)
            finally:
                db.close()

    def obtener_auditoria(self, limit: int = 100) -> list:
        """Recupera los últimos registros de auditoría del proyecto."""
        if not self.SessionLocal:
            return []
        with self._io_lock:
            db = self.SessionLocal()
            try:
                logs = db.query(AuditLog).order_by(AuditLog.id.desc()).limit(limit).all()
                return [{
                    "id": l.id,
                    "timestamp": l.timestamp.isoformat() if l.timestamp else "",
                    "modulo": l.modulo,
                    "accion": l.accion,
                    "detalle": l.detalle,
                    "usuario": l.usuario
                } for l in logs]
            finally:
                db.close()

    def set_session_activa(self, activa: bool):
        """Marca el flag de sesión activa en hidro_proyecto para detección de crash."""
        if not self.SessionLocal:
            return
        with self._io_lock:
            db = self.SessionLocal()
            try:
                proy = db.query(Proyecto).first()
                if proy:
                    proy.session_activa = activa
                    db.commit()
            except Exception:
                db.rollback()
            finally:
                db.close()

    def verificar_recuperacion_sesion(self) -> bool:
        """Indica si el proyecto quedó con sesión activa tras una caída o cierre inesperado."""
        if not self.SessionLocal:
            return False
        with self._io_lock:
            db = self.SessionLocal()
            try:
                proy = db.query(Proyecto).first()
                return bool(proy and proy.session_activa)
            except Exception:
                return False
            finally:
                db.close()

    # --- PERSISTENCIA Y REHIDRATACIÓN DE SESIÓN (FASE 1 & FASE 2) ---
    
    def save_project(self, path: str, session_data: dict) -> bool:
        """
        Guarda todo el session_data en la tabla SessionState de SQLite usando empaquetado recursivo.
        Crea un respaldo .bak antes de escribir para garantizar atomicidad y tolerancia a fallos.
        """
        path = self._enforce_extension(path)
        with self._io_lock:
            bak_path = path + ".bak"
            if os.path.exists(path):
                try:
                    shutil.copy2(path, bak_path)
                except Exception as e:
                    print(f"[WARN] No se pudo crear respaldo .bak: {e}", file=sys.stderr)

            if not self.engine:
                self.conectar_proyecto(path, is_new=True)
                
            db = self.SessionLocal()
            try:
                with self.engine.connect() as conn:
                    conn.execute(text("PRAGMA busy_timeout = 10000;"))

                db.query(SessionState).delete()
                
                for k, v in session_data.items():
                    if v is None:
                        continue
                    k_str = str(k)

                    # Caso 1: Backup de Carpeta
                    if isinstance(v, dict) and v.get("__type__") == "folder_backup":
                        folder_path = v.get("path")
                        if folder_path and os.path.exists(folder_path):
                            zip_io = io.BytesIO()
                            with zipfile.ZipFile(zip_io, 'w', zipfile.ZIP_DEFLATED) as zf:
                                for root, _, files in os.walk(folder_path):
                                    for f in files:
                                        if f.endswith(".txt"):
                                            zf.write(os.path.join(root, f), arcname=f)
                            zip_io.seek(0)
                            db.add(SessionState(
                                key=k_str,
                                data_type="folder_backup",
                                blob_data=zip_io.read()
                            ))
                        continue

                    # Caso 2: DataFrame de primer nivel (rápido y compatible)
                    if isinstance(v, (pd.DataFrame, pd.Series)):
                        is_series = isinstance(v, pd.Series)
                        df_safe = pd.DataFrame(v) if is_series else v.copy(deep=True)
                        df_safe.columns = df_safe.columns.astype(str)
                        if df_safe.index.dtype == object:
                            df_safe.index = df_safe.index.astype(str)
                        for col in df_safe.columns:
                            if df_safe[col].dtype == object:
                                try:
                                    df_safe[col] = pd.to_numeric(df_safe[col])
                                except Exception:
                                    df_safe[col] = df_safe[col].astype(str)
                                    
                        f_io = io.BytesIO()
                        df_safe.to_parquet(f_io, engine="pyarrow", index=True)
                        f_io.seek(0)
                        db.add(SessionState(
                            key=k_str,
                            data_type="parquet",
                            blob_data=f_io.read()
                        ))
                        continue

                    # Caso 3: Empaquetado recursivo universal
                    packed = self.pack_tree(v)
                    json_bytes = json.dumps(packed, cls=NumpyEncoder).encode("utf-8")
                    db.add(SessionState(
                        key=k_str,
                        data_type="json_tree",
                        blob_data=json_bytes
                    ))
                    
                db.commit()
                self.registrar_auditoria("PERSISTENCIA", "GUARDAR_PROYECTO", f"Proyecto guardado exitosamente: {len(session_data)} llaves")
                return True
                
            except Exception as e:
                print(f"Error crítico al guardar en GeoPackage: {e}", file=sys.stderr)
                traceback.print_exc()
                db.rollback()
                if os.path.exists(bak_path):
                    try:
                        shutil.copy2(bak_path, path)
                        print("[INFO] Proyecto restaurado desde .bak debido a falla durante el guardado.", file=sys.stderr)
                    except Exception as restore_err:
                        print(f"[CRITICAL] Error al restaurar .bak: {restore_err}", file=sys.stderr)
                return False
            finally:
                db.close()

    def load_project(self, file_path: str, progress_callback=None, log_callback=None) -> dict:
        """Hidrata el diccionario de sesión desde el SQLite GeoPackage."""
        file_path = self._enforce_extension(file_path)
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"El archivo de proyecto no existe: {file_path}")
            
        with self._io_lock:
            self.conectar_proyecto(file_path)
            db = self.SessionLocal()
            recovered_data = {}
            
            try:
                states = db.query(SessionState).all()
                total_files = len(states)
                processed = 0
                
                for state in states:
                    processed += 1
                    if log_callback: log_callback(f"Descomprimiendo tensor DB: {state.key}")
                    if progress_callback and total_files > 0:
                        progress_callback(processed / total_files)
                        
                    if state.data_type in ["json", "json_tree"]:
                        raw_val = json.loads(state.blob_data.decode("utf-8"))
                        recovered_data[state.key] = self.unpack_tree(raw_val, cache_dir=self._cache_dir)
                        
                    elif state.data_type == "parquet":
                        f_io = io.BytesIO(state.blob_data)
                        recovered_data[state.key] = pd.read_parquet(f_io, engine="pyarrow")
                        
                    elif state.data_type == "folder_backup":
                        if not self._cache_dir:
                            self._cache_dir = tempfile.mkdtemp(prefix="hidro_cache_load_")
                            atexit.register(lambda d=self._cache_dir: shutil.rmtree(d, ignore_errors=True))
                            
                        zip_io = io.BytesIO(state.blob_data)
                        out_folder = os.path.join(self._cache_dir, f"{state.key}_unzipped")
                        os.makedirs(out_folder, exist_ok=True)
                        with zipfile.ZipFile(zip_io, 'r') as zf:
                            zf.extractall(out_folder)
                        recovered_data[state.key] = {"__type__": "folder_backup", "path": out_folder}
                        
                    elif state.data_type.startswith("disk_cache_"):
                        orig_type = state.data_type.replace("disk_cache_", "")
                        if not self._cache_dir:
                            self._cache_dir = tempfile.mkdtemp(prefix="hidro_cache_load_")
                            atexit.register(lambda d=self._cache_dir: shutil.rmtree(d, ignore_errors=True))
                            
                        ext = ".parquet" if orig_type in ["df", "parquet"] else ".txt" if orig_type == "b64" else ".tmp"
                        out_path = os.path.join(self._cache_dir, f"{state.key}{ext}")
                        with open(out_path, "wb") as f:
                            f.write(state.blob_data)
                        recovered_data[state.key] = {"type": orig_type, "path": out_path}

                if progress_callback: progress_callback(1.0)
                self.registrar_auditoria("PERSISTENCIA", "CARGAR_PROYECTO", f"Proyecto cargado: {len(recovered_data)} llaves")
                return recovered_data
                
            except Exception as e:
                print(f"❌ [CRÍTICO] Error al cargar el proyecto {file_path}:", file=sys.stderr)
                traceback.print_exc()
                raise RuntimeError(f"Falla de descompresión DB: {str(e)}")
            finally:
                db.close()

    # --- GESTIÓN DE CAPAS ESPACIALES OGC GPKG (FASE 1 & FASE 2) ---

    def guardar_capa_espacial(self, nombre_capa: str, gdf: gpd.GeoDataFrame):
        """
        Persiste un GeoDataFrame como capa nativa OGC dentro del GeoPackage (.hds).
        Esto permite su lectura nativa e inmediata en QGIS y en el Módulo 7.
        """
        if not self.db_path:
            raise RuntimeError("Proyecto no inicializado. Carga o crea un proyecto primero.")
        with self._io_lock:
            try:
                if self.engine:
                    self.engine.dispose()
                    import time
                    time.sleep(0.05)
                
                import pyogrio
                has_gpkg = False
                if os.path.exists(self.db_path):
                    conn = sqlite3.connect(self.db_path, timeout=10.0)
                    try:
                        cur = conn.cursor()
                        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='gpkg_contents';")
                        has_gpkg = bool(cur.fetchone())
                        if has_gpkg:
                            cur.execute(f"DROP TABLE IF EXISTS \"{nombre_capa}\";")
                            cur.execute("DELETE FROM gpkg_contents WHERE table_name = ?;", (nombre_capa,))
                            cur.execute("DELETE FROM gpkg_geometry_columns WHERE table_name = ?;", (nombre_capa,))
                            conn.commit()
                    finally:
                        conn.close()

                pyogrio.write_dataframe(gdf, self.db_path, driver="GPKG", layer=nombre_capa, append=has_gpkg)
                self.registrar_auditoria("SIG", "GUARDAR_CAPA", f"Capa '{nombre_capa}' persistida ({len(gdf)} entidades)")
            except Exception as e:
                traceback.print_exc()
                raise RuntimeError(f"Error bloqueante al escribir capa '{nombre_capa}' en GeoPackage: {str(e)}")

    def cargar_capa_espacial(self, nombre_capa: str):
        """
        Carga una capa vectorial nativa OGC desde el archivo GeoPackage (.hds).
        Retorna un GeoDataFrame o None si la capa no existe.
        """
        if not self.db_path or not os.path.exists(self.db_path):
            return None
        with self._io_lock:
            try:
                capas = self.listar_capas_espaciales()
                if nombre_capa not in capas:
                    return None
                import pyogrio
                return pyogrio.read_dataframe(self.db_path, layer=nombre_capa)
            except Exception as e:
                try:
                    return gpd.read_file(self.db_path, layer=nombre_capa)
                except Exception:
                    print(f"[WARN] Error al leer capa espacial '{nombre_capa}': {e}", file=sys.stderr)
                    return None

    def listar_capas_espaciales(self) -> list:
        """Lista todas las capas vectoriales disponibles en el GeoPackage activo."""
        if not self.db_path or not os.path.exists(self.db_path):
            return []
        with self._io_lock:
            try:
                import pyogrio
                layers_info = pyogrio.list_layers(self.db_path)
                return [row[0] for row in layers_info]
            except Exception:
                try:
                    conn = sqlite3.connect(self.db_path, timeout=5.0)
                    cur = conn.cursor()
                    cur.execute("SELECT table_name FROM gpkg_contents WHERE data_type = 'features';")
                    layers = [r[0] for r in cur.fetchall()]
                    conn.close()
                    return layers
                except Exception:
                    return []

    def capa_espacial_existe(self, nombre_capa: str) -> bool:
        """Determina si una capa vectorial está presente en el archivo de proyecto."""
        return nombre_capa in self.listar_capas_espaciales()

# Singleton global para su uso en main.py y todas las vistas
project_manager_instance = GeoProjectManager()