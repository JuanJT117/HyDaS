import flet as ft
import os
import threading
import webbrowser
import asyncio
import pandas as pd
import tempfile

# --- ARQUITECTURA HEXAGONAL: Importación desde el Core ---
from core import imputacion_logic 
from ui.components import COLOR_ACENTO, COLOR_SUPERFICIE, FUENTE_PRINCIPAL, add_opacity

def build_imputacion_view(page: ft.Page):
    
    # ==========================================
    # 1. INICIALIZACIÓN DE SESIÓN (Protegida)
    # ==========================================
    if not page.session.get("imput_folder_path"): page.session.set("imput_folder_path", None)
    if not page.session.get("imput_output_folder"): page.session.set("imput_output_folder", None)
    if not page.session.get("imput_station_files"): page.session.set("imput_station_files", {})
    if not page.session.get("imput_map_path"): page.session.set("imput_map_path", None)
    
    # ESTRUCTURA DE DATOS PARA PROCESAMIENTO MÚLTIPLE
    if not page.session.get("db_series_crudas"): page.session.set("db_series_crudas", {})
    if not page.session.get("cola_imputacion"): page.session.set("cola_imputacion", [])

    # ==========================================
    # 2. VARIABLES DE ESTADO Y UI
    # ==========================================
    # Variable de control de hilos (Global dentro de la vista)
    state = {"imputing": False}
    
    # --- SISTEMA DE BLOQUEO ZERO-TRUST ---
    
    # 1. Nuevos controles de progreso lineal y numérico
    texto_porcentaje = ft.Text("0%", color="#00f0ff", weight="bold", size=50, font_family="Roboto Mono")
    pb = ft.ProgressBar(width=600, color="#00f0ff", bgcolor="#111111", value=0)
    pbl = ft.Text("Preparando entorno matemático...", color="#00f0ff", size=12, italic=True, font_family="Roboto Mono")
    
    terminal_radiactiva = ft.ListView(expand=True, spacing=5, auto_scroll=True)
    
    bloqueo_radiactivo = ft.Container(
        content=ft.Column([
            texto_porcentaje,  # <--- El porcentaje gigante
            pb,                # <--- La barra lineal llenándose
            pbl,               # <--- El texto que dice en qué paso va
            ft.Divider(color="transparent", height=10),
            ft.Text("SISTEMA DE IMPUTACIÓN ACTIVO", color="#00f0ff", weight="bold", size=22, font_family="Roboto Mono"),
            ft.Text("Procesando Tensores SARIMAX y Regresión Múltiple. No cierre la ventana.", color="grey", size=12),
            ft.Container(
                content=terminal_radiactiva, height=250, width=700,
                bgcolor="#050505", border=ft.border.all(1, "#00f0ff"), border_radius=5, padding=10
            )
        ], alignment=ft.MainAxisAlignment.CENTER, horizontal_alignment=ft.CrossAxisAlignment.CENTER),
        bgcolor=add_opacity("#050505", 0.95), expand=True, alignment=ft.alignment.center, visible=False
    )
    for c in list(page.overlay):
        if getattr(c, "_is_imput_bloqueo", False):
            page.overlay.remove(c)
    bloqueo_radiactivo._is_imput_bloqueo = True
    page.overlay.append(bloqueo_radiactivo)

    def _get_or_create_picker(key, on_result):
        p = page.session.get(key)
        if not p:
            p = ft.FilePicker(on_result=on_result)
            page.session.set(key, p)
            page.overlay.append(p)
        else:
            p.on_result = on_result
        return p

    # 2. Clase Interceptora (Proxy Pattern): Traduce el progreso crudo a porcentaje visual
    class ProgressInterceptor:
        def __init__(self, pb_real, txt_porc):
            self.pb = pb_real
            self.txt = txt_porc
        @property
        def value(self): 
            return self.pb.value
        @value.setter
        def value(self, v):
            self.pb.value = v
            if v is not None: 
                self.txt.value = f"{int(v * 100)}%"

    pb_interceptado = ProgressInterceptor(pb, texto_porcentaje)
    
    def log_radiactivo(msg):
        def _update():
            terminal_radiactiva.controls.append(ft.Text(f"> {msg}", color="#00f0ff", font_family="monospace", size=11))
            if len(terminal_radiactiva.controls) > 50:
                terminal_radiactiva.controls.pop(0)
            page.update()
        page.run_thread(_update)

    # --- SISTEMA DE EXPORTACIÓN CSV INDIVIDUAL ---
    estacion_a_exportar = [None]
    def manejar_exportacion(e: ft.FilePickerResultEvent):
        if e.path and estacion_a_exportar[0] is not None:
            try:
                estacion_a_exportar[0].to_csv(e.path, index=False)
                page.snack_bar = ft.SnackBar(ft.Text("✅ Archivo CSV Exportado con Éxito"), bgcolor="#00f0ff", open=True)
            except Exception as ex:
                page.snack_bar = ft.SnackBar(ft.Text(f"❌ Error al guardar: {ex}"), bgcolor="red", open=True)
            page.update()

    picker_exportar_csv = _get_or_create_picker("imput_picker_exportar_csv", manejar_exportacion)
    
    # --- SISTEMA DE EXPORTACIÓN LOG INDIVIDUAL (QA) ---
    log_a_exportar = [None]
    def manejar_exportacion_log(e: ft.FilePickerResultEvent):
        if e.path and log_a_exportar[0] is not None:
            try:
                import datetime
                timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                texto_log = str(log_a_exportar[0])
                if not texto_log.startswith("==="):
                    header = (
                        "=================================================================\n"
                        " REPORTE DE CONTROL DE CALIDAD (QA) - IMPUTACIÓN MATEMÁTICA\n"
                        "=================================================================\n"
                        f" Fecha de Descarga : {timestamp}\n"
                        "=================================================================\n\n"
                    )
                    footer = "\n\n=================================================================\n FIN DEL REPORTE\n"
                    texto_final = header + texto_log + footer
                else:
                    texto_final = texto_log

                with open(e.path, 'w', encoding='utf-8') as f:
                    f.write(texto_final)
                page.snack_bar = ft.SnackBar(ft.Text("✅ Informe QA Exportado con Éxito"), bgcolor="#00f0ff", open=True)
            except Exception as ex:
                page.snack_bar = ft.SnackBar(ft.Text(f"❌ Error al guardar log: {ex}"), bgcolor="red", open=True)
            page.update()

    picker_exportar_log = _get_or_create_picker("imput_picker_exportar_log", manejar_exportacion_log)
    
    # --- SISTEMA DE EXPORTACIÓN THIESSEN ---
    def manejar_exportacion_thiessen(e: ft.FilePickerResultEvent):
        if e.path:
            station_files = page.session.get("imput_station_files")
            if not station_files:
                page.snack_bar = ft.SnackBar(ft.Text("❌ No hay estaciones en memoria para calcular Thiessen."), bgcolor="red", open=True)
                page.update()
                return
            
            try:
                page.snack_bar = ft.SnackBar(ft.Text("⏳ Generando Polígonos de Thiessen..."), bgcolor="orange", open=True)
                page.update()
                
                path_shp = imputacion_logic.generar_poligonos_thiessen(station_files, e.path)
                if path_shp:
                    page.session.set("thiessen_shp_path", path_shp)
                    page.snack_bar = ft.SnackBar(ft.Text("✅ Polígonos de Thiessen exportados con éxito"), bgcolor="#00f0ff", open=True)
                else:
                    page.snack_bar = ft.SnackBar(ft.Text("❌ No se pudieron generar los polígonos."), bgcolor="red", open=True)
            except Exception as ex:
                page.snack_bar = ft.SnackBar(ft.Text(f"❌ Error al calcular Thiessen: {ex}"), bgcolor="red", open=True)
            page.update()

    picker_exportar_thiessen = _get_or_create_picker("imput_picker_exportar_thiessen", manejar_exportacion_thiessen)
    # ------------------------------------------------
    
    # Controles de Texto y Estado
    selected_input_folder = ft.Text("Ninguna carpeta seleccionada", color="grey", size=12)
    selected_output_folder = ft.Text("Carpeta de salida: Pendiente", color="grey", size=12)
    station_count_text = ft.Text("", visible=False, color=COLOR_ACENTO, weight="bold")
    
    # Consola y Progreso
    # Consola y Progreso
    log_result = ft.Text("", visible=False, size=11, font_family="monospace")
    terminal_list = ft.ListView(expand=True, spacing=5, auto_scroll=True)

    # Contenedores Dinámicos
    station_files_container = ft.Column(spacing=10, scroll=ft.ScrollMode.AUTO)
    cola_espera_ui = ft.Column(spacing=5, scroll=ft.ScrollMode.AUTO)
    visor_resultados_grid = ft.Row(wrap=True, spacing=10, scroll=ft.ScrollMode.AUTO)

    # ==========================================
    # 3. MANEJADORES DE ARCHIVOS (PICKERS)
    # ==========================================
    def auto_procesar_fuente(ruta_fuente):
        """Motor central de lectura. Puede ser llamado por el Picker o por auto-detección."""
        if not ruta_fuente or not os.path.exists(ruta_fuente): return
        
        page.session.set("imput_folder_path", ruta_fuente)
        selected_input_folder.value = f"Fuente (Auto-detectada): {ruta_fuente}"
        
        estaciones = imputacion_logic.leer_estaciones(ruta_fuente)
        if estaciones:
            page.session.set("imput_station_files", estaciones)
            map_path = imputacion_logic.generar_mapa_html(estaciones, ruta_fuente)
            if map_path:
                page.session.set("imput_map_path", {"type": "file", "path": map_path})
            
            restore_station_ui(estaciones)
            open_map_button.disabled = False
            export_thiessen_button.disabled = False
            
            area_c = page.session.get("area_cuenca_km2") or 0.0
            impute_masiva_button.disabled = (area_c <= 25)
            impute_masiva_button.tooltip = "Disponible solo para cuencas > 25 km²" if area_c <= 25 else "Procesa todas las estaciones por bloques"
            
            page.snack_bar = ft.SnackBar(ft.Text(f"✅ {len(estaciones)} estaciones enlazadas desde la memoria del proyecto."), bgcolor="#00ff41", open=True)
        else:
            page.snack_bar = ft.SnackBar(ft.Text("⚠️ La carpeta detectada no contiene archivos .txt válidos"), bgcolor="orange", open=True)
        page.update()

    def on_input_dir_result(e: ft.FilePickerResultEvent):
        if e.path:
            auto_procesar_fuente(e.path)

    def on_output_dir_result(e: ft.FilePickerResultEvent):
        if e.path:
            page.session.set("imput_output_folder", e.path)
            selected_output_folder.value = f"Destino: {e.path}"
            page.update()

    file_picker_input = _get_or_create_picker("imput_file_picker_input", on_input_dir_result)
    file_picker_output = _get_or_create_picker("imput_file_picker_output", on_output_dir_result)

    # ==========================================
    # 4. LÓGICA DE COLA, MEMORIA E INSPECCIÓN
    # ==========================================
    def _resolver_ruta_bd():
        """Resuelve de forma robusta la ruta hacia el archivo SQLite/GeoPackage activo (.hds)."""
        ruta_bd = page.session.get("current_project_path")
        if not ruta_bd:
            ruta_bd = page.session.get("ruta_bd_activa")
        if not ruta_bd:
            out_folder = page.session.get("imput_output_folder") or os.getcwd()
            ruta_bd = os.path.join(out_folder, "HyDaS_Proyecto.hds")
            page.session.set("current_project_path", ruta_bd)

        from core.imputacion_logic import resolver_ruta_hds
        return resolver_ruta_hds(ruta_bd)

    def agregar_a_cola(sid):
        cola = page.session.get("cola_imputacion")
        if sid not in cola:
            cola.append(sid)
            page.session.set("cola_imputacion", cola)
            render_cola_visual()
            
            # --- FEEDBACK VISUAL EN TIEMPO REAL ---
            indicador_estacion.value = f"🟢 [LISTO] Estación {sid} añadida a la cola de procesamiento."
            indicador_estacion.color = "#00f0ff"
            
            # --- RE-RENDER REACTIVO: Forzamos el rediseño de las tarjetas para aplicar el azul radiactivo ---
            estaciones_actuales = page.session.get("imput_station_files") or {}
            restore_station_ui(estaciones_actuales, actualizar_textos=False)
            
            page.snack_bar = ft.SnackBar(ft.Text(f"Estación {sid} en cola"), bgcolor="blue", open=True)
            page.update()

    def quitar_de_cola(sid):
        cola = page.session.get("cola_imputacion")
        if sid in cola: cola.remove(sid)
        page.session.set("cola_imputacion", cola)
        render_cola_visual()
        
        # --- RE-RENDER REACTIVO: Restauramos la tarjeta a su estado normal al salir de la cola ---
        estaciones_actuales = page.session.get("imput_station_files") or {}
        restore_station_ui(estaciones_actuales, actualizar_textos=False)

    def render_cola_visual():
        cola_espera_ui.controls.clear()
        for sid in (page.session.get("cola_imputacion") or []):
            cola_espera_ui.controls.append(
                ft.Row([
                    ft.Icon(ft.Icons.TIMER, color="orange", size=20),
                    ft.Text(f"{sid} (Espera)", expand=True),
                    ft.IconButton(ft.Icons.DELETE_OUTLINE, icon_color="red", on_click=lambda e, s=sid: quitar_de_cola(s))
                ])
            )
        page.update()

    def render_visor_resultados():
        visor_resultados_grid.controls.clear()
        db = page.session.get("db_series_crudas") or {}
        
        def _trigger_download(sid):
            try:
                import sqlite3
                ruta_hds = _resolver_ruta_bd()
                if not ruta_hds or not os.path.exists(ruta_hds):
                    raise Exception("Base de datos no disponible.")
                    
                conn = sqlite3.connect(ruta_hds)
                df = pd.read_sql_query(f"SELECT * FROM serie_imputada_{sid}", conn)
                conn.close()
                
                if df.empty:
                    raise Exception(f"No hay datos registrados para la estación {sid}.")
                
                estacion_a_exportar[0] = df
                picker_exportar_csv.save_file(
                    dialog_title=f"Exportar {sid} a CSV",
                    file_name=f"{sid}_imputado.csv",
                    allowed_extensions=["csv"]
                )
            except Exception as e:
                page.snack_bar = ft.SnackBar(ft.Text(f"❌ Error al exportar de BD: {e}"), bgcolor="red", open=True)
                page.update()

        def _trigger_download_log(sid, data_dict=None):
            if data_dict is None:
                db_crudas = page.session.get("db_series_crudas") or {}
                data_dict = db_crudas.get(sid, {})
                
            log_text = data_dict.get("log", "") if isinstance(data_dict, dict) else ""
            log_path_src = data_dict.get("log_path") if isinstance(data_dict, dict) else None
            
            if log_path_src and os.path.exists(log_path_src):
                try:
                    with open(log_path_src, "r", encoding="utf-8") as f:
                        log_text = f.read()
                except Exception:
                    pass
                    
            if not log_text:
                ruta_hds = _resolver_ruta_bd()
                log_text = imputacion_logic.obtener_log_de_sqlite_hds(sid, ruta_hds)
                
            if not log_text:
                log_text = f"Informe de control de calidad para estación {sid}.\nProceso de imputación finalizado."
                
            log_a_exportar[0] = log_text
            picker_exportar_log.save_file(
                dialog_title=f"Descargar Informe QA de {sid}",
                file_name=f"QA_Log_{sid}.txt",
                allowed_extensions=["txt"]
            )

        def _eliminar_registro(sid):
            try:
                # 1. Eliminar de la sesión activa
                db_session = page.session.get("db_series_crudas") or {}
                if sid in db_session:
                    del db_session[sid]
                    page.session.set("db_series_crudas", db_session)
                    
                # 2. Eliminar de la base de datos SQLite (.gpkg / .hds)
                ruta_hds = _resolver_ruta_bd()
                imputacion_logic.eliminar_estacion_de_sqlite_hds(sid, ruta_hds)
                
                # 3. Refrescar interfaz gráfica
                render_visor_resultados()
                estaciones_actuales = page.session.get("imput_station_files") or {}
                restore_station_ui(estaciones_actuales, actualizar_textos=False)
                
                page.snack_bar = ft.SnackBar(ft.Text(f"🗑️ Registro de imputación de {sid} eliminado."), bgcolor="orange", open=True)
                page.update()
            except Exception as ex:
                page.snack_bar = ft.SnackBar(ft.Text(f"❌ Error al eliminar {sid}: {ex}"), bgcolor="red", open=True)
                page.update()

        def _confirmar_eliminar(sid):
            dialog_confirm = ft.AlertDialog(
                title=ft.Row([
                    ft.Icon(ft.Icons.WARNING_AMBER_ROUNDED, color="red"),
                    ft.Text("Confirmar Eliminación", color="white", weight="bold")
                ]),
                content=ft.Text(f"¿Deseas eliminar el registro de imputación de la estación {sid}? Esta acción borrará los datos calculados de la sesión y de la base de datos."),
                actions=[
                    ft.TextButton("Cancelar", on_click=lambda _: page.close(dialog_confirm)),
                    ft.ElevatedButton("Eliminar", bgcolor="red", color="white", on_click=lambda _: [page.close(dialog_confirm), _eliminar_registro(sid)])
                ],
                actions_alignment=ft.MainAxisAlignment.END
            )
            page.open(dialog_confirm)

        for sid, data in db.items():
            visor_resultados_grid.controls.append(
                ft.Container(
                    content=ft.Row([
                        ft.Icon(ft.Icons.CHECK_CIRCLE, color="green", size=16), 
                        ft.Text(sid, size=12, weight="bold"),
                        # Botón Inspeccionar
                        ft.IconButton(ft.Icons.REMOVE_RED_EYE, icon_color="white", icon_size=16, tooltip="Inspeccionar Datos", on_click=lambda e, s=sid: abrir_inspeccion(s)),
                        # Botón Exportar CSV
                        ft.IconButton(ft.Icons.DOWNLOAD, icon_color="#00f0ff", icon_size=16, tooltip="Exportar a CSV", on_click=lambda e, s=sid: _trigger_download(s)),
                        # Botón Exportar Informe QA (LOG)
                        ft.IconButton(ft.Icons.RECEIPT_LONG, icon_color="orange", icon_size=16, tooltip="Descargar Informe QA", on_click=lambda e, s=sid, d=data: _trigger_download_log(s, d)),
                        # Botón Eliminar Registro de Imputación
                        ft.IconButton(ft.Icons.DELETE_OUTLINE, icon_color="#ff5555", icon_size=16, tooltip="Eliminar Registro de Imputación", on_click=lambda e, s=sid: _confirmar_eliminar(s))
                    ]),
                    bgcolor="#1e2a1e", padding=5, border_radius=8
                )
            )
        page.update()

    def abrir_inspeccion(sid):
        db_crudas = page.session.get("db_series_crudas") or {}
        data = db_crudas.get(sid) or {}
        
        # Leemos los datos directamente de SQLite (Protección OOM)
        import sqlite3
        ruta_hds = _resolver_ruta_bd()
        df_tail = pd.DataFrame()
        total_filas = 0
        
        if ruta_hds and os.path.exists(ruta_hds):
            try:
                conn = sqlite3.connect(ruta_hds)
                try:
                    cur = conn.cursor()
                    cur.execute(f"SELECT COUNT(*) FROM serie_imputada_{sid}")
                    total_filas = cur.fetchone()[0]
                except Exception:
                    pass
                    
                query = f"SELECT * FROM serie_imputada_{sid} ORDER BY rowid DESC LIMIT 8"
                df_tail = pd.read_sql_query(query, conn)
                df_tail = df_tail.iloc[::-1] # Invertimos para que quede cronológico
                conn.close()
            except Exception as ex:
                print(f"Error inspección SQLite: {ex}")
                
        if df_tail.empty:
            df_preview_str = "Datos no disponibles en BD."
        else:
            cols_show = [c for c in ['FECHA', 'PRECIP', 'PRECIP_ORIGINAL', 'clave_estacion'] if c in df_tail.columns]
            if not cols_show: cols_show = df_tail.columns[:5].tolist()
            df_preview_str = f"Total de registros: {total_filas}\n\n" + df_tail[cols_show].to_string(index=False)
            
        log_text = ""
        if isinstance(data, dict):
            if "log_path" in data and data["log_path"] and os.path.exists(data["log_path"]):
                try:
                    with open(data["log_path"], "r", encoding="utf-8") as f:
                        log_text = f.read()
                except Exception:
                    pass
            if not log_text and "log" in data:
                log_text = data.get("log", "")
                
        if not log_text:
            log_text = imputacion_logic.obtener_log_de_sqlite_hds(sid, ruta_hds)
            
        if not log_text:
            log_text = "Registro de log de calidad no disponible en memoria."
            
        dialog = ft.AlertDialog(
            title=ft.Row([
                ft.Icon(ft.Icons.INSIGHTS, color=COLOR_ACENTO),
                ft.Text(f"Inspección: Estación {sid}", color=COLOR_ACENTO, weight="bold")
            ]),
            content=ft.Container(
                content=ft.Column([
                    ft.Text("Muestra de datos (Últimos registros):", weight="bold", size=13, color="white"),
                    ft.Container(
                        content=ft.Text(df_preview_str, size=10, font_family="monospace", color="#00f0ff"), 
                        bgcolor="#0a0a0a", padding=10, border_radius=6, border=ft.border.all(1, "#333333")
                    ),
                    ft.Divider(height=15, color="#333333"),
                    ft.Text("Log de Calidad (QA):", weight="bold", size=13, color="white"),
                    ft.Container(
                        content=ft.ListView([
                            ft.Text(log_text, size=11, color="#cccccc", font_family="monospace")
                        ], auto_scroll=False), 
                        height=180, bgcolor="#0a0a0a", padding=10, border_radius=6, border=ft.border.all(1, "#333333")
                    )
                ], tight=True, spacing=8),
                width=650
            ),
            actions=[
                ft.TextButton("Descargar CSV", icon=ft.Icons.DOWNLOAD, on_click=lambda _: [_trigger_download(sid)]),
                ft.TextButton("Descargar QA Log", icon=ft.Icons.RECEIPT_LONG, on_click=lambda _: [_trigger_download_log(sid, data)]),
                ft.ElevatedButton("Cerrar", bgcolor=COLOR_ACENTO, color="black", on_click=lambda _: page.close(dialog))
            ],
            actions_alignment=ft.MainAxisAlignment.END
        )
        page.open(dialog)

    # ==========================================
    # 5. MOTOR DE IMPUTACIÓN Y PROTECCIONES
    # ==========================================
    def on_click_impute(e):
        cola = page.session.get("cola_imputacion")
        if not cola:
            page.snack_bar = ft.SnackBar(ft.Text("❌ Cola vacía"), bgcolor="red", open=True)
            return

        def task():
            state["imputing"] = True
            
            # --- 1. ACTIVACIÓN DEL ESCUDO RADIACTIVO Y BLOQUEO GLOBAL ---
            def _start_visuals():
                bloqueo_radiactivo.visible = True
                terminal_radiactiva.controls.clear()
                page.window.prevent_close = True # Guillotina de SO bloqueada
                
                main_rail = page.session.get("main_rail")
                if main_rail: main_rail.disabled = True
                
                tabs.selected_index = 2 # Salto automático a pestaña "3. Proceso"
                page.update()
            page.run_thread(_start_visuals)
            
            db_crudas = page.session.get("db_series_crudas") or {}
            out_folder = page.session.get("imput_output_folder") or os.getcwd()
            radius_km = int(dd_radio.value)
            station_files = page.session.get("imput_station_files")

            # --- PRECALCULAR RANGO GLOBAL UNA SOLA VEZ (ORQUESTADOR) ---
            pbl.value = "Calculando rango de fechas global..."
            page.update()
            
            try:
                global_range = imputacion_logic.obtener_rango_global_fechas(station_files)
                if global_range is None:
                    raise Exception("No se pudieron determinar fechas globales de la base de datos.")
                log_radiactivo(f"Rango Global: {global_range.min().date()} a {global_range.max().date()}")
            except Exception as e:
                log_radiactivo(f"❌ Error de inicio: {e}")
                state["imputing"] = False
                # Apagar Escudo Radiactivo prematuramente
                bloqueo_radiactivo.visible = False
                page.window.prevent_close = False
                main_rail = page.session.get("main_rail")
                if main_rail: main_rail.disabled = False
                page.update()
                return

            for idx, sid in enumerate(cola):
                if not state["imputing"]: break
                
                # Reiniciamos la UI para la nueva estación
                pb_interceptado.value = 0 
                log_radiactivo(f"--- INICIANDO ESTACIÓN {sid} ({idx+1}/{len(cola)}) ---")

                try:
                    # INYECCIÓN DEL CALLBACK PROTEGIDO Y RANGO GLOBAL (HEXAGONAL + EAFP)
                    def ui_progress(pct, msg):
                        def _upd():
                            if pct is not None: pb_interceptado.value = pct
                            if msg is not None: pbl.value = msg
                            page.update()
                        page.run_thread(_upd)

                    df_res, log_msg = imputacion_logic.impute_target_station(
                        sid, station_files, radius_km, global_range, 
                        progress_callback=ui_progress, log_callback=log_radiactivo
                    )
                    
                    if df_res is not None:
                        ruta_bd = _resolver_ruta_bd()
                        if ruta_bd:
                            imputacion_logic.guardar_en_sqlite_hds(df_res, sid, ruta_bd)
                            if log_msg:
                                imputacion_logic.guardar_log_en_sqlite_hds(log_msg, sid, ruta_bd)
                            print(f"📦 Estación {sid} inyectada en SQLite con éxito.")
                            
                        out_folder = page.session.get("imput_output_folder")
                        log_path = None
                        if out_folder and os.path.exists(out_folder) and log_msg:
                            log_path = imputacion_logic.save_imputation_log(log_msg, sid, out_folder)
                        
                        # Actualizar db_crudas para marcarla como imputada y conservar el log para inspección/exportación
                        db_crudas[sid] = {
                            "log_path": log_path,
                            "log": log_msg,
                            "imputada": True
                        }
                        
                        log_radiactivo(f"✅ Estación {sid} procesada y guardada con éxito.")
                        
                        # LIMPIADO DE MEMORIA (Protección OOM)
                        del df_res
                except Exception as ex:
                    log_radiactivo(f"❌ Error crítico en {sid}: {ex}")
                
                # RECOLECCIÓN DE BASURA OBLIGATORIA POR CICLO
                import gc
                gc.collect()
            
            # --- 2. FINALIZACIÓN Y APAGADO DEL ESCUDO ---
            def _finalize_batch():
                page.session.set("db_series_crudas", db_crudas)
                page.session.set("cola_imputacion", [])
                
                out_folder = page.session.get("imput_output_folder")
                if out_folder and os.path.exists(out_folder):
                    page.session.set("txt_backup_imputados", {"__type__": "folder_backup", "path": out_folder})
                
                render_cola_visual()
                render_visor_resultados()
                
                # Apagar Escudo Radiactivo
                bloqueo_radiactivo.visible = False
                page.window.prevent_close = False
                main_rail = page.session.get("main_rail")
                if main_rail: main_rail.disabled = False
                
                state["imputing"] = False
                page.update()
            page.run_thread(_finalize_batch)

        threading.Thread(target=task, daemon=True).start()

    def on_click_impute_masiva(e):
        station_files = page.session.get("imput_station_files")
        if not station_files:
            page.snack_bar = ft.SnackBar(ft.Text("❌ No hay estaciones cargadas"), bgcolor="red", open=True)
            return

        def task():
            state["imputing"] = True
            
            def _start_visuals():
                bloqueo_radiactivo.visible = True
                terminal_radiactiva.controls.clear()
                page.window.prevent_close = True
                main_rail = page.session.get("main_rail")
                if main_rail: main_rail.disabled = True
                tabs.selected_index = 2
                page.update()
            page.run_thread(_start_visuals)
            
            db_crudas = page.session.get("db_series_crudas") or {}
            out_folder = page.session.get("imput_output_folder") or os.getcwd()
            radius_km = int(dd_radio.value)
            
            pbl.value = "Calculando rango de fechas global..."
            page.update()
            
            try:
                global_range = imputacion_logic.obtener_rango_global_fechas(station_files)
                if global_range is None: raise Exception("Rango global no disponible.")
            except Exception as e:
                log_radiactivo(f"❌ Error de inicio masivo: {e}")
                state["imputing"] = False
                def _abort():
                    bloqueo_radiactivo.visible = False
                    page.window.prevent_close = False
                    main_rail = page.session.get("main_rail")
                    if main_rail: main_rail.disabled = False
                    page.update()
                page.run_thread(_abort)
                return

            todas_estaciones_bruto = list(station_files.keys())
            todas_estaciones = []
            
            # FILTRO DE REANUDACIÓN: Omitir estaciones ya imputadas exitosamente
            for s in todas_estaciones_bruto:
                if s in db_crudas and db_crudas[s].get("imputada") is True:
                    continue
                todas_estaciones.append(s)
                
            if not todas_estaciones:
                log_radiactivo("✅ Todas las estaciones de la cuenca ya han sido imputadas previamente. Nada que hacer.")
                def _abort_success():
                    page.snack_bar = ft.SnackBar(ft.Text("✅ Todas las estaciones ya estaban imputadas."), bgcolor="green", open=True)
                    bloqueo_radiactivo.visible = False
                    page.window.prevent_close = False
                    main_rail = page.session.get("main_rail")
                    if main_rail: main_rail.disabled = False
                    state["imputing"] = False
                    page.update()
                page.run_thread(_abort_success)
                return
                
            estaciones_omitidas = len(todas_estaciones_bruto) - len(todas_estaciones)
            if estaciones_omitidas > 0:
                log_radiactivo(f"♻️ Reanudación Inteligente: Se omitieron {estaciones_omitidas} estaciones ya procesadas.")
            
            chunk_size = 5
            total_chunks = (len(todas_estaciones) + chunk_size - 1) // chunk_size
            
            # BLOQUES (Chunking)
            for chunk_idx in range(total_chunks):
                if not state["imputing"]: break
                
                inicio = chunk_idx * chunk_size
                fin = inicio + chunk_size
                bloque_actual = todas_estaciones[inicio:fin]
                
                log_radiactivo(f"\n==========================================")
                log_radiactivo(f"🚀 INICIANDO BLOQUE {chunk_idx + 1} DE {total_chunks}")
                log_radiactivo(f"==========================================")
                
                for idx, sid in enumerate(bloque_actual):
                    if not state["imputing"]: break
                    
                    pb_interceptado.value = 0 
                    log_radiactivo(f"--- [Bloque {chunk_idx + 1}] ESTACIÓN {sid} ({idx+1}/{len(bloque_actual)}) ---")

                    try:
                        def ui_progress(pct, msg):
                            def _upd():
                                if pct is not None: pb_interceptado.value = pct
                                if msg is not None: 
                                    pbl.value = f"[B:{chunk_idx+1}/{total_chunks}] {msg}"
                                page.update()
                            page.run_thread(_upd)

                        df_res, log_msg = imputacion_logic.impute_target_station(
                            sid, station_files, radius_km, global_range, 
                            progress_callback=ui_progress, log_callback=log_radiactivo
                        )
                        
                        if df_res is not None:
                            ruta_bd = _resolver_ruta_bd()
                            if ruta_bd:
                                imputacion_logic.guardar_en_sqlite_hds(df_res, sid, ruta_bd)
                                if log_msg:
                                    imputacion_logic.guardar_log_en_sqlite_hds(log_msg, sid, ruta_bd)
                                print(f"📦 Estación {sid} inyectada en SQLite con éxito.")
                                
                            out_folder = page.session.get("imput_output_folder")
                            log_path = None
                            if out_folder and os.path.exists(out_folder) and log_msg:
                                log_path = imputacion_logic.save_imputation_log(log_msg, sid, out_folder)

                            db_crudas[sid] = {
                                "log_path": log_path,
                                "log": log_msg,
                                "imputada": True
                            }
                            log_radiactivo(f"✅ Estación {sid} completada.")
                            
                            del df_res
                    except Exception as ex:
                        log_radiactivo(f"❌ Error crítico en {sid}: {ex}")
                    
                    import gc
                    gc.collect()
                
                # Checkpointing por bloque
                log_radiactivo(f"💾 Guardando progreso del Bloque {chunk_idx + 1} en memoria de sesión...")
                page.session.set("db_series_crudas", db_crudas)
                def _update_ui():
                    render_visor_resultados()
                page.run_thread(_update_ui)
                
                # Volcado automático a Disco Duro (Auto-Save .hds) para evitar colapso de RAM
                current_project = page.session.get("current_project_path")
                if current_project:
                    log_radiactivo(f"💾 Volcando datos a Disco Duro (.hds) para liberar RAM...")
                    try:
                        session_dict = {}
                        BLACKLIST_KEYS = ["catalogo_conagua", "df_estaciones_nacionales", "db_climatologica_masiva"]
                        for k in page.session.get_keys():
                            val = page.session.get(k)
                            if callable(val) or isinstance(val, ft.Control) or "flet" in type(val).__module__: continue
                            if k in BLACKLIST_KEYS: continue
                            session_dict[k] = val
                            
                        from infrastructure.project_manager import project_manager_instance
                        project_manager_instance.save_project(current_project, session_dict)
                        del session_dict
                        log_radiactivo(f"✅ Volcado a disco completado.")
                    except Exception as ev:
                        log_radiactivo(f"⚠️ Aviso: Error al guardar .hds automático: {ev}")

                # Enfriamiento Térmico (Yielding)
                import time
                time.sleep(2)
                import gc
                gc.collect()

            # FIN BATCH
            def _finalize_batch():
                page.session.set("db_series_crudas", db_crudas)
                page.session.set("cola_imputacion", [])
                
                out_folder = page.session.get("imput_output_folder")
                if out_folder and os.path.exists(out_folder):
                    page.session.set("txt_backup_imputados", {"__type__": "folder_backup", "path": out_folder})
                
                render_cola_visual()
                render_visor_resultados()
                
                bloqueo_radiactivo.visible = False
                page.window.prevent_close = False
                main_rail = page.session.get("main_rail")
                if main_rail: main_rail.disabled = False
                
                state["imputing"] = False
                page.update()
                page.snack_bar = ft.SnackBar(ft.Text("✅ IMPUTACIÓN MASIVA COMPLETADA"), bgcolor="green", open=True)
                page.update()
            page.run_thread(_finalize_batch)

        threading.Thread(target=task, daemon=True).start()

    def abort_imputation(e):
        state["imputing"] = False
        terminal_list.controls.append(ft.Text("🛑 Deteniendo proceso...", color="red", weight="bold"))
        page.update()

    def interceptar_cambio_tab(e):
        if state["imputing"]:
            tabs.selected_index = 2 # Fuerza a quedarse en la pestaña de Proceso
            page.snack_bar = ft.SnackBar(ft.Text("⚠️ Imputación en curso. Detenga el proceso para navegar."), bgcolor="orange", open=True)
            page.update()

    # ==========================================
    # 6. COMPONENTES Y DISEÑO
    # ==========================================
    dd_radio = ft.Dropdown(label="Radio (Km)", width=150, options=[
        ft.dropdown.Option("50"), ft.dropdown.Option("150"), 
        ft.dropdown.Option("250"), ft.dropdown.Option("300")
    ], value="150")
    
    indicador_estacion = ft.Text("Ninguna estación en cola.", color="grey", size=12, italic=True)
    
    open_map_button = ft.ElevatedButton("Ver Mapa", icon=ft.Icons.MAP, disabled=True, 
                                       on_click=lambda _: webbrowser.open(page.session.get("imput_map_path").get("path") if isinstance(page.session.get("imput_map_path"), dict) else page.session.get("imput_map_path")))
    export_thiessen_button = ft.ElevatedButton("Exportar Thiessen (SHP)", icon=ft.Icons.MAP_OUTLINED, disabled=True,
                                               on_click=lambda _: picker_exportar_thiessen.save_file(file_name="Thiessen_Estaciones.shp", allowed_extensions=["shp"]))
    
    # --- BUSCADOR Y CONTENEDOR DE TARJETAS ---
    search_station_input = ft.TextField(
        label="Buscar estación por ID o nombre...",
        prefix_icon=ft.Icons.SEARCH,
        visible=False, # Se oculta hasta que haya datos
        on_change=lambda e: filtrar_estaciones(e.control.value),
        border_color=COLOR_ACENTO
    )
    # expand=True es vital para que Flet sepa que este contenedor debe estirarse y permitir scroll
    station_files_container = ft.Column(spacing=10, scroll=ft.ScrollMode.AUTO, expand=True)

    impute_button = ft.ElevatedButton("Iniciar Lote", icon=ft.Icons.PLAY_ARROW, on_click=on_click_impute, bgcolor=COLOR_ACENTO, color="black")
    
    area_c = page.session.get("area_cuenca_km2") or 0.0
    impute_masiva_button = ft.ElevatedButton(
        "🚀 IMPUTACIÓN MASIVA", 
        icon=ft.Icons.ROCKET_LAUNCH, 
        on_click=on_click_impute_masiva, 
        bgcolor="orange", 
        color="black",
        disabled=(area_c <= 25),
        tooltip="Disponible solo para cuencas > 25 km²" if area_c <= 25 else "Procesa todas las estaciones por bloques"
    )

    detener_button = ft.ElevatedButton("Detener", icon=ft.Icons.STOP, visible=False, on_click=abort_imputation, bgcolor="red", color="white")

    # ==========================================
    # 7. ESTRUCTURA DE PESTAÑAS (TABS)
    # ==========================================
    view_fuente = ft.Container(content=ft.Column([
        ft.Text("1. Carpeta de Datos", size=18, weight="bold", color=COLOR_ACENTO),
        ft.Row([ft.ElevatedButton("Seleccionar Fuente", icon=ft.Icons.FOLDER, on_click=lambda _: file_picker_input.get_directory_path()), open_map_button, export_thiessen_button]),
        selected_input_folder, station_count_text,
        ft.Divider(),
        search_station_input,
        station_files_container
    ]), padding=20,
        expand=True)

    view_objetivo = ft.Container(content=ft.Column([
        ft.Text("2. Configuración y Cola", size=18, weight="bold", color=COLOR_ACENTO),
        ft.Row([dd_radio, ft.ElevatedButton("Carpeta Salida", icon=ft.Icons.SAVE, on_click=lambda _: file_picker_output.get_directory_path())]),
        selected_output_folder,
        ft.Divider(),
        ft.Text("Cola de Trabajo:"),
        cola_espera_ui,
        ft.Row([impute_button, impute_masiva_button], alignment=ft.MainAxisAlignment.CENTER)
    ], scroll=ft.ScrollMode.AUTO), padding=20, expand=True)

    view_exec = ft.Container(content=ft.Column([
        ft.Text("3. Ejecución en Memoria", size=18, weight="bold", color=COLOR_ACENTO),
        ft.Row([detener_button], alignment=ft.MainAxisAlignment.END), # <--- FRENO DE EMERGENCIA AQUÍ
        ft.Container(content=terminal_list, bgcolor="black", expand=True, padding=10),
        ft.Text("Base de Datos en Sesión (Estaciones Listas):"),
        visor_resultados_grid
    ], scroll=ft.ScrollMode.AUTO), padding=20, expand=True)

    tabs = ft.Tabs(
        selected_index=0,
        on_change=interceptar_cambio_tab,
        tabs=[
            ft.Tab(text="1. Fuente", icon=ft.Icons.FOLDER, content=view_fuente),
            ft.Tab(text="2. Cola", icon=ft.Icons.LIST, content=view_objetivo),
            ft.Tab(text="3. Proceso", icon=ft.Icons.MEMORY, content=view_exec),
        ], expand=True
    )

    # REHIDRATACIÓN
    def filtrar_estaciones(query):
        estaciones_totales = page.session.get("imput_station_files") or {}
        if not query:
            # Si el buscador está vacío, mostramos todas
            restore_station_ui(estaciones_totales, actualizar_textos=False)
            return
        
        # Filtro en tiempo real ignorando mayúsculas/minúsculas
        filtradas = {k: v for k, v in estaciones_totales.items() if query.lower() in str(k).lower()}
        restore_station_ui(filtradas, actualizar_textos=False)

    def restore_station_ui(estaciones, actualizar_textos=True):
        station_files_container.controls.clear()
        cola_activa = page.session.get("cola_imputacion") or []
        
        # --- ALGORITMO TOP-FLOAT: Clasificación por estado de selección ---
        seleccionadas = []
        no_seleccionadas = []
        
        for station_id, info in estaciones.items():
            if station_id in cola_activa:
                seleccionadas.append((station_id, info))
            else:
                no_seleccionadas.append((station_id, info))
                
        # Consolidamos colocando el enjambre seleccionado al inicio de la lista
        lista_ordenada = seleccionadas + no_seleccionadas
        
        for station_id, info in lista_ordenada:
            en_cola = station_id in cola_activa
            
            # Encapsulamos en Container para evadir las limitaciones estéticas del ft.Card nativo
            card_content = ft.Container(
                content=ft.Column([
                    ft.ListTile(
                        leading=ft.Icon(
                            ft.Icons.CHECK_CIRCLE if en_cola else ft.Icons.GPS_FIXED, 
                            color="#00f0ff" if en_cola else COLOR_ACENTO
                        ),
                        title=ft.Text(station_id, weight="bold", color="#00f0ff" if en_cola else "white"),
                        subtitle=ft.Text(
                            f"Lat: {info['lat']}, Lon: {info['lon']}", 
                            color=add_opacity("#00f0ff", 0.7) if en_cola else "grey"
                        ),
                    ),
                    ft.Row([
                        ft.TextButton(
                            text="✔ En Cola" if en_cola else "➕ Añadir a Cola",
                            disabled=en_cola, # Bloqueo estricto anti-duplicados
                            style=ft.ButtonStyle(color="#00f0ff" if en_cola else COLOR_ACENTO),
                            on_click=lambda e, sid=station_id: agregar_a_cola(sid)
                        ),
                    ], alignment=ft.MainAxisAlignment.END),
                ]),
                padding=10,
                # Identidad visual radical: borde neón y fondo translúcido si está activa
                border=ft.border.all(1, "#00f0ff" if en_cola else "transparent"),
                bgcolor=add_opacity("#00f0ff", 0.05) if en_cola else COLOR_SUPERFICIE,
                border_radius=8
            )
            
            station_files_container.controls.append(ft.Card(content=card_content))
            
        if actualizar_textos:
            station_count_text.value = f"Estaciones encontradas: {len(estaciones)}"
            station_count_text.visible = True
            search_station_input.visible = True # Hacemos visible el buscador
            
        page.update()

    # --- MOTOR DE HIDRATACIÓN AUTOMÁTICA (VFS) ---
    async def trigger_hydration():
        await asyncio.sleep(0.1) # Breve pausa para asegurar que Flet haya renderizado el DOM
        
        ruta_en_memoria = page.session.get("imput_folder_path")
        estaciones_actuales = page.session.get("imput_station_files")
        
        # Escenario 1: Hay una ruta validada en la memoria, pero aún no se leen los archivos
        # Esto ocurre cuando vienes del Módulo 1 o recién abres un proyecto .hds
        if ruta_en_memoria and os.path.exists(ruta_en_memoria):
            if not estaciones_actuales:
                auto_procesar_fuente(ruta_en_memoria)
            else:
                # Escenario 2: Ya estaban leídos (Ej. si regresaste de otra pestaña)
                selected_input_folder.value = f"Fuente Activa: {ruta_en_memoria}"
                open_map_button.disabled = False if page.session.get("imput_map_path") else True
                export_thiessen_button.disabled = False
                
                area_c = page.session.get("area_cuenca_km2") or 0.0
                impute_masiva_button.disabled = (area_c <= 25)
                impute_masiva_button.tooltip = "Disponible solo para cuencas > 25 km²" if area_c <= 25 else "Procesa todas las estaciones por bloques"
                
                restore_station_ui(estaciones_actuales)
                
    # Lanzamos el gatillo asíncrono
    page.run_task(trigger_hydration)
    
    render_visor_resultados()

    return tabs