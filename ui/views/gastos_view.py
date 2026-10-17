import flet as ft
import pandas as pd
import base64
import traceback
import io
import asyncio
import threading
import copy
import numpy as np
from core import generador_latex
import os

# --- ARQUITECTURA HEXAGONAL: Importación desde el Core ---
from core import gastos_logic as gastos
from infrastructure.project_manager import project_manager_instance
from core.modelos_espaciales import SubcuencaSchema, UsoSueloSchema, CaucesSchema
from core.analisis_espacial import MotorEspacial

def build_gastos_view(page: ft.Page):
    
    # ==========================================
    # 1. INICIALIZACIÓN Y GESTOR DE ESCENARIOS
    # ==========================================
    
    # Las llaves que pertenecen exclusivamente a un escenario
    KEYS_ESCENARIO = [
        "datos_cuencas_config", "df_cuencas_base", "df_cotas", "df_hms", 
        "df_intensidad", "df_altura", "res_racional", "res_chow", "df_variables", 
        "estaciones_db", "pesos_estaciones", "station_counter", "target_station_id",
        "res_hms_peak", "res_hidrogramas", "modo_lluvia_activo", "aplicar_temez"
    ]
    
    # Inicialización de la Base de Datos de Escenarios
    if not page.session.get("db_escenarios"):
        # Si no hay escenarios, creamos el "Escenario Base" por defecto
        page.session.set("db_escenarios", {"Escenario Base": {k: None for k in KEYS_ESCENARIO}})
        page.session.set("escenario_activo", "Escenario Base")
    
    # Variables protectoras para el inicio
    for k in KEYS_ESCENARIO: 
        if page.session.get(k) is None: 
            if k in ["datos_cuencas_config", "estaciones_db", "pesos_estaciones", "res_hidrogramas"]:
                page.session.set(k, {})
            elif k == "station_counter":
                page.session.set(k, 1)
            elif k == "modo_lluvia_activo":
                page.session.set(k, "simple")
            else:
                page.session.set(k, None)

    # --- MOTOR DE ESCENARIOS (Logica Swap) ---
    
    # 1. EL CLONADOR INTELIGENTE (Cura para el Memory Leak)
    def clonar_dato_seguro(obj):
        """Clona objetos evitando la fragmentación de memoria en Pandas."""
        if isinstance(obj, pd.DataFrame):
            return obj.copy(deep=True) # Usa el motor en C++ de Pandas
        elif isinstance(obj, dict):
            return {k: clonar_dato_seguro(v) for k, v in obj.items()}
        elif isinstance(obj, list):
            return [clonar_dato_seguro(v) for v in obj]
        else:
            return copy.deepcopy(obj) # Fallback para números, strings y booleanos

    # --- MOTOR DE ESCENARIOS (Logica Swap) ---
    def guardar_escenario_actual():
        activo = page.session.get("escenario_activo")
        db = page.session.get("db_escenarios")
        snapshot = {}
        
        # QA Memory Management: Evita colapso de RAM (OOM) al usar DataFrames pesados
        for k in KEYS_ESCENARIO:
            val = page.session.get(k)
            # No copiamos los resultados pesados que pueden recalcularse o ensucian la memoria
            if k in ["res_racional", "res_chow", "res_hms_peak", "res_hidrogramas", "df_variables"]:
                snapshot[k] = val
            elif isinstance(val, pd.DataFrame):
                snapshot[k] = val.copy(deep=True) # Copiado vectorizado nativo
            elif val is None:
                snapshot[k] = None
            else:
                try:
                    snapshot[k] = copy.deepcopy(val) # Solo para diccionarios y tipos nativos
                except Exception:
                    snapshot[k] = val # Fallback seguro
                    
        db[activo] = snapshot
        page.session.set("db_escenarios", db)

    def cargar_escenario(nombre_escenario):
        db = page.session.get("db_escenarios")
        if nombre_escenario in db:
            page.session.set("escenario_activo", nombre_escenario)
            data = db[nombre_escenario]
            # Desempacamos el snapshot
            for k in KEYS_ESCENARIO:
                val = data.get(k)
                if val is None and k in ["datos_cuencas_config", "estaciones_db", "pesos_estaciones", "res_hidrogramas"]: val = {}
                elif val is None and k == "station_counter": val = 1
                page.session.set(k, val)
            
            # Rehidratar toda la interfaz
            if 'sw_aplicar_temez' in locals():
                sw_aplicar_temez.value = bool(page.session.get("aplicar_temez") or False)
            rg_modo_calculo.value = page.session.get("modo_lluvia_activo") or "simple"
            cambiar_modo_lluvia(None)
            render_manual_tables()
            validar_nombres_cuencas()
            upd_tbl()
            render_estaciones()
            
            # Recuperar visualizaciones si hay resultados
            if page.session.get("res_racional") is not None:
                construir_visualizacion_resultados(
                    page.session.get("res_racional"), page.session.get("res_chow"),
                    page.session.get("res_hms_peak"), page.session.get("res_hidrogramas")
                )
                tabs_res.visible = True
            else:
                tabs_res.visible = False
            
            page.snack_bar = ft.SnackBar(ft.Text(f"🔄 Escenario cambiado a: {nombre_escenario}"), bgcolor="blue", open=True)
            page.update()

    # ==========================================
    # 2. CONTROLES UI Y ESTADOS
    # ==========================================
    st_int = ft.Text("No cargado", color="orange", size=12)
    st_alt = ft.Text("No cargado", color="orange", size=12) 
    st_hms_simple = ft.Text("No cargado (Opcional)", color="grey", size=12)
    
    validation_msg = ft.Text("", size=12, weight="bold")
    log_txt = ft.Text("Listo.", color="grey")
    
    col_estaciones_list = ft.Column(spacing=10)
    tabla_pesos = ft.DataTable(columns=[ft.DataColumn(ft.Text("ID Cuenca"))], rows=[], border=ft.border.all(1, "grey"))
    tabla_cuencas_suelos = ft.DataTable(columns=[ft.DataColumn(ft.Text(t)) for t in ["ID","Área","LCP","Estado","Editar"]], rows=[])
    
    btn_calc = ft.ElevatedButton("CALCULAR", icon=ft.Icons.CALCULATE, disabled=True, style=ft.ButtonStyle(color="#00ff41"))
    btn_detener = ft.ElevatedButton("DETENER CÁLCULO", icon=ft.Icons.STOP, visible=False, style=ft.ButtonStyle(color="white", bgcolor="#cc0000"), on_click=lambda _: gastos.señal_abortar_gastos.set())
    tabs_res = ft.Tabs(selected_index=0, visible=False)
    
    # =========================================================================
    # RESTRUCTURACIÓN DEL ENTORNO PLUVIAL Y CONTROL DE ESCALA - FLET 0.28+
    # =========================================================================
    rg_modo_calculo = ft.RadioGroup(
        content=ft.Column([
            ft.Radio(value="simple", label="Modo Simple (Global Pluvial puntual)", fill_color="#00ff41"),
            ft.Radio(value="dist", label="Modo Distribuido (Polígonos de Thiessen)", fill_color="#00ff41"),
            # Nuevas opciones de la Directiva de Diseño v2.0
            ft.Radio(value="regional_arf", label="Cuenca Regional Active (Modelo ARF)", fill_color="#00ff41", disabled=True),
            ft.Radio(value="multicuenca_arf", label="Multicuenca Regional (ARF - Placeholder)", fill_color="#00ff41", disabled=True)
        ]),
        value="simple"
    )
    
    # Contenedor dinámico que renderizará la tabla proveniente de ARF
    contenedor_tablas_regional = ft.Container(visible=False, padding=10)
    rg_metodo_tc = ft.RadioGroup(content=ft.Row([ft.Radio(value="Kirpich", label="Kirpich (Urbano/Canal)", fill_color="#00ff41"), ft.Radio(value="Temez", label="Témez (Rural/SCT)", fill_color="#00ff41")]), value="Kirpich")

    # NUEVO: Mensaje de error específico para la Matriz de Thiessen
    msg_error_thiessen = ft.Text("", color="#cc0000", size=12, weight="bold")
    
    # =======================================================
    # --- NUEVOS PARÁMETROS COMERCIALES (FASE V10.1) ---
    # =======================================================
    
    inp_tr_lista = ft.TextField(
        label="Periodos de Retorno (TR)", 
        value="2, 5, 10, 20, 50, 100, 500, 1000", 
        width=300, 
        border_color="#00ff41", focused_border_color="#00ff41",
        tooltip="Ingresa los TR deseados separados por coma."
    )
    
    # 1. NUEVO: Selector de Método de Tormenta
    dd_tormenta = ft.Dropdown(
        label="Distribución de la Tormenta", width=250, value="bloques",
        options=[
            ft.dropdown.Option("bloques", "Bloques Alternos (IDF)"), 
            ft.dropdown.Option("scs_ii", "Tormenta SCS Tipo II (24h)"), 
            ft.dropdown.Option("scs_iii", "Tormenta SCS Tipo III (24h)")
        ],
        tooltip="SCS Tipo II/III buscan automáticamente la P24. Bloques usa la curva IDF completa."
    )

    # 2. NUEVO: Selector de Cinemática (Lag Time)
    dd_cinematica = ft.Dropdown(
        label="Cinemática (Lag Time)", width=250, value="dinamico",
        options=[
            ft.dropdown.Option("fijo_06", "Fijo Natural (0.6 * Tc)"), 
            ft.dropdown.Option("fijo_04", "Fijo Urbano (0.4 * Tc)"), 
            ft.dropdown.Option("dinamico", "Dinámico Automático (Por CN)")
        ],
        tooltip="Dinámico ajustará el factor entre 0.2 y 0.6 dependiendo de qué tan urbanizada esté la cuenca."
    )
    
    inp_fcc = ft.TextField(label="Cambio Climático (FCC)", value="1.0", width=180)
    inp_qbase = ft.TextField(label="Flujo Base (m³/s)", value="0.0", width=160)
    dd_amc = ft.Dropdown(
        label="Humedad Antecedente", width=180, value="II",
        options=[
            ft.dropdown.Option("I", "AMC I (Seco)"), 
            ft.dropdown.Option("II", "AMC II (Medio)"), 
            ft.dropdown.Option("III", "AMC III (Saturado)")
        ]
    )

    banner_advertencia = ft.Container(
        content=ft.Row([
            ft.Icon(ft.Icons.WARNING_AMBER_ROUNDED, color="#ffffff"),
            ft.Text("", color="#ffffff", size=12, weight="bold")
        ], alignment=ft.MainAxisAlignment.START),
        bgcolor="#ff9900", padding=ft.padding.all(12), border_radius=ft.border_radius.all(6),
        margin=ft.margin.only(bottom=10), visible=False
    )

    # 3. Ensamblaje del nuevo Panel y Banner de Advertencia ARF
    banner_alerta_arf = ft.Container(
        content=ft.Row([
            ft.Icon(ft.Icons.INFO_OUTLINE, color="orange"),
            ft.Text("Análisis ACF Desactivado: El área de la cuenca es menor a 25 km². No se requiere reducción areal por normatividad de la OMM.", color="orange", weight="bold")
        ]),
        bgcolor="#331a00", padding=10, border=ft.border.all(1, "orange"), border_radius=8, margin=ft.margin.only(bottom=10),
        visible=False # Se controla por la sesión en la rehidratación
    )

    sw_aplicar_temez = ft.Switch(
        label="Aplicar Reducción Areal de Témez en Racional (KA)",
        value=bool(page.session.get("aplicar_temez") or False), # Por defecto desactivado para ver gasto pleno
        active_color="#00ff41",
        tooltip="Atenúa la intensidad en cuencas > 1 km² mediante Témez. Mantener apagado para microcuencas o si se usa el Módulo 4 (ARF)."
    )

    def on_cambio_temez(e):
        page.session.set("aplicar_temez", sw_aplicar_temez.value)
        page.update()
    sw_aplicar_temez.on_change = on_cambio_temez

    panel_parametros_avanzados = ft.Container(
        content=ft.Column([
            banner_advertencia,
            banner_alerta_arf, # <--- BANNER INYECTADO
            ft.Text("Configuración del Modelo Hidrológico (Frontera y Cinemática)", weight="bold", color="#00ff41"),
            ft.Row([inp_tr_lista, dd_tormenta, dd_cinematica], wrap=True, spacing=15),
            ft.Row([inp_fcc, dd_amc, inp_qbase], wrap=True, spacing=15),
            ft.Row([sw_aplicar_temez], alignment=ft.MainAxisAlignment.START)
        ], spacing=10),
        padding=15, border=ft.border.all(1, "#333333"), border_radius=10, margin=ft.margin.only(top=10, bottom=10)
    )
    
    # ==========================================
    # 3. INTERFAZ: GESTOR DE ESCENARIOS (TOP BAR)
    # ==========================================
    
    # --- PANTALLA DE CARGA GLOBAL (BLOQUEO CERO-TRUST) ---
    dlg_cargando_escenario = ft.AlertDialog(
        modal=True, # Bloquea clics fuera de la ventana
        content=ft.Container(
            content=ft.Column([
                ft.ProgressRing(color="#00ff41", stroke_width=5),
                ft.Text("Sincronizando Escenario...", weight="bold", color="#00ff41"),
                ft.Text("Procesando memoria y tablas. Por favor, espera.", size=12, color="grey")
            ], horizontal_alignment=ft.CrossAxisAlignment.CENTER, tight=True, spacing=15),
            padding=20
        )
    )

    def ejecutar_con_pantalla_carga(funcion_pesada):
        """Envoltorio arquitectónico para bloquear la UI mientras un hilo trabaja."""
        page.open(dlg_cargando_escenario)
        page.update()

        def worker():
            try:
                funcion_pesada()
            except Exception as ex:
                print(f"Error crítico en cambio de escenario: {ex}")
                traceback.print_exc()
            finally:
                page.close(dlg_cargando_escenario)
                page.update()

        # Lanzamos el trabajo en un hilo para no congelar la animación del ProgressRing
        threading.Thread(target=worker, daemon=True).start()

    # --- EVENTOS REFACTORIZADOS CON BLOQUEO ---
    def on_escenario_change(e):
        val = e.control.value
        def tarea():
            guardar_escenario_actual()
            cargar_escenario(val)
        ejecutar_con_pantalla_carga(tarea)

    def on_nuevo_escenario(e):
        def tarea():
            guardar_escenario_actual()
            db = page.session.get("db_escenarios")
            nuevo_nombre = f"Escenario {len(db) + 1}"
            db[nuevo_nombre] = {k: None for k in KEYS_ESCENARIO} # Limpio
            page.session.set("db_escenarios", db)
            actualizar_dropdown_escenarios(nuevo_nombre)
            cargar_escenario(nuevo_nombre)
        ejecutar_con_pantalla_carga(tarea)

    def on_duplicar_escenario(e):
        def tarea():
            guardar_escenario_actual()
            db = page.session.get("db_escenarios")
            activo = page.session.get("escenario_activo")
            nuevo_nombre = f"{activo} (Copia)"
            
            # Duplicamos usando el Clonador Inteligente en vez del deepcopy crudo
            db[nuevo_nombre] = clonar_dato_seguro(db[activo])
            
            page.session.set("db_escenarios", db)
            actualizar_dropdown_escenarios(nuevo_nombre)
            cargar_escenario(nuevo_nombre)
        ejecutar_con_pantalla_carga(tarea)

    dd_escenarios = ft.Dropdown(label="Escenario Activo", width=300, border_color="#00ff41", on_change=on_escenario_change)
    
    def actualizar_dropdown_escenarios(seleccionar=None):
        db = page.session.get("db_escenarios")
        dd_escenarios.options = [ft.dropdown.Option(k) for k in db.keys()]
        if seleccionar: dd_escenarios.value = seleccionar
        elif not dd_escenarios.value: dd_escenarios.value = list(db.keys())[0]
        page.update()

    # --- LÓGICA DE GESTIÓN AVANZADA (CRUD ESCENARIOS) ---
    
    inp_nuevo_nombre = ft.TextField(label="Nuevo Nombre del Escenario", width=300, border_color="#00ff41")

    def confirmar_renombrar(e):
        nuevo_nombre = inp_nuevo_nombre.value.strip()
        db = page.session.get("db_escenarios")
        activo = page.session.get("escenario_activo")

        if not nuevo_nombre or nuevo_nombre in db:
            page.snack_bar = ft.SnackBar(ft.Text("Nombre inválido o el escenario ya existe."), bgcolor="red", open=True)
            page.update()
            return

        # POR QUÉ: Extraemos el diccionario existente y lo reasignamos a la nueva llave para evitar clonación innecesaria en RAM
        db[nuevo_nombre] = db.pop(activo)
        page.session.set("db_escenarios", db)
        page.session.set("escenario_activo", nuevo_nombre)

        actualizar_dropdown_escenarios(nuevo_nombre)
        page.close(dlg_renombrar)
        page.snack_bar = ft.SnackBar(ft.Text(f"✅ Escenario renombrado a '{nuevo_nombre}'"), bgcolor="#00ff41", color="black", open=True)
        page.update()

    dlg_renombrar = ft.AlertDialog(
        title=ft.Text("Renombrar Escenario", color="#00ff41"),
        content=inp_nuevo_nombre,
        actions=[
            ft.TextButton("Cancelar", on_click=lambda e: page.close(dlg_renombrar)),
            ft.ElevatedButton("Renombrar", on_click=confirmar_renombrar, bgcolor="#00ff41", color="black")
        ]
    )

    def on_renombrar_escenario(e):
        inp_nuevo_nombre.value = page.session.get("escenario_activo")
        page.open(dlg_renombrar) # Flet 0.28+ standard

    def on_eliminar_escenario(e):
        db = page.session.get("db_escenarios")
        activo = page.session.get("escenario_activo")

        # POR QUÉ: Candado DevSecOps. Si borramos el último escenario, la UI intentará acceder a valores nulos y Flet crasheará.
        if len(db) <= 1:
            page.snack_bar = ft.SnackBar(ft.Text("🛑 No puedes eliminar el único escenario existente."), bgcolor="#cc0000", open=True)
            page.update()
            return

        # Destrucción explícita para forzar al recolector de basura de Python
        del db[activo]
        page.session.set("db_escenarios", db)

        # Fallback de seguridad al primer escenario de la lista
        nuevo_activo = list(db.keys())[0]
        actualizar_dropdown_escenarios(nuevo_activo)
        cargar_escenario(nuevo_activo)

        page.snack_bar = ft.SnackBar(ft.Text(f"🗑️ Escenario '{activo}' eliminado de la memoria."), bgcolor="orange", color="black", open=True)
        page.update()

    # --- ENSAMBLAJE DE LA BARRA SUPERIOR ---
    
    top_bar_escenarios = ft.Container(
        content=ft.Row([
            ft.Icon(ft.Icons.LAYERS, color="#00ff41"),
            ft.Text("LABORATORIO MULTIESCENARIO:", weight="bold"),
            dd_escenarios,
            # Iconos compactos para las operaciones destructivas/edición
            ft.IconButton(icon=ft.Icons.EDIT, tooltip="Renombrar Escenario Activo", icon_color="#00ff41", on_click=on_renombrar_escenario),
            ft.IconButton(icon=ft.Icons.DELETE_FOREVER, tooltip="Eliminar Escenario Activo", icon_color="red", on_click=on_eliminar_escenario),
            ft.Container(width=10), # Spacer
            ft.ElevatedButton("Nuevo en Blanco", icon=ft.Icons.ADD, on_click=on_nuevo_escenario),
            ft.ElevatedButton("Duplicar Actual", icon=ft.Icons.COPY, on_click=on_duplicar_escenario)
        ]), padding=10, bgcolor="#111111", border=ft.border.all(1, "#333333"), border_radius=5
    )

    # ==========================================
    # 4. SISTEMA DE ENTRADA MANUAL (CUENCAS Y COTAS)
    # ==========================================
    tbl_manual_cuencas = ft.DataTable(columns=[ft.DataColumn(ft.Text("ID")), ft.DataColumn(ft.Text("Área (km²)")), ft.DataColumn(ft.Text("Acción"))], rows=[])
    tbl_manual_cotas = ft.DataTable(columns=[ft.DataColumn(ft.Text("ID Cuenca")), ft.DataColumn(ft.Text("Dist 1")), ft.DataColumn(ft.Text("Dist 2")), ft.DataColumn(ft.Text("Cota May")), ft.DataColumn(ft.Text("Cota Men")), ft.DataColumn(ft.Text("Acción"))], rows=[])

    inp_c_id = ft.TextField(label="ID Cuenca (Ej. C1)", width=150, height=45)
    inp_c_area = ft.TextField(label="Área (km²)", width=120, height=45)
    
    inp_cot_id = ft.TextField(label="ID Cuenca", width=120, height=45)
    inp_cot_d1 = ft.TextField(label="Dist 1 (m)", width=100, height=45)
    inp_cot_d2 = ft.TextField(label="Dist 2 (m)", width=100, height=45)
    inp_cot_cmay = ft.TextField(label="Cota Mayor", width=100, height=45)
    inp_cot_cmen = ft.TextField(label="Cota Menor", width=100, height=45)

    def render_manual_tables():
        df_c = page.session.get("df_cuencas_base")
        df_cot = page.session.get("df_cotas")

        rows_c = []
        if df_c is not None and not df_c.empty:
            for idx, row in df_c.iterrows():
                # Formateo a 4 decimales para el Área
                area_val = f"{float(row['area']):.4f}" 
                rows_c.append(ft.DataRow([
                    ft.DataCell(ft.Text(str(idx), color="#00ff41", weight="bold")),
                    ft.DataCell(ft.Text(area_val)),
                    ft.DataCell(ft.IconButton(ft.Icons.DELETE, icon_color="red", on_click=lambda e, cid=str(idx): del_cuenca_manual(cid)))
                ]))
        tbl_manual_cuencas.rows = rows_c

        rows_cot = []
        if df_cot is not None and not df_cot.empty:
            for idx, row in df_cot.iterrows():
                # Forzamos 4 decimales en Distancias y Cotas para evitar redondeos visuales
                d1 = f"{float(row['distancia1']):.4f}"
                d2 = f"{float(row['distancia2']):.4f}"
                c_may = f"{float(row['cota mayor']):.4f}"
                c_men = f"{float(row['cota menor']):.4f}"
                
                rows_cot.append(ft.DataRow([
                    ft.DataCell(ft.Text(str(row['cuenca']), color="#00ff41", weight="bold")),
                    ft.DataCell(ft.Text(d1)),
                    ft.DataCell(ft.Text(d2)),
                    ft.DataCell(ft.Text(c_may)),
                    ft.DataCell(ft.Text(c_men)),
                    ft.DataCell(ft.IconButton(ft.Icons.DELETE, icon_color="red", on_click=lambda e, i=idx: del_cota_manual(i)))
                ]))
        tbl_manual_cotas.rows = rows_cot
        page.update()

    def add_cuenca_manual(e):
        cid = inp_c_id.value.strip()
        if not cid: return
        try: area = float(inp_c_area.value)
        except: return
        
        df = page.session.get("df_cuencas_base")
        if df is None: df = pd.DataFrame(columns=["area"])
        
        df.loc[cid] = [area]
        page.session.set("df_cuencas_base", df)
        
        inp_c_id.value = ""; inp_c_area.value = ""
        render_manual_tables(); upd_tbl(); validar_nombres_cuencas()

    def del_cuenca_manual(cid):
        df = page.session.get("df_cuencas_base")
        if df is not None and cid in df.index:
            df = df.drop(index=cid)
            page.session.set("df_cuencas_base", df)
            render_manual_tables(); upd_tbl(); validar_nombres_cuencas()

    def add_cota_manual(e):
        cid = inp_cot_id.value.strip()
        if not cid: return
        try:
            d1 = float(inp_cot_d1.value); d2 = float(inp_cot_d2.value)
            cmay = float(inp_cot_cmay.value); cmen = float(inp_cot_cmen.value)
        except: return

        df = page.session.get("df_cotas")
        if df is None: df = pd.DataFrame(columns=['cuenca', 'distancia1', 'distancia2', 'cota mayor', 'cota menor'])
        
        new_row = pd.DataFrame([{'cuenca': cid, 'distancia1': d1, 'distancia2': d2, 'cota mayor': cmay, 'cota menor': cmen}])
        df = pd.concat([df, new_row], ignore_index=True)
        page.session.set("df_cotas", df)
        
        inp_cot_d1.value = ""; inp_cot_d2.value = ""; inp_cot_cmay.value = ""; inp_cot_cmen.value = ""
        render_manual_tables(); validar_nombres_cuencas()

    def del_cota_manual(idx):
        df = page.session.get("df_cotas")
        if df is not None and idx in df.index:
            df = df.drop(index=idx).reset_index(drop=True)
            page.session.set("df_cotas", df)
            render_manual_tables(); validar_nombres_cuencas()

    # ==========================================
    # 5. LÓGICA CORE Y HELPERS 
    # ==========================================
    def leer_csv_robusto(path, index_col=0):
        errores = []
        configs = [('utf-8', ','), ('latin1', ','), ('utf-8', ';'), ('latin1', ';')]
        for enc, sep in configs:
            try: return pd.read_csv(path, header=0, index_col=index_col, sep=sep, encoding=enc)
            except Exception as e: errores.append(f"{enc}/{sep}: {e}")
        try: return pd.read_csv(path, header=0, index_col=index_col, sep=None, engine='python')
        except Exception: raise ValueError("No se pudo leer el CSV.")

    def upd_tbl():
        df_base = page.session.get("df_cuencas_base")
        if df_base is None: return
        rows, rdy = [], True
        config_data = page.session.get("datos_cuencas_config") or {}
        for i, r in df_base.iterrows():
            cfg = config_data.get(str(i))
            if not cfg: rdy = False
            rows.append(ft.DataRow([
                ft.DataCell(ft.Text(str(i))), ft.DataCell(ft.Text(f"{float(r['area']):.4f}")), 
                ft.DataCell(ft.Text(f"{float(cfg['LCP']):.4f}" if cfg else "-")),
                ft.DataCell(ft.Icon(ft.Icons.CHECK if cfg else ft.Icons.WARNING, color="green" if cfg else "orange")),
                ft.DataCell(ft.IconButton(ft.Icons.EDIT, on_click=lambda e, c=str(i): open_conf(c)))
            ]))
        tabla_cuencas_suelos.rows = rows; btn_calc.disabled = not rdy; page.update()
        if page.session.get("estaciones_db"): upd_tabla_pesos()

    def validar_nombres_cuencas():
        df_area = page.session.get("df_cuencas_base")
        df_cotas = page.session.get("df_cotas")
        if df_area is not None and df_cotas is not None and not df_area.empty and not df_cotas.empty:
            ids_area = set(str(x).strip().replace('.0','') for x in df_area.index)
            if 'cuenca' not in df_cotas.columns: return
            ids_cotas = set(str(x).strip().replace('.0','') for x in df_cotas['cuenca'].unique())
            diff = ids_area - ids_cotas
            if diff: validation_msg.value = f"⚠️ Faltan Cotas para cuencas: {list(diff)[:3]}..."; validation_msg.color = "orange"
            else: validation_msg.value = "✅ Áreas y Cotas Emparejadas Correctamente."; validation_msg.color = "#00ff41"
        else: validation_msg.value = "Faltan datos espaciales."; validation_msg.color = "grey"
        page.update()

    def load(e, session_key, ui_status, index_col):
        if e.files:
            try: 
                df = leer_csv_robusto(e.files[0].path, index_col=index_col)
                page.session.set(session_key, df)
                if ui_status: ui_status.value = "OK"; ui_status.color = "green"
                
                render_manual_tables()
                upd_tbl()
                validar_nombres_cuencas()
                
            except Exception as ex: 
                page.snack_bar = ft.SnackBar(ft.Text(f"Error carga: {ex}"), bgcolor="red", open=True)
            page.update()

    # Pickers cacheados en page.session
    def _get_or_create_picker(key, on_result):
        p = page.session.get(key)
        if not p:
            p = ft.FilePicker(on_result=on_result)
            page.session.set(key, p)
            page.overlay.append(p)
        else:
            p.on_result = on_result
        return p

    pk_area = _get_or_create_picker("gastos_pk_area", lambda e: load(e, "df_cuencas_base", None, 0))
    pk_cot  = _get_or_create_picker("gastos_pk_cot", lambda e: load(e, "df_cotas", None, None))
    pk_int = _get_or_create_picker("gastos_pk_int", lambda e: load(e, "df_intensidad", st_int, 0))
    pk_alt = _get_or_create_picker("gastos_pk_alt", lambda e: load(e, "df_altura", st_alt, 0))

    # ==========================================
    # 6. GESTOR DE ESTACIONES (THIESSEN AUTOMÁTICO)
    # ==========================================
    lista_estaciones_ui = ft.Column(spacing=5)
    
    def auto_cargar_thiessen(silencioso=False):
        """El puente mágico con el Módulo 3. Inyecta curvas IDF guardadas como estaciones."""
        db_curvas = page.session.get("db_curvas_procesadas")
        
        # --- CHEQUEO DE INTEGRIDAD (Cero fallas silenciosas) ---
        if not db_curvas:
            if not silencioso:
                page.snack_bar = ft.SnackBar(
                    ft.Text("⚠️ Faltan Datos: El Módulo 3 no ha generado curvas. Completa el análisis primero.", color="white", weight="bold"), 
                    bgcolor="#cc0000", open=True
                )
                page.update()
            return # Abortamos la carga porque no hay nada que cargar
            
        db_estaciones = page.session.get("estaciones_db") or {}
        cambios = False
        
        for sid, data in db_curvas.items():
            if sid not in db_estaciones and data.get("df_intensidad") is not None:
                # --- LÓGICA DE REHIDRATACIÓN DESDE CACHÉ ---
                df_int = data.get("df_intensidad")
                if isinstance(df_int, dict):
                    if df_int.get("type") == "df":
                        try: df_int = pd.read_parquet(df_int["path"])
                        except: continue # Skip si el parquet ya no existe
                    elif df_int.get("type") == "raw":
                        df_int = df_int.get("value")
                
                # Check safety
                if df_int is None or (isinstance(df_int, pd.DataFrame) and df_int.empty):
                    continue

                df_alt = data.get("df_altura")
                if isinstance(df_alt, dict):
                    if df_alt.get("type") == "df":
                        try: df_alt = pd.read_parquet(df_alt["path"])
                        except: df_alt = None
                    elif df_alt.get("type") == "raw":
                        df_alt = df_alt.get("value")

                # Estación nueva detectada
                db_estaciones[sid] = {
                    "nombre": f"{sid} (Procesada)",
                    "intensidad": df_int,
                    "altura": df_alt
                }
                cambios = True
                
        if cambios:
            page.session.set("estaciones_db", db_estaciones)
            render_estaciones()

    def validar_integridad_calculo():
        """
        Revisa si tanto la configuración de suelos como la matriz de Thiessen 
        están listas para habilitar el botón de CALCULAR.
        """
        # 1. Verificar Suelos (rdy del código original)
        df_base = page.session.get("df_cuencas_base")
        config_data = page.session.get("datos_cuencas_config") or {}
        suelos_ok = True
        if df_base is not None:
            for i in df_base.index:
                if str(i) not in config_data:
                    suelos_ok = False
                    break
        
        # 2. Verificar Thiessen (si está en modo distribuido)
        thiessen_ok = True
        msg_error_thiessen.value = ""
        if rg_modo_calculo.value == "dist":
            pesos = page.session.get("pesos_estaciones") or {}
            for cid, estaciones in pesos.items():
                suma = sum(estaciones.values())
                if abs(1.0 - suma) > 0.001:
                    suelos_ok = False # Bloqueamos el cálculo
                    thiessen_ok = False
                    msg_error_thiessen.value = f"⚠️ Error: Pesos de Cuenca '{cid}' suman {suma:.2f} (Debe ser 1.0)"
                    break
        
        btn_calc.disabled = not suelos_ok
        page.update()

    def update_peso_val(cid, eid, val_str):
        """Maneja el ingreso de datos con corrección centesimal automática."""
        try:
            val = float(val_str) if val_str else 0.0
            
            # --- CORRECCIÓN AUTOMÁTICA CENTESIMAL ---
            # Si el usuario pone 50 o 100, asumimos porcentaje y dividimos entre 100
            if val > 1.0:
                val = val / 100.0
            
            pesos = page.session.get("pesos_estaciones")
            pesos[cid][eid] = round(val, 4)
            page.session.set("pesos_estaciones", pesos)
            
            # Validamos y refrescamos el estado del botón calcular
            validar_integridad_calculo()
        except ValueError:
            msg_error_thiessen.value = f"⚠️ Error: Valor inválido '{val_str}' en Cuenca '{cid}'"
            page.update()
        except Exception: 
            pass

    def upd_tabla_pesos():
        """Dibuja la tabla con validación visual por celda."""
        df_base = page.session.get("df_cuencas_base")
        db = page.session.get("estaciones_db")
        pesos = page.session.get("pesos_estaciones")
        if df_base is None or df_base.empty or not db: return

        sorted_ids = sorted(db.keys())
        cols = [ft.DataColumn(ft.Text("ID Cuenca", color="#00ff41"))] + \
               [ft.DataColumn(ft.Text(db[eid]['nombre'][:15])) for eid in sorted_ids]
        
        rows = []
        for cid in [str(x) for x in df_base.index]:
            if cid not in pesos:
                pesos[cid] = {eid: 0.0 for eid in sorted_ids}
                if len(sorted_ids) == 1: pesos[cid][sorted_ids[0]] = 1.0
            
            # Calculamos la suma actual de la fila para feedback visual
            suma_fila = sum(pesos[cid].values())
            color_borde = "#00ff41" if abs(1.0 - suma_fila) < 0.001 else "orange"
            if suma_fila > 1.001: color_borde = "#cc0000"

            cells = [ft.DataCell(ft.Text(cid, weight="bold", color=color_borde))]
            for eid in sorted_ids:
                val_actual = pesos[cid].get(eid, 0.0)
                cells.append(ft.DataCell(
                    ft.TextField(
                        value=f"{val_actual:.3f}", 
                        width=80, 
                        text_align=ft.TextAlign.CENTER,
                        border_color=color_borde, # El TextField reacciona al estado de la fila
                        on_change=lambda e, c=cid, est=eid: update_peso_val(c, est, e.control.value),
                        on_submit=lambda _: upd_tabla_pesos() # Refresca colores al dar Enter
                    )
                ))
            rows.append(ft.DataRow(cells))
            
        page.session.set("pesos_estaciones", pesos)
        tabla_pesos.columns = cols
        tabla_pesos.rows = rows
        validar_integridad_calculo()

    # --- Configuración de Suelos ---
    # --- Configuración de Suelos (Gestor Reactivo) ---
    c_id = ft.Text(weight="bold", color="#00ff41", size=16)
    inp_lcp = ft.TextField(label="LCP (m)", width=150)
    col_usos = ft.Column(scroll=ft.ScrollMode.ALWAYS, height=250)
    
    # NUEVO: Elementos del Semáforo UX y Control de Seguridad (Zero-Trust)
    txt_semaforo = ft.Text("Total: 0.0% (Faltan 100.0%)", color="orange", weight="bold", size=15)
    btn_guardar_usos = ft.ElevatedButton("Guardar", bgcolor="#00ff41", color="black", disabled=True)

    def evaluar_suma_porcentajes(e=None):
        """Motor de evaluación en tiempo real (El Semáforo)"""
        tot = 0.0
        for r in col_usos.controls:
            try: 
                # Sumamos de forma segura (EAFP) el valor del TextField de porcentaje
                val_str = r.controls[0].value
                if val_str: tot += float(val_str)
            except ValueError:
                pass
        
        # Lógica Termodinámica: La masa debe conservarse al 100% exacto
        diferencia = round(100.0 - tot, 3)
        
        if abs(diferencia) < 0.01: # Tolerancia flotante para evitar errores de binario
            txt_semaforo.value = f"Total: {tot:.1f}% (Balance Perfecto)"
            txt_semaforo.color = "#00ff41" # Verde Matrix
            btn_guardar_usos.disabled = False
        elif diferencia > 0:
            txt_semaforo.value = f"Total: {tot:.1f}% (Faltan {diferencia:.1f}%)"
            txt_semaforo.color = "orange"
            btn_guardar_usos.disabled = True
        else:
            txt_semaforo.value = f"Total: {tot:.1f}% (Exceso de {abs(diferencia):.1f}%)"
            txt_semaforo.color = "#cc0000" # Rojo Sangre (Alerta)
            btn_guardar_usos.disabled = True
            
        page.update()

    def remover_fila_uso(row):
        """Elimina la fila y recalcula el dominó"""
        col_usos.controls.remove(row)
        evaluar_suma_porcentajes()

    def add_uso_row(e, d=None):
        """Añade una fila con Auto-completado Jerárquico"""
        
        # Calculamos cuánto porcentaje falta antes de crear la fila
        tot_actual = 0.0
        for r in col_usos.controls:
            try: tot_actual += float(r.controls[0].value) if r.controls[0].value else 0.0
            except ValueError: pass
            
        # Asignamos lo que falta para llegar a 100 como valor por defecto
        pct_default = max(0.0, 100.0 - tot_actual) if d is None else float(d['pct'])

        row = ft.Row([
            # Inyectamos el evento on_change para disparar el semáforo con cada tecla
            ft.TextField(value=f"{pct_default:.2f}" if d is None else str(d['pct']), label="%", width=80, on_change=evaluar_suma_porcentajes),
            ft.Dropdown(options=[ft.dropdown.Option(str(k),v) for k,v in gastos.hidrologia_mx.OPCIONES_C.items()], value=str(d['c']) if d else None, width=350, label="Coef. C"),
            ft.Dropdown(options=[ft.dropdown.Option(str(k),v) for k,v in gastos.hidrologia_mx.OPCIONES_N.items()], value=str(d['n']) if d else None, width=350, label="Curva N"),
            ft.Dropdown(options=[ft.dropdown.Option(x) for x in "ABCD"], value=d['g'] if d else None, width=80, label="Grupo"),
            ft.IconButton(ft.Icons.DELETE, icon_color="red", on_click=lambda e: remover_fila_uso(row))
        ])
        col_usos.controls.append(row)
        evaluar_suma_porcentajes()

    def save_conf(e):
        """Guarda la configuración y cierra el Modal"""
        usos = []
        for r in col_usos.controls:
            try: 
                p = float(r.controls[0].value or 0)
                # Validar explícitamente antes de parsear para evitar TypeError
                c_val = r.controls[1].value
                n_val = r.controls[2].value
                if not c_val or not n_val:
                    raise ValueError("Falta valor C o N")
                usos.append({"pct":p, "c":int(c_val), "n":int(n_val), "g":r.controls[3].value or "A"})
            except Exception as e: 
                page.snack_bar = ft.SnackBar(ft.Text(f"⚠️ Error en fila: Valores incompletos o inválidos."), bgcolor="orange", open=True)
                page.update()
                return # Detenemos el guardado y evitamos corrupción
            
        cfg = page.session.get("datos_cuencas_config")
        cfg[c_id.value] = {"LCP": float(inp_lcp.value or 0), "usos": usos}
        page.session.set("datos_cuencas_config", cfg)
        
        # Sintaxis Flet 0.28 obligatoria
        page.close(dlg_suelos)
        upd_tbl()

    btn_guardar_usos.on_click = save_conf

    # Definición del Cuadro de Diálogo usando Flet moderno
    dlg_suelos = ft.AlertDialog(
        title=ft.Text("Parametrización de Cuenca", color="#00ff41"), 
        content=ft.Container(
            ft.Column([
                ft.Row([ft.Text("ID Cuenca:"), c_id, ft.Container(width=20), inp_lcp]), 
                ft.ElevatedButton("Agregar Uso de Suelo", icon=ft.Icons.ADD, on_click=lambda e: add_uso_row(e)), 
                col_usos,
                ft.Divider(color="#333333"),
                # El Semáforo se inyecta justo arriba de los botones de acción
                ft.Row([txt_semaforo], alignment=ft.MainAxisAlignment.END)
            ]), 
            height=400, width=1000
        ), 
        actions=[
            ft.TextButton("Cancelar", on_click=lambda e: page.close(dlg_suelos)), 
            btn_guardar_usos
        ]
    )

    def open_conf(cid):
        """Abre la ventana de configuración y la hidrata"""
        c_id.value = str(cid)
        d = page.session.get("datos_cuencas_config").get(str(cid), {})
        inp_lcp.value = d.get("LCP", "")
        col_usos.controls.clear()
        
        # Hidratamos filas existentes
        for u in d.get("usos", []): 
            add_uso_row(None, u)
            
        # Si es una cuenca nueva, añadimos una fila (se auto-completará con el 100%)
        if not d.get("usos"): 
            add_uso_row(None) 
            
        evaluar_suma_porcentajes()
        
        # Sintaxis Flet 0.28 obligatoria
        page.open(dlg_suelos) 
        page.update()

    # ==========================================
    # 7. VISUALIZACIÓN DE RESULTADOS
    # ==========================================
    def dataframe_to_datatable(df, max_filas=50):
        if df is None or df.empty: return ft.Text("Sin datos")
        try:
            df_view = df.head(max_filas).copy()
            if df_view.index.name is not None or not isinstance(df_view.index, pd.RangeIndex): df_view.reset_index(inplace=True)
            df_view.columns = df_view.columns.astype(str)
            
            # --- NUEVO: Función de formateo estricto para Flet ---
            def formato_celda(v):
                if isinstance(v, float): 
                    return f"{v:.4f}" # Fuerza 4 decimales en la UI
                return str(v)[:20]

            cols = [ft.DataColumn(ft.Text(col[:15], weight="bold", color="#00ff41")) for col in df_view.columns]
            rows = [ft.DataRow([ft.DataCell(ft.Text(formato_celda(val), size=11)) for val in row]) for _, row in df_view.iterrows()]
            
            return ft.Container(content=ft.Column(controls=[ft.Row([ft.DataTable(columns=cols, rows=rows, column_spacing=15, border=ft.border.all(1, "#333333"), vertical_lines=ft.border.BorderSide(1, "#333333"))], scroll=ft.ScrollMode.AUTO)], scroll=ft.ScrollMode.AUTO), height=250)
        except Exception as e: return ft.Text(f"Error tabla: {e}", color="red")
    
    def construir_visualizacion_resultados(res_r, res_c, res_h, res_hydros):
        tabs_lista = []
        df_vars = page.session.get("df_variables")

        def preparar_df_visual(df):
            if df is None: return None
            df_v = df.copy()
            renames = {col: str(col).replace("TR_", "") + " años" for col in df_v.columns if "TR_" in str(col)}
            df_v.rename(columns=renames, inplace=True)
            return df_v

        # 1. Pestaña de Variables Físicas
        if df_vars is not None:
            tabs_lista.append(ft.Tab(
                text="Variables", icon=ft.Icons.LIST_ALT,
                content=ft.Column([
                    ft.Text("Parámetros Físicos por Cuenca", weight="bold", color="#00ff41"),
                    ft.Container(content=dataframe_to_datatable(df_vars), border=ft.border.all(1, "#333333"), padding=10)
                ], scroll=ft.ScrollMode.AUTO)
            ))

        # 2. Pestaña de Gastos Pico
        col_tablas = ft.Column([
            ft.Text("Detalle de Gastos Máximos (m³/s)", size=18, weight="bold", color="#00ff41"),
            ft.Divider(),
            ft.Text("MÉTODO RACIONAL", weight="bold"), dataframe_to_datatable(preparar_df_visual(res_r)),
            ft.Text("MÉTODO DE CHOW", weight="bold"), dataframe_to_datatable(preparar_df_visual(res_c)),
            ft.Text("MÉTODO HEC-HMS (SCS)", weight="bold"), dataframe_to_datatable(preparar_df_visual(res_h)),
        ], scroll=ft.ScrollMode.AUTO, spacing=25)
        tabs_lista.append(ft.Tab(text="Gasto Pico", icon=ft.Icons.TABLE_CHART, content=ft.Container(content=col_tablas, padding=20)))

        # 3. Pestaña de Gráficos Globales (Multicuenca y Comparativos)
        col_barras = ft.Column(scroll=ft.ScrollMode.AUTO, spacing=20)
        
        try:
            # --- 3.1 Gráficos de Evolución por Método (Cuencas vs TRs) ---
            graficos_metodos = gastos.generar_graficos_por_metodo(res_r, res_c, res_h)
            if graficos_metodos:
                page.session.set("graficos_metodos", graficos_metodos) # <--- NUEVO: Capturamos en sesión
                col_barras.controls.append(ft.Text("Evolución de Gastos Pico por Método", size=18, weight="bold", color="#00ff41"))
                for nombre_metodo, b64 in graficos_metodos:
                    col_barras.controls.append(
                        ft.Container(
                            content=ft.Column([
                                ft.Row([ft.Icon(ft.Icons.AUTO_GRAPH, color="#00ff41"), ft.Text(f"Análisis Multicuenca - {nombre_metodo}", size=16, weight="bold", color="white")]),
                                ft.Container(content=ft.Image(src_base64=b64, fit=ft.ImageFit.CONTAIN), alignment=ft.alignment.center, expand=True),
                                ft.Divider(height=20, color="#222222")
                            ]), padding=10
                        )
                    )
            
            # --- 3.2 Gráficos Tradicionales de Comparación (Modelos vs TR) ---
            graficos_tr = gastos.generar_graficos_comparativos(res_r, res_c, res_h, pd.DataFrame())
            if graficos_tr:
                page.session.set("graficos_tr", graficos_tr) # <--- NUEVO: Capturamos en sesión
                col_barras.controls.append(ft.Text("Comparativa de Modelos por Periodo de Retorno", size=18, weight="bold", color="#00ff41"))
                for tr_val, b64 in graficos_tr:
                    col_barras.controls.append(
                        ft.Container(
                            content=ft.Column([
                                ft.Row([ft.Icon(ft.Icons.BAR_CHART, color="#00ff41"), ft.Text(f"Comparativa de Métodos - TR {tr_val} Años", size=16, weight="bold", color="white")]),
                                ft.Container(content=ft.Image(src_base64=b64, fit=ft.ImageFit.CONTAIN), alignment=ft.alignment.center, expand=True),
                                ft.Divider(height=20, color="#222222")
                            ]), padding=10
                        )
                    )
                    
            if col_barras.controls:
                tabs_lista.append(ft.Tab(text="Gráficos Globales", icon=ft.Icons.BAR_CHART, content=ft.Container(col_barras, padding=20)))
                
        except Exception as e:
            print(f"Error generando gráficos de barras: {e}")

        # 4. Pestaña de Hidrogramas y Memoria de Cálculo (PRO LEVEL)
        if res_hydros:
            col_hydros = ft.Column(scroll=ft.ScrollMode.AUTO)
            for tr_key, data in res_hydros.items():
                
                # --- GESTOR DE DATOS (PRO & LEGACY) ---
                if isinstance(data, dict) and "detalle" in data:
                    df_detalle = data["detalle"]
                    resumen = data.get("resumen", {})
                elif isinstance(data, pd.DataFrame):
                    df_detalle = data
                    resumen = {}
                else:
                    t_axis, q_hidro = data
                    df_detalle = pd.DataFrame({
                        "Tiempo (minutos)": np.array(t_axis) * 60 if max(t_axis) < 24 else t_axis,
                        "Caudal Directo (m3/s)": q_hidro
                    })
                    resumen = {}

                # --- EXTRACCIÓN A PRUEBA DE BALAS ---
                col_nombres = df_detalle.columns.tolist()
                
                if "Tiempo (minutos)" in col_nombres: t_hr = df_detalle["Tiempo (minutos)"].values / 60.0
                elif "Tiempo (min)" in col_nombres: t_hr = df_detalle["Tiempo (min)"].values / 60.0
                else: t_hr = df_detalle.iloc[:, 0].values / 60.0 
                
                if "Caudal Directo (m3/s)" in col_nombres: Q_val = df_detalle["Caudal Directo (m3/s)"].values
                elif "Q Directo (m3/s)" in col_nombres: Q_val = df_detalle["Q Directo (m3/s)"].values
                else: Q_val = df_detalle.iloc[:, -1].values

                # --- GRÁFICA BASE64 ---
                img_b64 = gastos.generar_grafico_hidrograma(t_hr, Q_val, f"Hidrograma de Diseño - {tr_key}")

                # --- A. TABLA COMPUTED RESULTS (ESTILO HMS) ---
                if resumen:
                    res_rows = [
                        ft.DataRow([
                            ft.DataCell(ft.Text(k, weight="bold", color="white")), 
                            ft.DataCell(ft.Text(str(v), color="#00ff41", weight="bold"))
                        ]) for k, v in resumen.items()
                    ]
                    tabla_resumen = ft.Container(
                        content=ft.DataTable(
                            columns=[ft.DataColumn(ft.Text("")), ft.DataColumn(ft.Text(""))],
                            rows=res_rows, heading_row_height=0, column_spacing=40
                        ),
                        bgcolor="#111111", border=ft.border.all(1, "#333333"), border_radius=8, padding=15
                    )
                else:
                    tabla_resumen = ft.Text("⚠️ Sin resumen de balance. Recalcula para generar.", color="orange")

                # --- C. TABLA DE MEMORIA (CONVOLUCIÓN) ---
                cols_mem = [ft.DataColumn(ft.Text(str(c), weight="bold", color="#00ff41", size=11)) for c in col_nombres]
                rows_mem = []
                for _, r in df_detalle.head(150).iterrows():
                    rows_mem.append(ft.DataRow([ft.DataCell(ft.Text(f"{float(v):.3f}" if isinstance(v, (int, float)) else str(v), size=11)) for v in r]))

                tabla_memoria = ft.Container(
                    content=ft.Column([ft.Row([ft.DataTable(columns=cols_mem, rows=rows_mem, column_spacing=15)], scroll=ft.ScrollMode.AUTO)], scroll=ft.ScrollMode.ALWAYS),
                    height=350, border=ft.border.all(1, "#333333"), border_radius=8, bgcolor="#080808", padding=10
                )

                # --- ENSAMBLAJE FINAL EN CASCADA ---
                bloque = ft.Column([
                    ft.Row([ft.Icon(ft.Icons.ANALYTICS, color="#00ff41"), ft.Text(f"Resultados Computados: {tr_key}", size=18, weight="bold", color="#00ff41")]),
                    ft.Text("Resumen de Balance Hídrico Global", color="grey", size=14, weight="bold"),
                    tabla_resumen,
                    ft.Divider(height=10, color="transparent"),
                    ft.Text("📈 Gráfica del Hidrograma", size=16, weight="bold"),
                    ft.Container(content=ft.Image(src_base64=img_b64, fit=ft.ImageFit.CONTAIN), alignment=ft.alignment.center, expand=True),
                    ft.Text("📋 Memoria de Tránsito (Paso a Paso)", size=14, weight="bold", color="grey"),
                    tabla_memoria,
                    ft.Divider(height=40, color="#222222")
                ], spacing=15, horizontal_alignment=ft.CrossAxisAlignment.STRETCH)

                col_hydros.controls.append(ft.Container(content=bloque, padding=20))

            tabs_lista.append(ft.Tab(text="Hidrogramas", icon=ft.Icons.SHOW_CHART, content=col_hydros))

        tabs_res.tabs = tabs_lista
        page.update()
    
    # ==========================================
    # 8. EJECUCIÓN DEL CÁLCULO
    # ==========================================
    
    # 1. Definición del Escudo Modal de Cálculo
    dlg_calculando_gastos = ft.AlertDialog(
        modal=True,
        content=ft.Container(
            content=ft.Column([
                ft.ProgressRing(color="#00ff41", stroke_width=5),
                ft.Text("Computando Modelación Hidrológica...", weight="bold", color="#00ff41", size=16),
                ft.Text("Evaluando Racional, Chow y Convolución HEC-HMS.\nPor favor, no cierres la aplicación.", size=12, color="grey", text_align=ft.TextAlign.CENTER)
            ], horizontal_alignment=ft.CrossAxisAlignment.CENTER, tight=True, spacing=15),
            padding=20
        ),
        actions=[
            # MUDAMOS EL FRENO DE EMERGENCIA AQUÍ ADENTRO
            ft.ElevatedButton("ABORTAR CÁLCULO DE EMERGENCIA", icon=ft.Icons.STOP, bgcolor="#cc0000", color="white", on_click=lambda _: gastos.señal_abortar_gastos.set())
        ],
        actions_alignment=ft.MainAxisAlignment.CENTER
    )

    def run(e=None):
        # Limpiamos la señal de aborto por si fue activada en un cálculo previo
        gastos.señal_abortar_gastos.clear()
        
        btn_calc.disabled = True 
        log_txt.value = "Iniciando motor matemático..."; log_txt.color = "white"
        
        # Bloqueamos la interfaz desplegando el Modal
        page.open(dlg_calculando_gastos)
        page.update()

        def task():
            main_rail = page.session.get("main_rail")
            try:
                if main_rail: main_rail.disabled = True; main_rail.update()

                df_base = page.session.get("df_cuencas_base")
                if df_base is None: raise ValueError("No hay datos de cuencas cargados.")
                
                df = df_base.copy()
                lcp, pct, c_vals, n_vals, g_vals = [], [], [], [], []
                cfg = page.session.get("datos_cuencas_config") or {}
                
                for i in df.index:
                    d = cfg.get(str(i), {})
                    lcp.append(d.get('LCP', 0))
                    pct.append(",".join([str(u['pct']) for u in d.get('usos', [])]))
                    c_vals.append(",".join([str(u['c']) for u in d.get('usos', [])]))
                    n_vals.append(",".join([str(u['n']) for u in d.get('usos', [])]))
                    g_vals.append(",".join([str(u['g']) for u in d.get('usos', [])]))

                df['LCP'], df['Porcentaje_terreno'] = lcp, pct
                df['Coeficiente_C_por_Cuenca'], df['indice_Uso_terreno'] = c_vals, n_vals
                df['Grupo_hidrologico'] = g_vals

                # --- EXTRACCIÓN DE PARÁMETROS V10.1 ---
                try: v_fcc = float(inp_fcc.value)
                except ValueError: v_fcc = 1.0
                
                try: v_qbase = float(inp_qbase.value)
                except ValueError: v_qbase = 0.0

                v_amc = dd_amc.value or "II"
                v_tormenta = dd_tormenta.value or "bloques"
                v_cinematica = dd_cinematica.value or "dinamico"

                # Procesamiento de TR Personalizados
                try:
                    lista_raw = [t.strip() for t in inp_tr_lista.value.split(",")]
                    tr_cols_calc = [str(int(t)) for t in lista_raw if t.isdigit()]
                    if not tr_cols_calc: tr_cols_calc = ['2', '5', '10', '20', '50', '100']
                except Exception:
                    tr_cols_calc = ['2', '5', '10', '20', '50', '100']

                # Extracción del parámetro ARF para enviarlo al backend
                aplicar_reduccion_areal = page.session.get("arf_requerido") if page.session.get("arf_requerido") is not None else True

                # Ejecución de Core Lógico (Preparado para la ETAPA A del backend)
                res_r, res_c, res_h, df_vars, res_hydros, l = gastos.calcular_coeficientes_y_gastos(
                    df, page.session.get("df_cotas"), metodo_tc=rg_metodo_tc.value, 
                    modo_distribuido=(rg_modo_calculo.value == "dist"),
                    estaciones_db=page.session.get("estaciones_db"), pesos_estaciones=page.session.get("pesos_estaciones"),
                    df_int_global=page.session.get("df_intensidad"), df_alt_global=page.session.get("df_altura"),
                    fcc=v_fcc, condicion_amc=v_amc, flujo_base=v_qbase, lista_tr=tr_cols_calc,
                    tipo_tormenta=v_tormenta, tipo_cinematica=v_cinematica,
                    aplicar_temez=sw_aplicar_temez.value, # <--- ENLACE DIRECTO
                    aplicar_arf=aplicar_reduccion_areal, # <--- PARÁMETRO DE PUENTE INYECTADO
                    df_intensidad_arf=page.session.get("arf_df_intensidad_maestra"),
                    df_altura_arf=page.session.get("arf_df_altura_maestra"),
                    page=page
                )
                
                if res_r is not None:
                    page.session.set("res_racional", res_r)
                    page.session.set("res_chow", res_c)
                    page.session.set("res_hms_peak", res_h)
                    page.session.set("df_variables", df_vars)
                    page.session.set("res_hidrogramas", res_hydros)
                    
                    log_txt.value = "✅ Cálculo finalizado exitosamente."
                    log_txt.color = "green"
                    
                    if rg_modo_calculo.value == "regional_arf":
                        cargar_tablas_desde_arf()
                        
                    # --- GUARDADO AUTOMÁTICO EN EL ESCENARIO ACTUAL ---
                    guardar_escenario_actual()
                    
                    construir_visualizacion_resultados(res_r, res_c, res_h, res_hydros)
                    tabs_res.visible = True
                else:
                    # --- ESCUDO ANTI COLAPSOS SILENCIOSOS ---
                    log_txt.value = f"❌ Fallo crítico en el núcleo matemático:\n{l}"
                    log_txt.color = "red"
                    page.update()

            except Exception as ex:
                log_txt.value = f"❌ Error de Sistema: {str(ex)}"
                log_txt.color = "red"
                page.update()
            
            finally:
                # --- DESBLOQUEO ABSOLUTO DEL SISTEMA ---
                page.close(dlg_calculando_gastos)
                btn_calc.disabled = False
                if main_rail: main_rail.disabled = False; main_rail.update()
                
                # QA Memory: Forzamos la recolección de basura
                import gc
                gc.collect()
                
                page.update()

        threading.Thread(target=task, daemon=True).start()

    btn_calc.on_click = run

    # ==========================================
    # 9. DISEÑO DE INTERFAZ (TABS) Y MÓDULOS
    # ==========================================
    
    # --- Gestor de Estaciones Manual (Para mezclar) ---
    inp_est_nombre = ft.TextField(label="Nombre Estación", width=200)
    st_est_int, st_est_alt = ft.Text("Pendiente", color="orange", size=11), ft.Text("Pendiente", color="orange", size=11)
    temp_est_data = {"int": None, "alt": None}
    
    def load_est_file(e, tipo):
        if e.files:
            try:
                df = leer_csv_robusto(e.files[0].path, index_col=0)
                temp_est_data[tipo] = df
                if tipo == "int": st_est_int.value, st_est_int.color = "OK", "green"
                else: st_est_alt.value, st_est_alt.color = "OK", "green"
                page.update()
            except Exception as ex: pass

    pk_est_int = ft.FilePicker(on_result=lambda e: load_est_file(e, "int"))
    pk_est_alt = ft.FilePicker(on_result=lambda e: load_est_file(e, "alt"))
    page.overlay.extend([pk_est_int, pk_est_alt])
    
    def agregar_estacion(e):
        if not inp_est_nombre.value: return
        db = page.session.get("estaciones_db") or {}
        eid = f"EST_MAN_{len(db)+1}"
        db[eid] = {"nombre": inp_est_nombre.value, "intensidad": temp_est_data["int"], "altura": temp_est_data["alt"]}
        page.session.set("estaciones_db", db)
        inp_est_nombre.value = ""; temp_est_data["int"], temp_est_data["alt"] = None, None
        st_est_int.value, st_est_alt.value = "Pendiente", "Pendiente"; st_est_int.color, st_est_alt.color = "orange", "orange"
        render_estaciones()

    def borrar_estacion(eid):
        db = page.session.get("estaciones_db")
        if eid in db: del db[eid]
        page.session.set("estaciones_db", db)
        render_estaciones()

    def render_estaciones():
        lista_estaciones_ui.controls.clear()
        db = page.session.get("estaciones_db") or {}
        for eid, data in db.items():
            lista_estaciones_ui.controls.append(ft.Row([
                ft.Icon(ft.Icons.SENSORS, color="#00ff41", size=20),
                ft.Text(f"{data['nombre']}", width=150),
                ft.IconButton(ft.Icons.DELETE, icon_color="red", on_click=lambda e, id_est=eid: borrar_estacion(id_est))
            ]))
        upd_tabla_pesos()
        page.update()

    # --- NUEVA LÓGICA: Selección de Lluvia desde Memoria para MODO SIMPLE ---
    dd_estacion_simple = ft.Dropdown(
        label="Seleccionar Estación desde Memoria (Opcional)",
        width=400,
        border_color="#00ff41",
        tooltip="Elige una estación procesada en el Módulo 3 para evitar cargar CSVs."
    )

    def on_estacion_simple_change(e):
        estacion_id = e.control.value
        db = page.session.get("estaciones_db") or {}
        
        if estacion_id and estacion_id in db:
            data = db[estacion_id]
            # Asignamos a las variables globales del escenario actual
            if data.get("intensidad") is not None:
                page.session.set("df_intensidad", data["intensidad"])
                st_int.value, st_int.color = "OK (Memoria)", "green"
            
            if data.get("altura") is not None:
                page.session.set("df_altura", data["altura"])
                st_alt.value, st_alt.color = "OK (Memoria)", "green"
            
            # Guardar en las propiedades del escenario para que no se pierda el Dropdown
            page.session.set("target_station_id", estacion_id) 
            page.update()

    dd_estacion_simple.on_change = on_estacion_simple_change

    def actualizar_dropdown_simple():
        """Llena el dropdown con las estaciones disponibles en memoria."""
        db = page.session.get("estaciones_db") or {}
        dd_estacion_simple.options = [ft.dropdown.Option(k, data["nombre"]) for k, data in db.items()]
        
        # Restaurar selección si existe
        target = page.session.get("target_station_id")
        if target and target in db:
            dd_estacion_simple.value = target
            # --- CORRECCIÓN UX: Reflejar visualmente que la data está conectada al cargar ---
            if db[target].get("intensidad") is not None:
                st_int.value, st_int.color = "OK (Memoria)", "green"
            if db[target].get("altura") is not None:
                st_alt.value, st_alt.color = "OK (Memoria)", "green"
            
        page.update()

    modulo_simple = ft.Column([
        ft.Text("Ingresa los datos globales de lluvia para toda la cuenca:", color="grey"),
        dd_estacion_simple,
        ft.Text("O carga archivos CSV manualmente si no tienes estaciones en memoria:", size=11, color="grey"),
        ft.Row([ft.ElevatedButton("Cargar I-D-TR (Racional/HMS)", on_click=lambda _: pk_int.pick_files()), st_int]),
        ft.Row([ft.ElevatedButton("Cargar HP-D-TR (Chow)", on_click=lambda _: pk_alt.pick_files()), st_alt]),
    ], visible=True)

    modulo_distribuido = ft.Column([
        ft.Text("Gestor de Estaciones Climáticas (Thiessen):", weight="bold", color="#00ff41"),
        ft.Text("Las estaciones procesadas en el Módulo 3 se cargan solas. También puedes añadir manuales.", color="grey", size=12),
        ft.Container(
            content=ft.Column([
                ft.Row([inp_est_nombre, ft.ElevatedButton("Subir I-D-TR", on_click=lambda _: pk_est_int.pick_files()), ft.ElevatedButton("Subir Altura", on_click=lambda _: pk_est_alt.pick_files()), ft.ElevatedButton("AÑADIR", on_click=agregar_estacion, bgcolor="#00ff41", color="black")]),
                lista_estaciones_ui
            ]), padding=10, border=ft.border.all(1, "#333333"), border_radius=10
        ),
        ft.Text("Matriz de Pesos de Thiessen (% de influencia por Cuenca):", weight="bold"),
        msg_error_thiessen,
        ft.Container(content=ft.Column([ft.Row([tabla_pesos], scroll=ft.ScrollMode.AUTO)], scroll=ft.ScrollMode.ADAPTIVE), height=300, border=ft.border.all(1, "#333333"))
    ], visible=False)

    def cargar_tablas_desde_arf():
        """
        [Directriz 1 - Persistencia & Zero-Trust] Hidrata la interfaz visual de Gastos 
        con la matriz de intensidades del ESCENARIO ACTIVO, sin mutar ni sobreescribir 
        variables de sesión para evitar el State Clobbering.
        """
        df_i_esc = page.session.get("df_intensidad")
        
        if df_i_esc is not None and isinstance(df_i_esc, (pd.DataFrame, pd.Series)) and not (hasattr(df_i_esc, "empty") and df_i_esc.empty):
            columnas = [ft.DataColumn(ft.Text("Duración (min)", color="#00ff41"))] + \
                       [ft.DataColumn(ft.Text(f"Tr {col} años", color="#00ff41")) for col in df_i_esc.columns]
            
            filas_render = []
            for duracion, row in df_i_esc.iterrows():
                celdas = [ft.DataCell(ft.Text(str(duracion), weight="bold"))] + \
                         [ft.DataCell(ft.Text(f"{val:.2f}")) for val in row.values]
                filas_render.append(ft.DataRow(cells=celdas))
                
            tabla_flet = ft.DataTable(
                columns=columnas,
                rows=filas_render,
                heading_row_color="#1a1a1a",
                divider_thickness=1
            )
            
            contenedor_tablas_regional.content = ft.Column([
                ft.Text("MATRIZ DE INTENSIDADES (ESCENARIO ACTIVO - mm/h)", weight="bold", size=13, color="#00ff41"),
                ft.Container(content=ft.Row([tabla_flet], scroll=ft.ScrollMode.AUTO), border=ft.border.all(1, "#333333"), border_radius=4)
            ], scroll=ft.ScrollMode.ADAPTIVE)
            contenedor_tablas_regional.visible = True
        else:
            contenedor_tablas_regional.content = ft.Column([
                ft.Text("MATRIZ DE INTENSIDADES (ESCENARIO ACTIVO)", weight="bold", size=13, color="#00ff41"),
                ft.Text("No hay datos de lluvia importados para este escenario. Presione 'CALCULAR' para ejecutar el Bypass Regional e importar los datos del Módulo 4.", color="grey", size=12)
            ], scroll=ft.ScrollMode.ADAPTIVE)
            contenedor_tablas_regional.visible = True
        page.update()

    def evaluar_escala_cuenca_y_actualizar_ui():
        """
        [Directriz 6 - Zero-Trust] Evalúa el área de la cuenca y altera dinámicamente
        los contratos de la interfaz Flet para evitar errores metodológicos del usuario.
        """
        area_cuenca = page.session.get("area_cuenca_km2") or 0.0
        
        if area_cuenca > 25.0:
            rg_modo_calculo.content.controls[2].disabled = False  # Cuenca Regional (ARF)
            rg_modo_calculo.content.controls[3].disabled = False  # Multicuenca (ARF)
            rg_modo_calculo.value = "regional_arf"
            
            banner_advertencia.content.controls[1].value = (
                f"Área de cuenca crítica detectada ({area_cuenca:.2f} km² > 25 km²). "
                "Modo Regional ARF activado automáticamente. Los métodos empíricos (Racional/Chow) "
                "se computarán únicamente con fines normativos/referenciales."
            )
            banner_advertencia.visible = True
            # ELIMINADO: cargar_tablas_desde_arf() - Evitar State Clobbering al inicializar.
            if page.session.get("df_intensidad") is not None:
                cargar_tablas_desde_arf()
        else:
            rg_modo_calculo.content.controls[2].disabled = True
            rg_modo_calculo.content.controls[3].disabled = True
            banner_advertencia.visible = False

    def cambiar_modo_lluvia(e):
        is_dist = (rg_modo_calculo.value == "dist")
        is_reg = (rg_modo_calculo.value == "regional_arf")
        modulo_simple.visible = not is_dist and not is_reg
        modulo_distribuido.visible = is_dist
        if is_reg:
            contenedor_tablas_regional.visible = True
            # No sobreescribir datos desde ARF por la fuerza en el on_change
            cargar_tablas_desde_arf()
        else:
            contenedor_tablas_regional.visible = False
        page.session.set("modo_lluvia_activo", rg_modo_calculo.value)
        page.update()

    rg_modo_calculo.on_change = cambiar_modo_lluvia

    # --- GENERADOR DE PLANTILLAS ESPACIALES ---
    dlg_cargando_espacial = ft.AlertDialog(
        modal=True,
        content=ft.Container(
            content=ft.Column([
                ft.ProgressRing(color="#00ff41", stroke_width=5),
                ft.Text("Generando Contratos Espaciales OGC...", weight="bold", color="#00ff41"),
            ], horizontal_alignment=ft.CrossAxisAlignment.CENTER, tight=True, spacing=15),
            padding=20
        )
    )

    def trigger_generar_plantillas(e):
        def _task():
            page.open(dlg_cargando_espacial)
            page.update()
            try:
                # 1. Generar Plantillas Vacías
                gdf_cuenca = MotorEspacial.generar_plantilla(SubcuencaSchema, "Polygon")
                gdf_suelo = MotorEspacial.generar_plantilla(UsoSueloSchema, "Polygon")
                gdf_cauces = MotorEspacial.generar_plantilla(CaucesSchema, "LineString")
                
                # 2. Inyectar al GeoPackage del Proyecto Activo
                pm = project_manager_instance
                pm.guardar_capa_espacial("shp_cuenca_limite", gdf_cuenca)
                pm.guardar_capa_espacial("shp_uso_suelo", gdf_suelo)
                pm.guardar_capa_espacial("shp_cauces", gdf_cauces)
                
                # Notificación Cyberpunk
                page.snack_bar = ft.SnackBar(
                    ft.Text("✅ Capas Base generadas en el archivo de proyecto. Listas para QGIS.", color="#050505", weight="bold"), 
                    bgcolor="#00ff41", open=True
                )
            except Exception as ex:
                page.snack_bar = ft.SnackBar(ft.Text(f"❌ Error Geoespacial: {str(ex)}"), bgcolor="#cc0000", open=True)
            finally:
                page.close(dlg_cargando_espacial)
                page.update()
                
        # Asegúrate de ejecutar en un hilo separado
        threading.Thread(target=_task, daemon=True).start()

    btn_generar_plantillas = ft.ElevatedButton("Generar Plantillas GIS (.hds)", icon=ft.Icons.MAP, on_click=trigger_generar_plantillas, bgcolor="#1c75fa", color="white")

    # --- PESTAÑAS ---
    tab_area_cotas = ft.Tab(text="1. Geometría", icon=ft.Icons.LANDSCAPE, content=ft.Container(ft.Column([
        ft.Text("Paso 1: Parámetros Físicos", color="#00ff41", size=20, weight="bold"), validation_msg, ft.Divider(),
        ft.Row([ft.Text("Integración QGIS: ", weight="bold", color="grey"), btn_generar_plantillas]),
        ft.Text("Áreas de Aportación", size=16, weight="bold"),
        ft.Row([ft.ElevatedButton("Importar CSV (Áreas)", icon=ft.Icons.UPLOAD_FILE, on_click=lambda _: pk_area.pick_files())]),
        ft.Row([inp_c_id, inp_c_area, ft.ElevatedButton("Agregar Cuenca", icon=ft.Icons.ADD, on_click=add_cuenca_manual, bgcolor="#00ff41", color="black")], alignment=ft.MainAxisAlignment.START),
        ft.Container(content=ft.Column([ft.Row([tbl_manual_cuencas], scroll=ft.ScrollMode.AUTO)], scroll=ft.ScrollMode.ADAPTIVE), height=200, border=ft.border.all(1, "#333333")),
        ft.Divider(),
        ft.Text("Cotas y Desniveles (LCP)", size=16, weight="bold"),
        ft.Row([ft.ElevatedButton("Importar CSV (Cotas)", icon=ft.Icons.UPLOAD_FILE, on_click=lambda _: pk_cot.pick_files())]),
        ft.Row([inp_cot_id, inp_cot_d1, inp_cot_d2, inp_cot_cmay, inp_cot_cmen, ft.ElevatedButton("Agregar Tramo", icon=ft.Icons.ADD, on_click=add_cota_manual, bgcolor="#00ff41", color="black")], alignment=ft.MainAxisAlignment.START),
        ft.Container(content=ft.Column([ft.Row([tbl_manual_cotas], scroll=ft.ScrollMode.AUTO)], scroll=ft.ScrollMode.ADAPTIVE), height=200, border=ft.border.all(1, "#333333"))
    ], scroll=ft.ScrollMode.AUTO), padding=20))

    tab_conf = ft.Tab(text="2. Suelos", icon=ft.Icons.SETTINGS, content=ft.Container(ft.Column([
        ft.Text("Paso 2: Coeficientes C y N", color="#00ff41", size=20), 
        ft.Container(ft.Column([ft.Row([tabla_cuencas_suelos], scroll=ft.ScrollMode.AUTO)], scroll=ft.ScrollMode.AUTO), height=500, border=ft.border.all(1, "grey"))
    ]), padding=20))
    


    tab_data = ft.Tab(text="3. Entorno", icon=ft.Icons.WATER_DROP, content=ft.Container(ft.Column([
        ft.Text("Paso 3: Condiciones Ambientales y Tormentas", color="#00ff41", size=20, weight="bold"),
        ft.Row([
            ft.Container(content=ft.Column([ft.Text("Método de Tc:", weight="bold"), rg_metodo_tc]), padding=10, border=ft.border.all(1, "#333333"), border_radius=10, expand=True),
            ft.Container(content=ft.Column([ft.Text("Distribución Lluvia:", weight="bold"), rg_modo_calculo]), padding=10, border=ft.border.all(1, "#333333"), border_radius=10, expand=True),
        ]),
        panel_parametros_avanzados,
        ft.Divider(), modulo_simple, modulo_distribuido, contenedor_tablas_regional
    ], scroll=ft.ScrollMode.AUTO), padding=20))
    
    # ==========================================
    # --- MOTOR DE EXPORTACIÓN LATEX (CON DIÁLOGO) ---
    # ==========================================
    
    # 1. Función que se ejecuta DESPUÉS de que el usuario elige la carpeta
    def generar_latex_en_ruta(e: ft.FilePickerResultEvent):
        # Si el usuario cierra la ventana sin elegir nada, abortamos
        if not e.path: 
            return 

        ruta_salida = e.path
        
        # Extracción masiva de todos los módulos
        res_hydros = page.session.get("res_hidrogramas")
        df_vars = page.session.get("df_variables")
        res_r = page.session.get("res_racional")
        res_c = page.session.get("res_chow")
        res_h = page.session.get("res_hms_peak")
        graficos_metodos = page.session.get("graficos_metodos")
        graficos_tr = page.session.get("graficos_tr")
        
        # Llamamos al motor con toda la artillería
        exito, msj = generador_latex.exportar_informe_latex(
            res_hydros, df_vars, res_r, res_c, res_h, 
            graficos_metodos, graficos_tr, ruta_salida, "Estudio Lluvia-Escurrimiento"
        )
        
        color_sb = "#00ff41" if exito else "red"
        page.snack_bar = ft.SnackBar(ft.Text(msj, color="black", weight="bold"), bgcolor=color_sb, open=True)
        page.update()

    # 2. Declaramos el FilePicker (Selector de Archivos/Carpetas invisible)
    file_picker_latex = ft.FilePicker(on_result=generar_latex_en_ruta)
    
    # Lo añadimos a la capa superior (overlay) de la página para que pueda abrir pop-ups
    if file_picker_latex not in page.overlay:
        page.overlay.append(file_picker_latex)

    # 3. La acción que dispara el botón
    def accion_exportar_latex(e):
        res_hydros = page.session.get("res_hidrogramas")
        
        # Validación de seguridad
        if not res_hydros:
            page.snack_bar = ft.SnackBar(ft.Text("Primero debes CALCULAR el modelo.", color="black", weight="bold"), bgcolor="orange", open=True)
            page.update()
            return
            
        # Abrimos el cuadro de diálogo de Windows/Mac pidiendo una CARPETA
        file_picker_latex.get_directory_path(dialog_title="Selecciona la carpeta para guardar el Reporte LaTeX")

    # 4. El botón de la Interfaz
    btn_latex = ft.ElevatedButton(
        "Exportar Memoria LaTeX", 
        icon=ft.Icons.PICTURE_AS_PDF, 
        bgcolor="#1c75fa", 
        color="white",
        on_click=accion_exportar_latex
    )
    
    # ==========================================
    # --- MOTOR DE EXPORTACIÓN EXCEL (CON DIÁLOGO) ---
    # ==========================================
    def guardar_excel_en_ruta(e: ft.FilePickerResultEvent):
        if not e.path: return
        
        # Extracción de sesión
        df_vars = page.session.get("df_variables")
        res_r = page.session.get("res_racional")
        res_c = page.session.get("res_chow")
        res_h = page.session.get("res_hms_peak")
        res_hydros = page.session.get("res_hidrogramas")
        
        exito, msj = gastos.exportar_resultados_excel(e.path, df_vars, res_r, res_c, res_h, res_hydros)
        
        color_sb = "#00ff41" if exito else "red"
        page.snack_bar = ft.SnackBar(ft.Text(msj, color="black", weight="bold"), bgcolor=color_sb, open=True)
        page.update()

    file_picker_excel = ft.FilePicker(on_result=guardar_excel_en_ruta)
    if file_picker_excel not in page.overlay: page.overlay.append(file_picker_excel)

    def accion_exportar_excel(e):
        if not page.session.get("res_hidrogramas"):
            page.snack_bar = ft.SnackBar(ft.Text("Primero debes CALCULAR el modelo.", color="black", weight="bold"), bgcolor="orange", open=True)
            page.update()
            return
        file_picker_excel.save_file(dialog_title="Guardar Matriz de Resultados", file_name="Resultados_Hidrologicos.xlsx", allowed_extensions=["xlsx"])

    btn_excel = ft.ElevatedButton(
        "Matriz Excel", 
        icon=ft.Icons.TABLE_VIEW, 
        bgcolor="#217346", # Verde corporativo de MS Excel
        color="white",
        on_click=accion_exportar_excel
    )
    
    tab_res = ft.Tab(
        text="4. Cálculos", 
        icon=ft.Icons.ANALYTICS, 
        content=ft.Container(
            content=ft.Column([
                ft.Text("Paso 4: Caudales Máximos y Memorias", color="#00ff41", size=20, weight="bold"),
                ft.Divider(), 
                # AÑADIMOS EL BOTÓN AQUÍ:
                ft.Row([btn_calc, btn_detener, btn_latex, btn_excel], alignment=ft.MainAxisAlignment.CENTER), 
                log_txt, 
                ft.Container(tabs_res, padding=ft.padding.only(top=20)) 
            ], scroll=ft.ScrollMode.AUTO), 
            padding=20, 
            expand=True
        )
    )
    
    main_tabs = ft.Tabs(selected_index=0, animation_duration=300, tabs=[tab_area_cotas, tab_conf, tab_data, tab_res], expand=True)

    # ==========================================
    # 10. RESTAURACIÓN Y AUTO-THIESSEN
    # ==========================================
    async def delayed_restore():
        await asyncio.sleep(0.1) 
        try:
            # 1. Cargamos Dropdown de escenarios
            actualizar_dropdown_escenarios()
            
            # 2. Inyección Mágica: Traemos datos procesados del Módulo 3 (silencioso al arranque)
            auto_cargar_thiessen(silencioso=True)
            
            # --- EVALUACIÓN FASE 7.1 (ESTADO ARF) ---
            requiere_arf = page.session.get("arf_requerido")
            if requiere_arf is False:
                banner_alerta_arf.visible = True
                dd_tormenta.tooltip = "El Factor de Reducción Areal (ARF) está apagado para esta cuenca."
            else:
                banner_alerta_arf.visible = False
                dd_tormenta.tooltip = "SCS Tipo II/III buscan automáticamente la P24. (Se aplicará ARF Dinámico calculado en Módulo 4)."

            # 3. Restaurar UI
            render_manual_tables()
            validar_nombres_cuencas()
            evaluar_escala_cuenca_y_actualizar_ui()
            if rg_modo_calculo.value != "regional_arf":
                rg_modo_calculo.value = page.session.get("modo_lluvia_activo") or "simple"
            cambiar_modo_lluvia(None)
            upd_tbl()
            render_estaciones()
            
            if page.session.get("res_racional") is not None:
                construir_visualizacion_resultados(
                    page.session.get("res_racional"), page.session.get("res_chow"),
                    page.session.get("res_hms_peak"), page.session.get("res_hidrogramas")
                )
                tabs_res.visible = True
            page.update()
        except Exception as ex: print(f"Error en rehidratación: {ex}")

    page.run_task(delayed_restore)
    
    # RETORNO CON EL GESTOR DE ESCENARIOS ENCABEZANDO Y AJUSTE DE MÁRGENES
    return ft.Column([
        top_bar_escenarios,
        # POR QUÉ: Expandimos el padding derecho a 20 para empujar la barra contra el límite de la ventana y liberar el espacio de visualización de las tablas de gastos.
        ft.Container(
            content=main_tabs, 
            expand=True, 
            padding=ft.padding.only(left=10, right=20, top=10, bottom=10)
        )
    ], expand=True, horizontal_alignment=ft.CrossAxisAlignment.STRETCH) # <--- CORRECCIÓN DE MAR