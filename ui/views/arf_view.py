import flet as ft
import traceback
import os
import threading
import pandas as pd
import sqlite3

from core.arf_metodo_frecuencias import MotorFrecuencias
from core.arf_logic import MotorFiltradoARF, MotorCalculoARF, GestorMatricesARF
from core.arf_metodo_empirico import MotorEmpirico

def build_arf_view(page: ft.Page):
    def _resolver_ruta_bd(ruta_base=None):
        if not ruta_base:
            ruta_base = page.session.get("current_project_path") or page.session.get("ruta_bd_activa")
        if not ruta_base: return None
        from core.imputacion_logic import resolver_ruta_hds
        return resolver_ruta_hds(ruta_base)

    acciones_globales = {}
    
    # --- VARIABLES DE ESTADO Y SESIÓN ---
    ruta_imputados = page.session.get("imput_output_folder") 
    
    # Rehidratación previa de variables de Fase 7 desde base de datos (.hds / .gpkg) si la sesión está vacía
    ruta_hds = _resolver_ruta_bd()
    if ruta_hds and os.path.exists(ruta_hds):
        try:
            import sqlite3, json
            conn_db = sqlite3.connect(ruta_hds, timeout=10.0)
            conn_db.execute("PRAGMA busy_timeout = 10000;")
            cursor = conn_db.cursor()
            if page.session.get("arf_df_intensidad_maestra") is None:
                cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='arf_df_intensidad_maestra'")
                if cursor.fetchone():
                    df_i_db = pd.read_sql_query("SELECT * FROM arf_df_intensidad_maestra", conn_db, index_col="Duracion_min")
                    df_i_db.columns = [int(c) if str(c).isdigit() else c for c in df_i_db.columns]
                    page.session.set("arf_df_intensidad_maestra", df_i_db)
            if page.session.get("arf_df_altura_maestra") is None:
                cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='arf_df_altura_maestra'")
                if cursor.fetchone():
                    df_h_db = pd.read_sql_query("SELECT * FROM arf_df_altura_maestra", conn_db, index_col="Duracion_min")
                    df_h_db.columns = [int(c) if str(c).isdigit() else c for c in df_h_db.columns]
                    page.session.set("arf_df_altura_maestra", df_h_db)
            if page.session.get("arf_df_hietograma_maestro") is None:
                cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='arf_df_hietograma_maestro'")
                if cursor.fetchone():
                    df_hiet_db = pd.read_sql_query("SELECT * FROM arf_df_hietograma_maestro", conn_db, index_col="Tiempo_min")
                    df_hiet_db.columns = [int(c) if str(c).isdigit() else c for c in df_hiet_db.columns]
                    page.session.set("arf_df_hietograma_maestro", df_hiet_db)
            if not page.session.get("fase7_resultados"):
                cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='arf_fase7_metadata'")
                if cursor.fetchone():
                    df_meta = pd.read_sql_query("SELECT value FROM arf_fase7_metadata WHERE key='fase7_json'", conn_db)
                    if not df_meta.empty:
                        page.session.set("fase7_resultados", json.loads(df_meta.iloc[0]["value"]))
            conn_db.close()
        except Exception as ex_rehyd_db:
            print(f"⚠️ Aviso: Error al precargar Fase 7 desde SQLite: {ex_rehyd_db}")

    
    # --- INDICADORES LED DE FLUJO SECUENCIAL ---
    def get_led(): return ft.Icon(ft.Icons.CIRCLE, color="#333333", size=14)
    led_f1, led_f2, led_f3, led_f4, led_f5, led_f6 = get_led(), get_led(), get_led(), get_led(), get_led(), get_led()
    
    btn_fase1 = ft.ElevatedButton("1. FILTRADO MASIVO", icon=ft.Icons.FILTER_ALT, bgcolor="#1a1a1a", color="grey", height=45)
    btn_fase2 = ft.ElevatedButton("2. GENERAR LMA", icon=ft.Icons.GRID_ON, disabled=True, bgcolor="#1a1a1a", color="grey", height=45)
    btn_fase3 = ft.ElevatedButton("3. ARF EMPÍRICO", icon=ft.Icons.CALCULATE, disabled=True, bgcolor="#1a1a1a", color="grey", height=45)
    btn_fase4 = ft.ElevatedButton("4. ARF ÁREA FIJA", icon=ft.Icons.AUTO_GRAPH, disabled=True, bgcolor="#1a1a1a", color="grey", height=45)
    btn_fase5 = ft.ElevatedButton("5. ARF SEVERIANO", icon=ft.Icons.STORM, disabled=True, bgcolor="#1a1a1a", color="grey", height=45)
    btn_fase6 = ft.ElevatedButton("6. TORMENTA DE DISEÑO", icon=ft.Icons.WATER_DROP, disabled=True, bgcolor="#1a1a1a", color="grey", height=45)
    
    progreso_arf = ft.ProgressBar(width=None, color="#00ff41", bgcolor="#1a1a1a", value=0, visible=False)
    
    # =========================================================================
    # GESTOR DE EXPORTACIÓN (FILEPICKER)
    # =========================================================================
    export_dir_dialog = page.session.get("arf_export_dir_dialog")
    if not export_dir_dialog:
        export_dir_dialog = ft.FilePicker()
        page.session.set("arf_export_dir_dialog", export_dir_dialog)
        page.overlay.append(export_dir_dialog)
    
    def on_export_dir_selected(e: ft.FilePickerResultEvent):
        if e.path:
            df_i = page.session.get("arf_df_intensidad_maestra")
            df_h = page.session.get("arf_df_altura_maestra")
            df_hiet = page.session.get("arf_df_hietograma_maestro")
            area = page.session.get("area_cuenca_km2") or 0.0
            
            if df_i is not None and df_h is not None:
                from core.arf_metodo_diseno import MotorDiseno
                # Pasamos la orden al backend asíncrono
                MotorDiseno.exportar_paquete_regional_async(e.path, df_i, df_h, df_hiet, area, page)
            else:
                page.open(ft.SnackBar(content=ft.Text("❌ Faltan datos en memoria. Ejecute el modelo nuevamente.", color="white"), bgcolor="#cc0000"))

    export_dir_dialog.on_result = on_export_dir_selected
    
    # =========================================================================
    # PESTAÑA 1: MATRIZ DE CONTINUIDAD (ADUANA)
    # =========================================================================
    dt_continuidad = ft.DataTable(
        columns=[
            ft.DataColumn(ft.Text("Estación", color="#00ff41")),
            ft.DataColumn(ft.Text("Inicio - Fin")),
            ft.DataColumn(ft.Text("Días Efectivos")),
            ft.DataColumn(ft.Text("Max Gap")),
            ft.DataColumn(ft.Text("Integridad"))
        ],
        rows=[], border=ft.border.all(1, "#333333"), heading_row_color="#1a1a1a",
    )

    def abrir_tabla_completa():
        dlg = ft.AlertDialog(
            title=ft.Text("Matriz de Continuidad Completa", color="#00ff41"),
            content=ft.Container(width=page.window.width * 0.9, height=page.window.height * 0.7, content=ft.Column([dt_continuidad], scroll=ft.ScrollMode.ADAPTIVE)),
            bgcolor="#111111"
        )
        page.open(dlg)

    def build_tab_calidad():
        return ft.Container(
            content=ft.Column([
                ft.Row([ft.Text("Matriz de Continuidad Temporal", weight="bold", size=16), ft.IconButton(ft.Icons.FULLSCREEN, icon_color="#00ff41", on_click=lambda e: abrir_tabla_completa())], alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
                ft.Text("Resultado inmutable de la purga C1, C2 y C3.", color="grey", size=12),
                ft.Container(content=ft.Column([dt_continuidad], scroll=ft.ScrollMode.ADAPTIVE), expand=True, border=ft.border.all(1, "#222222"))
            ]), padding=20
        )

    # =========================================================================
    # PESTAÑA 2: DASHBOARD BENTO BOX (LMA)
    # =========================================================================
    # Contenedor dinámico que será inyectado por el hilo al terminar la Fase 2
    bento_container = ft.Container(
        content=ft.Text("Esperando ejecución de Fase 2 (GENERAR LMA)...", color="grey"),
        alignment=ft.alignment.center, expand=True
    )

    def renderizar_bento_box(data):
        def kpi_card(title, value, icon, color):
            return ft.Container(
                content=ft.Column([
                    ft.Row([ft.Icon(icon, color=color, size=16), ft.Text(title, color="grey", size=10, weight="bold")]),
                    ft.Text(value, color="white", size=16, weight="bold")
                ], spacing=2, alignment=ft.MainAxisAlignment.CENTER),
                bgcolor="#111111", padding=15, border_radius=10, border=ft.border.all(1, "#333333"),
                col={"sm": 6, "md": 4, "lg": 4} # Responsive
            )

        # 1. KPIs
        kpis = ft.ResponsiveRow([
            kpi_card("PERIODO HISTÓRICO", f"{data['fecha_inicio'][:4]} - {data['fecha_fin'][:4]}", ft.Icons.CALENDAR_MONTH, "#1c75fa"),
            kpi_card("ESTACIONES (ÉLITE)", str(len(data['claves_activas'])), ft.Icons.SENSORS, "#1c75fa"),
            kpi_card("AUDITORÍA OMM", f"{data['densidad']} km²/est", ft.Icons.POLICY, data['color_omm']),
            kpi_card("DÍAS SECOS", f"{data['pct_secos']:.1f}%", ft.Icons.WB_SUNNY, "#ffaa00"),
            kpi_card("LMA MÁX. (HISTÓRICA)", f"{data['lma_maxima']:.1f} mm", ft.Icons.SHOW_CHART, "#ff0044"),
            kpi_card("ESTATUS DE RED", data['eval_omm'], ft.Icons.FACT_CHECK, data['color_omm']),
        ])

        # 2. Gráfico Cinemático Full Width
        grafico_cinematico = ft.Container(content=ft.Image(src_base64=data['plot_serie'], fit=ft.ImageFit.CONTAIN), bgcolor="#0a0a0a", border=ft.border.all(1, "#333333"), border_radius=10, padding=10)

        # 3. Exportación y AMS
        def exportar_csv(e):
            ruta_hds = _resolver_ruta_bd(page.session.get("ruta_bd_activa"))
            if ruta_hds:
                try:
                    import sqlite3
                    import pandas as pd
                    conn = sqlite3.connect(ruta_hds)
                    df_lma = pd.read_sql_query("SELECT * FROM arf_matriz_maestra", conn, parse_dates=["Fecha"], index_col="Fecha")
                    conn.close()
                    ruta_salida = os.path.join(str(ruta_hds) + "_LMA_Diaria.csv")
                    df_lma.to_csv(ruta_salida)
                    page.snack_bar = ft.SnackBar(ft.Text(f"✅ Matriz LMA Exportada a: {ruta_salida}"), bgcolor="#00ff41", open=True)
                except Exception as ex:
                    page.snack_bar = ft.SnackBar(ft.Text(f"❌ Error al exportar: {ex}"), bgcolor="#cc0000", open=True)
                page.update()

        btn_exportar = ft.ElevatedButton("📥 DESCARGAR MATRIZ LMA (CSV)", bgcolor="#00ff41", color="black", on_click=exportar_csv)
        
        filas_ams = [ft.DataRow(cells=[ft.DataCell(ft.Text(str(r['anio']))), ft.DataCell(ft.Text(str(r['lma']), color="#00ff41", weight="bold"))]) for r in data['tabla_ams']]
        tabla_ams_ui = ft.Container(
            content=ft.Column([
                ft.Row([ft.Text("Simultaneidad (Máximos Areales)", weight="bold", color="white"), btn_exportar], alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
                ft.Container(content=ft.Column([ft.DataTable(columns=[ft.DataColumn(ft.Text("Año")), ft.DataColumn(ft.Text("LMA Máxima (mm)"))], rows=filas_ams, heading_row_height=30, data_row_max_height=30)], scroll=ft.ScrollMode.ADAPTIVE), height=300)
            ]), bgcolor="#111111", border=ft.border.all(1, "#333333"), border_radius=10, padding=15
        )
        grafico_ams = ft.Container(content=ft.Image(src_base64=data['plot_ams'], fit=ft.ImageFit.CONTAIN), bgcolor="#0a0a0a", border=ft.border.all(1, "#333333"), border_radius=10, padding=10)

        # 4. El Motor Geo-Espacial (Time-Slider de Isoyetas + Envolvente)
        años_mapa = sorted([int(y) for y, v in data.get('mapas_isoyetas', {}).items() if v.get('b64')])
        
        if años_mapa:
            tiene_envolvente = 9999 in años_mapa
            años_reales = [y for y in años_mapa if y != 9999]
            
            min_y, max_y = min(años_reales), max(años_reales)
            
            # Arrancamos mostrando la joya de la corona si existe
            año_inicial = 9999 if tiene_envolvente else max_y
            img_mapa = ft.Image(src_base64=data['mapas_isoyetas'][str(año_inicial)]['b64'], fit=ft.ImageFit.CONTAIN, expand=True)
            
            def get_titulo_epicentro(year):
                info = data['mapas_isoyetas'][str(year)]
                if year == 9999:
                    return f"🌟 MAPA COMPUESTO: Envolvente de Susceptibilidad (LMA Máxima por Epicentro)"
                return f"📅 Fecha: {info['fecha']} | 🎯 Estación Crítica: {info['estacion']} | 💧 Lluvia Máx: {info['valor']:.1f} mm"

            lbl_fecha = ft.Text(get_titulo_epicentro(año_inicial), weight="bold", color="#ffdd00" if tiene_envolvente else "#00ff41", size=15)
            
            def on_slider_change(e):
                y_sel = int(e.control.value)
                
                if tiene_envolvente and y_sel > max_y:
                    img_mapa.src_base64 = data['mapas_isoyetas']["9999"]['b64']
                    lbl_fecha.value = get_titulo_epicentro(9999)
                    lbl_fecha.color = "#ffdd00"
                else:
                    nearest = min(años_reales, key=lambda x: abs(x - y_sel))
                    img_mapa.src_base64 = data['mapas_isoyetas'][str(nearest)]['b64']
                    
                    if nearest != y_sel:
                        lbl_fecha.value = f"{get_titulo_epicentro(nearest)} (Gap en {y_sel})"
                        lbl_fecha.color = "orange"
                    else:
                        lbl_fecha.value = get_titulo_epicentro(nearest)
                        lbl_fecha.color = "#00ff41"
                        
                img_mapa.update()
                lbl_fecha.update()
                
            # Configuración del Slider: Si hay envolvente, le sumamos 1 paso ficticio a la derecha
            val_max_slider = max_y + 1 if tiene_envolvente else max_y
            
            if min_y == max_y and not tiene_envolvente:
                slider_isoyetas = ft.Slider(min=min_y, max=max_y+1, divisions=1, value=min_y, disabled=True)
            else:
                # Usamos un label custom para que si llega al max+1 diga "ENVOLVENTE"
                def formatear_label(valor):
                    if tiene_envolvente and valor > max_y: return "MAPA COMPUESTO"
                    return str(int(valor))
                    
                slider_isoyetas = ft.Slider(
                    min=min_y, max=val_max_slider, 
                    divisions=val_max_slider - min_y, 
                    value=val_max_slider, 
                    label="{value}", 
                    on_change=on_slider_change, 
                    active_color="#ff0044" if tiene_envolvente else "#00ff41", 
                    inactive_color="#333333"
                )
            
            mapa_ui = ft.Container(
                content=ft.Column([
                    ft.Row([ft.Icon(ft.Icons.MAP, color="#1c75fa"), ft.Text("Motor Geo-Espacial de Isoyetas Regionales", weight="bold", size=18, color="white")]),
                    lbl_fecha,
                    slider_isoyetas,
                    ft.Container(content=img_mapa, height=650, alignment=ft.alignment.center)
                ]), bgcolor="#111111", border=ft.border.all(1, "#333333"), border_radius=10, padding=20
            )
        else:
            mapa_ui = ft.Container(content=ft.Text("Geometría de cuenca no disponible para mapeo espacial.", color="grey"), padding=20)

        return ft.Column([
            kpis, 
            grafico_cinematico, 
            ft.ResponsiveRow([
                ft.Column([grafico_ams], col={"md": 12, "lg": 7}), 
                ft.Column([tabla_ams_ui], col={"md": 12, "lg": 5})
            ]),
            mapa_ui
        ], scroll=ft.ScrollMode.ADAPTIVE, expand=True)

    def build_tab_matriz(): return ft.Container(content=bento_container, padding=15)

    # (Placeholders para las siguientes pestañas)
    # =========================================================================
    # PESTAÑA 3: MÉTODO EMPÍRICO (Aislado)
    # =========================================================================

    def build_tab_empirico(on_complete_callback):
        from core.arf_metodo_empirico import MotorEmpirico
        
        lbl_uswb = ft.Text("0.000", size=24, weight="bold", color="#1c75fa")
        lbl_temez = ft.Text("0.000", size=24, weight="bold", color="#9900ff")
        img_plot_area = ft.Image(src_base64="", fit=ft.ImageFit.CONTAIN, expand=True)
        img_plot_tiempo = ft.Image(src_base64="", fit=ft.ImageFit.CONTAIN, expand=True)
        
        dt_temporal = ft.DataTable(
            columns=[
                ft.DataColumn(ft.Text("Minutos", color="#00ff41")), ft.DataColumn(ft.Text("Horas", color="grey")),
                ft.DataColumn(ft.Text("USWB (TP-29)")), ft.DataColumn(ft.Text("Témez (España)"))
            ], rows=[], border=ft.border.all(1, "#333333"), heading_row_color="#1a1a1a"
        )
        
        slider_duracion = ft.Slider(min=1, max=24, divisions=23, value=24, label="{value} horas", active_color="#00ff41")
        
        def _kpi_empirico(titulo, texto_obj, color_borde):
            return ft.Container(content=ft.Column([ft.Text(titulo, color="grey", size=11, weight="bold"), texto_obj], alignment=ft.MainAxisAlignment.CENTER, horizontal_alignment=ft.CrossAxisAlignment.CENTER), bgcolor="#111111", border=ft.border.all(1, color_borde), border_radius=10, padding=15, expand=True)

        def ejecutar_empirico():
            area_cuenca = page.session.get("area_cuenca_km2")
            if not area_cuenca:
                page.snack_bar = ft.SnackBar(ft.Text("Área de la cuenca no encontrada. Ejecute Módulo 1."), bgcolor="#cc0000", open=True); page.update()
                if on_complete_callback: on_complete_callback(False)
                return
            duracion = slider_duracion.value

            def _task():
                res = MotorEmpirico.ejecutar_analisis(area_cuenca, duracion)
                if res["exito"]:
                    def _success():
                        lbl_uswb.value = f"{res['fra_uswb']:.3f}"
                        lbl_temez.value = f"{res['fra_temez']:.3f}"
                        img_plot_area.src_base64 = res['plot1_b64']
                        img_plot_tiempo.src_base64 = res['plot2_b64']
                        dt_temporal.rows.clear()
                        for fila in res['tabla_datos']:
                            dt_temporal.rows.append(ft.DataRow(cells=[
                                ft.DataCell(ft.Text(f"{fila['minutos']} min", weight="bold")), ft.DataCell(ft.Text(f"{fila['horas']} h", color="grey")),
                                ft.DataCell(ft.Text(f"{fila['uswb']:.3f}", color="#1c75fa")), ft.DataCell(ft.Text(f"{fila['temez']:.3f}", color="#9900ff"))
                            ]))
                        page.session.set("arf_fase3_res", res)
                        page.update()
                        if on_complete_callback: on_complete_callback(True)
                    page.run_thread(_success)
                else:
                    def _fail():
                        page.snack_bar = ft.SnackBar(ft.Text(f"Error: {res['error']}"), bgcolor="#cc0000", open=True); page.update()
                        if on_complete_callback: on_complete_callback(False)
                    page.run_thread(_fail)
            threading.Thread(target=_task, daemon=True).start()

        saved_f3 = page.session.get("arf_fase3_res")
        if saved_f3:
            lbl_uswb.value = f"{saved_f3['fra_uswb']:.3f}"
            lbl_temez.value = f"{saved_f3['fra_temez']:.3f}"
            img_plot_area.src_base64 = saved_f3['plot1_b64']
            img_plot_tiempo.src_base64 = saved_f3['plot2_b64']
            for fila in saved_f3['tabla_datos']:
                dt_temporal.rows.append(ft.DataRow(cells=[
                    ft.DataCell(ft.Text(f"{fila['minutos']} min", weight="bold")), ft.DataCell(ft.Text(f"{fila['horas']} h", color="grey")),
                    ft.DataCell(ft.Text(f"{fila['uswb']:.3f}", color="#1c75fa")), ft.DataCell(ft.Text(f"{fila['temez']:.3f}", color="#9900ff"))
                ]))

        cuerpo_scroll = ft.Column([
            ft.Row([
                ft.Column([ft.Text("MÉTODO 1: Formulaciones Empíricas", weight="bold", size=18, color="#00ff41"), ft.Text("Evaluación estática (geométrica) para acotar físicamente el Factor de Reducción.", color="grey", size=12)], expand=True),
                ft.Icon(ft.Icons.AUTO_GRAPH, color="#00ff41", size=30) # Icono decorativo en lugar de botón
            ], alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
            ft.Divider(color="#333333"),
            ft.Container(content=ft.Row([ft.Text("Duración (D):", color="white"), ft.Container(content=slider_duracion, expand=True)]), bgcolor="#111111", padding=10, border_radius=10, border=ft.border.all(1, "#333333")),
            ft.Row([_kpi_empirico("FRA - USWB (TP-29)", lbl_uswb, "#1c75fa"), _kpi_empirico("FRA - TÉMEZ (España)", lbl_temez, "#9900ff")]),
            ft.Container(content=img_plot_area, bgcolor="#0a0a0a", border=ft.border.all(1, "#333333"), border_radius=10, padding=10),
            ft.Container(content=img_plot_tiempo, bgcolor="#0a0a0a", border=ft.border.all(1, "#333333"), border_radius=10, padding=10),
            ft.Row([ft.Icon(ft.Icons.TABLE_CHART, color="#00ff41"), ft.Text("Discretización Temporal", weight="bold", color="white", size=14)]),
            ft.Container(content=ft.Column([dt_temporal], scroll=ft.ScrollMode.ADAPTIVE), border=ft.border.all(1, "#333333"), border_radius=10, height=400)
        ], spacing=15, scroll=ft.ScrollMode.ADAPTIVE)

        return ft.Container(content=cuerpo_scroll, padding=20, expand=True), ejecutar_empirico
    # =========================================================================
    # FÁBRICA DE PESTAÑAS: ANÁLISIS DE FRECUENCIAS (Área Fija Espacial)
    # =========================================================================

    def crear_pestana_frecuencias(modo_calc: str, titulo_metodo: str, descripcion: str, color_tema: str, on_complete_callback):
        img_lluvia_tr = ft.Image(src_base64="", fit=ft.ImageFit.CONTAIN, expand=True)
        img_fra_tr = ft.Image(src_base64="", fit=ft.ImageFit.CONTAIN, expand=True)
        lbl_tr100, lbl_tr1000, lbl_tr10000 = ft.Text("---", size=24, weight="bold", color=color_tema), ft.Text("---", size=24, weight="bold", color="#ffdd00"), ft.Text("---", size=24, weight="bold", color="#ff0044")
        
        dt_resultados = ft.DataTable(columns=[ft.DataColumn(ft.Text("Tr (Años)", color="#00ff41")), ft.DataColumn(ft.Text("LMA Areal (mm)", color="#1c75fa")), ft.DataColumn(ft.Text("Kriging Regional (mm)", color="#ffdd00")), ft.DataColumn(ft.Text("FRA Oficial", color="white", weight="bold"))], rows=[], border=ft.border.all(1, "#333333"), heading_row_color="#1a1a1a")
        dt_distribuciones = ft.DataTable(columns=[ft.DataColumn(ft.Text("Estación / Cuenca", color="grey")), ft.DataColumn(ft.Text("Mejor Ajuste", color="grey"))], rows=[], border=ft.border.all(1, "#222222"))

        img_mapa_tr = ft.Image(src_base64="", fit=ft.ImageFit.CONTAIN, expand=True)
        lbl_mapa_tr = ft.Text("Riesgo Espacial: Superficie Kriging", weight="bold", color="white", size=14)
        estado_pestana = {"mapas": {}}
        tr_lista = [2, 5, 10, 20, 50, 100, 500, 1000, 10000]

        def on_tr_slider_change(e):
            idx = int(e.control.value)
            tr_sel = tr_lista[idx]
            if str(tr_sel) in estado_pestana["mapas"]:
                img_mapa_tr.src_base64 = estado_pestana["mapas"][str(tr_sel)]
                lbl_mapa_tr.value = f"Riesgo Espacial: Superficie Kriging (Tr {tr_sel} Años)"
                img_mapa_tr.update(); lbl_mapa_tr.update()

        slider_tr = ft.Slider(min=0, max=len(tr_lista)-1, divisions=len(tr_lista)-1, value=len(tr_lista)-1, on_change=on_tr_slider_change, active_color=color_tema, inactive_color="#333333")
        contenedor_mapa = ft.Container(content=ft.Column([ft.Row([ft.Icon(ft.Icons.MAP, color=color_tema), lbl_mapa_tr]), slider_tr, ft.Container(content=img_mapa_tr, height=500, alignment=ft.alignment.center)]), bgcolor="#111111", border=ft.border.all(1, color_tema), border_radius=10, padding=15, visible=False)

        def _kpi_freq(titulo, texto_obj, color_borde):
            return ft.Container(content=ft.Column([ft.Text(titulo, color="grey", size=11, weight="bold"), texto_obj], alignment=ft.MainAxisAlignment.CENTER, horizontal_alignment=ft.CrossAxisAlignment.CENTER), bgcolor="#111111", border=ft.border.all(1, color_borde), border_radius=10, padding=15, expand=True)

        def ejecutar_frecuencias():
            ruta_hds = _resolver_ruta_bd(page.session.get("ruta_bd_activa"))
            cuenca_wkt = page.session.get("cuenca_geom_wkt")
            
            if not ruta_hds or not cuenca_wkt:
                page.snack_bar = ft.SnackBar(ft.Text("⚠️ Falta Geometría o BD."), bgcolor="#cc0000", open=True); page.update()
                if on_complete_callback: on_complete_callback(False)
                return

            def _task():
                try:
                    import sqlite3
                    import pandas as pd
                    conn = sqlite3.connect(ruta_hds)
                    df_maestro = pd.read_sql_query("SELECT * FROM arf_matriz_maestra", conn, parse_dates=["Fecha"], index_col="Fecha")
                    
                    df_ams_lma = df_maestro[['LMA_Base']].groupby(df_maestro.index.year).max()
                    df_ams_lma.rename(columns={'LMA_Base': 'LMA_Max_Anual'}, inplace=True)
                    
                    claves = [col for col in df_maestro.columns if col != 'LMA_Base']
                    conn.close()
                    
                    estaciones_dict = page.session.get("imput_station_files") or {}
                    df_indice = page.session.get("indice_bd_local")
                    coords_dict = {}
                    if estaciones_dict:
                        for k, v in estaciones_dict.items():
                            coords_dict[str(k).upper()] = {"LATITUD": v.get("lat", 0), "LONGITUD": v.get("lon", 0)}
                    elif df_indice is not None and not df_indice.empty:
                        for _, row in df_indice.iterrows():
                            coords_dict[str(row.get('clave', '')).upper()] = {"LATITUD": row.get('lat', 0), "LONGITUD": row.get('lon', 0)}
                    
                    datos_coords = []
                    for c in claves:
                        if c in coords_dict:
                            datos_coords.append({"clave_estacion": c, "LATITUD": coords_dict[c]["LATITUD"], "LONGITUD": coords_dict[c]["LONGITUD"]})
                    df_coords = pd.DataFrame(datos_coords).set_index('clave_estacion') if datos_coords else pd.DataFrame()

                    res = MotorFrecuencias.ejecutar_analisis_espacial(df_maestro, df_ams_lma, df_coords, cuenca_wkt, modo=modo_calc)
                    if res["exito"]:
                        def _success():
                            dt_resultados.rows.clear()
                            for fila in res['tabla']:
                                dt_resultados.rows.append(ft.DataRow(cells=[ft.DataCell(ft.Text(str(fila['tr']), weight="bold")), ft.DataCell(ft.Text(str(fila['lma']), color="#1c75fa")), ft.DataCell(ft.Text(str(fila['kriging']), color="#ffdd00")), ft.DataCell(ft.Text(str(fila['fra']), color="white", weight="bold"))]))
                                if fila['tr'] == 100: lbl_tr100.value = str(fila['fra'])
                                if fila['tr'] == 1000: lbl_tr1000.value = str(fila['fra'])
                                if fila['tr'] == 10000: lbl_tr10000.value = str(fila['fra'])
                                
                            dt_distribuciones.rows.clear()
                            for est, nombre_dist in res["distribuciones"].items():
                                col_text = color_tema if est == "LMA Areal (Cuenca)" else "white"
                                dt_distribuciones.rows.append(ft.DataRow(cells=[ft.DataCell(ft.Text(est, color=col_text, weight="bold")), ft.DataCell(ft.Text(nombre_dist, color="grey"))]))

                            img_lluvia_tr.src_base64 = res['plot_lluvia']
                            img_fra_tr.src_base64 = res['plot_arf']
                            
                            if res.get('mapas_tr'):
                                estado_pestana["mapas"] = res['mapas_tr']
                                img_mapa_tr.src_base64 = res['mapas_tr']["10000"]
                                lbl_mapa_tr.value = "Riesgo Espacial: Superficie Kriging (Tr 10000 Años)"
                                slider_tr.value = 8
                                contenedor_mapa.visible = True
                            
                            page.session.set(f"arf_fase4_res_{modo_calc}", res)
                            page.update()
                            if on_complete_callback: on_complete_callback(True)
                        page.run_thread(_success)
                    else:
                        def _fail():
                            page.snack_bar = ft.SnackBar(ft.Text(f"❌ Error Cálculo: {res['error']}"), bgcolor="#cc0000", open=True); page.update()
                            if on_complete_callback: on_complete_callback(False)
                        page.run_thread(_fail)
                except Exception as ex:
                    import traceback; traceback.print_exc()
                    page.run_thread(lambda: on_complete_callback(False) if on_complete_callback else None)
            threading.Thread(target=_task, daemon=True).start()

        saved_res = page.session.get(f"arf_fase4_res_{modo_calc}")
        if saved_res:
            for fila in saved_res['tabla']:
                dt_resultados.rows.append(ft.DataRow(cells=[ft.DataCell(ft.Text(str(fila['tr']), weight="bold")), ft.DataCell(ft.Text(str(fila['lma']), color="#1c75fa")), ft.DataCell(ft.Text(str(fila['kriging']), color="#ffdd00")), ft.DataCell(ft.Text(str(fila['fra']), color="white", weight="bold"))]))
                if fila['tr'] == 100: lbl_tr100.value = str(fila['fra'])
                if fila['tr'] == 1000: lbl_tr1000.value = str(fila['fra'])
                if fila['tr'] == 10000: lbl_tr10000.value = str(fila['fra'])
            for est, nombre_dist in saved_res["distribuciones"].items():
                col_text = color_tema if est == "LMA Areal (Cuenca)" else "white"
                dt_distribuciones.rows.append(ft.DataRow(cells=[ft.DataCell(ft.Text(est, color=col_text, weight="bold")), ft.DataCell(ft.Text(nombre_dist, color="grey"))]))
            img_lluvia_tr.src_base64 = saved_res['plot_lluvia']
            img_fra_tr.src_base64 = saved_res['plot_arf']
            if saved_res.get('mapas_tr'):
                estado_pestana["mapas"] = saved_res['mapas_tr']
                img_mapa_tr.src_base64 = saved_res['mapas_tr']["10000"]
                lbl_mapa_tr.value = "Riesgo Espacial: Superficie Kriging (Tr 10000 Años)"
                slider_tr.value = 8
                contenedor_mapa.visible = True

        return ft.Container(
            content=ft.Column([
                ft.Row([
                    ft.Column([ft.Text(titulo_metodo, weight="bold", size=18, color=color_tema), ft.Text(descripcion, color="grey", size=12)], expand=True),
                    ft.Icon(ft.Icons.MULTILINE_CHART, color=color_tema, size=30)
                ], alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
                ft.Divider(color="#333333"),
                ft.Row([_kpi_freq("FRA Diseño (100 Años)", lbl_tr100, "#333333"), _kpi_freq("FRA Crítico (1,000 Años)", lbl_tr1000, "#ffdd00"), _kpi_freq("FRA Extremo (10k Años)", lbl_tr10000, "#ff0044")]),
                ft.ResponsiveRow([ft.Container(content=img_lluvia_tr, bgcolor="#0a0a0a", border=ft.border.all(1, "#333333"), border_radius=10, padding=10, col={"md": 12, "lg": 6}), ft.Container(content=img_fra_tr, bgcolor="#0a0a0a", border=ft.border.all(1, "#333333"), border_radius=10, padding=10, col={"md": 12, "lg": 6})]),
                contenedor_mapa,
                ft.ResponsiveRow([ft.Container(content=ft.Column([ft.Text("Tabla de Cálculo y Truncamiento", color="white", weight="bold"), dt_resultados], scroll=ft.ScrollMode.ADAPTIVE), border=ft.border.all(1, "#333333"), border_radius=10, col={"md": 12, "lg": 8}, height=350), ft.Container(content=ft.Column([ft.Text("Modelos 'Best-Fit'", color="white", weight="bold"), dt_distribuciones], scroll=ft.ScrollMode.ADAPTIVE), border=ft.border.all(1, "#333333"), border_radius=10, col={"md": 12, "lg": 4}, height=350)])
            ], spacing=15, scroll=ft.ScrollMode.ADAPTIVE), padding=20, expand=True
        ), ejecutar_frecuencias
    # =========================================================================
    # PESTAÑA 6: MÉTODO I.I. UNAM (Tormenta Centrada Topológica)
    # =========================================================================

    def build_tab_unam(on_complete_callback):
        from core.arf_metodo_unam import MotorUNAM
        import sqlite3
        import pandas as pd

        img_mapa_tormenta, img_plot_dad = ft.Image(src_base64="", fit=ft.ImageFit.CONTAIN, expand=True), ft.Image(src_base64="", fit=ft.ImageFit.CONTAIN, expand=True)
        lbl_area_cuenca, lbl_area_dominio, lbl_estatus, lbl_fra_oficial = ft.Text("---", size=20, weight="bold", color="white"), ft.Text("---", size=20, weight="bold", color="grey"), ft.Text("---", size=20, weight="bold", color="white"), ft.Text("---", size=24, weight="bold", color="#ff0044")
        
        dt_envolvente = ft.DataTable(columns=[ft.DataColumn(ft.Text("Área (km²)")), ft.DataColumn(ft.Text("T1", color="#1c75fa")), ft.DataColumn(ft.Text("T2", color="#9900ff")), ft.DataColumn(ft.Text("T3", color="#ffaa00")), ft.DataColumn(ft.Text("T4", color="#00ff41")), ft.DataColumn(ft.Text("T5", color="#ff00ff")), ft.DataColumn(ft.Text("T6", color="#00ffff")), ft.DataColumn(ft.Text("ENVOLVENTE", color="#ff0044", weight="bold"))], rows=[], border=ft.border.all(1, "#333333"), heading_row_color="#1a1a1a")
        
        estado_unam = {"mapas": {}, "asc_data": {}, "filenames": {}}
        
        def actualizar_mapa(e):
            if e.control.value in estado_unam["mapas"]:
                img_mapa_tormenta.src_base64 = estado_unam["mapas"][e.control.value]
                img_mapa_tormenta.update()

        dd_tormentas = ft.Dropdown(label="Inspeccionar Super-Celda", options=[], on_change=actualizar_mapa, color="#ff0044", border_color="#333333", expand=True)

        def exportar_grid_actual(e):
            id_sel = dd_tormentas.value
            ruta_hds = _resolver_ruta_bd(page.session.get("ruta_bd_activa"))
            if id_sel in estado_unam["asc_data"] and ruta_hds:
                try:
                    ruta_qgis = os.path.join(os.path.dirname(ruta_hds), "Exportacion_QGIS_UNAM")
                    os.makedirs(ruta_qgis, exist_ok=True)
                    ruta_archivo = os.path.join(ruta_qgis, estado_unam["filenames"][id_sel])
                    with open(ruta_archivo, 'w', encoding='utf-8') as f: f.write(estado_unam["asc_data"][id_sel])
                    page.snack_bar = ft.SnackBar(ft.Text(f"✅ GRID exportado a: {ruta_archivo}"), bgcolor="#00ff41", open=True, duration=5000)
                except Exception as ex: page.snack_bar = ft.SnackBar(ft.Text(f"❌ Error al exportar: {str(ex)}"), bgcolor="#cc0000", open=True)
                page.update()

        btn_exportar_qgis = ft.ElevatedButton("Exportar GRID a QGIS (.asc)", icon=ft.Icons.DOWNLOAD, bgcolor="#1c75fa", color="white", disabled=True, on_click=exportar_grid_actual)

        def _kpi_unam(titulo, texto_obj, color_borde, icon):
            return ft.Container(content=ft.Column([ft.Row([ft.Icon(icon, color=color_borde, size=16), ft.Text(titulo, color="grey", size=11, weight="bold")]), texto_obj], alignment=ft.MainAxisAlignment.CENTER, horizontal_alignment=ft.CrossAxisAlignment.CENTER), bgcolor="#111111", border=ft.border.all(1, color_borde), border_radius=10, padding=15, col={"md": 6, "lg": 3})

        def ejecutar_unam():
            ruta_hds = _resolver_ruta_bd(page.session.get("ruta_bd_activa"))
            cuenca_wkt = page.session.get("cuenca_geom_wkt")
            if not ruta_hds or not cuenca_wkt:
                if on_complete_callback: on_complete_callback(False)
                return

            def _task():
                try:
                    import sqlite3
                    import pandas as pd
                    conn = sqlite3.connect(ruta_hds)
                    df_maestro = pd.read_sql_query("SELECT * FROM arf_matriz_maestra", conn, parse_dates=["Fecha"], index_col="Fecha")
                    claves = [col for col in df_maestro.columns if col != 'LMA_Base']
                    conn.close()
                    
                    estaciones_dict = page.session.get("imput_station_files") or {}
                    df_indice = page.session.get("indice_bd_local")
                    coords_dict = {}
                    if estaciones_dict:
                        for k, v in estaciones_dict.items():
                            coords_dict[str(k).upper()] = {"LATITUD": v.get("lat", 0), "LONGITUD": v.get("lon", 0)}
                    elif df_indice is not None and not df_indice.empty:
                        for _, row in df_indice.iterrows():
                            coords_dict[str(row.get('clave', '')).upper()] = {"LATITUD": row.get('lat', 0), "LONGITUD": row.get('lon', 0)}
                    
                    datos_coords = []
                    for c in claves:
                        if c in coords_dict:
                            datos_coords.append({"clave_estacion": c, "LATITUD": coords_dict[c]["LATITUD"], "LONGITUD": coords_dict[c]["LONGITUD"]})
                    df_coords = pd.DataFrame(datos_coords).set_index('clave_estacion') if datos_coords else pd.DataFrame()

                    res = MotorUNAM.ejecutar_analisis_tormenta_centrada(df_maestro, df_coords, cuenca_wkt)
                    if res["exito"]:
                        def _success():
                            page.session.set("arf_fase6_res", res)
                            page.session.set("unam_tabla_masiva", res["tabla_masiva"])
                            lbl_area_cuenca.value = f"{res['area_cuenca']} km²"
                            lbl_area_dominio.value = f"{res['area_dominio']} km²"
                            lbl_fra_oficial.value = f"{res['fra_oficial']}"
                            lbl_estatus.value = "VÁLIDO (Análisis DAD)" if res['area_cuenca'] >= 25.0 else "MICROCUENCA (FRA = 1.0)"
                            lbl_estatus.color = "#00ff41" if res['area_cuenca'] >= 25.0 else "#ffdd00"

                            dd_tormentas.options.clear(); estado_unam["mapas"].clear(); estado_unam["asc_data"].clear(); estado_unam["filenames"].clear()
                            for mapa in res["mapas"]:
                                dd_tormentas.options.append(ft.dropdown.Option(mapa["id"]))
                                estado_unam["mapas"][mapa["id"]] = mapa["b64"]; estado_unam["asc_data"][mapa["id"]] = mapa["asc_data"]; estado_unam["filenames"][mapa["id"]] = mapa["filename"]
                            if res["mapas"]:
                                dd_tormentas.value = res["mapas"][0]["id"]
                                img_mapa_tormenta.src_base64 = estado_unam["mapas"][dd_tormentas.value]
                            
                            img_plot_dad.src_base64 = res["plot_dad"]
                            dt_envolvente.rows.clear()
                            for fila in res["tabla_masiva"]:
                                dt_envolvente.rows.append(ft.DataRow(cells=[ft.DataCell(ft.Text(f"{fila['area']:.1f}")), ft.DataCell(ft.Text(f"{fila.get('t1', 1.0):.3f}", color="#1c75fa")), ft.DataCell(ft.Text(f"{fila.get('t2', 1.0):.3f}", color="#9900ff")), ft.DataCell(ft.Text(f"{fila.get('t3', 1.0):.3f}", color="#ffaa00")), ft.DataCell(ft.Text(f"{fila.get('t4', 1.0):.3f}", color="#00ff41")), ft.DataCell(ft.Text(f"{fila.get('t5', 1.0):.3f}", color="#ff00ff")), ft.DataCell(ft.Text(f"{fila.get('t6', 1.0):.3f}", color="#00ffff")), ft.DataCell(ft.Text(f"{fila['fra_env']:.3f}", weight="bold", color="#ff0044"))]))
                            
                            btn_exportar_qgis.disabled = False
                            contenedor_resultados.visible = True
                            page.update()
                            if on_complete_callback: on_complete_callback(True)
                        page.run_thread(_success)
                    else:
                        if on_complete_callback: page.run_thread(lambda: on_complete_callback(False))
                except Exception:
                    import traceback; traceback.print_exc()
                    if on_complete_callback: page.run_thread(lambda: on_complete_callback(False))

            threading.Thread(target=_task, daemon=True).start()

        contenedor_resultados = ft.Container(content=ft.Column([ft.ResponsiveRow([_kpi_unam("ÁREA CUENCA", lbl_area_cuenca, "#00ff41", ft.Icons.LANDSCAPE), _kpi_unam("DOMINIO REGIONAL", lbl_area_dominio, "#333333", ft.Icons.MAP), _kpi_unam("ESTATUS DE ANÁLISIS", lbl_estatus, "#333333", ft.Icons.FACT_CHECK), _kpi_unam("FRA DE DISEÑO", lbl_fra_oficial, "#ff0044", ft.Icons.BOLT)]), ft.Container(content=ft.Column([ft.Row([ft.Row([ft.Icon(ft.Icons.RADAR, color="#ff0044"), ft.Text("Topología de la Tormenta", color="white", weight="bold")]), ft.Row([dd_tormentas, btn_exportar_qgis], alignment=ft.MainAxisAlignment.END, expand=True)], alignment=ft.MainAxisAlignment.SPACE_BETWEEN), ft.Container(content=ft.InteractiveViewer(content=img_mapa_tormenta, max_scale=5), height=500, alignment=ft.alignment.center)]), bgcolor="#111111", border=ft.border.all(1, "#ff0044"), border_radius=10, padding=15, margin=ft.margin.only(top=10)), ft.Container(content=ft.Column([ft.Row([ft.Icon(ft.Icons.SHOW_CHART, color="#00ff41"), ft.Text("Curvas Depth-Area-Duration (DAD)", color="white", weight="bold")]), ft.Container(content=img_plot_dad, height=500, alignment=ft.alignment.center)]), bgcolor="#111111", border=ft.border.all(1, "#333333"), border_radius=10, padding=15, margin=ft.margin.only(top=10)), ft.Container(content=ft.Column([ft.Row([ft.Icon(ft.Icons.TABLE_CHART, color="white"), ft.Text("Memoria de Cálculo DAD (Top 6 Tormentas)", weight="bold", color="white")]), ft.Container(content=ft.Column([dt_envolvente], scroll=ft.ScrollMode.ADAPTIVE), height=350)]), bgcolor="#111111", border=ft.border.all(1, "#333333"), border_radius=10, padding=15, margin=ft.margin.only(top=10))]), visible=False)

        def _render_unam(res):
            lbl_area_cuenca.value = f"{res['area_cuenca']} km²"
            lbl_area_dominio.value = f"{res['area_dominio']} km²"
            lbl_fra_oficial.value = f"{res['fra_oficial']}"
            lbl_estatus.value = "COMPLETADO (HD)"
            
            dd_tormentas.options.clear()
            estado_unam["mapas"].clear(); estado_unam["asc_data"].clear(); estado_unam["filenames"].clear()
            for mapa in res["mapas"]:
                dd_tormentas.options.append(ft.dropdown.Option(mapa["id"]))
                estado_unam["mapas"][mapa["id"]] = mapa["b64"]; estado_unam["asc_data"][mapa["id"]] = mapa["asc_data"]; estado_unam["filenames"][mapa["id"]] = mapa["filename"]
            if res["mapas"]:
                dd_tormentas.value = res["mapas"][0]["id"]
                img_mapa_tormenta.src_base64 = estado_unam["mapas"][dd_tormentas.value]
            
            img_plot_dad.src_base64 = res["plot_dad"]
            dt_envolvente.rows.clear()
            for fila in res["tabla_masiva"]:
                dt_envolvente.rows.append(ft.DataRow(cells=[ft.DataCell(ft.Text(f"{fila['area']:.1f}")), ft.DataCell(ft.Text(f"{fila.get('t1', 1.0):.3f}", color="#1c75fa")), ft.DataCell(ft.Text(f"{fila.get('t2', 1.0):.3f}", color="#9900ff")), ft.DataCell(ft.Text(f"{fila.get('t3', 1.0):.3f}", color="#ffaa00")), ft.DataCell(ft.Text(f"{fila.get('t4', 1.0):.3f}", color="#00ff41")), ft.DataCell(ft.Text(f"{fila.get('t5', 1.0):.3f}", color="#ff00ff")), ft.DataCell(ft.Text(f"{fila.get('t6', 1.0):.3f}", color="#00ffff")), ft.DataCell(ft.Text(f"{fila['fra_env']:.3f}", weight="bold", color="#ff0044"))]))
            
            btn_exportar_qgis.disabled = False
            contenedor_resultados.visible = True

        res_previo = page.session.get("arf_fase6_res")
        if res_previo:
            _render_unam(res_previo)
            
        return ft.Container(content=ft.Column([ft.Row([ft.Column([ft.Text("MÉTODO 3: Inst. Ingeniería UNAM (Tormenta Centrada Topológica)", weight="bold", size=18, color="#ff0044"), ft.Text("Algoritmo HPC en RAM. Genera isoyetas de Ultra-Alta Definición y permite exportación táctica a QGIS.", color="grey", size=12)], expand=True), ft.Icon(ft.Icons.STORM, color="#ff0044", size=30)], alignment=ft.MainAxisAlignment.SPACE_BETWEEN), ft.Divider(color="#333333"), contenedor_resultados], scroll=ft.ScrollMode.ADAPTIVE), padding=20, expand=True), ejecutar_unam



    # =========================================================================
    # =========================================================================
    # HILOS DE EJECUCIÓN SECUENCIAL DEL PANEL DE CONTROL
    # =========================================================================
    def update_progreso(val):
        progreso_arf.value = val; page.update()

    def ejecutar_fase1_aduana(e):
        ruta_hds = _resolver_ruta_bd(page.session.get("ruta_bd_activa"))
        claves_objetivo = page.session.get("claves_objetivo")
        if not ruta_hds or not os.path.exists(ruta_hds) or not claves_objetivo:
            page.snack_bar = ft.SnackBar(ft.Text("⚠️ Faltan datos o BD. Ejecute Módulo 1 y 2."), bgcolor="#cc0000", open=True); page.update()
            return
            
        btn_fase1.disabled = True; progreso_arf.visible = True; progreso_arf.value = 0; page.update()

        def _task():
            try:
                stats = MotorFiltradoARF.procesar_gpkg_arf(ruta_hds, claves_objetivo, update_progreso)
                page.session.set("arf_stats_continuidad", stats)
                
                dt_continuidad.rows.clear()
                for s in stats:
                    color_text = "#00ff41" if s["Integridad_%"] > 80 else ("orange" if s["Integridad_%"] > 50 else "#cc0000")
                    dt_continuidad.rows.append(ft.DataRow(cells=[
                        ft.DataCell(ft.Text(s["Clave"], weight="bold")), ft.DataCell(ft.Text(f"{s['Inicio']} - {s['Fin']}", size=11)),
                        ft.DataCell(ft.Text(str(s["Dias_Efectivos"]))), ft.DataCell(ft.Text(str(s["Max_Gap_Dias"]), color="red" if s["Max_Gap_Dias"] > 30 else "white")),
                        ft.DataCell(ft.Text(f"{s['Integridad_%']}%", color=color_text))
                    ]))
                
                def _success():
                    led_f1.color = "#00ff41"
                    btn_fase2.disabled = False  # DESBLOQUEA PASO 2
                    page.snack_bar = ft.SnackBar(ft.Text(f"✅ Fase 1 Completada."), bgcolor="#00ff41", open=True)
                    progreso_arf.visible = False; btn_fase1.disabled = False
                    page.update()
                page.run_thread(_success)

            except Exception as ex:
                traceback.print_exc()
                msg = str(ex)
                def _fail():
                    page.snack_bar = ft.SnackBar(ft.Text(f"❌ Error Fase 1: {msg}"), bgcolor="#cc0000", open=True)
                    btn_fase1.disabled = False; progreso_arf.visible = False; page.update()
                page.run_thread(_fail)
        threading.Thread(target=_task, daemon=True).start()

    def ejecutar_fase2_lma(e):
        stats = page.session.get("arf_stats_continuidad")
        ruta_hds = _resolver_ruta_bd(page.session.get("ruta_bd_activa"))
        area_cuenca = page.session.get("area_cuenca_km2") or 1.0
        
        # --- PARCHE WKT: Auto-Reconstrucción de Geometría ---
        cuenca_wkt = page.session.get("cuenca_geom_wkt") or ""
        if not cuenca_wkt:
            cuenca_geojson = page.session.get("cuenca_geojson")
            if cuenca_geojson:
                try:
                    import json
                    from shapely.geometry import shape
                    from shapely.ops import unary_union
                    
                    if isinstance(cuenca_geojson, str):
                        cuenca_geojson = json.loads(cuenca_geojson)
                        
                    if "features" in cuenca_geojson:
                        polys = [shape(f["geometry"]) for f in cuenca_geojson["features"]]
                        cuenca_wkt = unary_union(polys).wkt
                    elif "geometry" in cuenca_geojson:
                        cuenca_wkt = shape(cuenca_geojson["geometry"]).wkt
                    else:
                        cuenca_wkt = shape(cuenca_geojson).wkt
                        
                    page.session.set("cuenca_geom_wkt", cuenca_wkt) # Persistir para Fases posteriores
                except Exception as ex:
                    print(f"⚠️ Fallo convirtiendo GeoJSON a WKT: {ex}")

        if not stats or not ruta_hds:
            page.snack_bar = ft.SnackBar(ft.Text("⚠️ Faltan datos de Continuidad. Ejecute Paso 1."), bgcolor="#cc0000", open=True); page.update()
            return
            
        # --- EXTRACCIÓN DE COORDENADAS DESDE MEMORIA RAM ---
        estaciones_dict = page.session.get("imput_station_files") or {}
        df_indice = page.session.get("indice_bd_local")
        
        coords_dict = {}
        if estaciones_dict:
            for k, v in estaciones_dict.items():
                coords_dict[k] = {"LATITUD": v.get("lat", 0), "LONGITUD": v.get("lon", 0)}
        elif df_indice is not None and not df_indice.empty:
            for _, row in df_indice.iterrows():
                coords_dict[str(row.get('clave', '')).upper()] = {"LATITUD": row.get('lat', 0), "LONGITUD": row.get('lon', 0)}

        btn_fase2.disabled = True
        progreso_arf.visible = True
        progreso_arf.value = None
        page.update()

        def _task():
            exito, data = GestorMatricesARF.construir_matriz_dinamica(ruta_hds, stats, area_cuenca, cuenca_wkt, coordenadas_extra=coords_dict, umbral_lluvia=0.5)
            if exito:
                # Ya no guardamos df_maestro en RAM (Optimización SQLite)
                def _success():
                    try:
                        page.session.set("arf_fase2_bento_data", data)
                        # 1. Reemplazo y Repintado Forzoso
                        bento_container.content = renderizar_bento_box(data)
                        bento_container.alignment = None # Elimina el centrado del texto de espera
                        bento_container.update() # <--- OBLIGA A REFRESCAR LA PANTALLA
                        
                        # 2. Actualización de LEDs y Botones
                        led_f2.color = "#00ff41"
                        led_f2.update()
                        
                        btn_fase3.disabled = False
                        btn_fase3.update()
                        
                        tabs_principales.selected_index = 1
                        tabs_principales.update()
                        
                        btn_fase2.disabled = False
                        btn_fase2.update()
                        
                        progreso_arf.visible = False
                        progreso_arf.update()
                        
                        page.snack_bar = ft.SnackBar(ft.Text("✅ Matriz Espacial y Mapas Generados."), bgcolor="#00ff41", open=True)
                        page.update()
                    except Exception as ui_ex:
                        import traceback; traceback.print_exc()
                        page.snack_bar = ft.SnackBar(ft.Text(f"❌ Error interno de UI: {str(ui_ex)}"), bgcolor="#cc0000", open=True)
                        btn_fase2.disabled = False; progreso_arf.visible = False; page.update()
                page.run_thread(_success)
            else:
                def _fail():
                    page.snack_bar = ft.SnackBar(ft.Text(f"❌ Error Fase 2: {data}"), bgcolor="#cc0000", open=True)
                    btn_fase2.disabled = False; progreso_arf.visible = False; page.update()
                page.run_thread(_fail)
        threading.Thread(target=_task, daemon=True).start()

    # =========================================================================
    # PANEL DE CONTROL E INTERFAZ MAESTRA
    # =========================================================================
    # Botones con la misma estética, centralizados e interbloqueados.
    style_btn = ft.ButtonStyle(shape=ft.RoundedRectangleBorder(radius=5))
    
    btn_fase1 = ft.ElevatedButton("1. FILTRADO MASIVO", icon=ft.Icons.SHIELD, bgcolor="#1a1a1a", color="#00ff41", style=style_btn, on_click=ejecutar_fase1_aduana)
    btn_fase2 = ft.ElevatedButton("2. GENERAR LMA", icon=ft.Icons.GRID_ON, bgcolor="#1a1a1a", color="grey", disabled=True, style=style_btn, on_click=ejecutar_fase2_lma)
    
    # --- LÓGICA DEL ORQUESTADOR (CADENA DE EJECUCIÓN) ---
    def on_f3_done(success):
        btn_fase3.disabled = False
        if success:
            led_f3.color = "#00ff41"; btn_fase4.disabled = False
        else: led_f3.color = "#cc0000"
        page.update()

    def on_f4a_done(success):
        if success:
            tabs_principales.selected_index = 4; page.update()
            exec_tab5() # Ejecuta Frecuencia Heterogénea automáticamente
        else:
            led_f4.color = "#cc0000"; btn_fase4.disabled = False; page.update()

    def on_f4b_done(success):
        btn_fase4.disabled = False
        if success:
            led_f4.color = "#00ff41"; btn_fase5.disabled = False
        else: led_f4.color = "#cc0000"
        page.update()

    def on_f5_done(success):
        btn_fase5.disabled = False
        if success:
            led_f5.color = "#00ff41"; btn_fase6.disabled = False
        else: led_f5.color = "#cc0000"
        page.update()

    def on_f6_done(success):
        btn_fase6.disabled = False
        if success:
            led_f6.color = "#00ff41"
        else: led_f6.color = "#cc0000"
        page.update()

    # Conexión de Botones con Pestañas
    btn_fase3.on_click = lambda e: (setattr(btn_fase3, 'disabled', True), setattr(led_f3, 'color', '#ffdd00'), setattr(tabs_principales, 'selected_index', 2), page.update(), exec_tab3())
    btn_fase4.on_click = lambda e: (setattr(btn_fase4, 'disabled', True), setattr(led_f4, 'color', '#ffdd00'), setattr(tabs_principales, 'selected_index', 3), page.update(), exec_tab4())
    btn_fase5.on_click = lambda e: (setattr(btn_fase5, 'disabled', True), setattr(led_f5, 'color', '#ffdd00'), setattr(tabs_principales, 'selected_index', 5), page.update(), exec_tab6())
    btn_fase6.on_click = lambda e: (setattr(btn_fase6, 'disabled', True), setattr(led_f6, 'color', '#ffdd00'), setattr(tabs_principales, 'selected_index', 6), page.update(), exec_tab7())
    
    # Invocación de constructores de UI
    content_tab3, exec_tab3 = build_tab_empirico(on_f3_done)
    content_tab4, exec_tab4 = crear_pestana_frecuencias('homogeneo', "MÉTODO 2A: Frec. (Homogéneo)", "Forzamiento Regional Suave", "#1c75fa", on_f4a_done)
    content_tab5, exec_tab5 = crear_pestana_frecuencias('heterogeneo', "MÉTODO 2B: Frec. (Heterogéneo)", "Ajuste Geo-Estocástico de Microclimas", "#9900ff", on_f4b_done)
    content_tab6, exec_tab6 = build_tab_unam(on_f5_done)

    # =========================================================================
    # PESTAÑA 8: CHECKPOINT REGIONAL Y EXPORTACIÓN
    # =========================================================================
    
    # Contenedores vacíos iniciales
    img_plot_intensidad = ft.Image(src_base64="", visible=False, expand=True, fit=ft.ImageFit.CONTAIN)
    img_plot_altura = ft.Image(src_base64="", visible=False, expand=True, fit=ft.ImageFit.CONTAIN)
    img_plot_hietograma = ft.Image(src_base64="", visible=False, expand=True, fit=ft.ImageFit.CONTAIN)
    
    contenedor_tabla_i = ft.Container(expand=True)
    contenedor_tabla_h = ft.Container(expand=True)
    contenedor_tabla_hiet = ft.Container(expand=True)
    
    btn_exportar_regional = ft.ElevatedButton(
        "Exportar Paquete Regional (XLSX y PNG)",
        icon=ft.Icons.DOWNLOAD,
        color="#111111", bgcolor="#00ff41",
        on_click=lambda _: export_dir_dialog.get_directory_path(),
        disabled=True
    )
    
    listview_regional_default = ft.ListView([
        ft.Text("1. INTENSIDAD DE PRECIPITACIÓN REGIONAL (I-D-Tr)", weight="bold", color="#00ff41", size=16),
        ft.Container(content=img_plot_intensidad, height=350, border=ft.border.all(1, "#333333"), padding=10, bgcolor="white"),
        ft.Container(content=contenedor_tabla_i, height=200, border=ft.border.all(1, "#333333")),
        ft.Divider(color="#222222", height=30),
        
        ft.Text("2. LÁMINA DE PRECIPITACIÓN ACUMULADA (A-D-Tr)", weight="bold", color="#00ff41", size=16),
        ft.Container(content=img_plot_altura, height=350, border=ft.border.all(1, "#333333"), padding=10, bgcolor="white"),
        ft.Container(content=contenedor_tabla_h, height=200, border=ft.border.all(1, "#333333")),
        ft.Divider(color="#222222", height=30),
        
        ft.Text("3. HIETOGRAMA DE DISEÑO REGIONAL (BLOQUES ALTERNOS)", weight="bold", color="#00ff41", size=16),
        ft.Container(content=img_plot_hietograma, height=350, border=ft.border.all(1, "#333333"), padding=10, bgcolor="white"),
        ft.Container(content=contenedor_tabla_hiet, height=200, border=ft.border.all(1, "#333333")),
        ft.Divider(color="#222222", height=30),
        
        ft.Container(
            content=ft.Row([
                ft.Icon(ft.Icons.VERIFIED_USER, color="#00ff41"),
                ft.Text("Aduana Aprobada: Las 3 matrices (I-D-Tr, A-D-Tr y Hietograma) están listas.", expand=True, color="white"),
                btn_exportar_regional
            ], alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
            bgcolor="#1a1a1a", padding=15, border_radius=8
        )
    ], spacing=15, expand=True)

    contenedor_resultados_arf = ft.Container(
        padding=20,
        content=listview_regional_default
    )

    tab_curvas_regionales = ft.Tab(
        text="8. Curvas de Cuenca",
        #icon=ft.Icons.SSID_CHART,
        content=contenedor_resultados_arf
    )

    def poblar_pestaña_8_regionales(df_i=None, df_h=None, df_hiet=None, b64_i=None, b64_h=None, b64_hiet=None):
        """Convierte los DataFrames en componentes Flet y activa la descarga o el renderizado reactivo multicuenca."""
        try:
            res_guardado = page.session.get("fase7_resultados") or {}
            def _get_valid_df(df_arg, dict_key, session_key):
                if isinstance(df_arg, (pd.DataFrame, pd.Series)):
                    return df_arg
                val = res_guardado.get(dict_key)
                if isinstance(val, (pd.DataFrame, pd.Series)):
                    return val
                val_sess = page.session.get(session_key)
                if isinstance(val_sess, (pd.DataFrame, pd.Series)):
                    return val_sess
                return None

            df_i = _get_valid_df(df_i, "df_intensidades_arf", "arf_df_intensidad_maestra")
            df_h = _get_valid_df(df_h, "df_alturas_arf", "arf_df_altura_maestra")
            df_hiet = _get_valid_df(df_hiet, "df_hietogramas_arf", "arf_df_hietograma_maestro")
            
            b64_i = b64_i or res_guardado.get("arf_b64_curva_i", "") or (page.session.get("arf_b64_curva_i") or "")
            b64_h = b64_h or res_guardado.get("arf_b64_curva_h", "") or (page.session.get("arf_b64_curva_h") or "")
            b64_hiet = b64_hiet or res_guardado.get("arf_b64_curva_hiet", "") or (page.session.get("arf_b64_curva_hiet") or "")

            def _df_to_flet_table(df, col_label="Duración (min)"):
                if df is None or not isinstance(df, (pd.DataFrame, pd.Series)) or (hasattr(df, "empty") and df.empty):
                    return ft.Text("No hay datos disponibles", color="grey")
                cols = [ft.DataColumn(ft.Text(col_label, color="#00ff41"))]
                cols += [ft.DataColumn(ft.Text(f"Tr {c} años", color="#00ff41")) for c in df.columns]
                
                rows = []
                for idx, row in df.iterrows():
                    cells = [ft.DataCell(ft.Text(str(idx), weight="bold"))]
                    for val in row.values:
                        try:
                            val_str = f"{float(val):.2f}"
                        except (ValueError, TypeError):
                            val_str = str(val)
                        cells.append(ft.DataCell(ft.Text(val_str)))
                    rows.append(ft.DataRow(cells=cells))
                return ft.Column([
                    ft.Row([ft.DataTable(columns=cols, rows=rows, heading_row_color="#1a1a1a")], scroll=ft.ScrollMode.AUTO)
                ], scroll=ft.ScrollMode.AUTO)

            # =========================================================================
            # FASE 6: RENDERIZADO REACTIVO MULTICUENCA (ZERO-DOM-OVERLOAD)
            # =========================================================================
            dict_maestro = page.session.get("arf_multicuenca_maestro")
            
            if dict_maestro:
                def guardar_excel_callback(e: ft.FilePickerResultEvent, dict_maest):
                    if not e.path: return
                    try:
                        import re
                        ruta_salida = e.path if e.path.endswith(".xlsx") else e.path + ".xlsx"
                        with pd.ExcelWriter(ruta_salida, engine='xlsxwriter') as writer:
                            for cuenca, trs in dict_maest.items():
                                nombre_limpio = re.sub(r'[:\\/?*\[\]]', '_', str(cuenca)).strip()
                                nombre_hoja_seguro = nombre_limpio[:31] if nombre_limpio else "Subcuenca"
                                base_nombre = nombre_hoja_seguro
                                contador = 1
                                while nombre_hoja_seguro in writer.sheets:
                                    sufijo = f"_{contador}"
                                    nombre_hoja_seguro = base_nombre[:31 - len(sufijo)] + sufijo
                                    contador += 1

                                df_hiet_cons = trs.get("df_hiet")
                                df_i_cons = trs.get("df_i")
                                df_h_cons = trs.get("df_h")
                                
                                if df_hiet_cons is None or df_i_cons is None or df_h_cons is None:
                                    tr_keys = [k for k in trs.keys() if str(k).isdigit()]
                                    if tr_keys:
                                        prim = trs[tr_keys[0]] if isinstance(trs.get(tr_keys[0]), dict) else {}
                                        df_hiet_cons = df_hiet_cons if df_hiet_cons is not None else prim.get("df_hiet")
                                        df_i_cons = df_i_cons if df_i_cons is not None else prim.get("df_i")
                                        df_h_cons = df_h_cons if df_h_cons is not None else prim.get("df_h")
                                
                                if df_hiet_cons is None:
                                    df_cons = pd.DataFrame()
                                    for tr_val, matrices in trs.items():
                                        if not str(tr_val).isdigit(): continue
                                        df_hiet_tr = matrices.get("df_hiet") if isinstance(matrices, dict) else None
                                        if df_hiet_tr is not None:
                                            df_cons = pd.concat([df_cons, df_hiet_tr.add_suffix(f'_Tr{tr_val}')], axis=1)
                                    df_hiet_cons = df_cons

                                fila_actual = 0
                                if df_hiet_cons is not None and not df_hiet_cons.empty:
                                    df_hiet_cons.to_excel(writer, sheet_name=nombre_hoja_seguro, startrow=fila_actual + 1)
                                    ws = writer.sheets[nombre_hoja_seguro]
                                    ws.write_string(fila_actual, 0, "1. HIETOGRAMA DE DISEÑO - BLOQUES ALTERNOS (mm)")
                                    fila_actual += len(df_hiet_cons) + 4
                                    
                                if df_i_cons is not None and not df_i_cons.empty:
                                    df_i_cons.to_excel(writer, sheet_name=nombre_hoja_seguro, startrow=fila_actual + 1)
                                    ws = writer.sheets[nombre_hoja_seguro]
                                    ws.write_string(fila_actual, 0, "2. INTENSIDADES DE PRECIPITACIÓN (mm/h)")
                                    fila_actual += len(df_i_cons) + 4

                                if df_h_cons is not None and not df_h_cons.empty:
                                    df_h_cons.to_excel(writer, sheet_name=nombre_hoja_seguro, startrow=fila_actual + 1)
                                    ws = writer.sheets[nombre_hoja_seguro]
                                    ws.write_string(fila_actual, 0, "3. LÁMINA DE PRECIPITACIÓN ACUMULADA (mm)")
                        
                        page.open(ft.SnackBar(content=ft.Text(f"✅ Libro Maestro guardado en: {ruta_salida}"), bgcolor="green"))
                    except Exception as ex:
                        traceback.print_exc()
                        page.open(ft.SnackBar(content=ft.Text(f"❌ Error al exportar: {ex}"), bgcolor="red"))

                def exportar_libro_maestro(e):
                    if not dict_maestro: return
                    dlg_guardar = ft.FilePicker(
                        on_result=lambda ev: guardar_excel_callback(ev, dict_maestro)
                    )
                    page.overlay.append(dlg_guardar)
                    page.update()
                    dlg_guardar.save_file(
                        dialog_title="Guardar Libro Maestro de Diseño",
                        file_name="Proyecto_Multicuenca_ARF.xlsx",
                        allowed_extensions=["xlsx"]
                    )

                btn_exportar_maestro = ft.ElevatedButton(
                    text="Exportar Libro Excel (.xlsx)", 
                    icon=ft.Icons.FILE_DOWNLOAD, 
                    color="white", bgcolor="#0055ff",
                    on_click=exportar_libro_maestro
                )

                # 1. Contenedores vacíos que actuarán como 'Lienzos' dinámicos
                lienzo_graficas = ft.Column(scroll=ft.ScrollMode.ADAPTIVE, expand=True)
                lienzo_tablas = ft.Column(scroll=ft.ScrollMode.ADAPTIVE, expand=True)
                
                # 2. Función Observer: Se dispara al cambiar el Dropdown
                def on_cuenca_seleccionada(e):
                    nombre_cuenca = e.control.value
                    datos_cuenca = dict_maestro.get(nombre_cuenca, {})
                    
                    # Limpiamos los lienzos
                    lienzo_graficas.controls.clear()
                    lienzo_tablas.controls.clear()
                    
                    # Extraemos el primer Periodo de Retorno disponible para visualización rápida
                    tr_keys = [k for k in datos_cuenca.keys() if str(k).isdigit()]
                    primer_tr = sorted(tr_keys, key=lambda x: int(x))[0] if tr_keys else (list(datos_cuenca.keys())[0] if datos_cuenca else None)
                    
                    if primer_tr:
                        data_tr = datos_cuenca[primer_tr] if isinstance(datos_cuenca.get(primer_tr), dict) else {}
                        df_i_sub = datos_cuenca.get("df_i") if datos_cuenca.get("df_i") is not None else data_tr.get("df_i")
                        df_h_sub = datos_cuenca.get("df_h") if datos_cuenca.get("df_h") is not None else data_tr.get("df_h")
                        df_hiet_sub = datos_cuenca.get("df_hiet") if datos_cuenca.get("df_hiet") is not None else data_tr.get("df_hiet")
                        
                        lienzo_tablas.controls.append(ft.Text("1. INTENSIDADES DE PRECIPITACIÓN (I-D-Tr)", color="#00ff41", weight="bold", size=14))
                        lienzo_tablas.controls.append(_df_to_flet_table(df_i_sub, "Duración (min)"))
                        lienzo_tablas.controls.append(ft.Divider(color="#222222", height=15))
                        
                        lienzo_tablas.controls.append(ft.Text("2. LÁMINA DE PRECIPITACIÓN ACUMULADA (A-D-Tr)", color="#00ff41", weight="bold", size=14))
                        lienzo_tablas.controls.append(_df_to_flet_table(df_h_sub, "Duración (min)"))
                        lienzo_tablas.controls.append(ft.Divider(color="#222222", height=15))
                        
                        lienzo_tablas.controls.append(ft.Text("3. HIETOGRAMA DE DISEÑO (BLOQUES ALTERNOS)", color="#00ff41", weight="bold", size=14))
                        lienzo_tablas.controls.append(_df_to_flet_table(df_hiet_sub, "Tiempo (min)"))
                        
                        lienzo_graficas.controls.append(
                            ft.Text(f"Visualizando: {nombre_cuenca} | Tr: {primer_tr} años", color="#00ff41", weight="bold", size=18)
                        )
                        
                        if tr_keys:
                            cols_p = [ft.DataColumn(ft.Text("Periodo Retorno (Tr)", color="#00ff41")), ft.DataColumn(ft.Text("Lluvia Neta Areal (mm)", color="white"))]
                            rows_p = []
                            for t_val in sorted(tr_keys, key=lambda x: int(x)):
                                p_neta = datos_cuenca[t_val].get("P_Neta", "---") if isinstance(datos_cuenca.get(t_val), dict) else "---"
                                rows_p.append(ft.DataRow(cells=[ft.DataCell(ft.Text(f"{t_val} años", weight="bold")), ft.DataCell(ft.Text(f"{p_neta} mm", color="#ffdd00", weight="bold"))]))
                            
                            lienzo_graficas.controls.append(ft.Container(
                                content=ft.Column([
                                    ft.Text("Resumen Espectro de Diseño Areal (MAPP * ARF):", color="grey", size=12, weight="bold"),
                                    ft.DataTable(columns=cols_p, rows=rows_p, heading_row_color="#1a1a1a", border=ft.border.all(1, "#333333"))
                                ]),
                                bgcolor="#111111", padding=15, border_radius=8, border=ft.border.all(1, "#222222"), margin=ft.margin.only(top=10)
                            ))
                    
                    page.update()

                # 3. Construcción del Menú Desplegable
                nombres_cuencas = list(dict_maestro.keys())
                dd_selector_cuenca = ft.Dropdown(
                    label="Seleccione Subcuenca a Auditar",
                    options=[ft.dropdown.Option(nombre) for nombre in nombres_cuencas],
                    value=nombres_cuencas[0] if nombres_cuencas else "",
                    width=400,
                    border_color="#00ff41",
                    on_change=on_cuenca_seleccionada
                )

                # 4. Renderizado inicial forzado
                if nombres_cuencas:
                    on_cuenca_seleccionada(type('Event', (), {'control': dd_selector_cuenca})())
                
                # 5. Ensamblaje Final en el Contenedor Principal de la Pestaña
                contenedor_resultados_arf.content = ft.Column([
                    ft.Row([dd_selector_cuenca, btn_exportar_maestro], alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
                    ft.Divider(color="#333333"),
                    ft.Row([
                        ft.Container(content=lienzo_tablas, expand=3),
                        ft.Container(content=lienzo_graficas, expand=2)
                    ], expand=True)
                ])
                led_f6.color = "#00ff41"
            else:
                contenedor_resultados_arf.content = listview_regional_default
                if b64_i:
                    img_plot_intensidad.src_base64 = b64_i
                    img_plot_intensidad.visible = True
                if b64_h:
                    img_plot_altura.src_base64 = b64_h
                    img_plot_altura.visible = True
                if b64_hiet:
                    img_plot_hietograma.src_base64 = b64_hiet
                    img_plot_hietograma.visible = True
                
                contenedor_tabla_i.content = _df_to_flet_table(df_i, "Duración (min)")
                contenedor_tabla_h.content = _df_to_flet_table(df_h, "Duración (min)")
                contenedor_tabla_hiet.content = _df_to_flet_table(df_hiet, "Tiempo (min)")
                btn_exportar_regional.disabled = False
            
            page.update()
        except Exception as e:
            print(f"❌ Error hidratando Pestaña 8: {e}")

    # =========================================================================
    # PESTAÑA 7: TORMENTA DE DISEÑO (ÁLGEBRA DE MAPAS Y HIETOGRAMAS)
    # =========================================================================
    def build_tab_diseno(on_complete_callback):
        from core.arf_metodo_diseno import MotorDiseno
        import sqlite3
        import pandas as pd
        
        opciones_fra = [
            "UNAM (Envolvente de Diseño)", "UNAM (Top 1)", "UNAM (Top 2)", "UNAM (Top 3)",
            "UNAM (Top 4)", "UNAM (Top 5)", "UNAM (Top 6)",
            "Frec. Homogéneo", "Frec. Heterogéneo", "Empírico (USWB)", "Empírico (Témez)"
        ]
        
        dd_metodo = ft.Dropdown(label="Matriz FRA a Aplicar", options=[ft.dropdown.Option(m) for m in opciones_fra], value="UNAM (Envolvente de Diseño)", color="#00ff41", border_color="#333333", expand=2)
        txt_dur = ft.TextField(label="Duración (h)", value="6", color="#00ff41", border_color="#333333", expand=1)
        txt_dt = ft.TextField(label="Δt (min)", value="15", color="#00ff41", border_color="#333333", expand=1)

        def on_click_generar_hietograma(e):
            try:
                area_cuenca = page.session.get("area_cuenca_km2")
                ruta_export = page.session.get("imput_output_folder")
                fuente_arf = page.session.get("arf_metodo_seleccionado") or "empirico"
                payload_arf = page.session.get("arf_payload_seleccionado") or {}

                if not area_cuenca:
                    page.open(ft.SnackBar(content=ft.Text("Aduana Espacial: Área de cuenca no definida en la sesión.", color="white"), bgcolor="#cc0000"))
                    return

                parametros_diseno = {
                    "area_km2": float(area_cuenca),
                    "dt_min": int(txt_dt.value) if txt_dt.value.isdigit() else 5,
                    "duracion_total": int(float(txt_dur.value) * 60) if txt_dur.value else 360,
                    "periodos_retorno": [2, 10, 50, 100, 500],
                    "fuente_arf": fuente_arf,
                    "payload_arf": payload_arf,
                    "ruta_export": ruta_export
                }

                page.open(ft.SnackBar(content=ft.Text("Iniciando cálculo topológico y generación de Hietograma...", color="black"), bgcolor="#00ff41"))
                MotorDiseno.generar_y_exportar_tormenta_asincrono(page, parametros_diseno)
                
            except Exception as ex:
                traceback.print_exc()
                page.open(ft.SnackBar(content=ft.Text(f"Error de UI: {str(ex)}", color="white"), bgcolor="#cc0000"))

        btn_generar_hietograma = ft.ElevatedButton(
            text="Generar Hietograma (Bloques Alternos)",
            icon=ft.Icons.THUNDERSTORM,
            bgcolor="#1c75fa",
            color="white",
            on_click=on_click_generar_hietograma
        )

        # ---------------------------------------------------------
        # BLINDAJE PARA LA PESTAÑA 7 (TORMENTA DISEÑO)
        # ---------------------------------------------------------
        def on_click_ejecutar_diseno(e):
            try:
                # 1. Extracción EAFP (Zero-Trust) desde el botón (NO durante la carga de la Pestaña)
                area_cuenca = page.session.get("area_cuenca_km2")
                cuenca_wkt = page.session.get("cuenca_wkt") or page.session.get("cuenca_geom_wkt")
                df_maestro = page.session.get("arf_df_maestro")
                df_coords = page.session.get("arf_df_coords")
                df_dictamen = page.session.get("arf_dictamen_df")

                # 2. Validación defensiva estricta
                if not area_cuenca or df_maestro is None or df_coords is None:
                    page.open(ft.SnackBar(content=ft.Text("Aduana Espacial: Faltan datos espaciales. Verifique las fases anteriores.", color="white"), bgcolor="#cc0000"))
                    return

                # Si el dictamen existe, creamos el diccionario de FRAs para la grilla. Si no, fallback seguro.
                dict_fra = {}
                if df_dictamen is not None and not df_dictamen.empty and 'Tr' in df_dictamen.columns and 'ENSEMBLE (Consenso)' in df_dictamen.columns:
                    dict_fra = dict(zip(df_dictamen['Tr'], df_dictamen['ENSEMBLE (Consenso)']))
                else:
                    page.open(ft.SnackBar(content=ft.Text("⚠️ Advertencia: No hay Dictamen Final. Se usará FRA = 1.0 (Sin reducción)", color="white"), bgcolor="#e6a800"))

                # Notificamos inicio
                page.open(ft.SnackBar(content=ft.Text("🚀 Inicializando Motor HPC de Diseño...", color="black"), bgcolor="#00ff41"))

                # 3. Función aislada para el hilo paralelo que protege a Flet
                def _procesar_diseno():
                    from core.arf_metodo_diseno import MotorDiseno
                    
                    # Ejecutar generación de matrices
                    res = MotorDiseno.generar_matrices_multitr(df_maestro, df_coords, cuenca_wkt, dict_fra)
                    
                    if res.get("exito"):
                        page.session.set("arf_fase8_mapas", res.get("mapas", {}))
                        page.run_thread(lambda: page.open(ft.SnackBar(content=ft.Text("✅ Módulo 7: Tormentas de Diseño renderizadas con éxito.", color="white"), bgcolor="#00ff41")))
                    else:
                        page.run_thread(lambda: page.open(ft.SnackBar(content=ft.Text(f"❌ Error en Motor 7: {res.get('error')}", color="white"), bgcolor="#cc0000")))

                threading.Thread(target=_procesar_diseno, daemon=True).start()

            except Exception as ex:
                traceback.print_exc()
                page.open(ft.SnackBar(content=ft.Text(f"Error UI Pestaña 7: {str(ex)}", color="white"), bgcolor="#cc0000"))

        btn_generar_matrices_grilladas = ft.ElevatedButton(
            text="Proyectar Matrices Grilladas (Fase 8)",
            icon=ft.Icons.MAP,
            bgcolor="#00ff41",
            color="black",
            on_click=on_click_ejecutar_diseno
        )

        def on_csv_export_result(e: ft.FilePickerResultEvent):
            if e.path:
                res = page.session.get("fase7_resultados")
                if res and 'tabla_maestra' in res:
                    try:
                        import pandas as pd
                        df = pd.DataFrame(res['tabla_maestra'])
                        df.to_csv(e.path, index=False)
                        page.snack_bar = ft.SnackBar(ft.Text(f"✅ Tabla exportada correctamente a CSV."), bgcolor="#00ff41", open=True)
                    except Exception as err:
                        page.snack_bar = ft.SnackBar(ft.Text(f"❌ Error al exportar: {str(err)}"), bgcolor="#cc0000", open=True)
                    page.update()

        file_picker_export_csv = ft.FilePicker(on_result=on_csv_export_result)
        page.overlay.append(file_picker_export_csv)

        dd_visor_tr = ft.Dropdown(
            label="Inspeccionar Periodo de Retorno",
            options=[ft.dropdown.Option(str(t), f"Tr = {t} años") for t in [2, 5, 10, 20, 50, 100, 500, 1000, 10000]],
            value="100", color="#ffdd00", border_color="#333333", expand=1
        )

        lbl_area, lbl_fra, lbl_lluvia, lbl_vol = ft.Text("---", size=24, weight="bold", color="#00ff41"), ft.Text("---", size=24, weight="bold", color="#ffdd00"), ft.Text("---", size=24, weight="bold", color="#1c75fa"), ft.Text("---", size=24, weight="bold", color="#ff0044")

        img_raw, img_arf, img_neta_global, img_neta_clip, img_hiet = ft.Image(src_base64="", fit=ft.ImageFit.CONTAIN, expand=True), ft.Image(src_base64="", fit=ft.ImageFit.CONTAIN, expand=True), ft.Image(src_base64="", fit=ft.ImageFit.CONTAIN, expand=True), ft.Image(src_base64="", fit=ft.ImageFit.CONTAIN, expand=True), ft.Image(src_base64="", fit=ft.ImageFit.CONTAIN, expand=True)

        dt_hiet = ft.DataTable(
            columns=[
                ft.DataColumn(ft.Text("Tiempo (min)", color="#00ff41")), ft.DataColumn(ft.Text("Tr 2")), ft.DataColumn(ft.Text("Tr 5")),
                ft.DataColumn(ft.Text("Tr 10")), ft.DataColumn(ft.Text("Tr 20")), ft.DataColumn(ft.Text("Tr 50")),
                ft.DataColumn(ft.Text("Tr 100", color="#ffdd00")), ft.DataColumn(ft.Text("Tr 500")), ft.DataColumn(ft.Text("Tr 1000")),
                ft.DataColumn(ft.Text("Tr 10000", color="#ff0044"))
            ], rows=[], border=ft.border.all(1, "#333333"), heading_row_color="#1a1a1a"
        )
        
        def _kpi_diseno(titulo, texto_obj, color_borde, icon): return ft.Container(content=ft.Column([ft.Row([ft.Icon(icon, color=color_borde, size=16), ft.Text(titulo, color="grey", size=11, weight="bold")]), texto_obj], alignment=ft.MainAxisAlignment.CENTER, horizontal_alignment=ft.CrossAxisAlignment.CENTER), bgcolor="#111111", border=ft.border.all(1, color_borde), border_radius=10, padding=15, col={"md": 6, "lg": 3})
        def _card_grafico(titulo, subtitulo, imagen, icono, color, col_size={"md": 12, "lg": 6}): return ft.Container(content=ft.Column([ft.Row([ft.Icon(icono, color=color), ft.Column([ft.Text(titulo, color="white", weight="bold"), ft.Text(subtitulo, color="grey", size=10)], spacing=0)]), ft.Container(content=ft.InteractiveViewer(content=imagen, max_scale=5), height=350, alignment=ft.alignment.center)]), bgcolor="#111111", border=ft.border.all(1, color), border_radius=10, padding=15, col=col_size)

        def actualizar_visor(e=None):
            res = page.session.get("fase7_resultados")
            if not res: return
            data_tr = res["resultados_tr"].get(dd_visor_tr.value)
            if data_tr:
                lbl_area.value = f"{res['area_km2']} km²"
                lbl_fra.value = f"{data_tr['fra_final']}"
                lbl_lluvia.value = f"{data_tr['p_point']} ➔ {data_tr['p_areal']} mm"
                lbl_vol.value = f"{data_tr['vol_hm3']} Hm³"
                img_raw.src_base64 = data_tr['b64_raw']
                img_arf.src_base64 = data_tr['b64_arf']
                img_neta_global.src_base64 = data_tr['b64_neta_global']
                img_neta_clip.src_base64 = data_tr['b64_neta_clip']
                img_hiet.src_base64 = data_tr['b64_hiet']
                page.update()

        dd_visor_tr.on_change = actualizar_visor

        def ejecutar_diseno():
            ruta_hds = _resolver_ruta_bd(page.session.get("ruta_bd_activa"))
            cuenca_wkt = page.session.get("cuenca_geom_wkt")
            
            unam_tabla = page.session.get("unam_tabla_masiva")
            if not unam_tabla and page.session.get("arf_fase6_res"):
                unam_tabla = page.session.get("arf_fase6_res").get("tabla_masiva")
            
            if not ruta_hds or not cuenca_wkt:
                page.snack_bar = ft.SnackBar(ft.Text("⚠️ Complete las fases anteriores y cargue Cuenca primero."), bgcolor="#cc0000", open=True); page.update()
                if on_complete_callback: on_complete_callback(False)
                return

            if "UNAM" in dd_metodo.value and not unam_tabla:
                page.snack_bar = ft.SnackBar(ft.Text("⚠️ Ejecute la Fase 6 (UNAM) antes de usar un método de Tormenta Centrada."), bgcolor="#cc0000", open=True); page.update()
                if on_complete_callback: on_complete_callback(False)
                return

            try: dur_val, dt_val = float(txt_dur.value), int(txt_dt.value)
            except:
                if on_complete_callback: on_complete_callback(False)
                return

            def _task():
                try:
                    conn = sqlite3.connect(ruta_hds)
                    df_maestro = pd.read_sql_query("SELECT * FROM arf_matriz_maestra", conn, parse_dates=["Fecha"], index_col="Fecha")
                    conn.close()
                except Exception as db_err:
                    import traceback; traceback.print_exc()
                    page.run_thread(lambda: setattr(page, "snack_bar", ft.SnackBar(ft.Text(f"❌ Error leyendo matriz LMA: {str(db_err)}"), bgcolor="#cc0000", open=True)))
                    if on_complete_callback: page.run_thread(lambda: on_complete_callback(False))
                    page.run_thread(page.update)
                    return

                claves = [col for col in df_maestro.columns if col != 'LMA_Base']
                
                estaciones_dict = page.session.get("imput_station_files") or {}
                df_indice = page.session.get("indice_bd_local")
                coords_dict = {}
                if estaciones_dict:
                    for k, v in estaciones_dict.items():
                        coords_dict[str(k).upper()] = {"LATITUD": v.get("lat", 0), "LONGITUD": v.get("lon", 0)}
                elif df_indice is not None and not df_indice.empty:
                    for _, row in df_indice.iterrows():
                        coords_dict[str(row.get('clave', '')).upper()] = {"LATITUD": row.get('lat', 0), "LONGITUD": row.get('lon', 0)}
                
                datos_coords = []
                for c in claves:
                    if c in coords_dict:
                        datos_coords.append({"clave_estacion": c, "LATITUD": coords_dict[c]["LATITUD"], "LONGITUD": coords_dict[c]["LONGITUD"]})
                df_coords = pd.DataFrame(datos_coords).set_index('clave_estacion') if datos_coords else pd.DataFrame()

                res = MotorDiseno.generar_tormenta_masiva(df_maestro, df_coords, cuenca_wkt, dur_val, dt_val, dd_metodo.value, unam_tabla, page=page)
                
                if res["exito"]:
                    def _success():
                        res_save = res.copy()
                        res_save["df_intensidades_arf"] = None
                        res_save["df_alturas_arf"] = None
                        res_save["df_hietogramas_arf"] = None
                        page.session.set("fase7_resultados", res_save)
                        if res.get("df_intensidades_arf") is not None:
                            page.session.set("arf_df_intensidad_maestra", res["df_intensidades_arf"])
                            page.session.set("arf_df_altura_maestra", res["df_alturas_arf"])
                            if res.get("df_hietogramas_arf") is not None:
                                page.session.set("arf_df_hietograma_maestro", res["df_hietogramas_arf"])

                        # Persistencia formal en base de datos del proyecto (.hds / .gpkg)
                        if ruta_hds and os.path.exists(ruta_hds):
                            try:
                                import sqlite3, json
                                from infrastructure.project_manager import NumpyEncoder
                                conn_db = sqlite3.connect(ruta_hds)
                                if res.get("df_intensidades_arf") is not None:
                                    res["df_intensidades_arf"].to_sql("arf_df_intensidad_maestra", conn_db, if_exists="replace", index=True, index_label="Duracion_min")
                                if res.get("df_alturas_arf") is not None:
                                    res["df_alturas_arf"].to_sql("arf_df_altura_maestra", conn_db, if_exists="replace", index=True, index_label="Duracion_min")
                                if res.get("df_hietogramas_arf") is not None:
                                    res["df_hietogramas_arf"].to_sql("arf_df_hietograma_maestro", conn_db, if_exists="replace", index=True, index_label="Tiempo_min")
                                df_meta = pd.DataFrame([{"key": "fase7_json", "value": json.dumps(res_save, cls=NumpyEncoder)}])
                                df_meta.to_sql("arf_fase7_metadata", conn_db, if_exists="replace", index=False)
                                conn_db.close()
                                print("✅ [TLÁLOC] Matrices regionales guardadas exitosamente en la BD del proyecto (.hds).")
                            except Exception as ex_db:
                                print(f"⚠️ Aviso: Error al guardar matrices de Fase 7 en SQLite: {ex_db}")
                        
                        # Poblar la Super-Tabla
                        dt_hiet.rows.clear()
                        for fila in res['tabla_maestra']:
                            dt_hiet.rows.append(ft.DataRow(cells=[
                                ft.DataCell(ft.Text(f"{fila['t']}", color="white", weight="bold")), 
                                ft.DataCell(ft.Text(f"{fila.get('tr_2', '')}")), ft.DataCell(ft.Text(f"{fila.get('tr_5', '')}")),
                                ft.DataCell(ft.Text(f"{fila.get('tr_10', '')}")), ft.DataCell(ft.Text(f"{fila.get('tr_20', '')}")),
                                ft.DataCell(ft.Text(f"{fila.get('tr_50', '')}")), ft.DataCell(ft.Text(f"{fila.get('tr_100', '')}", color="#ffdd00")),
                                ft.DataCell(ft.Text(f"{fila.get('tr_500', '')}")), ft.DataCell(ft.Text(f"{fila.get('tr_1000', '')}")),
                                ft.DataCell(ft.Text(f"{fila.get('tr_10000', '')}", color="#ff0044", weight="bold"))
                            ]))
                        
                        actualizar_visor()
                        contenedor_resultados.visible = True
                        if page.session.get("arf_multicuenca_maestro") or (res.get("df_intensidades_arf") is not None and res.get("df_alturas_arf") is not None):
                            poblar_pestaña_8_regionales(
                                df_i=res.get("df_intensidades_arf"), df_h=res.get("df_alturas_arf"), df_hiet=res.get("df_hietogramas_arf"),
                                b64_i=res.get("arf_b64_curva_i", ""), b64_h=res.get("arf_b64_curva_h", ""), b64_hiet=res.get("arf_b64_curva_hiet", "")
                            )
                        page.snack_bar = ft.SnackBar(ft.Text("✅ Procesamiento Masivo Completado."), bgcolor="#00ff41", open=True)
                        page.update()
                        if on_complete_callback: on_complete_callback(True)
                    page.run_thread(_success)
                else:
                    def _fail():
                        page.snack_bar = ft.SnackBar(ft.Text(f"❌ Error: {res['error']}"), bgcolor="#cc0000", open=True)
                        page.update()
                        if on_complete_callback: on_complete_callback(False)
                    page.run_thread(_fail)

            import threading
            threading.Thread(target=_task, daemon=True).start()
        
        contenedor_resultados = ft.Container(
            content=ft.Column([
                ft.Row([ft.Text("VISOR DE ESPECTRO PROBABILÍSTICO:", color="grey", size=12), dd_visor_tr]),
                ft.ResponsiveRow([
                    _kpi_diseno("ÁREA CUENCA", lbl_area, "#00ff41", ft.Icons.LANDSCAPE),
                    _kpi_diseno("FRA MATRICIAL", lbl_fra, "#ffdd00", ft.Icons.PIE_CHART),
                    _kpi_diseno("P. PUNTUAL ➔ P. AREAL", lbl_lluvia, "#1c75fa", ft.Icons.WATER_DROP),
                    _kpi_diseno("VOLUMEN HÍDRICO (Hm³)", lbl_vol, "#ff0044", ft.Icons.WAVES),
                ]),
                
                ft.ResponsiveRow([
                    _card_grafico("1. Lluvia Puntual (P_point)", "Interpolación Espacial (RBF)", img_raw, ft.Icons.RADAR, "#1c75fa", col_size={"md": 12, "lg": 4}),
                    _card_grafico("2. Matriz Atenuación (FRA)", "El factor de reducción espacial seleccionado", img_arf, ft.Icons.FILTER_TILT_SHIFT, "#ffaa00", col_size={"md": 12, "lg": 4}),
                    _card_grafico("3. Lluvia Neta (P_areal)", "P_point * FRA recortado a nivel regional", img_neta_global, ft.Icons.PUBLIC, "#ff00ff", col_size={"md": 12, "lg": 4}),
                ]),

                ft.ResponsiveRow([
                    _card_grafico("4. Lluvia Areal Recortada", "Zoom a Cuenca - Volumen Físico", img_neta_clip, ft.Icons.CROP, "#ff0044", col_size={"md": 12, "lg": 6}),
                    _card_grafico("5. Hietograma de Proyecto", "Desagregación en Bloques Alternos", img_hiet, ft.Icons.BAR_CHART, "#00ff41", col_size={"md": 12, "lg": 6}),
                ]),
                
                ft.Container(
                    content=ft.Column([
                        ft.Row([
                            ft.Row([ft.Icon(ft.Icons.TABLE_ROWS, color="white"), ft.Text("Matriz de Tormentas Multi-Frecuencia (Input HEC-HMS Time-Series)", weight="bold", color="white")]),
                            ft.ElevatedButton("Exportar a CSV", icon=ft.Icons.DOWNLOAD, bgcolor="#00ff41", color="black", on_click=lambda _: file_picker_export_csv.save_file(file_name="Matriz_Tormentas_Diseno.csv", allowed_extensions=["csv"]))
                        ], alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
                        ft.Container(content=ft.Column([dt_hiet], scroll=ft.ScrollMode.ADAPTIVE), height=450)
                    ]), bgcolor="#111111", border=ft.border.all(1, "#333333"), border_radius=10, padding=15, margin=ft.margin.only(top=10)
                )
            ]), visible=False
        )

        # HYDRATION LOGIC PARA PESTAÑA 7
        res_guardado = page.session.get("fase7_resultados")
        if res_guardado:
            df_i_use = res_guardado.get("df_intensidades_arf") if isinstance(res_guardado.get("df_intensidades_arf"), (pd.DataFrame, pd.Series)) else page.session.get("arf_df_intensidad_maestra")
            df_h_use = res_guardado.get("df_alturas_arf") if isinstance(res_guardado.get("df_alturas_arf"), (pd.DataFrame, pd.Series)) else page.session.get("arf_df_altura_maestra")
            df_hiet_use = res_guardado.get("df_hietogramas_arf") if isinstance(res_guardado.get("df_hietogramas_arf"), (pd.DataFrame, pd.Series)) else page.session.get("arf_df_hietograma_maestro")
            if page.session.get("arf_multicuenca_maestro") or df_i_use is not None:
                if df_i_use is not None:
                    page.session.set("arf_df_intensidad_maestra", df_i_use)
                    page.session.set("arf_df_altura_maestra", df_h_use)
                    if df_hiet_use is not None:
                        page.session.set("arf_df_hietograma_maestro", df_hiet_use)
                poblar_pestaña_8_regionales(
                    df_i=df_i_use, df_h=df_h_use, df_hiet=df_hiet_use,
                    b64_i=res_guardado.get("arf_b64_curva_i", "") or (page.session.get("arf_b64_curva_i") or ""),
                    b64_h=res_guardado.get("arf_b64_curva_h", "") or (page.session.get("arf_b64_curva_h") or ""),
                    b64_hiet=res_guardado.get("arf_b64_curva_hiet", "") or (page.session.get("arf_b64_curva_hiet") or "")
                )
            dt_hiet.rows.clear()
            for fila in res_guardado['tabla_maestra']:
                dt_hiet.rows.append(ft.DataRow(cells=[
                    ft.DataCell(ft.Text(f"{fila['t']}", color="white", weight="bold")), 
                    ft.DataCell(ft.Text(f"{fila.get('tr_2', '')}")), ft.DataCell(ft.Text(f"{fila.get('tr_5', '')}")),
                    ft.DataCell(ft.Text(f"{fila.get('tr_10', '')}")), ft.DataCell(ft.Text(f"{fila.get('tr_20', '')}")),
                    ft.DataCell(ft.Text(f"{fila.get('tr_50', '')}")), ft.DataCell(ft.Text(f"{fila.get('tr_100', '')}", color="#ffdd00")),
                    ft.DataCell(ft.Text(f"{fila.get('tr_500', '')}")), ft.DataCell(ft.Text(f"{fila.get('tr_1000', '')}")),
                    ft.DataCell(ft.Text(f"{fila.get('tr_10000', '')}", color="#ff0044", weight="bold"))
                ]))
            actualizar_visor()
            contenedor_resultados.visible = True

        return ft.Container(
            content=ft.Column([
                ft.Row([
                    ft.Column([
                        ft.Text("Configuración del Entorno Físico (Variables Estáticas)", weight="bold", size=14, color="#1c75fa"),
                        ft.Text("Estas variables se aplicarán para generar TODA la serie de Periodos de Retorno.", color="grey", size=12)
                    ], expand=True),
                ], alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
                ft.Row([dd_metodo, txt_dur, txt_dt], alignment=ft.MainAxisAlignment.START),
                ft.Row([btn_generar_hietograma, btn_generar_matrices_grilladas], alignment=ft.MainAxisAlignment.START, spacing=15),
                ft.Divider(color="#333333"),
                contenedor_resultados
            ], scroll=ft.ScrollMode.ADAPTIVE), padding=20, expand=True
        ), ejecutar_diseno

    content_tab7, exec_tab7 = build_tab_diseno(on_f6_done)

    # --- CONSTRUCCIÓN DEL PANEL DE CONTROL ---
    def row_btn(btn, led): return ft.Row([led, btn], alignment=ft.MainAxisAlignment.START)

    panel_izquierdo = ft.Container(
        width=300, bgcolor="#0a0a0a", border=ft.border.only(right=ft.border.BorderSide(1, "#333333")), padding=20,
        content=ft.Column([
            ft.Text("ORQUESTADOR ARF", color="white", weight="bold", size=18),
            ft.Divider(color="#333333"),
            row_btn(btn_fase1, led_f1), row_btn(btn_fase2, led_f2),
            row_btn(btn_fase3, led_f3), row_btn(btn_fase4, led_f4),
            row_btn(btn_fase5, led_f5), row_btn(btn_fase6, led_f6),
            ft.Container(expand=True), # Spacer
            progreso_arf
        ])
    )

    tabs_principales = ft.Tabs(
        selected_index=0, animation_duration=300, indicator_color="#00ff41", label_color="#00ff41", unselected_label_color="grey",
        tabs=[
            ft.Tab(text="1. Continuidad", content=build_tab_calidad()),
            ft.Tab(text="2. Matriz LMA", content=bento_container),
            ft.Tab(text="3. Método Empírico", content=content_tab3),
            ft.Tab(text="4. Frec. (Homogéneo)", content=content_tab4),
            ft.Tab(text="5. Frec. (Heterogéneo)", content=content_tab5),
            ft.Tab(text="6. I.I. UNAM", content=content_tab6),
            ft.Tab(text="7. Tormenta Diseño", content=content_tab7),
            tab_curvas_regionales,
        ]
    )



    panel_derecho = ft.Container(expand=True, content=tabs_principales)

    # =========================================================================
    # MOTOR DE REHIDRATACIÓN GLOBAL (Panel Izquierdo y Pestañas Simples)
    # =========================================================================
    def _rehydrate_ui():
        # Fase 1: Matriz de Continuidad
        stats = page.session.get("arf_stats_continuidad")
        if stats:
            dt_continuidad.rows.clear()
            for s in stats:
                color_text = "#00ff41" if s.get("Integridad_%", 0) > 80 else ("orange" if s.get("Integridad_%", 0) > 50 else "#cc0000")
                dt_continuidad.rows.append(ft.DataRow(cells=[
                    ft.DataCell(ft.Text(s.get("Clave", ""), weight="bold")), ft.DataCell(ft.Text(f"{s.get('Inicio', '')} - {s.get('Fin', '')}", size=11)),
                    ft.DataCell(ft.Text(str(s.get("Dias_Efectivos", 0)))), ft.DataCell(ft.Text(str(s.get("Max_Gap_Dias", 0)), color="red" if s.get("Max_Gap_Dias", 0) > 30 else "white")),
                    ft.DataCell(ft.Text(f"{s.get('Integridad_%', 0)}%", color=color_text))
                ]))
            led_f1.color = "#00ff41"
            btn_fase2.disabled = False
            
        # Fase 2: LMA
        fase2_data = page.session.get("arf_fase2_bento_data")
        if fase2_data:
            bento_container.content = renderizar_bento_box(fase2_data)
            bento_container.alignment = None
            led_f2.color = "#00ff41"
            btn_fase3.disabled = False
            btn_fase4.disabled = False # Empírico es instantáneo, desbloqueamos 4 preventivamente
            
        # Desbloqueo Secuencial Inteligente (si tienen guardados resultados)
        if page.session.get("arf_fase3_res"):
            led_f3.color = "#00ff41"

        if page.session.get("arf_fase4_res_homogeneo"):
            led_f4.color = "#00ff41"
            btn_fase5.disabled = False
            
        if page.session.get("arf_fase4_res_heterogeneo"):
            led_f5.color = "#00ff41"
            btn_fase6.disabled = False
            
        if page.session.get("arf_fase6_res"):
            led_f6.color = "#00ff41"
            
        if page.session.get("arf_multicuenca_maestro") or (page.session.get("arf_df_intensidad_maestra") is not None and page.session.get("arf_df_altura_maestra") is not None):
            poblar_pestaña_8_regionales(
                df_i=page.session.get("arf_df_intensidad_maestra"),
                df_h=page.session.get("arf_df_altura_maestra"),
                df_hiet=page.session.get("arf_df_hietograma_maestro"),
                b64_i=(page.session.get("arf_b64_curva_i") or ""),
                b64_h=(page.session.get("arf_b64_curva_h") or ""),
                b64_hiet=(page.session.get("arf_b64_curva_hiet") or "")
            )

    _rehydrate_ui()
    
    return ft.Row([panel_izquierdo, panel_derecho], expand=True, spacing=0)