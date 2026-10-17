import pandas as pd
import numpy as np
import math
import traceback
import matplotlib
import matplotlib.pyplot as plt
import io
import base64
import concurrent.futures
from scipy import interpolate
from core import hidrologia_mx
import threading
import copy

import os
from datetime import datetime, timedelta

# Configurar backend de Matplotlib
matplotlib.use('Agg')

# Señal de interrupción para abortar convoluciones masivas
señal_abortar_gastos = threading.Event()

def interpolar_tr(valor_tr, df_datos):
    """
    Interpola linealmente los valores de lluvia o coeficientes para un TR personalizado.
    valor_tr: int o float (ej. 25)
    df_datos: DataFrame donde las columnas o el índice son los TR estándar.
    """
    # Convertir nombres de columnas/índice a números para buscar vecinos
    trs_disponibles = sorted([int(str(c)) for c in df_datos.columns if str(c).isdigit()])
    
    if valor_tr in trs_disponibles:
        return df_datos[str(valor_tr)]
    
    # Buscar límites para interpolación
    inferior = max([t for t in trs_disponibles if t < valor_tr] or [trs_disponibles[0]])
    superior = min([t for t in trs_disponibles if t > valor_tr] or [trs_disponibles[-1]])
    
    if inferior == superior: return df_datos[str(inferior)]
    
    # Interpolación lineal simple
    factor = (valor_tr - inferior) / (superior - inferior)
    val_inf = df_datos[str(inferior)]
    val_sup = df_datos[str(superior)]
    
    return val_inf + factor * (val_sup - val_inf)

def generar_hietograma_scs(tipo, p24, dt_calc):
    """
    Genera el hietograma de diseño a partir de la Curva de Masa SCS Tipo II o III.
    Interpola la curva estandarizada de 24 horas al paso de tiempo dinámico (dt_calc) del modelo.
    """
    # Tiempos estándar SCS (0 a 24 horas, en horas completas)
    t_horas = np.arange(0, 25)
    
    if tipo == "scs_ii":
        # Curva de masa típica SCS Tipo II (Salto abrupto en la hora 12)
        masa_acum = [0.0, 0.011, 0.022, 0.034, 0.048, 0.063, 0.08, 0.098, 0.12, 0.147, 0.181, 0.235, 
                     0.663, 0.772, 0.82, 0.855, 0.88, 0.898, 0.913, 0.926, 0.938, 0.949, 0.959, 0.969, 1.0]
    elif tipo == "scs_iii":
        # Curva de masa típica SCS Tipo III (Golfo de México, menos abrupta)
        masa_acum = [0.0, 0.01, 0.02, 0.031, 0.043, 0.057, 0.072, 0.089, 0.115, 0.148, 0.189, 0.25, 
                     0.5, 0.75, 0.811, 0.852, 0.883, 0.908, 0.928, 0.945, 0.959, 0.971, 0.982, 0.991, 1.0]
    else:
        return None
        
    # Interpolamos a nuestra resolución dt_calc (minutos)
    t_minutos_std = t_horas * 60
    t_minutos_calc = np.arange(0, 24 * 60 + dt_calc, dt_calc)
    
    # Scipy interpola la curva para sacar el % exacto de lluvia en cada minuto
    f_interp = interpolate.interp1d(t_minutos_std, masa_acum, kind='linear', bounds_error=False, fill_value=(0, 1))
    masa_acum_calc = f_interp(t_minutos_calc)
    
    # Calcular precipitación acumulada y luego incremental (hietograma)
    p_acum = masa_acum_calc * p24
    hietograma = np.diff(p_acum, prepend=0)
    
    return hietograma

# ==========================================
# 1. FUNCIONES ORIGINALES (Lógica "Legacy" Restaurada)
# ==========================================

def redondear_tc(tc):
    """Lógica original de redondeo (Snap to grid)"""
    if tc <= 10: return 10
    lower = (tc // 5) * 5
    return lower if tc - lower < 1 else lower + 5

def obtener_lista_desde_string(cadena, tipo_dato):
    try: return [tipo_dato(x.strip()) for x in str(cadena).split(',')]
    except: return []

# ==========================================
# 2. FUNCIONES HMS (AISLADAS - SANDBOX)
# ==========================================
# Estas funciones operan con copias de datos y bloques try/except propios.

def _hms_generar_hu(area_km2, tc_min, dt_min=10, factor_lag=0.6):
    try:
        # Aquí reemplazamos el 0.6 fijo por la variable dinámica
        t_lag = factor_lag * tc_min
        tp = (dt_min / 2.0) + t_lag
        # --- NUEVO: FRENO DE EMERGENCIA ---
        if señal_abortar_gastos.is_set():
            raise InterruptedError("🛑 CÁLCULO ABORTADO POR EL USUARIO")
        # ----------------------------------
        
        # Lag time y Tp
        tlag_hr = 0.6 * (tc_min / 60.0)
        dt_hr = dt_min / 60.0
        tp_hr = (dt_hr / 2.0) + tlag_hr
        
        # Caudal Pico (Métrico)
        qp_m3s = (2.08 * area_km2) / tp_hr
        
        # Curva Adimensional SCS
        t_ratios = np.array(hidrologia_mx.HU_SCS_ADIMENSIONAL["t_ratio"])
        q_ratios = np.array(hidrologia_mx.HU_SCS_ADIMENSIONAL["q_ratio"])
        
        times_hr = t_ratios * tp_hr
        flows_m3s = q_ratios * qp_m3s
        
        # Interpolación Cúbica para suavizar
        max_time = times_hr[-1]
        t_interp = np.arange(0, max_time, dt_hr)
        f_hu = interpolate.interp1d(times_hr, flows_m3s, kind='cubic', fill_value="extrapolate")
        hu_vals = np.maximum(f_hu(t_interp), 0)
        
        # Balance de Masa (Normalizar a 10mm)
        vol_teorico = (area_km2 * 1e6) * 0.01 
        vol_calc = np.sum(hu_vals) * dt_hr * 3600
        if vol_calc > 0:
            hu_vals = hu_vals * (vol_teorico / vol_calc)
            
        return hu_vals
    except:
        return None

def _hms_bloques_alternos(duracion_total, dt_min, df_intensidad, col_tr):
    """
    Generador de Hietograma Sintético Estricto (Método de Bloques Alternos - Ven Te Chow).
    Garantiza que la lluvia más violenta ocurra en el centro (tm), simulando el ojo de la tormenta.
    Totalmente vectorizado para no penalizar el rendimiento del hilo.
    """
    try:
        # 1. Extraer datos medidos (Lógica de adaptación al DataFrame de Flet)
        duraciones_medidas = df_intensidad.index.astype(float).values
        intensidades_medidas = df_intensidad[col_tr].astype(float).values
        
        # 2. Definir el vector de tiempo discreto (dt, 2dt, 3dt...)
        # Forzamos un número impar de bloques para garantizar un "Centro" matemático perfecto
        num_bloques = int(duracion_total / dt_min)
        if num_bloques % 2 == 0: num_bloques += 1
        duracion_ajustada = num_bloques * dt_min
        
        tiempos_dt = np.arange(dt_min, duracion_ajustada + dt_min, dt_min)
        
        # 3. Interpolación Log-Log exacta de la curva IDF
        f_int = interpolate.interp1d(np.log(duraciones_medidas), np.log(intensidades_medidas), kind='linear', fill_value="extrapolate")
        
        # Sanitización: Evitar evaluar t < 5 min para que la curva IDF no tienda a infinito
        tiempos_eval = np.maximum(tiempos_dt, 5.0)
        intensidades_dt = np.exp(f_int(np.log(tiempos_eval)))
        
        # 4. Calcular la Precipitación Acumulada en mm (P = I * t / 60)
        p_acumulada = intensidades_dt * (tiempos_dt / 60.0)
        
        # 5. Calcular la Lluvia Incremental (Diferencias sucesivas)
        p_incremental = np.zeros_like(p_acumulada)
        p_incremental[0] = p_acumulada[0]
        p_incremental[1:] = p_acumulada[1:] - p_acumulada[:-1]
        
        # Freno de emergencia matemático: ningún bloque puede ser negativo
        p_incremental = np.maximum(p_incremental, 0.0) 
        
        # 6. Ordenar bloques de mayor a menor (El pico será el índice 0)
        p_ordenada = np.sort(p_incremental)[::-1]
        
        # 7. Distribución Asimétrica Estricta (Zig-Zag de Ven Te Chow)
        n = len(p_ordenada)
        hietograma = np.zeros(n)
        
        centro = (n - 1) // 2 
        izq = centro - 1
        der = centro
        
        for i, bloque in enumerate(p_ordenada):
            if i % 2 == 0:  # Pares (0, 2, 4...) van al Centro y luego Derecha
                if der < n:
                    hietograma[der] = bloque
                    der += 1
            else:           # Impares (1, 3, 5...) van a la Izquierda
                if izq >= 0:
                    hietograma[izq] = bloque
                    izq -= 1
                    
        return hietograma

    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"Error en Bloques Alternos Estricto: {e}")
        return None

def _run_hms_isolated(area, tc, n_pond, 
                      df_int, df_alt, tr_cols, 
                      tipo_tormenta, tipo_cinematica, fcc=1.0, q_base=0.0):
    """Ejecuta HMS con paso de tiempo dinámico estricto y abstracción variable."""
    res_peak = {}
    res_series = {}
    
    try:
        df_i_local = df_int.copy()
        try: df_i_local.index = df_i_local.index.astype(float)
        except: pass
        
        # --- BLINDAJE MATEMÁTICO: Paso de Tiempo (dt) ---
        # HEC-HMS exige dt <= 0.133 * Tc para captura exacta del pico y balance de masa.
        dt_limite = 0.133 * tc
        pasos_estandar = [1, 2, 3, 4, 5, 6, 10, 12, 15, 20, 30]
        # Elegimos el dt estándar más grande que NO supere el límite. Si todos superan, forzamos 1 min.
        dt_calc = max([p for p in pasos_estandar if p <= max(1.0, dt_limite)] or [1])
        # ------------------------------------------------

        # --- CÁLCULO DE CINEMÁTICA (LAG FACTOR DINÁMICO) ---
        if tipo_cinematica == "fijo_06": factor_lag = 0.6
        elif tipo_cinematica == "fijo_04": factor_lag = 0.4
        else: # Dinámico por Número de Curva
            if n_pond <= 70: factor_lag = 0.6
            elif n_pond >= 90: factor_lag = 0.2
            else: factor_lag = 0.6 - ((n_pond - 70) / 20.0) * 0.4 # Interpolación lineal
            
        # Generamos el Hidrograma Unitario con el Lag ajustado
        hu = _hms_generar_hu(area, tc, dt_calc, factor_lag)
        
        if hu is not None:
            for tr in tr_cols:
                # --- GENERADOR DE TORMENTAS V10.1 ---
                if tipo_tormenta in ["scs_ii", "scs_iii"]:
                    # Búsqueda Automática de P24: Extrapolamos el TR y sacamos la lluvia máxima de 24h
                    serie_alturas = interpolar_tr(int(tr), df_alt)
                    p24 = serie_alturas.max() # La altura máxima registrada en la tabla
                    hieto_base = generar_hietograma_scs(tipo_tormenta, p24, dt_calc)
                else:
                    # Método Legacy: Bloques Alternos
                    col = next((c for c in [tr, int(tr), str(tr)] if c in df_i_local.columns), None)
                    if col is not None:
                        max_idf_time = df_i_local.index.max()
                        dur = min(max(tc * 2.5, 360), max_idf_time * 2) 
                        hieto_base = _hms_bloques_alternos(dur, dt_calc, df_i_local, col)
                    else: continue
                
                if hieto_base is None: continue
                hieto = hieto_base * fcc 
                
                # Método de Pérdidas SCS (Lambda se fijó al estándar 0.2)
                S_mm = (25400.0 / n_pond) - 254.0
                Ia_mm = 0.2 * S_mm
                # ... (El resto del código de convolución sigue igual hasta abajo)
                P_acum = np.cumsum(hieto)
                Pe_acum = np.zeros_like(P_acum)
                
                mask = P_acum > Ia_mm
                Pe_acum[mask] = ((P_acum[mask] - Ia_mm)**2) / (P_acum[mask] - Ia_mm + S_mm)
                Pe_inc = np.diff(Pe_acum, prepend=0)
                
                # Convolución
                pe_ratio = Pe_inc / 10.0 # Convertir mm a cm
                q_hidro = np.convolve(pe_ratio, hu) + q_base # Sumamos Flujo Base
                
                res_peak[f"TR_{tr}"] = round(np.max(q_hidro), 3)
                
                # --- ENSAMBLAJE COMERCIAL (MEMORIA DE CÁLCULO) ---
                # HEC-HMS produce un Q más largo que la lluvia debido al vaciado de la cuenca.
                # Rellenamos los vectores cortos con ceros para crear una tabla perfecta.
                len_q = len(q_hidro)
                t_axis_min = np.arange(len_q) * dt_calc
                
                p_inc_pad = np.pad(hieto, (0, max(0, len_q - len(hieto))), 'constant')[:len_q]
                pe_inc_pad = np.pad(Pe_inc, (0, max(0, len_q - len(Pe_inc))), 'constant')[:len_q]
                hu_pad = np.pad(hu, (0, max(0, len_q - len(hu))), 'constant')[:len_q]
                
                # --- CÁLCULO DE BALANCE HÍDRICO (HMS STYLE) ---
                precip_total_mm = np.sum(hieto)
                exceso_total_mm = np.sum(Pe_inc)
                perdidas_total_mm = precip_total_mm - exceso_total_mm
                
                # Volumen total en m3 y mm
                volumen_total_m3 = np.sum(q_hidro) * dt_calc * 60
                volumen_total_mm = volumen_total_m3 / (area * 1000.0)
                
                idx_pico = np.argmax(q_hidro)
                tiempo_pico_min = idx_pico * dt_calc
                # Formato de tiempo HH:MM
                tiempo_pico_str = f"{int(tiempo_pico_min // 60):02d}:{int(tiempo_pico_min % 60):02d}"

                summary_results = {
                    "Peak Discharge (m3/s)": round(np.max(q_hidro), 3),
                    "Precipitation Volume (mm)": round(precip_total_mm, 2),
                    "Loss Volume (mm)": round(perdidas_total_mm, 2),
                    "Excess Volume (mm)": round(exceso_total_mm, 2),
                    "Direct Runoff Volume (mm)": round(exceso_total_mm, 2),
                    "Total Runoff Volume (mm)": round(volumen_total_mm, 2),
                    "Time of Peak": tiempo_pico_str
                }

                # --- ENSAMBLAJE CON NOMBRES COMERCIALES ---
                df_memoria = pd.DataFrame({
                    "Tiempo (minutos)": t_axis_min,
                    "Precipitación Incremental (mm)": np.round(p_inc_pad, 3),
                    "Lluvia Efectiva (mm)": np.round(pe_inc_pad, 3),
                    "Ordenada del HU (m3/s)": np.round(hu_pad, 3),
                    "Caudal Directo (m3/s)": np.round(q_hidro, 3)
                })
                
                res_series[f"TR_{tr}"] = {
                    "detalle": df_memoria,
                    "resumen": summary_results
                }
                    
    except Exception as e:
        traceback.print_exc() # QA Audit
        print(f"Error HMS Sandbox: {e}")
        pass
        
    return res_peak, res_series

# ==========================================
# 3. LÓGICA PRINCIPAL (CÁLCULO)
# ==========================================

# =========================================================================
# 1. BYPASS DE LLUVIA PARA IMPORTACIÓN DE ESCENARIO REGIONAL (ARF)
# =========================================================================
def calcular_lluvias_escenario(page=None, modo_lluvia=None, session_dict=None):
    """
    [Directriz Zero-Trust & EAFP] Intercepta el motor de lluvia de Gastos.
    Si el modo activo es regional_arf, extrae los datos Read-Only del Módulo 4,
    hace una copia profunda y los asigna al escenario activo sin State Clobbering.
    Soporta tanto page.session como session_dict puro para pruebas y CLI.
    """
    try:
        session = session_dict
        if session is None and page is not None and hasattr(page, "session"):
            session = page.session
            
        if session is None:
            raise ValueError("Se requiere session_dict o page.session para calcular_lluvias_escenario.")

        # Adaptador flexible para get y set
        def _get(k, default=None):
            if hasattr(session, "get"):
                return session.get(k, default)
            elif isinstance(session, dict):
                return session.get(k, default)
            return default

        def _set(k, v):
            if hasattr(session, "set"):
                session.set(k, v)
            elif isinstance(session, dict):
                session[k] = v

        if modo_lluvia is None:
            modo_lluvia = _get("modo_lluvia_activo")
        
        if modo_lluvia == "regional_arf":
            print("|> [MOTOR GASTOS] Bypass Regional activado. Extrayendo datos del Módulo 4...")
            
            # 1. Lectura Defensiva de la Fuente de Verdad Global (Solo Lectura)
            df_i_arf = _get("arf_df_intensidad_maestra")
            df_h_arf = _get("arf_df_altura_maestra")
            df_hiet_arf = _get("arf_df_hietograma_maestro")
            
            if df_i_arf is None or df_h_arf is None:
                raise ValueError("No se encontraron matrices ARF globales. Asegúrese de haber calculado y exportado el Módulo 4.")
            
            # 2. Copia Profunda para Aislar el Escenario (Evitar State Clobbering)
            _set("df_intensidad", copy.deepcopy(df_i_arf))
            _set("df_altura", copy.deepcopy(df_h_arf))
            
            if df_hiet_arf is not None:
                _set("df_hms", copy.deepcopy(df_hiet_arf))
                
            print("|> [MOTOR GASTOS] Matrices ARF inyectadas en el Escenario Local con éxito.")
            return True, "Lluvia Regional (ARF) importada al escenario correctamente."

        return False, "Continuando con lógica clásica (Simple/Thiessen)."
    except Exception as e:
        print(f"❌ Error en calcular_lluvias_escenario: {e}")
        raise e

def calcular_coeficientes_y_gastos(
    df_configurado, df_cotas, metodo_tc="Kirpich", modo_distribuido=False, 
    estaciones_db=None, pesos_estaciones=None, df_int_global=None, df_alt_global=None,
    fcc=1.0, lambda_ia=0.2, condicion_amc="II", flujo_base=0.0,
    lista_tr=None, tipo_tormenta="bloques", tipo_cinematica="dinamico",
    aplicar_temez: bool = False, # <--- NUEVO INTERRUPTOR DE DOMINIO
    aplicar_arf: bool = False, df_intensidad_arf=None, df_altura_arf=None, page=None
):
    log = []
    res_hidrogramas = {}
    
    try:
        # Reemplazamos la lista estática por la lista del usuario
        if lista_tr and len(lista_tr) > 0:
            tr_cols = lista_tr
        else:
            tr_cols = ['2', '5', '10', '20', '50', '100', '500', '1000', '10000']
        
        # --- FASE 1: SANITIZACIÓN Y LAZY LOADING (NUEVO MOTOR DDD) ---
        def _sanitizar_lluvias(df_in):
            """
            Hidrata DataFrames desde el disco duro (Parquet) bajo demanda (JIT) para ahorrar RAM,
            y convierte el tiempo ('DURACION_MIN' o 'TR (AÑOS)') en el índice matemático estricto.
            """
            if df_in is None: return None
            
            # 0. HIDRATADOR JIT (Lazy Loading de Punteros)
            if isinstance(df_in, dict) and df_in.get("type") == "parquet":
                try: df_out = pd.read_parquet(df_in["path"])
                except Exception as e: log.append(f"Fallo al leer Parquet en disco: {e}"); return None
            elif isinstance(df_in, str) and df_in.endswith('.parquet'):
                try: df_out = pd.read_parquet(df_in)
                except: return None
            else:
                if hasattr(df_in, "empty") and df_in.empty: return None
                df_out = df_in.copy() # Flujo Legacy si ya era DataFrame
            
            # 1. Blindaje de Columna de Tiempo
            if 'DURACION_MIN' in df_out.columns:
                df_out.set_index('DURACION_MIN', inplace=True)
            elif 'TR (AÑOS)' in df_out.columns:
                df_out.set_index('TR (AÑOS)', inplace=True)
            elif 'index' in df_out.columns: 
                df_out.set_index('index', inplace=True)
                
            # 2. Blindaje de Tipos (Interpolación requiere flotantes np.float64)
            try:
                df_out.index = df_out.index.astype(np.float64)
            except Exception as e:
                log.append(f"Aviso Sanitizador: Fallo conversión de índice a float: {e}")
                
            # 3. Uniformidad EAFP: Todas las columnas de TR serán cadenas para evitar KeyError en búsquedas
            df_out.columns = [str(c) for c in df_out.columns]
            return df_out

        # =========================================================================
        # 1. BYPASS REGIONAL ARF (ADAPTER PATTERN & ZERO-TRUST)
        # =========================================================================
        modo_activo = page.session.get("modo_lluvia_activo") if page and hasattr(page, "session") else None
        es_modo_arf = bool(aplicar_arf or modo_activo == "regional_arf")
        if es_modo_arf:
            print(f"|> [MOTOR GASTOS] Iniciando cálculos. Modo ARF activo.")
            
            if page is not None and hasattr(page, "session"):
                exito, msg = calcular_lluvias_escenario(page, "regional_arf")
                print(f"|> [BYPASS ESCENARIO] {msg}")
                df_int_global = page.session.get("df_intensidad")
                df_alt_global = page.session.get("df_altura")
            elif df_intensidad_arf is not None and df_altura_arf is not None:
                df_int_global = copy.deepcopy(df_intensidad_arf)
                df_alt_global = copy.deepcopy(df_altura_arf)
            elif df_int_global is not None and df_alt_global is not None:
                df_int_global = copy.deepcopy(df_int_global)
                df_alt_global = copy.deepcopy(df_alt_global)
            else:
                raise ValueError("Se requiere el contexto de la página (page) o matrices válidas para extraer variables ARF.")
                
            if df_int_global is None or df_alt_global is None or (hasattr(df_int_global, "empty") and df_int_global.empty):
                raise ValueError("Contrato de Datos Roto: Matrices ARF ausentes. Ejecute el Módulo 4 primero.")
                
            print("|> [NÚCLEO FÍSICO] Omitiendo generación IDF local. Adoptando matrices de lluvia regionales aisladas (ARF).")
            df_int_global = _sanitizar_lluvias(df_int_global)
            df_alt_global = _sanitizar_lluvias(df_alt_global)
        else:
            # =========================================================================
            # LÓGICA CLÁSICA: Generación de curvas locales
            # =========================================================================
            print("|> [NÚCLEO FÍSICO] Ejecutando generación de lluvias clásica (Puntual/Thiessen).")
            # Sanitizamos los datos globales por defecto para el Modo Simple
            df_int_global = _sanitizar_lluvias(df_int_global)
            df_alt_global = _sanitizar_lluvias(df_alt_global)

        # 1. Coeficientes Ponderados (Usando Tablas Externas)
        # 1. Coeficientes Ponderados (Usando Interceptores Seguros)
        def _calc_pond(row):
            pcts = obtener_lista_desde_string(row['Porcentaje_terreno'], float)
            idxs_c = obtener_lista_desde_string(row['Coeficiente_C_por_Cuenca'], int)
            c_vals = {}
            
            # Matriz N sigue usando Pandas porque no depende del TR
            n_ref = pd.DataFrame(hidrologia_mx.MATRIZ_N_VALORES, index=range(1, 18))
            
            for tr in tr_cols:
                # AQUÍ ACTÚA EL ESCUDO: Usamos la función de hidrologia_mx
                val_c = sum((p/100) * hidrologia_mx.obtener_C_interpolado(i, tr) for p, i in zip(pcts, idxs_c))
                c_vals[f"C_pond_TR{tr}"] = val_c
            
            usos_n = obtener_lista_desde_string(row['indice_Uso_terreno'], int)
            grupos = obtener_lista_desde_string(row['Grupo_hidrologico'], str)
            n_val = sum((p/100)*n_ref.loc[i, g.strip().upper()] for p, i, g in zip(pcts, usos_n, grupos) if i in n_ref.index)
            return pd.Series({**c_vals, 'N_pond': n_val})
       
        df_pond = df_configurado.apply(_calc_pond, axis=1)
        df_completo = pd.concat([df_configurado, df_pond], axis=1)
        
        # 2. Geometría y Tc (Calculamos el raw, pero GUARDAMOS EL REDONDEADO)
        if 'cuenca' in df_cotas.columns: df_cotas['cuenca'] = df_cotas['cuenca'].astype(str)
        dist_cotas = {key: sub_df for key, sub_df in df_cotas.groupby('cuenca')}
        
        df_completo['Tc_aprox'] = np.nan
        df_completo['Pendiente_S'] = np.nan
        df_completo.index = df_completo.index.astype(str)
        
        for cid in df_completo.index:
            if cid in dist_cotas:
                sub = dist_cotas[cid]
                lcp = 0; den = 0
                for _, r in sub.iterrows():
                    d = abs(r['distancia2']-r['distancia1']) or 0.001
                    s = (abs(r['cota mayor']-r['cota menor'])/d) or 0.0001
                    lcp += d; den += d/math.sqrt(s)
                S = (lcp/(den or 0.001))**2
                
                # Selección de fórmula
                if metodo_tc == "Temez":
                    # CORRECCIÓN: Témez exige LCP en Kilómetros para dar horas.
                    lcp_km = lcp / 1000.0
                    tc_raw = 0.3 * (lcp_km**0.76) * (S**-0.19) * 60
                else: # Kirpich (Default/Original)
                    # Kirpich exige LCP en Metros.
                    tc_raw = 0.000325*((lcp**0.77)/(S**0.385))*60
                
                tc_exacto = max(tc_raw, 5.0) 
                
                df_completo.at[cid, 'Tc_aprox'] = tc_exacto
                df_completo.at[cid, 'Pendiente_S'] = S
            else:
                log.append(f"Aviso: Cuenca {cid} sin cotas.")

        cols_out = [f"TR_{tr}" for tr in tr_cols]
        df_racional = pd.DataFrame(0.0, index=df_completo.index, columns=cols_out)
        df_chow = pd.DataFrame(0.0, index=df_completo.index, columns=cols_out)
        df_hms_peak = pd.DataFrame(0.0, index=df_completo.index, columns=cols_out)

       # --- BUCLE DE CÁLCULO ---
        for cid, row in df_completo.iterrows():
            area = row['area']; tc = row['Tc_aprox']; n_pond_raw = row['N_pond']
            
            # Ajuste de Humedad Antecedente (AMC)
            # POR QUÉ: N_pond asume AMC-II. Si es I (seco) o III (saturado), aplicamos corrección NEH-4.
            if condicion_amc == "I":
                n_pond = (4.2 * n_pond_raw) / (10 + 0.058 * n_pond_raw) if n_pond_raw > 0 else 0
            elif condicion_amc == "III":
                n_pond = (23 * n_pond_raw) / (10 + 0.13 * n_pond_raw) if n_pond_raw > 0 else 0
            else:
                n_pond = n_pond_raw
                
            # Clamping de seguridad para N (No puede ser > 100 ni <= 0)
            n_pond = max(1.0, min(n_pond, 99.9))
            
            # Selección de Lluvia (Placeholder para Distribuido)
            # En modo simple, usamos las globales ya preparadas
            # Selección de Lluvia y Cálculo
            df_i_curr = df_int_global
            df_a_curr = df_alt_global
            
            if modo_distribuido:
                try:
                    pesos_cuenca = pesos_estaciones.get(cid, {})
                    est_validas = {eid: w for eid, w in pesos_cuenca.items() if w > 0}
                    
                    if est_validas and estaciones_db:
                        # 1. Normalización Estricta de Pesos (Conservación de Masa)
                        suma_pesos = sum(est_validas.values())
                        if suma_pesos <= 0: suma_pesos = 1.0 
                        
                        # --- EXTRACCIÓN DINÁMICA DE PERIODOS DE RETORNO (TR) ---
                        primera_est_id = list(est_validas.keys())[0]
                        df_referencia = _sanitizar_lluvias(estaciones_db.get(primera_est_id, {}).get('intensidad'))
                        periodos_retorno = df_referencia.columns.tolist() if df_referencia is not None else tr_cols
                        
                        # Diccionarios acumuladores
                        q_racional_acumulado = {f"TR_{tr}": 0.0 for tr in periodos_retorno}
                        q_chow_acumulado = {f"TR_{tr}": 0.0 for tr in periodos_retorno}
                        q_hms_peak_acumulado = {f"TR_{tr}": 0.0 for tr in periodos_retorno}
                        hms_series_acumulado = {f"TR_{tr}": None for tr in periodos_retorno}
                        
                        # 2. DEFINICIÓN DEL WORKER (AISLAMIENTO TOTAL)
                        def _worker_calcular_estacion(eid, peso_bruto):
                            peso_norm = peso_bruto / suma_pesos
                            datos_est = estaciones_db.get(eid, {})
                            
                            df_est_int = _sanitizar_lluvias(datos_est.get('intensidad'))
                            df_est_alt = _sanitizar_lluvias(datos_est.get('altura'))
                            
                            if df_est_int is None or df_est_alt is None: return None
                                
                            resultados_est = {
                                'peso': peso_norm, 'racional': {}, 'chow': {}, 
                                'hms_peak': {}, 'hms_series': {}
                            }
                            
                            # A) HEC-HMS (Cálculo Completo Aislado por Estación)
                            if pd.notna(tc) and n_pond > 0:
                                pk_hms, ser_hms = _run_hms_isolated(
                                    area, tc, n_pond, df_est_int, df_est_alt, periodos_retorno, 
                                    tipo_tormenta, tipo_cinematica, fcc, flujo_base
                                )
                                resultados_est['hms_peak'] = pk_hms
                                resultados_est['hms_series'] = ser_hms

                            for tr in periodos_retorno:
                                tr_key = f"TR_{tr}"
                                # B) MÉTODO RACIONAL (Con Factor Areal de Témez)
                                try:
                                    tiempos = df_est_int.index.values
                                    intensidades = df_est_int[str(tr)].values
                                    f_int_log = interpolate.interp1d(np.log(tiempos), np.log(intensidades), fill_value="extrapolate")
                                    
                                    tc_eval = max(min(tc, tiempos.max()), tiempos.min())
                                    I_exacta = np.exp(f_int_log(np.log(tc_eval))) * fcc
                                    
                                    if aplicar_temez and not es_modo_arf and area > 1.0: # Reducción Areal de Témez
                                        factor_areal = max(1.0 - (np.log10(area) / 15.0), 0.5)
                                        I_exacta *= factor_areal
                                        
                                    C_pond = row[f"C_pond_TR{tr}"]
                                    resultados_est['racional'][tr_key] = 0.278 * C_pond * I_exacta * area
                                except Exception:
                                    resultados_est['racional'][tr_key] = 0.0

                                # C) MÉTODO DE CHOW (Vectorizado Estricto)
                                try:
                                    L = float(row['LCP']) if pd.notna(row['LCP']) else 0
                                    S_porcentaje = max(min(row['Pendiente_S'] * 100.0, 50.0), 0.001)
                                    
                                    if L > 0:
                                        t_retraso = (0.00505 * (L / (S_porcentaje**0.5))**0.64)
                                        tr_min_ser = pd.Series(df_est_alt.index.values)
                                        ratio = (tr_min_ser / 60.0) / t_retraso
                                        
                                        Z_vals = np.select(
                                            [ratio > 2, (ratio >= 0.4) & (ratio <= 2)], 
                                            [1.0, 1.89 * (ratio**0.23) - 1.23], default=0.73 * (ratio**0.97)
                                        )
                                        Z_vals = np.maximum(Z_vals, 0.0)
                                        
                                        P_col_mm = df_est_alt[str(tr)].values * fcc
                                        S_mm = (25400.0 / n_pond) - 254.0
                                        Ia_cm = (lambda_ia * S_mm) / 10.0
                                        P_cm = P_col_mm / 10.0
                                        
                                        P_efectiva = np.maximum(P_cm - Ia_cm, 0.0)
                                        C_Ac_Z = 2.78 * area * Z_vals
                                        
                                        num = (P_efectiva**2) * C_Ac_Z
                                        den = P_cm + (203.2 / n_pond) - 2.032
                                        
                                        with np.errstate(divide='ignore', invalid='ignore'):
                                            res_vector = (num / den) / (tr_min_ser / 60.0)
                                        
                                        resultados_est['chow'][tr_key] = np.max(np.nan_to_num(res_vector, nan=0.0)) + flujo_base
                                except Exception:
                                    resultados_est['chow'][tr_key] = 0.0

                            return resultados_est

                        # 3. LANZAMIENTO DEL POOL CONCURRENTE
                        hilos_maximos = min(len(est_validas), os.cpu_count() or 4)
                        with concurrent.futures.ThreadPoolExecutor(max_workers=hilos_maximos) as executor:
                            futuros = {executor.submit(_worker_calcular_estacion, eid, w): eid for eid, w in est_validas.items()}
                            
                            for futuro in concurrent.futures.as_completed(futuros):
                                eid = futuros[futuro]
                                try:
                                    res = futuro.result()
                                    if res:
                                        peso = res['peso']
                                        # 4. FASE REDUCE: Ponderación de Gastos Pico
                                        for tr in periodos_retorno:
                                            tr_k = f"TR_{tr}"
                                            q_racional_acumulado[tr_k] += res['racional'].get(tr_k, 0.0) * peso
                                            q_chow_acumulado[tr_k] += res['chow'].get(tr_k, 0.0) * peso
                                            q_hms_peak_acumulado[tr_k] += res['hms_peak'].get(tr_k, 0.0) * peso
                                            
                                            # Consolidación del Hidrograma (Suma Ponderada de Vectores)
                                            if tr_k in res['hms_series']:
                                                datos_hms = res['hms_series'][tr_k]
                                                if hms_series_acumulado[tr_k] is None:
                                                    hms_series_acumulado[tr_k] = {
                                                        "detalle": datos_hms["detalle"].copy(),
                                                        "resumen": datos_hms["resumen"].copy()
                                                    }
                                                    # Aplicamos el peso al primer vector de caudal directo
                                                    hms_series_acumulado[tr_k]["detalle"]["Caudal Directo (m3/s)"] *= peso
                                                else:
                                                    # Superposición Ponderada (Laminación Espacial)
                                                    len_actual = len(hms_series_acumulado[tr_k]["detalle"])
                                                    len_nuevo = len(datos_hms["detalle"])
                                                    
                                                    # Si hay diferencias de longitud por el dt, abrochamos al menor
                                                    min_len = min(len_actual, len_nuevo)
                                                    vector_nuevo_ponderado = datos_hms["detalle"]["Caudal Directo (m3/s)"].iloc[:min_len] * peso
                                                    hms_series_acumulado[tr_k]["detalle"]["Caudal Directo (m3/s)"].iloc[:min_len] += vector_nuevo_ponderado
                                                    
                                                    # Actualizar resumen de volumen (Suma ponderada)
                                                    for key_res in ["Precipitation Volume (mm)", "Loss Volume (mm)", "Excess Volume (mm)", "Total Runoff Volume (mm)"]:
                                                        hms_series_acumulado[tr_k]["resumen"][key_res] += datos_hms["resumen"].get(key_res, 0.0) * peso

                                except Exception as exc:
                                    log.append(f"Error en hilo de estación {eid}: {exc}")

                        # 5. ASIGNACIÓN GLOBAL Y EVASIÓN DE CÁLCULO SIMPLE
                        for tr in periodos_retorno:
                            tr_k = f"TR_{tr}"
                            df_racional.at[cid, tr_k] = round(q_racional_acumulado[tr_k], 4)
                            df_chow.at[cid, tr_k] = round(q_chow_acumulado[tr_k], 4)
                            df_hms_peak.at[cid, tr_k] = round(q_hms_peak_acumulado[tr_k], 4)
                            if hms_series_acumulado[tr_k] is not None:
                                res_hidrogramas[f"{cid}_{tr_k}"] = hms_series_acumulado[tr_k]
                        
                        # Al terminar distribuido, saltamos la iteración para no ejecutar el modo simple abajo
                        continue
                        
                except Exception as e:
                    log.append(f"Error crítico en Modo Distribuido para cuenca {cid}: {e}")
                    traceback.print_exc()
            
            if df_i_curr is None or df_a_curr is None: 
                log.append(f"Omisión: Cuenca {cid} no tiene lluvias válidas asignadas.")
                continue

            # === MÉTODO RACIONAL (BLINDADO) ===
            # Extraemos la Intensidad (I) mediante interpolación Log-Log exacta, 
            # eliminando la dependencia de índices discretos en la tabla CSV.
            if pd.notna(tc) and tc > 0:
                try:
                    x_idf = df_i_curr.index.astype(float).values
                    
                    for tr in tr_cols:
                        col_name = int(tr) if int(tr) in df_i_curr.columns else str(tr)
                        if col_name in df_i_curr.columns:
                            y_idf = df_i_curr[col_name].astype(float).values
                            
                            # 1. Crear función de interpolación continua (Curva IDF exacta)
                            f_int_log = interpolate.interp1d(
                                np.log(x_idf), 
                                np.log(y_idf), 
                                kind='linear', 
                                fill_value="extrapolate"
                            )
                            
                            # 2. Límite de extrapolación defensivo (ESTRICTO)
                            # Se prohíbe extrapolar más allá de los datos medidos (SCT/CONAGUA)
                            tc_eval = max(min(tc, x_idf.max()), x_idf.min())
                            
                            # Aviso legal en el log si la cuenca excede la lluvia medida
                            if tc > x_idf.max():
                                log.append(f"Aviso Racional {cid}: Tc ({round(tc,1)} min) excede la curva IDF máxima ({x_idf.max()} min). Se topeó la intensidad.")
                            
                            # 3. Intensidad Exacta para la cuenca
                            I_exacta = np.exp(f_int_log(np.log(tc_eval)))
                            
                            # --- CORRECCIÓN FCC: Aplicamos Factor de Cambio Climático a la lluvia racional ---
                            I_exacta = I_exacta * fcc
                            
                            # === EVALUACIÓN DEL FACTOR DE REDUCCIÓN AREAL (TÉMEZ) ===
                            # Regla: Solo aplica si el usuario lo activó explícitamente Y el modelo NO viene reducido por ARF
                            if aplicar_temez and not es_modo_arf and area > 1.0:
                                factor_areal = max(1.0 - (np.log10(area) / 15.0), 0.5)
                                if str(tr) == str(tr_cols[0]):
                                    log.append(f"Cuenca {cid}: Reducción Témez activa (Área = {area:.2f} km², KA = {factor_areal:.3f})")
                            else:
                                factor_areal = 1.0
                                if str(tr) == str(tr_cols[0]):
                                    if es_modo_arf:
                                        log.append(f"Cuenca {cid}: Reducción Témez omitida (Lluvia ya reducida por Módulo ARF).")
                                    elif not aplicar_temez:
                                        log.append(f"Cuenca {cid}: Reducción Témez desactivada por usuario (Gasto pleno).")

                            I_exacta = I_exacta * factor_areal

                            # 4. Cálculo de Caudal (Ecuación Universal: Q = 0.278 * C * I * A)
                            C = row[f"C_pond_TR{tr}"] 
                            df_racional.at[cid, f"TR_{tr}"] = round(0.278 * C * I_exacta * area, 4)
                            
                except Exception as e:
                    log.append(f"Error Racional en cuenca {cid}: {e}")
            
            
            # === MÉ MÉTODO CHOW (BLINDADO Y CORREGIDO) ===
            L = float(row['LCP']) if pd.notna(row['LCP']) else 0
            
            # 1. BLINDAJE DE MAGNITUD FÍSICA:
            # Chow exige Pendiente en (%). Prevenimos divisiones por cero o pendientes suicidas > 50%
            S_decimal = row['Pendiente_S']
            S_porcentaje = max(min(S_decimal * 100.0, 50.0), 0.001)
            
            if L > 0 and S_porcentaje > 0 and pd.notna(n_pond):
                try:
                    Tiempo_retraso = (0.00505 * (L / (S_porcentaje**0.5))**0.64)
                    TR_min_series = pd.Series(df_a_curr.index.values)
                    ratio = (TR_min_series / 60.0) / Tiempo_retraso
                    
                    Z_values = np.select(
                        [ratio > 2, (ratio >= 0.4) & (ratio <= 2)], 
                        [1.0, 1.89 * (ratio**0.23) - 1.23], 
                        default=0.73 * (ratio**0.97)
                    )
                    Z_values = np.maximum(Z_values, 0.0)
                    
                    for tr in tr_cols:
                        col_name = next((c for c in [tr, int(tr), str(tr)] if c in df_a_curr.columns), None)
                        if col_name is not None:
                            # Aplicamos Factor de Cambio Climático a la lluvia
                            P_col_mm = df_a_curr[col_name].values * fcc
                            
                            # Ecuaciones de lluvia efectiva del SCS en cm con Lambda Dinámico
                            S_mm = (25400.0 / n_pond) - 254.0
                            Ia_cm = (lambda_ia * S_mm) / 10.0
                            P_cm = P_col_mm / 10.0
                            
                            # Filtro estricto: Solo hay escurrimiento si la lluvia supera la abstracción inicial
                            P_efectiva = np.maximum(P_cm - Ia_cm, 0.0)
                            
                            C1 = 50.8 / n_pond; C2 = 0.508; C3 = 203.2 / n_pond; C4 = 2.032
                            C_Ac_Z = 2.78 * area * Z_values
                            
                            num = (P_efectiva**2) * C_Ac_Z
                            den = P_cm + C3 - C4
                            
                            with np.errstate(divide='ignore', invalid='ignore'):
                                res_vector = (num / den) / (TR_min_series / 60.0)
                            
                            max_q = np.max(np.nan_to_num(res_vector, nan=0.0)) + flujo_base
                            df_chow.at[cid, f"TR_{tr}"] = round(max_q, 3)
                except Exception as e:
                    traceback.print_exc()
                    log.append(f"Error Chow {cid}: {e}")

            # === MÉTODO HMS (AISLADO) ===
            # Ejecuta solo si hay datos, y si falla no detiene lo demás
            if pd.notna(tc) and n_pond > 0:
                try:
                    # Usamos las variables correctas del bucle actual: area, tc, n_pond, df_i_curr, df_a_curr
                    res_peak_hms, res_series_hms = _run_hms_isolated(
                        area, tc, n_pond, df_i_curr, df_a_curr, tr_cols, 
                        tipo_tormenta, tipo_cinematica, fcc, flujo_base
                    )
                    
                    # Verificamos los resultados devueltos correctos
                    if res_peak_hms:
                        for k, v in res_peak_hms.items(): 
                            df_hms_peak.at[cid, k] = v
                        for k, v in res_series_hms.items(): 
                            res_hidrogramas[f"{cid}_{k}"] = v
                except Exception as e:
                    log.append(f"Error HMS aislando cuenca {cid}: {e}")

        log.append("Cálculos finalizados.")
        return df_racional, df_chow, df_hms_peak, df_completo, res_hidrogramas, "\n".join(log)

    except Exception as e:
        return None, None, None, None, None, f"Error General: {traceback.format_exc()}"

def generar_graficos_comparativos(df_racional, df_chow, df_hms_calc, df_hms_ext):
    graficos = []
    
    # --- 1. ESCUDO PROTECTOR PARA DATOS VACÍOS (NUEVO) ---
    if df_racional is None or df_chow is None:
        return graficos # Si no hay datos base, devolvemos lista vacía
        
    if df_hms_calc is None:
        df_hms_calc = pd.DataFrame() # Creamos DataFrame vacío en lugar de None
    if df_hms_ext is None:
        df_hms_ext = pd.DataFrame()
    # ----------------------------------------------------

    def norm_idx(idx): return str(idx).strip().replace('.0', '')
    
    df_racional.index = df_racional.index.map(norm_idx)
    df_chow.index = df_chow.index.map(norm_idx)
    
    # --- 2. SOLO MAPEAMOS SI HAY DATOS HMS ---
    if not df_hms_calc.empty:
        df_hms_calc.index = df_hms_calc.index.map(norm_idx)
    
    hms_ext_map = {}
    if not df_hms_ext.empty:
        df_hms_ext.index = df_hms_ext.index.map(norm_idx)
        for c in df_hms_ext.columns:
            if 'TR' in str(c): hms_ext_map[f"TR_{''.join(filter(str.isdigit, str(c)))}"] = c

    trs = ['2', '5', '10', '20', '50', '100', '500', '1000', '10000']
    
    with plt.style.context('default'):
        for tr in trs:
            col = f"TR_{tr}"
            if col in df_racional.columns:
                idx = df_racional.index.tolist()
                
                v_r = df_racional[col].fillna(0).tolist()
                v_c = df_chow[col].fillna(0).tolist() if col in df_chow.columns else [0]*len(idx)
                v_h = df_hms_calc[col].fillna(0).tolist() if col in df_hms_calc.columns else [0]*len(idx)
                
                v_ext = []
                if df_hms_ext is not None:
                    ext_c = hms_ext_map.get(col)
                    if ext_c and ext_c in df_hms_ext.columns:
                        for i in idx:
                            v = df_hms_ext.loc[i, ext_c] if i in df_hms_ext.index else 0
                            v_ext.append(float(v) if pd.notna(v) else 0)
                    else: v_ext = [0]*len(idx)
                else: v_ext = [0]*len(idx)

                fig, ax = plt.subplots(figsize=(10, 6))
                x = np.arange(len(idx)); w = 0.2
                
                ax.bar(x - 1.5*w, v_r, w, label='Racional', color="#00ccff", edgecolor='black', linewidth=0.5)
                ax.bar(x - 0.5*w, v_c, w, label='Chow (SCT)', color="#1c75fa", edgecolor='black', linewidth=0.5)
                ax.bar(x + 0.5*w, v_h, w, label='HMS (Calc)', color="#3600c9", edgecolor='black', linewidth=0.5)
                if any(v_ext):
                    ax.bar(x + 1.5*w, v_ext, w, label='HMS (Ext)', color='#2ca02c', edgecolor='black', linewidth=0.5)
                
                ax.set_title(f'Comparación Gasto Pico - TR {tr} Años', fontweight='bold')
                ax.set_ylabel('Gasto Máximo ($m^3/s$)')
                ax.set_xticks(x); ax.set_xticklabels(idx, rotation=45)
                ax.legend(); ax.grid(True, axis='y', linestyle='--')
                plt.tight_layout()
                
                buf = io.BytesIO()
                fig.savefig(buf, format='png', dpi=100)
                plt.close(fig)
                graficos.append((tr, base64.b64encode(buf.getvalue()).decode('utf-8')))
    
    return graficos

def generar_graficos_por_metodo(df_racional, df_chow, df_hms_calc):
    """
    Genera gráficos de barras agrupadas por CUENCA para cada MÉTODO.
    Muestra la evolución del Gasto Pico a través de todos los TRs solicitados.
    """
    graficos = []
    
    def plot_metodo(df, titulo, color_map):
        if df is None or df.empty: return None
        
        # Filtramos solo las columnas de Periodos de Retorno
        trs = [c for c in df.columns if "TR_" in str(c)]
        cuencas = df.index.astype(str).tolist()
        
        if not trs or not cuencas: return None

        with plt.style.context('default'):
            fig, ax = plt.subplots(figsize=(12, 6)) # Un poco más ancho para albergar más barras
            
            x = np.arange(len(cuencas))
            # Ajuste de grosor de barras dinámico
            width = 0.8 / len(trs) if len(trs) > 0 else 0.8
            
            # Paleta de colores ascendente (de claro a oscuro según el TR)
            cmap = plt.get_cmap(color_map)
            colores = [cmap(i) for i in np.linspace(0.4, 1.0, len(trs))]
            
            for i, tr in enumerate(trs):
                valores = df[tr].fillna(0).tolist()
                # Cálculo geométrico para centrar las barras agrupadas
                offset = (i - len(trs)/2) * width + width/2
                
                tr_label = str(tr).replace('TR_', '') + " Años"
                ax.bar(x + offset, valores, width, label=tr_label, color=colores[i], edgecolor='black', linewidth=0.5)
            
            ax.set_title(f'Análisis Multicuenca: Evolución de Gasto Pico ({titulo})', fontweight='bold', fontsize=14)
            ax.set_ylabel('Gasto Máximo de Diseño ($m^3/s$)')
            ax.set_xticks(x)
            ax.set_xticklabels(cuencas, rotation=0, fontweight='bold')
            
            # Leyenda por fuera del gráfico para no tapar barras
            ax.legend(title="Periodo de Retorno", bbox_to_anchor=(1.01, 1), loc='upper left')
            ax.grid(True, axis='y', linestyle='--', alpha=0.7)
            plt.tight_layout()
            
            buf = io.BytesIO()
            fig.savefig(buf, format='png', dpi=100)
            plt.close(fig)
            return base64.b64encode(buf.getvalue()).decode('utf-8')

    try:
        # Usamos paletas de colores distintas para cada método
        img_r = plot_metodo(df_racional, "Método Racional", "Blues")
        if img_r: graficos.append(("Racional", img_r))
        
        img_c = plot_metodo(df_chow, "Método de Ven Te Chow", "Greens")
        if img_c: graficos.append(("Chow", img_c))
        
        img_h = plot_metodo(df_hms_calc, "Método HEC-HMS (SCS)", "Purples")
        if img_h: graficos.append(("HEC-HMS", img_h))
    except Exception as e:
        traceback.print_exc()
        
    return graficos

def generar_grafico_hidrograma(t, Q, titulo):
    with plt.style.context('default'):
        fig, ax = plt.subplots(figsize=(10, 6))
        ax.plot(t, Q, color='blue', linewidth=2)
        ax.set_title(titulo, fontweight="bold")
        ax.set_xlabel("Tiempo (hr)"); ax.set_ylabel("Q (m³/s)")
        ax.grid(True, linestyle="--")
        ax.fill_between(t, Q, color='blue', alpha=0.1)
        
        buf = io.BytesIO()
        fig.savefig(buf, format='png', dpi=100)
        plt.close(fig)
        return base64.b64encode(buf.getvalue()).decode('utf-8')
    
# ==========================================
# 4. EXPORTACIÓN Y REPORTES COMERCIALES
# ==========================================

def exportar_a_hec_ras(res_hidrogramas, directorio_salida, fecha_inicio="01Jan2025"):
    """
    Toma los hidrogramas calculados y genera archivos .CSV compatibles 
    con la importación de 'Unsteady Flow Data' en HEC-RAS.
    """
    log_exportacion = []
    
    if not res_hidrogramas:
        return False, "No hay hidrogramas en memoria para exportar."
        
    try:
        # Asegurar que el directorio exista
        if not os.path.exists(directorio_salida):
            os.makedirs(directorio_salida)
            
        base_date = datetime.strptime(fecha_inicio, "%d%b%Y")
        archivos_generados = 0
        
        for key, data in res_hidrogramas.items():
            # key suele ser "C1_TR_100"
            t_axis_horas, q_hidro = data
            
            nombre_archivo = os.path.join(directorio_salida, f"HECRAS_{key}.csv")
            
            with open(nombre_archivo, 'w', encoding='utf-8') as f:
                # Cabeceras estándar requeridas por HEC-RAS y estándares de modelado
                f.write("Date,Time,Flow\n")
                
                for t_hr, q in zip(t_axis_horas, q_hidro):
                    # Convertir el tiempo en horas a un objeto datetime
                    minutos_totales = int(t_hr * 60)
                    tiempo_actual = base_date + timedelta(minutes=minutos_totales)
                    
                    # Formato estricto: DDMMMYYYY, HH:MM, Caudal
                    date_str = tiempo_actual.strftime("%d%b%Y").upper()
                    time_str = tiempo_actual.strftime("%H:%M")
                    
                    # Evitar notación científica y asegurar 3 decimales
                    q_str = f"{max(0.0, q):.3f}" 
                    
                    f.write(f"{date_str},{time_str},{q_str}\n")
            
            archivos_generados += 1
            
        log_exportacion.append(f"✅ Se exportaron {archivos_generados} hidrogramas para HEC-RAS en: {directorio_salida}")
        return True, "\n".join(log_exportacion)
        
    except Exception as e:
        traceback.print_exc()
        return False, f"Error exportando a HEC-RAS: {str(e)}"
    
# ==========================================
# MOTOR DE EXPORTACIÓN A MICROSOFT EXCEL
# ==========================================
def exportar_resultados_excel(ruta_archivo, df_vars, df_racional, df_chow, df_hms, res_hydros):
    """
    Consolida todos los DataFrames de la sesión en un solo archivo Excel (.xlsx),
    asignando cada tabla y memoria de cálculo a una pestaña independiente.
    """
    import re
    import os
    
    if not ruta_archivo.endswith('.xlsx'):
        ruta_archivo += '.xlsx'
        
    try:
        # Usamos el motor por defecto de Pandas (usualmente openpyxl)
        with pd.ExcelWriter(ruta_archivo, engine='openpyxl') as writer:
            
            # 1. Parámetros Físicos
            if df_vars is not None and not df_vars.empty:
                df_vars.to_excel(writer, sheet_name='Variables_Fisicas')
            
            # 2. Gastos Pico
            if df_racional is not None and not df_racional.empty:
                df_racional.to_excel(writer, sheet_name='Q_Pico_Racional')
            if df_chow is not None and not df_chow.empty:
                df_chow.to_excel(writer, sheet_name='Q_Pico_Chow')
            if df_hms is not None and not df_hms.empty:
                df_hms.to_excel(writer, sheet_name='Q_Pico_HMS')
                
            # 3. Memorias de Tránsito (Hidrogramas y Volúmenes)
            if res_hydros:
                for cuenca_tr, data in res_hydros.items():
                    # Excel prohíbe caracteres especiales y nombres > 31 caracteres
                    hoja_nombre = re.sub(r'[\\/*?:"<>|]', "", str(cuenca_tr))[:31]
                    
                    # Extraer el DataFrame de la convolución
                    if isinstance(data, dict) and "detalle" in data:
                        df_det = data["detalle"]
                        df_det.to_excel(writer, sheet_name=hoja_nombre, index=False)
                        
                        # Inyectar el resumen de volumen debajo del hidrograma
                        resumen = data.get("resumen", {})
                        if resumen:
                            df_resumen = pd.DataFrame([resumen])
                            # Lo escribimos 2 filas por debajo del final del hidrograma
                            start_row = len(df_det) + 2
                            df_resumen.to_excel(writer, sheet_name=hoja_nombre, startrow=start_row, index=False)
                            
                    elif isinstance(data, pd.DataFrame):
                        data.to_excel(writer, sheet_name=hoja_nombre, index=False)
                        
        return True, f"✅ Tablas exportadas exitosamente a: {os.path.basename(ruta_archivo)}"
        
    except Exception as e:
        import traceback
        traceback.print_exc()
        return False, f"❌ Error al exportar a Excel: {str(e)}"