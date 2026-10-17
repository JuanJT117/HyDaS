import flet as ft
import pandas as pd
import os
import asyncio
 # 2. --- PERSISTENCIA CRUCIAL (OPTIMIZADA A DISCO PARQUET) ---
import tempfile
import uuid
# --- ARQUITECTURA HEXAGONAL: Importación desde el Core ---
from core import climatologia_logic

def build_climatologia_view(page: ft.Page):
    
    # --- 1. INICIALIZACIÓN DE LA BASE DE DATOS DE PERSISTENCIA (Aislada) ---
    if not page.session.get("db_climatologia_procesada"):
        page.session.set("db_climatologia_procesada", {})

    # --- ESTADO VISUAL TEMPORAL AISLADO ---
    estado = {"df_activo": None, "nombre_estacion": "", "carpeta_base": ""}

    # 2. Cabeceras
    title = ft.Text("CLIMATOLOGÍA LOCAL", size=24, weight="bold", color="#00ff41")
    subtitle = ft.Text("Análisis de Extremos Termopluviométricos (Persistencia de Consulta Activa)", color="grey", size=14)

    # 3. Controles de Estado y Salida
    txt_status = ft.Text("Selecciona el directorio con los archivos .txt de las estaciones...", color="orange", size=12)
    img_climograma = ft.Image(src_base64="", visible=False, fit=ft.ImageFit.CONTAIN, expand=True)
    
    # Blindaje contra Crash de Flet (Mínimo un DataColumn visible requerido)
    dt_normales = ft.DataTable(columns=[ft.DataColumn(ft.Text(""))], rows=[], visible=False, column_spacing=20)
    dt_extremos = ft.DataTable(columns=[ft.DataColumn(ft.Text(""))], rows=[], visible=False)

    # --- 4. MOTOR DE EXPORTACIÓN LATEX ---
    def guardar_reporte_latex(e: ft.FilePickerResultEvent):
        if not e.path or estado["df_activo"] is None: return
        
        db_clima = page.session.get("db_climatologia_procesada") or {}
        est_id = estado["nombre_estacion"]
        
        # Recuperamos directamente del almacén persistente para evitar recalculaciones
        if est_id in db_clima:
            raw_norm = db_clima[est_id]["clima_df_normales"]
            if isinstance(raw_norm, pd.DataFrame):
                df_normales = raw_norm
            elif isinstance(raw_norm, dict) and "path" in raw_norm and os.path.exists(raw_norm["path"]):
                df_normales = pd.read_parquet(raw_norm["path"])
            elif isinstance(raw_norm, str) and os.path.exists(raw_norm):
                df_normales = pd.read_parquet(raw_norm)
            else:
                df_normales = climatologia_logic.calcular_normales_climatologicas(estado.get("df_activo"))
            extremos = db_clima[est_id]["clima_extremos"]
        else:
            df_normales = climatologia_logic.calcular_normales_climatologicas(estado["df_activo"])
            extremos = climatologia_logic.obtener_extremos_historicos(estado["df_activo"])
        
        exito, msj = climatologia_logic.exportar_reporte_climatico_latex(e.path, df_normales, extremos, est_id)
        color_sb = "#00ff41" if exito else "red"
        page.snack_bar = ft.SnackBar(ft.Text(msj, color="black", weight="bold"), bgcolor=color_sb, open=True)
        page.update()

    picker_reporte = page.session.get("clima_picker_reporte")
    if not picker_reporte:
        picker_reporte = ft.FilePicker(on_result=guardar_reporte_latex)
        page.session.set("clima_picker_reporte", picker_reporte)
        page.overlay.append(picker_reporte)
    else:
        picker_reporte.on_result = guardar_reporte_latex
    
    btn_reporte = ft.ElevatedButton(
        "Generar Reporte Climático",
        icon=ft.Icons.ARTICLE,
        on_click=lambda _: picker_reporte.save_file(dialog_title="Exportar Reporte LaTeX", file_name=f"Reporte_Climatico_{estado['nombre_estacion']}.tex", allowed_extensions=["tex"]),
        bgcolor="#00ff41", color="black", disabled=True
    )

    # ==========================================
    # CONTROLADOR DE ENTRADA Y REHIDRATACIÓN (CONTRATO POOL)
    # ==========================================
    def al_cambiar_estacion(est_id):
        if not est_id: return
        db_clima = page.session.get("db_climatologia_procesada") or {}
        
        # --- [FUENTE DE VERDAD] CAPA DE CONSULTA DIRECTA ---
        if est_id in db_clima:
            txt_status.value = f"🚀 Estación {est_id}: Recuperada instantáneamente de la memoria del proyecto."
            txt_status.color = "#00ff41"
            
            data_saved = db_clima[est_id]
            
            try:
                raw_act = data_saved["clima_df_activo"]
                estado["df_activo"] = raw_act if isinstance(raw_act, pd.DataFrame) else pd.read_parquet(raw_act["path"])
                raw_norm = data_saved["clima_df_normales"]
                df_normales_ui = raw_norm if isinstance(raw_norm, pd.DataFrame) else pd.read_parquet(raw_norm["path"])
            except Exception as e:
                txt_status.value, txt_status.color = f"❌ Error leyendo caché: {e}", "red"; page.update(); return
                
            estado["nombre_estacion"] = data_saved["clima_nombre_estacion"]
            btn_reporte.disabled = False
            
            # Pintamos de golpe las estructuras renderizadas guardadas
            rehidratar_tablas_y_graficos(
                df_normales_ui, 
                data_saved["clima_extremos"], 
                data_saved["clima_b64_img"]
            )
        else:
            # --- CAPA DE PROCESAMIENTO (Solo si no existe en la BD del proyecto) ---
            auto_procesar_estacion(est_id)

    dd_estaciones = ft.Dropdown(
        label="Estaciones Procesadas Disponibles", 
        width=350, border_color="#00ff41", disabled=True,
        on_change=lambda e: al_cambiar_estacion(e.control.value)
    )

    def procesar_directorio(e: ft.FilePickerResultEvent):
        if not e.path: return
        carpeta = e.path
        estado["carpeta_base"] = carpeta
        
        est_db = page.session.get("estaciones_db") or {}
        opciones_validas = []
        
        for est_id in est_db.keys():
            ruta_esperada = os.path.join(carpeta, f"{est_id}.txt")
            if os.path.exists(ruta_esperada):
                opciones_validas.append(ft.dropdown.Option(str(est_id)))
                
        if opciones_validas:
            dd_estaciones.options = opciones_validas
            dd_estaciones.disabled = False
            txt_status.value = f"✅ Vinculación Exitosa: {len(opciones_validas)} estaciones listas para consulta."
            txt_status.color = "#00ff41"
        else:
            dd_estaciones.options = []
            dd_estaciones.disabled = True
            txt_status.value = "❌ No se encontraron archivos físicos (.txt) para las estaciones del proyecto en esta carpeta."
            txt_status.color = "red"
        page.update()

    picker_carpeta = page.session.get("clima_picker_carpeta")
    if not picker_carpeta:
        picker_carpeta = ft.FilePicker(on_result=procesar_directorio)
        page.session.set("clima_picker_carpeta", picker_carpeta)
        page.overlay.append(picker_carpeta)
    else:
        picker_carpeta.on_result = procesar_directorio

    # ==========================================
    # CEREBRO DE CÁLCULO Y VOLCADO A PERSISTENCIA
    # ==========================================
    def auto_procesar_estacion(est_id):
        txt_status.value = f"Ejecutando fusión termopluviométrica para {est_id}..."
        txt_status.color = "white"
        page.update()

        ruta_txt = os.path.join(estado["carpeta_base"], f"{est_id}.txt")
        exito, df_raw, msj = climatologia_logic.leer_estacion_climatologica(ruta_txt)
        if not exito:
            txt_status.value = f"❌ Error Crítico: {msj}"; txt_status.color = "red"; page.update(); return

        df_filt = climatologia_logic.obtener_lluvia_limpia_automatica(est_id, page.session)
        if df_filt is None:
            txt_status.value = f"⚠️ Usando datos crudos. Falta análisis de probabilidad para {est_id}."
            txt_status.color = "orange"
            estado["df_activo"] = df_raw
        else:
            exito_fus, df_fusion, msj_fus = climatologia_logic.fusionar_precipitacion_filtrada(df_raw, df_filt)
            if exito_fus:
                estado["df_activo"] = df_fusion
                txt_status.value = f"✅ Estación {est_id} procesada y guardada en el proyecto."
                txt_status.color = "#00ff41"
            else:
                txt_status.value = f"❌ Fallo al fusionar: {msj_fus}"; txt_status.color = "red"; return

        estado["nombre_estacion"] = est_id
        btn_reporte.disabled = False
        
        # 1. Generar estructuras analíticas finales
        df_normales = climatologia_logic.calcular_normales_climatologicas(estado["df_activo"])
        extremos = climatologia_logic.obtener_extremos_historicos(estado["df_activo"])
        b64_img = climatologia_logic.generar_climograma_b64(df_normales, titulo_estacion=estado["nombre_estacion"])
        
        # Generamos una bóveda temporal estricta para esta sesión
        cache_dir = os.path.join(tempfile.gettempdir(), "HyDaS_Clima_Cache")
        os.makedirs(cache_dir, exist_ok=True)
        
        # Sanitizamos (Forzamos strings) y guardamos en disco para no ahogar la RAM
        path_activo = os.path.join(cache_dir, f"activo_{est_id}_{uuid.uuid4().hex[:6]}.parquet")
        df_safe_activo = estado["df_activo"].copy()
        df_safe_activo.columns = df_safe_activo.columns.astype(str)
        df_safe_activo.to_parquet(path_activo, engine="pyarrow")
        
        path_normales = os.path.join(cache_dir, f"norm_{est_id}_{uuid.uuid4().hex[:6]}.parquet")
        df_safe_norm = df_normales.copy()
        df_safe_norm.columns = df_safe_norm.columns.astype(str)
        df_safe_norm.to_parquet(path_normales, engine="pyarrow")

        # Volcamos SOLO LAS RUTAS al diccionario maestro de la sesión
        db_clima = page.session.get("db_climatologia_procesada") or {}
        db_clima[est_id] = {
            "clima_df_activo": {"type": "parquet", "path": path_activo},
            "clima_df_normales": {"type": "parquet", "path": path_normales},
            "clima_extremos": extremos,
            "clima_b64_img": b64_img,
            "clima_nombre_estacion": estado["nombre_estacion"]
        }
        page.session.set("db_climatologia_procesada", db_clima)
        
        # 3. Renderizar vista de inmediato
        rehidratar_tablas_y_graficos(df_normales, extremos, b64_img)

    def rehidratar_tablas_y_graficos(df_normales, extremos, b64_img):
        """ Inyecta los objetos directamente en los controles visuales Flet """
        if extremos:
            dt_extremos.columns = [ft.DataColumn(ft.Text("Evento", color="#00ff41")), ft.DataColumn(ft.Text("Registro"))]
            dt_extremos.rows = [
                ft.DataRow([ft.DataCell(ft.Text("Temp. Máxima Absoluta", weight="bold")), ft.DataCell(ft.Text(f"{extremos.get('Temp_Max_Absoluta')} °C  ({extremos.get('Fecha_Temp_Max')})", color="#cc0000"))]),
                ft.DataRow([ft.DataCell(ft.Text("Temp. Mínima Absoluta", weight="bold")), ft.DataCell(ft.Text(f"{extremos.get('Temp_Min_Absoluta')} °C  ({extremos.get('Fecha_Temp_Min')})", color="#00aaff"))])
            ]
            dt_extremos.visible = True

        if df_normales is not None:
            df_normales['Mes'] = df_normales['Mes'].astype(int)
            cols = [ft.DataColumn(ft.Text(str(c).replace('_', ' '), color="#00ff41", weight="bold")) for c in df_normales.columns]
            rows = [ft.DataRow([ft.DataCell(ft.Text(str(val))) for val in row]) for _, row in df_normales.iterrows()]
            dt_normales.columns = cols
            dt_normales.rows = rows
            dt_normales.visible = True

        if b64_img:
            img_climograma.src_base64 = b64_img
            img_climograma.visible = True

        page.update()

    # ==========================================
    # ENSAMBLAJE DE UI CON AUTO-RESTAURACIÓN AL MONTAR
    # ==========================================
    panel_seleccion = ft.Container(
        content=ft.Column([
            ft.Text("VINCULACIÓN DE ARCHIVOS FÍSICOS", weight="bold", color="#1c75fa"),
            ft.Row([
                ft.ElevatedButton("Cargar Carpeta de Estaciones (.txt)", icon=ft.Icons.FOLDER_OPEN, on_click=lambda _: picker_carpeta.get_directory_path(), bgcolor="#222222", color="white"),
                dd_estaciones,
                btn_reporte
            ]),
            txt_status
        ]),
        padding=15, border=ft.border.all(1, "#333333"), border_radius=8, bgcolor="#0a0a0a"
    )

    async def trigger_hydration():
        """ Auto-restaurar selección si el usuario ya venía trabajando en una estación """
        await asyncio.sleep(0.1)
        db_clima = page.session.get("db_climatologia_procesada") or {}
        
        # --- NUEVO: AUTO-DESCUBRIMIENTO DE CARPETA DEL PROYECTO ---
        respaldo = page.session.get("txt_backup_imputados") or page.session.get("txt_backup")
        carpeta_fuente = respaldo.get("path") if isinstance(respaldo, dict) else page.session.get("imput_folder_path")
        if carpeta_fuente and os.path.exists(carpeta_fuente):
            class MockEvent:
                path = carpeta_fuente
            procesar_directorio(MockEvent())
            
        # Si ya hay procesos guardados en memoria del proyecto, re-llenamos el Dropdown preventivamente
        if db_clima:
            opciones_actuales = [opt.key for opt in dd_estaciones.options] if dd_estaciones.options else []
            for k in db_clima.keys():
                if str(k) not in opciones_actuales:
                    if dd_estaciones.options is None:
                        dd_estaciones.options = []
                    dd_estaciones.options.append(ft.dropdown.Option(str(k)))
                    
            dd_estaciones.disabled = False
            txt_status.value = f"📊 Proyecto con {len(db_clima)} estaciones climatológicas en persistencia activa."
            txt_status.color = "#00ff41"
            page.update()

    page.run_task(trigger_hydration)

    content = ft.Column([
        title, subtitle, ft.Divider(color="#222222"),
        panel_seleccion,
        ft.Divider(color="transparent", height=10),
        
        ft.Row([
            ft.Column([
                ft.Row([ft.Icon(ft.Icons.WARNING_AMBER, color="#00ff41"), ft.Text("Extremos Térmicos", weight="bold", size=16)]), 
                ft.Container(content=ft.Row([dt_extremos], scroll=ft.ScrollMode.AUTO), border=ft.border.all(1, "#333333"), border_radius=5, padding=10, bgcolor="#0a0a0a")
            ], alignment=ft.MainAxisAlignment.START),
            
            ft.Column([
                ft.Row([ft.Icon(ft.Icons.CALENDAR_MONTH, color="#00ff41"), ft.Text("Estadística de Máximas Mensuales y Evaporación", weight="bold", size=16)]), 
                ft.Container(content=ft.Row([dt_normales], scroll=ft.ScrollMode.AUTO), border=ft.border.all(1, "#333333"), border_radius=5)
            ], alignment=ft.MainAxisAlignment.START)
        ], vertical_alignment=ft.CrossAxisAlignment.START, spacing=30, scroll=ft.ScrollMode.AUTO),
        
        ft.Divider(color="transparent", height=15),
        ft.Row([ft.Icon(ft.Icons.AREA_CHART, color="#00ff41"), ft.Text("Climograma de Eventos Extremos y Evaporación", weight="bold", size=18)]),
        ft.Container(content=img_climograma, alignment=ft.alignment.center, padding=10, border=ft.border.all(1, "#333333"), border_radius=10, bgcolor="#ffffff", expand=True)
        
    ], scroll=ft.ScrollMode.AUTO, expand=True) 

    return ft.Container(content=content, expand=True, padding=20)