# core/exceptions.py
"""
Taxonomía de Excepciones y Advertencias del Dominio Hidrológico (HyDaS v11.2).
Proporciona clases de error estructuradas para capturar fallos matemáticos,
inconsistencias geoespaciales y violaciones de contratos de persistencia.
"""

class HyDaSBaseException(Exception):
    """Clase base de todas las excepciones específicas de HyDaS."""
    def __init__(self, mensaje: str, codigo_error: str = "ERR_GENERIC", sugerencia: str = ""):
        super().__init__(mensaje)
        self.mensaje = mensaje
        self.codigo_error = codigo_error
        self.sugerencia = sugerencia

    def __str__(self):
        msg = f"[{self.codigo_error}] {self.mensaje}"
        if self.sugerencia:
            msg += f" -> Sugerencia: {self.sugerencia}"
        return msg


# --- 1. EXCEPCIONES DE DATOS Y SERIES CLIMÁTICAS ---

class ClimaDataError(HyDaSBaseException):
    """Errores en la ingesta o calidad de series temporales de precipitación/temperatura."""
    pass

class ClimaDataIncompleteError(ClimaDataError):
    """La serie temporal no cuenta con los registros mínimos para análisis de frecuencia."""
    def __init__(self, anios_disponibles: int, anios_minimos: int = 15, estacion_id: str = ""):
        msg = f"La estación '{estacion_id}' tiene solo {anios_disponibles} años de datos (mínimo requerido: {anios_minimos})."
        sug = "Seleccione otra estación vecina o impute los vacíos temporales en el Módulo 2."
        super().__init__(msg, codigo_error="ERR_CLIMA_DATA_SHORT", sugerencia=sug)

class ContinuousGapTooLargeError(ClimaDataError):
    """La brecha continua (gap) supera la capacidad de interpolación del modelo."""
    def __init__(self, dias_gap: int, dias_max: int = 365, estacion_id: str = ""):
        msg = f"La estación '{estacion_id}' presenta una laguna continua de {dias_gap} días sin datos."
        sug = "Aplique el Filtro C2 en el Módulo 4 o utilice un modelo regional multivariable (MLR)."
        super().__init__(msg, codigo_error="ERR_CLIMA_GAP_EXCEEDED", sugerencia=sug)


# --- 2. EXCEPCIONES Y ADVERTENCIAS FÍSICAS E HIDROLÓGICAS ---

class HydrologicalPhysicsError(HyDaSBaseException):
    """Inconsistencias que violan leyes físicas de la hidrología o hidráulica."""
    pass

class NonMonotonicIDRError(HydrologicalPhysicsError):
    """La curva IDF generada no decrece monotónicamente respecto a la duración."""
    def __init__(self, duracion_falla: float, tr: float):
        msg = f"Inconsistencia en Curva IDF para Tr={tr} años: dI/dt no es decreciente en t={duracion_falla} min."
        sug = "Revise el factor R de Chen o verifique que los máximos anuales no contengan valores anómalos."
        super().__init__(msg, codigo_error="ERR_NON_MONOTONIC_IDF", sugerencia=sug)

class ChenClampingWarning(UserWarning):
    """Advertencia emitida cuando el ratio R excede los límites físicos y es acotado defensivamente."""
    def __init__(self, r_original: float, r_acotado: float, estacion_id: str = ""):
        self.r_original = r_original
        self.r_acotado = r_acotado
        self.estacion_id = estacion_id
        super().__init__(f"Estación {estacion_id}: Ratio Chen R original={r_original:.4f} fuera de rango. Acotado forzosamente a {r_acotado:.3f}.")


# --- 3. EXCEPCIONES GEOESPACIALES Y DE PROYECCIÓN (CRSS) ---

class SpatialCRSError(HyDaSBaseException):
    """Errores o discrepancias en sistemas de coordenadas y proyecciones espaciales."""
    pass

class SpatialCRSMismatchWarning(UserWarning):
    """Advertencia cuando una capa vectorial requiere reproyección automática a la zona UTM local."""
    def __init__(self, crs_origen: str, crs_destino: str, capa: str = ""):
        self.crs_origen = crs_origen
        self.crs_destino = crs_destino
        super().__init__(f"Capa '{capa}': CRS de entrada ({crs_origen}) reproyectado automáticamente a {crs_destino} para estandarización WGS84 UTM.")

class InvalidGeometryError(SpatialCRSError):
    """La geometría de la cuenca o cauce contiene auto-intersecciones o polígonos abiertos."""
    def __init__(self, detalle: str = ""):
        msg = f"Geometría no válida detectada en la capa espacial: {detalle}"
        sug = "El sistema aplicará buffer(0) automático; si persiste, repare el Shapefile en QGIS/ArcGIS."
        super().__init__(msg, codigo_error="ERR_INVALID_GEOMETRY", sugerencia=sug)


# --- 4. EXCEPCIONES DE PERSISTENCIA Y MOTOR .HDS ---

class PersistenceError(HyDaSBaseException):
    """Errores de base de datos, bloqueo en Windows o corrupción en archivos .hds."""
    pass

class WinFileLockTimeoutError(PersistenceError):
    """El archivo .hds permaneció bloqueado por Windows (antivirus/indexador) tras expirar el timeout."""
    def __init__(self, ruta_archivo: str, timeout_ms: int = 10000):
        msg = f"No se pudo adquirir acceso exclusivo al archivo '{ruta_archivo}' tras {timeout_ms/1000}s (WinError 5 / WinError 32)."
        sug = "Cierre programas que puedan tener abierto el archivo (ej. visor de base de datos) y reintente."
        super().__init__(msg, codigo_error="ERR_WIN_FILE_LOCK", sugerencia=sug)

class IncompatibleProjectVersionError(PersistenceError):
    """El archivo .hds pertenece a una versión incompatible que requiere migración formal."""
    def __init__(self, version_encontrada: str, version_esperada: str = "11.2"):
        msg = f"El archivo tiene versión de esquema {version_encontrada}, pero el sistema espera {version_esperada}."
        sug = "Utilice el asistente de migración de proyectos para actualizar la estructura relacional."
        super().__init__(msg, codigo_error="ERR_PROJECT_VERSION_MISMATCH", sugerencia=sug)
