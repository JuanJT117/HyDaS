# core/modelos_clima.py
from pydantic import BaseModel, Field
from typing import Dict, List, Optional, Any, Literal

class EntradaDescriptoresChen(BaseModel):
    """Contrato de entrada para la calibración de Chen (Isoyetas SCT o Factor R Regional)."""
    station_id: str
    best_fit_name: str
    metodo_r: Literal["isoyetas_sct", "r_regional"] = Field(default="r_regional")
    p1_10_manual: Optional[float] = Field(default=None, gt=0.0)
    p1_25_manual: Optional[float] = Field(default=None, gt=0.0)
    p1_50_manual: Optional[float] = Field(default=None, gt=0.0)
    r_regional_manual: float = Field(default=0.3882, ge=0.10, le=0.699)
    df_maximos_mensuales: Any = Field(description="DataFrame con la Serie Anual de Máximos (SAM)")
    aplicar_weiss: bool = Field(default=True)

class ResultadoDescriptoresChen(BaseModel):
    """Contrato de salida inmutable con trazabilidad paramétrica completa."""
    station_id: str
    distribucion_aplicada: str
    metodo_r_utilizado: str
    p1_10_mm: float
    p1_25_mm: float
    p1_50_mm: float
    p24_fijo_10_mm: float
    p24_fijo_25_mm: float
    p24_fijo_50_mm: float
    p24_fijo_100_mm: float
    factor_weiss: float = 1.13
    p24_movil_10_mm: float
    p24_movil_25_mm: float
    p24_movil_50_mm: float
    p24_movil_100_mm: float
    r_10: float
    r_25: float
    r_50: float
    r_promedio_crudo: float
    r_acotado: float = Field(ge=0.10, le=0.699)
    f_calculado: float = Field(gt=1.0)
    param_a: float
    param_b: float
    param_c: float
    advertencias: List[str] = []
