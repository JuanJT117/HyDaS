import flet as ft
import flet.canvas as cv
import threading
import os
import asyncio

# --- ARQUITECTURA HEXAGONAL: Importación desde el Core ---
from core import descarga_logic
from core.analisis_espacial import MotorEspacial
from core.descarga_logic import GestorExtraccion

def build_descarga_view(page: ft.Page):
    
    # --- 1. VARIABLES DE SESIÓN (Protegidas) ---
    cuenca_cargada = page.session.get("cuenca_geojson") is not None
    modo_inicial = "CUENCA OBJETIVO" if cuenca_cargada else "POR ESTADO"
    modo_actual = ft.Text(modo_inicial, color="#00ff41", weight="bold", size=16)
    elementos_seleccionados = [] 
    poligonos_cargados = [] 
    estados_auditados = {}
    
    ruta_bd_activa = page.session.get("ruta_bd_activa")
    indice_activo = page.session.get("indice_bd_local")
    
    # --- 2. CONTROLES DE UI Y TERMINAL ---
    terminal_list = ft.ListView(expand=True, spacing=5, auto_scroll=True)
    progreso_bar = ft.ProgressBar(width=None, color="#00ff41", bgcolor="#1a1a1a", value=0, visible=False)
    
    lbl_bd_activa = ft.Text(f"BD: {os.path.basename(ruta_bd_activa) if ruta_bd_activa else 'NINGUNA (Requiere Vinculación)'}", color="#00ff41" if ruta_bd_activa else "grey", size=10, selectable=True)
    lbl_indice_status = ft.Text(f"Índice HDS: Activo ({len(indice_activo)} est.)" if indice_activo is not None else "Índice HDS: Vacío", color="#00ff41" if indice_activo is not None else "red", size=10)
    
    # Hidratación del Módulo 1 desde BD (.hds)
    area_guardada = page.session.get("area_cuenca_km2")
    arf_guardado = page.session.get("arf_activado")
    estaciones_guardadas = page.session.get("total_estaciones_extraidas")
    
    lbl_area = ft.Text(f"Área: {area_guardada} km²" if area_guardada else "Área: N/A", color="#00ff41", weight="bold")
    lbl_arf_status = ft.Text("Módulo 4 (ARF): HABILITADO" if arf_guardado else ("Módulo 4 (ARF): NO REQUIERE" if arf_guardado is False else "Módulo ARF: INACTIVO"), color="#00ff41" if arf_guardado else ("orange" if arf_guardado is False else "grey"))
    lbl_estaciones = ft.Text(f"Estaciones en Rango: {estaciones_guardadas}" if estaciones_guardadas is not None else "Estaciones en Rango: 0", color="white")
    
    progreso_espacial = ft.ProgressRing(visible=False, color="#00ff41", width=20, height=20)

    # =========================================================================
    # MECANISMO DE TEAR-DOWN: PURGA DE ESTADO Y MEMORIA (DIRECTRIZ 1 y 4)
    # =========================================================================
    
    def ejecutar_purga():
        try:
            page.close(dlg_confirmacion_purga)
            
            # 1. Aislamiento y Destrucción de Variables en RAM
            keys_to_remove = [
                "cuenca_geojson", "gdf_multicuenca", "area_cuenca_km2", 
                "wkt_envolvente_busqueda", "es_multicuenca_arf",
                "buffer_geojson", "cuenca_bbox", "arf_activado",
                "buffer_geom", "buffer_geom_shapely", "cuenca_activa_ruta",
                "claves_objetivo", "total_estaciones_extraidas"
            ]
            for key in keys_to_remove:
                if page.session.contains_key(key):
                    page.session.remove(key)
            
            # 2. Restauración de UI al estado por defecto
            modo_actual.value = "POR ESTADO"
            modo_actual.color = "#00ff41"
            lbl_area.value = "Área: N/A"
            lbl_estaciones.value = "Estaciones en Rango: 0"
            lbl_arf_status.value = "Módulo ARF: INACTIVO"
            lbl_arf_status.color = "grey"
            elementos_seleccionados.clear()
            
            # Limpiamos terminal e informamos
            terminal_list.controls.clear()
            terminal_list.controls.append(ft.Text("> [SISTEMA] Geometría purgada. Sistema listo para nueva ingesta.", color="#00ff41"))
            
            # Limpiamos o redibujamos el contenedor del mapa
            try:
                dibujar_mapa()
            except Exception:
                try:
                    contenedor_mapa.content = ft.Container(
                        content=ft.Text("SIN GEOMETRÍA CARGADA", color="#333333", size=20, weight="bold"),
                        alignment=ft.alignment.center,
                        expand=True
                    )
                except Exception:
                    pass
            
            page.update()
            
            # 3. Recolección de Basura Explícita (Gestión Big Data)
            import gc
            gc.collect()
            print("|> [MOTOR ESPACIAL] Purga ejecutada. RAM liberada.")
            
        except Exception as ex:
            import traceback
            traceback.print_exc()
            page.open(
                ft.SnackBar(content=ft.Text(f"ERROR al purgar memoria: {ex}", color="#ffffff"), bgcolor="#cc0000")
            )

    # Diálogo modal Flet 0.28+
    dlg_confirmacion_purga = ft.AlertDialog(
        modal=True,
        title=ft.Row([ft.Icon(ft.Icons.WARNING_AMBER_ROUNDED, color="#cc0000"), ft.Text("ADVERTENCIA CRÍTICA")]),
        content=ft.Text("¿Desea purgar la geometría actual y reiniciar el módulo?\nSe perderán los polígonos en la memoria RAM."),
        actions=[
            ft.TextButton("Cancelar", on_click=lambda e: page.close(dlg_confirmacion_purga)),
            ft.TextButton("Purgar Entradas", on_click=lambda e: ejecutar_purga(), style=ft.ButtonStyle(color="#cc0000")),
        ],
        actions_alignment=ft.MainAxisAlignment.END,
    )

    def confirmar_purga(e):
        page.open(dlg_confirmacion_purga)
        
    btn_purgar = ft.IconButton(
        icon=ft.Icons.DELETE_SWEEP, 
        icon_color="#cc0000", 
        tooltip="Purgar Geometría y Reiniciar", 
        on_click=confirmar_purga
    )

    def on_shp_selected(e: ft.FilePickerResultEvent):
        if e.files:
            progreso_espacial.visible = True
            page.update()
            ruta_shp = e.files[0].path
            
            def _on_success():
                try:
                    df_cat = page.session.get("indice_bd_local")
                    claves = []
                    
                    buffer_geom = page.session.get("buffer_geom_shapely")
                    wkt_env = page.session.get("wkt_envolvente_busqueda")
                    if df_cat is not None and not df_cat.empty and (buffer_geom or wkt_env):
                        claves = GestorExtraccion.filtrar_estaciones_por_buffer(buffer_geom, df_cat, wkt_envolvente=wkt_env, buffer_km=100.0)
                    
                    page.session.set("cuenca_activa_ruta", ruta_shp)
                    page.session.set("claves_objetivo", claves)
                    page.session.set("total_estaciones_extraidas", len(claves))
                    
                    area_km2 = page.session.get("area_cuenca_km2")
                    gdf_multi = page.session.get("gdf_multicuenca")
                    num_cuencas = len(gdf_multi) if gdf_multi is not None else 1
                    
                    lbl_area.value = f"Área: {area_km2} km² ({num_cuencas} cuencas)"
                    lbl_estaciones.value = f"Estaciones en Rango: {len(claves)}"
                    
                    # 2. Evaluación Topológica: Disparador del Modo ARF (PARTE 2)
                    if area_km2 > 25.0:
                        page.session.set("es_multicuenca_arf", True)
                        page.session.set("arf_activado", True)
                        lbl_arf_status.value = "Módulo 4 (ARF): HABILITADO (Multicuenca)"
                        lbl_arf_status.color = "#00ff41"
                        
                        # Alerta UI Flet 0.28+ (Acento Hacker/Cyberpunk)
                        page.open(
                            ft.SnackBar(
                                content=ft.Row([
                                    ft.Icon(ft.Icons.WARNING_AMBER_ROUNDED, color="#00ff41"),
                                    ft.Text(
                                        f"ESCALA REGIONAL DETECTADA: {num_cuencas} cuenca(s) con {area_km2:.2f} km². "
                                        f"Modo ARF Multicuenca activado automáticamente.",
                                        color="#ffffff", weight="bold"
                                    )
                                ]),
                                bgcolor="#111111", duration=6000
                            )
                        )
                    else:
                        page.session.set("es_multicuenca_arf", False)
                        page.session.set("arf_activado", False)
                        lbl_arf_status.value = "Módulo 4 (ARF): NO REQUIERE"
                        lbl_arf_status.color = "orange"
                        
                        page.open(
                            ft.SnackBar(
                                content=ft.Text(f"Éxito: {num_cuencas} cuenca(s) con {area_km2:.2f} km². Escala local (No requiere ARF).", color="#ffffff"),
                                bgcolor="#00ff41", duration=4000
                            )
                        )
                    
                    modo_actual.value = "CUENCA OBJETIVO"
                    dibujar_mapa()
                    
                except Exception as ex:
                    import traceback; traceback.print_exc()
                    page.open(ft.SnackBar(content=ft.Text(f"Error UI: {str(ex)}", color="#ffffff"), bgcolor="#cc0000"))
                finally:
                    progreso_espacial.visible = False
                    page.update()

            def _on_error():
                progreso_espacial.visible = False
                page.update()

            # Disparar la tarea asíncrona del núcleo espacial mitigando el bloqueo del GIL
            MotorEspacial.calcular_area_cuenca_async(ruta_shp, epsg_utm=32614, page=page, buffer_km=100, on_success=_on_success, on_error=_on_error)

    def _get_or_create_picker(key, on_result):
        p = page.session.get(key)
        if not p:
            p = ft.FilePicker(on_result=on_result)
            page.session.set(key, p)
            page.overlay.append(p)
        else:
            p.on_result = on_result
        return p

    shp_picker = _get_or_create_picker("descarga_shp_picker", on_shp_selected)

    btn_cuenca_objetivo = ft.OutlinedButton(
        "CUENCA OBJETIVO (SHP)",
        icon=ft.Icons.MAP_OUTLINED,
        on_click=lambda _: shp_picker.pick_files(allowed_extensions=["shp"]),
        style=ft.ButtonStyle(color="white")
    )

    def log_terminal(mensaje):
        def _update():
            terminal_list.controls.append(ft.Text(mensaje, color="#00ff41", font_family="Roboto Mono", size=12))
            # --- PROTECCIÓN OOM: Buffer Cíclico ---
            # Mantenemos el árbol de renderizado ligero eliminando registros viejos
            if len(terminal_list.controls) > 150:
                terminal_list.controls.pop(0)
            page.update()
        page.run_thread(_update)

    def update_progreso(valor):
        def _update():
            progreso_bar.value = valor
            page.update()
        page.run_thread(_update)
    
    def saltar_a_imputacion(e):
        nav_func = page.session.get("navigate_to_module")
        if nav_func: nav_func(1)

    # --- NUEVOS CONTROLADORES DE ARCHIVO (In-Memory) ---
    def on_vincular_bd_result(e: ft.FilePickerResultEvent):
        if e.files:
            ruta = e.files[0].path
            page.session.set("ruta_bd_activa", ruta)
            lbl_bd_activa.value = f"BD: {os.path.basename(ruta)}"
            lbl_bd_activa.color = "#00ff41"
            log_terminal(f"> 📦 VINCULANDO BASE DE DATOS: {ruta}")
            
            bloquear_interfaz(True)
            progreso_bar.visible = True; progreso_bar.value = 0
            
            def _task():
                try:
                    descarga_logic.señal_abortar.clear()
                    cuencas = descarga_logic.cargar_poligonos("POR CUENCA")
                    log_terminal("> Escaneando compresión LZMA y reconstruyendo Índice HDS (8 Columnas)...")
                    df_indice = descarga_logic.indexar_base_datos_tar(ruta, cuencas, log_terminal, update_progreso)
                    
                    if df_indice is not None and not df_indice.empty:
                        page.session.set("indice_bd_local", df_indice) # PERSISTENCIA HDS
                        def _success():
                            lbl_indice_status.value = f"Índice HDS: Activo ({len(df_indice)} est.)"
                            lbl_indice_status.color = "#00ff41"
                            log_terminal("> ✅ Índice reconstruido e inyectado en la memoria del proyecto.")
                            page.update()
                        page.run_thread(_success)
                    else: log_terminal("> ❌ Error: No se pudo generar el índice.")
                except Exception as ex: log_terminal(f"> ❌ Error: {ex}")
                finally:
                    def _finalize():
                        bloquear_interfaz(False)
                        btn_detener.visible = False
                        page.update()
                    page.run_thread(_finalize)
            threading.Thread(target=_task, daemon=True).start()

    def on_exportar_csv_result(e: ft.FilePickerResultEvent):
        if e.path:
            df = page.session.get("indice_bd_local")
            if df is not None:
                df.to_csv(e.path, index=False, encoding='utf-8')
                log_terminal(f"> ✅ Índice exportado a CSV en: {e.path}")
            else: log_terminal("> [ERROR] No hay índice en memoria.")

    def on_guardar_bd_masiva(e: ft.FilePickerResultEvent):
        if e.path: run_extraction_thread(ruta_target=e.path)

    picker_vincular_bd = _get_or_create_picker("descarga_picker_vincular_bd", on_vincular_bd_result)
    picker_exportar_csv = _get_or_create_picker("descarga_picker_exportar_csv", on_exportar_csv_result)
    picker_guardar_bd = _get_or_create_picker("descarga_picker_guardar_bd", on_guardar_bd_masiva)

    # --- 3. BOTONES DE CONTROL ---
    btn_modo_estado = ft.TextButton("🗺️ POR ESTADO", on_click=lambda e: cambiar_modo(e, "POR ESTADO"), style=ft.ButtonStyle(color="#00ff41"))
    btn_modo_cuenca = ft.TextButton("🌊 POR CUENCA", on_click=lambda e: cambiar_modo(e, "POR CUENCA"), style=ft.ButtonStyle(color="#00ff41"))
    btn_modo_cuenca_obj = ft.TextButton("🎯 CUENCA OBJETIVO", on_click=lambda e: cambiar_modo(e, "CUENCA OBJETIVO"), style=ft.ButtonStyle(color="#00ff41"))
    btn_modo_masivo = ft.TextButton("📦 RESPALDO MASIVO", on_click=lambda e: cambiar_modo(e, "RESPALDO MASIVO"), style=ft.ButtonStyle(color="#1c75fa"))
    
    # --- MOTOR DE AUDITORÍA Y PICKERS ---
    def procesar_auditoria_hilo(ruta_salida):
        ruta_bd = page.session.get("ruta_bd_activa")
        if not ruta_bd:
            log_terminal("> [ERROR] Vincula una BD (.tar.xz) primero.")
            return
        bloquear_interfaz(True)
        progreso_bar.visible = True; progreso_bar.value = 0
        btn_detener.visible, btn_iniciar.visible = True, False
        def task():
            try:
                exito, msj, stats_audit = descarga_logic.auditar_base_datos_profunda(ruta_bd, ruta_salida, log_terminal, update_progreso)
                
                def final():
                    bloquear_interfaz(False)
                    btn_detener.visible, btn_iniciar.visible = False, True
                    
                    if exito and stats_audit:
                        estados_auditados.clear()
                        for k, v in stats_audit.items():
                            estados_auditados[k] = v["sanas"] > 0
                        dibujar_mapa()
                    
                    page.snack_bar = ft.SnackBar(ft.Text(msj, weight="bold", color="black"), bgcolor="#00ff41" if exito else "red", open=True)
                    log_terminal(f"> {msj}")
                    page.update()
                page.run_thread(final)
            except Exception as e: log_terminal(f"> [CRÍTICO] Fallo de auditoría: {e}")
        threading.Thread(target=task, daemon=True).start()

    def on_audit_dir_result(e: ft.FilePickerResultEvent):
        if e.path:
            modo_actual.value = "AUDITORÍA PROFUNDA"
            elementos_seleccionados.clear(); dibujar_mapa()
            procesar_auditoria_hilo(e.path)

    picker_auditoria = _get_or_create_picker("descarga_picker_auditoria", on_audit_dir_result)

    btn_auditar = ft.TextButton("✅ AUDITAR BD LOCAL", on_click=lambda _: picker_auditoria.get_directory_path(dialog_title="Guardar Informe"), style=ft.ButtonStyle(color="#ff9900"))
    btn_vincular_bd = ft.OutlinedButton("VINCULAR BD (.xz)", icon=ft.Icons.LINK, on_click=lambda _: picker_vincular_bd.pick_files(allowed_extensions=["xz"]), style=ft.ButtonStyle(color="white"))
    btn_exportar_csv = ft.OutlinedButton("EXPORTAR ÍNDICE (CSV)", icon=ft.Icons.TABLE_CHART, on_click=lambda _: picker_exportar_csv.save_file(file_name="Indice_Tlaloc.csv", allowed_extensions=["csv"]), style=ft.ButtonStyle(color="white"))
    
    def on_iniciar_click(e):
        if modo_actual.value == "RESPALDO MASIVO": picker_guardar_bd.save_file(file_name="Tlaloc_BD_Nacional_Comprimida.tar.xz", allowed_extensions=["xz"])
        else:
            if not page.session.get("ruta_bd_activa"): log_terminal("> [ERROR] Debes VINCULAR una BD (.tar.xz) primero."); return
            run_extraction_thread(ruta_target=page.session.get("ruta_bd_activa"))

    btn_iniciar = ft.ElevatedButton("INICIAR EXTRACCIÓN", color="#050505", bgcolor="#00ff41", icon=ft.Icons.CLOUD_DOWNLOAD, on_click=on_iniciar_click)
    btn_detener = ft.ElevatedButton("DETENER", color="white", bgcolor="#cc0000", icon=ft.Icons.STOP, visible=False, on_click=lambda e: detener_proceso(e))
    # Hidratación del botón de avance
    puente_visible = page.session.get("imput_folder_path") is not None
    btn_puente = ft.ElevatedButton("IR A IMPUTACIÓN", color="white", bgcolor="#1c75fa", icon=ft.Icons.ARROW_FORWARD, visible=puente_visible, on_click=saltar_a_imputacion)
    # Botón de Inspección
    btn_inspeccionar = ft.ElevatedButton("🔍 INSPECCIONAR ZONA", color="white", bgcolor="#9900ff", visible=True, on_click=lambda e: abrir_visor_flotante())

    def abrir_visor_flotante():
        if modo_actual.value == "RESPALDO MASIVO":
            log_terminal("> [AVISO] El visor requiere modo 'POR ESTADO' o 'POR CUENCA'.")
            return
            
        if not elementos_seleccionados:
            log_terminal("> [ERROR] Selecciona al menos una zona en el mapa (clic) para inspeccionarla.")
            return
            
        # El catálogo se extrae directamente de la memoria RAM (Persistencia HDS)
        df_cat = page.session.get("indice_bd_local")
        estaciones = descarga_logic.obtener_catalogo_visor(modo_actual.value, elementos_seleccionados, df_cat)
        
        if not estaciones:
            log_terminal("> [ERROR] No hay estaciones locales para la zona. Realiza el Respaldo Masivo primero.")
            return

        dd_estaciones = ft.Dropdown(
            label="Selecciona Estación (Max 150 enlistadas)",
            options=[ft.dropdown.Option(key=str(est['clave']), text=f"[{est['clave']}] {est['nombre']} ({est['estado_origen']})") for est in estaciones[:150]],
            width=500, bgcolor="#1a1a1a", color="white"
        )
        
        txt_local = ft.Text("Esperando consulta...", font_family="Roboto Mono", size=11, color="#888888")
        txt_server = ft.Text("Esperando consulta...", font_family="Roboto Mono", size=11, color="#888888")
        txt_status = ft.Text("ESTADO: EN ESPERA", weight="bold", size=14, color="white")
        
        def on_consultar_click(e):
            if not dd_estaciones.value: return
            clave_sel = dd_estaciones.value
            est_sel = next(s for s in estaciones if str(s['clave']) == clave_sel)
            
            txt_local.value, txt_server.value, txt_status.value, txt_status.color = "Consultando local...", "Conectando Servidor...", "ANALIZANDO...", "white"
            page.update()
            
            # Leemos la ruta vinculada, independientemente de la carpeta donde se encuentre
            ruta_tar = page.session.get("ruta_bd_activa")
            local_data, server_data = descarga_logic.inspeccionar_estacion_aislada(clave_sel, est_sel['estado_origen'], ruta_tar)
            
            txt_local.value, txt_server.value = "\n".join(local_data), "\n".join(server_data)
            
            if local_data == server_data and "Error" not in local_data[0]:
                txt_status.value, txt_status.color = "✅ SISTEMA ACTUALIZADO", "#00ff41"
            elif "Error" in local_data[0] or "Error" in server_data[0]:
                txt_status.value, txt_status.color = "⚠️ ERROR DE LECTURA", "red"
            else:
                txt_status.value, txt_status.color = "⚠️ DATOS NUEVOS DETECTADOS", "#ffdd00"
            page.update()

        dlg_visor = ft.AlertDialog(
            modal=False,
            title=ft.Text("SONDA DE TELEMETRÍA", color="#9900ff", weight="bold"),
            content=ft.Container(
                width=800, height=350,
                content=ft.Column([
                    ft.Row([dd_estaciones, ft.ElevatedButton("COMPARAR", bgcolor="#9900ff", color="white", on_click=on_consultar_click)]),
                    ft.Divider(color="#333333"),
                    ft.Row([
                        ft.Column([ft.Text("📁 BD LOCAL (tar.xz)", weight="bold", color="white"), ft.Container(content=txt_local, bgcolor="#000000", padding=10, border=ft.border.all(1, "#333333"), border_radius=5)], expand=1),
                        ft.Column([ft.Text("🌐 SERVIDOR (En Vivo)", weight="bold", color="white"), ft.Container(content=txt_server, bgcolor="#000000", padding=10, border=ft.border.all(1, "#333333"), border_radius=5)], expand=1)
                    ], expand=True),
                    ft.Divider(color="#333333"),
                    ft.Row([txt_status], alignment=ft.MainAxisAlignment.CENTER)
                ])
            ), bgcolor="#111111"
        )
        page.open(dlg_visor)
    
    def bloquear_interfaz(bloquear: bool):
        """Aislamiento total de la interfaz y menú global durante la descarga."""
        # 1. Controles Locales
        btn_modo_estado.disabled = bloquear
        btn_modo_cuenca.disabled = bloquear
        btn_modo_cuenca_obj.disabled = bloquear
        btn_cuenca_objetivo.disabled = bloquear
        btn_modo_masivo.disabled = bloquear
        
        # NUEVOS BOTONES IN-MEMORY (Reemplazan a btn_cambiar_dir)
        btn_vincular_bd.disabled = bloquear
        btn_exportar_csv.disabled = bloquear
        btn_auditar.disabled = bloquear
        
        # --- REGLA DE DOMINIO ZERO-TRUST ---
        # Bloqueamos el botón de Auditoría para evitar I/O Race Conditions 
        # (intentar leer con Deep Scan mientras el hilo de Descarga está escribiendo en el disco).
        btn_auditar.disabled = bloquear
        
        # <-- NUEVO: Bloqueamos los botones flotantes de la vista
        btn_inspeccionar.disabled = bloquear 
        btn_puente.disabled = bloquear 
        
        # 2. Lienzo Espacial
        contenedor_mapa.disabled = bloquear 
        
        # 3. Visibilidad de Motores
        btn_iniciar.visible = not bloquear
        btn_detener.visible = bloquear
        btn_detener.disabled = False # El freno de emergencia siempre debe estar libre
        
        # 4. Bloqueo Quirúrgico del NavigationRail Global (main.py)
        main_rail = page.session.get("main_rail")
        if main_rail:
            main_rail.disabled = bloquear # Apaga destinos: Extracción, Imputación, Análisis, Gastos
            
            # Apagamos los botones inferiores (Guardar / Cerrar)
            if hasattr(main_rail, "trailing") and main_rail.trailing:
                for ctrl in main_rail.trailing.controls:
                    ctrl.disabled = bloquear
                    
            main_rail.update() # Forzamos el redibujado del menú
        
        page.update()

    # --- 4. LIENZO ESPACIAL ---
    mapa_canvas = cv.Canvas(expand=True)
    
    # NUEVO: Diccionario mutable para zoom dinámico y preservación estricta de coordenadas
    map_dim = {"w": 800, "h": 450}

    def dibujar_mapa(porcentajes_estados=None):
        if porcentajes_estados is None: porcentajes_estados = {}
        mapa_canvas.shapes.clear()
        try:
            nonlocal poligonos_cargados
            
            # 1. Determinar el BBOX dinámico para Zoom
            b = descarga_logic.BBOX_MEXICO
            if modo_actual.value == "CUENCA OBJETIVO" and page.session.get("cuenca_bbox"):
                cb = page.session.get("cuenca_bbox") # [minx, miny, maxx, maxy]
                margen_x = max((cb[2] - cb[0]) * 0.1, 0.01)
                margen_y = max((cb[3] - cb[1]) * 0.1, 0.01)
                b = {
                    "min_x": cb[0] - margen_x,
                    "max_x": cb[2] + margen_x,
                    "min_y": cb[1] - margen_y,
                    "max_y": cb[3] + margen_y
                }

            # Aplicación de la matriz dinámica a escala
            scale_x, scale_y = map_dim["w"] / (b["max_x"] - b["min_x"]), map_dim["h"] / (b["max_y"] - b["min_y"])

            # 2. Dibujar fondo (Estados o Cuencas generales)
            tipo_fondo = "POR ESTADO" if modo_actual.value in ["RESPALDO MASIVO", "CUENCA OBJETIVO"] else modo_actual.value
            
            if not poligonos_cargados or page.session.get("ultimo_tipo_cargado") != tipo_fondo:
                poligonos_cargados = descarga_logic.cargar_poligonos(tipo_fondo)
                page.session.set("ultimo_tipo_cargado", tipo_fondo)

            for pol in poligonos_cargados:
                geo = pol["geometria"]
                nombre_poligono = pol["nombre"]
                
                if modo_actual.value == "RESPALDO MASIVO":
                    clave_corta = descarga_logic.obtener_clave_estado(nombre_poligono).upper()
                    pct_avance = porcentajes_estados.get(clave_corta, 0.0)
                    alfa_hex = f"{int(pct_avance * 153):02x}"
                    color_fill, color_stroke = (f"#{alfa_hex}00ff41" if pct_avance > 0 else "#000000"), ("#00ff41" if pct_avance >= 1.0 else "#004411")
                
                elif modo_actual.value == "CUENCA OBJETIVO":
                    # Estados de fondo 100% transparentes (solo el contorno visible)
                    color_fill, color_stroke = None, "#004411"
                    
                elif modo_actual.value in ["AUDITORÍA", "AUDITORÍA PROFUNDA"]:
                    clave_corta = descarga_logic.obtener_clave_estado(nombre_poligono).upper()
                    tiene_datos = estados_auditados.get(clave_corta, False)
                    color_fill = "#6600ff41" if tiene_datos else "#66ff0000"
                    color_stroke = "#00ff41" if tiene_datos else "#ff0000"
                
                else:
                    is_selected = nombre_poligono in elementos_seleccionados
                    color_fill, color_stroke = ("#6600ff41" if is_selected else "#000000"), ("#00ff41" if is_selected else "#005522")

                coords_list = [geo["coordinates"]] if geo["type"] == "Polygon" else geo["coordinates"]
                for poligono_anillos in coords_list:
                    path_elements = []
                    for idx, (lon, lat) in enumerate(poligono_anillos[0]):
                        px, py = (lon - b["min_x"]) * scale_x, (b["max_y"] - lat) * scale_y 
                        path_elements.append(cv.Path.MoveTo(px, py) if idx == 0 else cv.Path.LineTo(px, py))
                    path_elements.append(cv.Path.Close())
                    if color_fill:
                        mapa_canvas.shapes.append(cv.Path(elements=path_elements, paint=ft.Paint(color=color_fill, style=ft.PaintingStyle.FILL)))
                    if color_stroke:
                        mapa_canvas.shapes.append(cv.Path(elements=path_elements, paint=ft.Paint(color=color_stroke, style=ft.PaintingStyle.STROKE, stroke_width=1)))
                
                if modo_actual.value == "RESPALDO MASIVO" and pct_avance > 0:
                    centroide = pol["shapely_obj"].centroid
                    cx, cy = (centroide.x - b["min_x"]) * scale_x, (b["max_y"] - centroide.y) * scale_y 
                    mapa_canvas.shapes.append(cv.Text(x=cx - 10, y=cy + 5, text=f"{int(pct_avance * 100)}%", style=ft.TextStyle(size=10, weight="bold", color="#ffffff" if pct_avance >= 1.0 else "#00ff41", font_family="Roboto Mono")))

            # 3. Dibujar Capas Especiales (Geometrías Inyectadas)
            if modo_actual.value == "CUENCA OBJETIVO" and page.session.get("cuenca_geojson"):
                def dibujar_geojson(geo_dict, color_fill, color_stroke, stroke_width=1):
                    if not geo_dict: return
                    
                    # HERENCIA LÉXICA: Usamos 'b', 'scale_x', y 'scale_y' calculados por el padre (dibujar_mapa)
                    # Esto garantiza que el polígono UTM coincida exactamente con el mapa base.
                    coords_list = [geo_dict["coordinates"]] if geo_dict["type"] == "Polygon" else geo_dict["coordinates"]
                    for pol_rings in coords_list:
                        for anillo in pol_rings:
                            path_elements = []
                            for idx, (lon, lat) in enumerate(anillo):
                                px, py = (lon - b["min_x"]) * scale_x, (b["max_y"] - lat) * scale_y 
                                path_elements.append(cv.Path.MoveTo(px, py) if idx == 0 else cv.Path.LineTo(px, py))
                            path_elements.append(cv.Path.Close())
                            
                            if color_fill:
                                mapa_canvas.shapes.append(cv.Path(elements=path_elements, paint=ft.Paint(color=color_fill, style=ft.PaintingStyle.FILL)))
                            if color_stroke:
                                mapa_canvas.shapes.append(cv.Path(elements=path_elements, paint=ft.Paint(color=color_stroke, style=ft.PaintingStyle.STROKE, stroke_width=stroke_width)))
                
                dibujar_geojson(page.session.get("buffer_geojson"), color_fill=None, color_stroke="#cc0000", stroke_width=2)
                dibujar_geojson(page.session.get("cuenca_geojson"), color_fill="#6600ff41", color_stroke="#00ff41", stroke_width=1.5)

            page.update()
        except Exception as e: 
            print(f"Error en dibujar mapa: {e}")
            pass

    # --- 5. GESTOR DE EVENTOS ---
    def on_mapa_click(e):
        if modo_actual.value in ["RESPALDO MASIVO", "AUDITORÍA"] or btn_detener.visible: return
        
        if modo_actual.value == "CUENCA OBJETIVO":
            claves_obj = page.session.get("claves_objetivo")
            ruta_shp = page.session.get("cuenca_activa_ruta")
            if claves_obj is not None:
                log_terminal("> [INFO] Cuenca Objetivo seleccionada en el mapa.")
                if ruta_shp:
                    import os
                    nombre_shp = os.path.basename(ruta_shp)
                    log_terminal(f"> [+] Seleccionado automáticamente: {nombre_shp}")
                log_terminal(f"> 🎯 Buffer activo con {len(claves_obj)} estaciones listas.")
                log_terminal("> ✅ Presiona 'INICIAR EXTRACCIÓN' para descomprimirlas.")
            return

        b = descarga_logic.BBOX_MEXICO
        # Aplicación de matriz inversa para la detección geoespacial estricta
        scale_x, scale_y = map_dim["w"] / (b["max_x"] - b["min_x"]), map_dim["h"] / (b["max_y"] - b["min_y"])
        lon_clic, lat_clic = (e.local_x / scale_x) + b["min_x"], b["max_y"] - (e.local_y / scale_y)
        pol_detectado = descarga_logic.detectar_clic_poligono(poligonos_cargados, lon_clic, lat_clic)
        
        if pol_detectado:
            nombre = pol_detectado["nombre"] if pol_detectado["nombre"] != "Desconocido" else f"Zona ID: {pol_detectado['id']}"
            if nombre in elementos_seleccionados:
                elementos_seleccionados.remove(nombre)
                log_terminal(f"> [-] Deseleccionado: {nombre}")
            else:
                elementos_seleccionados.append(nombre)
                log_terminal(f"> [+] Seleccionado: {nombre}")
            dibujar_mapa()

    # Contenedor dinámico (inyecta dict en tiempo de renderizado)
    contenedor_mapa = ft.GestureDetector(on_tap_down=on_mapa_click, content=ft.Container(content=mapa_canvas, bgcolor="#0a0a0a", width=map_dim["w"], height=map_dim["h"], border=ft.border.all(1, "#00ff41"), border_radius=5))

    # --- NUEVO: MOTOR DE ZOOM (FIT-TO-SCREEN) ---
    def ajustar_zoom_mapa(e=None):
        if not contenedor_mapa.page: return # Prevención si la vista no está activa
        try:
            pw = page.width if page.width else 1200
            ph = page.height if page.height else 1000
            
            # Cálculo de márgenes dinámicos respetando los Flexboxes (expand=5 vs expand=3)
            espacio_w = max(400, pw - 320) # 250px panel izq + paddings
            espacio_h = max(250, (ph - 150) * 0.6) # Aproximadamente el 60% para el lienzo
            
            # Determinamos el Aspect Ratio BBOX
            b = descarga_logic.BBOX_MEXICO
            if modo_actual.value == "CUENCA OBJETIVO" and page.session.get("cuenca_bbox"):
                cb = page.session.get("cuenca_bbox")
                margen_x = max((cb[2] - cb[0]) * 0.1, 0.01)
                margen_y = max((cb[3] - cb[1]) * 0.1, 0.01)
                b = {
                    "min_x": cb[0] - margen_x, "max_x": cb[2] + margen_x,
                    "min_y": cb[1] - margen_y, "max_y": cb[3] + margen_y
                }
                
            aspect_ratio = (b["max_x"] - b["min_x"]) / (b["max_y"] - b["min_y"])
            
            # Restricción 'Fit to Contain' pura
            calc_h = espacio_w / aspect_ratio
            if calc_h > espacio_h:
                calc_h = espacio_h
                calc_w = calc_h * aspect_ratio
            else:
                calc_w = espacio_w
                
            map_dim["w"] = calc_w
            map_dim["h"] = calc_h
            contenedor_mapa.content.width = map_dim["w"]
            contenedor_mapa.content.height = map_dim["h"]
            
            if poligonos_cargados:
                dibujar_mapa()
        except Exception:
            pass

    # Suscripción segura al loop de eventos del Sistema Operativo (On Resize)
    page.on_resized = ajustar_zoom_mapa

    def cambiar_modo(e, nuevo_modo):
        modo_actual.value = nuevo_modo; elementos_seleccionados.clear();
        # Si NO es modo cuenca objetivo, limpiamos el caché de cuenca para volver al BBOX nacional
        if nuevo_modo != "CUENCA OBJETIVO":
            # No borramos el file de disco ni el session_get(cuenca_activa_ruta), 
            # solo re-ajustamos la vista borrando el poligonos_cargados para forzar recarga.
            poligonos_cargados.clear()
        
        log_terminal(f"> ---------------------------------\n> MODO DE VISUALIZACIÓN: {nuevo_modo}")
        ajustar_zoom_mapa() # Ajustamos la matriz de proyección (Zoom In / Zoom Out)
        dibujar_mapa()

    # --- 6. HILO DE EJECUCIÓN ---
    def detener_proceso(e):
        log_terminal("> [SISTEMA] Freno de emergencia activado. Sellando base de datos...")
        btn_detener.disabled = True
        descarga_logic.señal_abortar.set()
        page.update()

    def run_extraction_thread(ruta_target):
        if not elementos_seleccionados and modo_actual.value not in ["RESPALDO MASIVO", "CUENCA OBJETIVO"]:
            log_terminal("> [ERROR] SELECCIONE AL MENOS UNA ZONA."); return
            
        # Si es Cuenca Objetivo, usamos la lista de estaciones previamente calculadas en memoria
        if modo_actual.value == "CUENCA OBJETIVO":
            claves_obj = page.session.get("claves_objetivo")
            buffer_geom = page.session.get("buffer_geom") or page.session.get("buffer_geom_shapely")
            df_cat = page.session.get("indice_bd_local")
            
            # --- CORRECCIÓN CRÍTICA: Auto-recálculo si la BD se vinculó DESPUÉS de subir el SHP ---
            if (not claves_obj or len(claves_obj) == 0) and (buffer_geom is not None or page.session.get("wkt_envolvente_busqueda")) and df_cat is not None:
                log_terminal("> [SISTEMA] Sincronizando cruce espacial (BD vinculada posterior al SHP)...")
                claves_obj = descarga_logic.GestorExtraccion.filtrar_estaciones_por_buffer(
                    buffer_geom, df_cat, wkt_envolvente=page.session.get("wkt_envolvente_busqueda"), buffer_km=100.0
                )
                page.session.set("claves_objetivo", claves_obj)
                
            if not claves_obj:
                log_terminal("> [ERROR] No hay estaciones dentro del buffer. Asegúrate de tener una BD vinculada.")
                return
            
            # Falsificamos 'elementos_seleccionados' con las claves de estaciones para el motor
            elementos_seleccionados_temp = claves_obj
        else:
            elementos_seleccionados_temp = elementos_seleccionados
            
        progreso_bar.visible = True
        progreso_bar.value = 0 
        bloquear_interfaz(True)
        
        def task():
            try:
                ruta_activa_sesion = page.session.get("imput_folder_path")
                df_cat = page.session.get("indice_bd_local")
                
                ruta_final, df_generado = descarga_logic.procesar_descarga(
                    modo_actual.value, elementos_seleccionados_temp, ruta_target, df_cat,
                    log_terminal, update_progreso, callback_mapa=dibujar_mapa,
                    carpeta_previa=ruta_activa_sesion 
                )
            except Exception as ex:
                log_terminal(f"> [CRÍTICO] Error en hilo principal: {ex}")
                ruta_final, df_generado = None, None
            finally:
                def _finalize():
                    btn_detener.visible, btn_iniciar.visible = False, True
                    bloquear_interfaz(False)
                    if not descarga_logic.señal_abortar.is_set():
                        
                        # Inyectar Automáticamente en Memoria si fue Respaldo Masivo
                        if modo_actual.value == "RESPALDO MASIVO" and df_generado is not None:
                            page.session.set("ruta_bd_activa", ruta_target)
                            page.session.set("indice_bd_local", df_generado)
                            lbl_bd_activa.value = f"BD: {os.path.basename(ruta_target)}"
                            lbl_bd_activa.color = "#00ff41"
                            lbl_indice_status.value = f"Índice HDS: Activo ({len(df_generado)} est.)"
                            lbl_indice_status.color = "#00ff41"
                        
                        # Habilitar paso a Imputación si fue extracción de datos (Local)
                        if ruta_final and modo_actual.value != "RESPALDO MASIVO":
                            page.session.set("imput_folder_path", ruta_final) 
                            page.session.set("txt_backup", {"__type__": "folder_backup", "path": ruta_final})
                            btn_puente.visible = True
                    page.update()
                page.run_thread(_finalize)
            
        threading.Thread(target=task, daemon=True).start()

    # --- 7. CARGA INICIAL ASÍNCRONA ---
    async def render_mapa_inicial():
        await asyncio.sleep(0.5)
        ajustar_zoom_mapa() # Detonamos el Zoom Dinámico al inicio para que nazca ajustado a la ventana
        dibujar_mapa()
    page.run_task(render_mapa_inicial)

    # --- 8. LAYOUT ---
    return ft.Container(
        content=ft.Row([
            ft.Container(
                content=ft.Column([
                    ft.Text("EXTRACCIÓN", size=18, weight="bold"),
                    ft.Divider(color="#222222"),
                    btn_vincular_bd, btn_exportar_csv, btn_cuenca_objetivo,
                    ft.Divider(color="#222222"),
                    ft.Text("MÉTODOS DE SELECCIÓN", color="white", size=10, weight="bold"),
                    btn_modo_estado, btn_modo_cuenca, btn_modo_cuenca_obj,
                    ft.Divider(color="#222222"),
                    ft.Text("DB CONTROL", color="white", size=10, weight="bold"),
                    btn_modo_masivo, btn_auditar,
                    ft.Divider(color="#222222"),
                    ft.Text("ESTATUS DE PROCESOS", color="white", size=10, weight="bold"),
                    progreso_espacial, lbl_area, lbl_arf_status, lbl_estaciones,
                    ft.Divider(color="#222222"),
                    ft.Text("ESTADO DE BASE DE DATOS", color="white", size=10, weight="bold"),
                    lbl_bd_activa, lbl_indice_status,
                ], spacing=15, scroll=ft.ScrollMode.AUTO),
                width=250, padding=20, border=ft.border.only(right=ft.border.BorderSide(1, "#222222"))
            ),
            ft.Container(
                content=ft.Column([
                    ft.Container(
                        content=ft.Column([
                            ft.Row([
                                ft.Row([ft.Text("MAPA GEOESPACIAL: ", color="white", weight="bold"), modo_actual]),
                                ft.Row([btn_purgar, btn_inspeccionar, btn_iniciar, btn_detener, btn_puente])
                            ], alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
                            ft.Stack([
                                contenedor_mapa
                            ], expand=True),
                            ft.Text("Modo Normal: Clic para seleccionar | SHP: Calcula Buffer UTM 100km.", color="grey", size=11, italic=True)
                        ]), expand=5, padding=20
                    ),
                    ft.Divider(color="#222222", height=1),
                    ft.Container(
                        content=ft.Column([
                            ft.Text("CONSOLA DE PROCESO", color="white", weight="bold", size=12),
                            progreso_bar,
                            ft.Container(content=terminal_list, bgcolor="#000000", expand=True, padding=10, border=ft.border.all(1, "#222222"))
                        ]), expand=3, padding=20
                    )
                ]), expand=True, padding=0
            )
        ], expand=True, spacing=0),
        bgcolor="#050505", expand=True
    )