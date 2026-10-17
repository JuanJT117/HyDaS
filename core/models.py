# core/models.py
import uuid
from sqlalchemy import Column, String, Float, Integer, DateTime, Boolean, LargeBinary
from sqlalchemy.orm import declarative_base
from datetime import datetime, timezone

Base = declarative_base()

class Proyecto(Base):
    """Entidad raíz del proyecto. Define los metadatos globales."""
    __tablename__ = 'hidro_proyecto'
    
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    nombre = Column(String, nullable=False)
    fecha_creacion = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    autor = Column(String, default="HidroSistem User")
    crs_epsg = Column(Integer, default=32614)  # Forzar proyección UTM por defecto
    version_esquema = Column(String, default="11.2")
    session_activa = Column(Boolean, default=False)

class EstacionClimatologica(Base):
    """Catálogo relacional de estaciones descargadas/imputadas."""
    __tablename__ = 'hidro_estaciones'
    
    id = Column(String, primary_key=True)  # Ej. 'CNA-19045'
    nombre = Column(String, nullable=False)
    latitud = Column(Float, nullable=False)
    longitud = Column(Float, nullable=False)
    altitud = Column(Float)
    es_objetivo = Column(Boolean, default=False)
    # Nota: La geometría espacial real (Punto) se manejará vía GeoPandas en otra tabla OGC.

class SessionState(Base):
    """Registro serializado para memoria volátil (reemplazo del JSON/Zip tree)."""
    __tablename__ = 'hidro_session_state'
    
    key = Column(String, primary_key=True)
    data_type = Column(String, nullable=False) # 'json', 'parquet', 'bytes', 'folder_backup'
    blob_data = Column(LargeBinary, nullable=False)

class AuditLog(Base):
    """Registro de trazabilidad y auditoría de eventos del proyecto."""
    __tablename__ = 'hidro_audit_log'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    timestamp = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    modulo = Column(String, nullable=False)
    accion = Column(String, nullable=False)
    detalle = Column(String, nullable=True)
    usuario = Column(String, default="HidroSistem User")

