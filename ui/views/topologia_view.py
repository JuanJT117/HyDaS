import flet as ft
import traceback
import os
import threading
from infrastructure.project_manager import project_manager_instance as pm
from core.analisis_espacial import MotorEspacial
from core.modelos_espaciales import SubcuencaSchema, UsoSueloSchema, CaucesSchema
from core.renderizador_espacial import VisorEspacial
from core.fisica_cuenca import MotorFisicoRichDEM

# [NUEVO] Constante del Manual Técnico en Markdown
MANUAL_MARKDOWN = """
# 📖 Manual Técnico de Atributos Espaciales (HyDaS)

**Regla de Oro:** NUNCA cambie el nombre de las columnas ni altere el tipo de dato. Trabaje en proyección métrica (ej. UTM Zona 14N - EPSG:32614).

### 1. Capa de Subcuencas (Polígono)
| Columna | Tipo | Rango Esperado | Descripción |
| :--- | :--- | :--- | :--- |
| **ID_Cuenca** | Entero | `1` a `9999` | Identificador único de la subcuenca. |
| **Nombre** | Texto | Libre | Nombre descriptivo. |
| **Area_km2** | Decimal | `> 0.0` | Área en km². (HyDaS puede recalcularla). |
| **Perimetro_km**| Decimal | `> 0.0` | Perímetro en km. |
| **Pendient_pct**| Decimal | `0.01` a `150.0`| Pendiente media en porcentaje (%). |
| **Tc_minutos** | Decimal | `> 0.0` | Tiempo de Concentración en minutos. |

### 2. Capa de Uso de Suelo (Polígono)
| Columna | Tipo | Rango Esperado | Descripción |
| :--- | :--- | :--- | :--- |
| **ID_Poligon** | Entero | `1` a `99999` | ID único del parche. |
| **Grupo_Hidro**| Texto | `A`, `B`, `C`, o `D`| Grupo de Suelos SCS. |
| **Uso_Cobert** | Texto | Libre | Descripción (ej. Bosque, Urbano). |
| **Condicion** | Texto | `Pobre`, `Regular`, `Buena` | Condición hidrológica. |
| **CN_Asignad** | Decimal| `30.0` a `100.0` | Número de Curva (Curve Number). |

### 3. Capa de Red Hídrica (Línea)
| Columna | Tipo | Rango Esperado | Descripción |
| :--- | :--- | :--- | :--- |
| **ID_Cauce** | Entero | `1` a `99999` | ID único del tramo. |
| **Orden_Stra** | Entero | `1` a `7` | Orden de Strahler. |
| **Es_Princip** | Entero | `0` (No) o `1` (Sí)| Define el Cauce Principal. |
| **Longitud_m** | Decimal| `> 0.0` | Longitud en metros. |
| **Desnivel_m** | Decimal| `>= 0.0` | Diferencia de elevación en metros. |
| **Manning_n** | Decimal| `0.010` a `0.150` | Coeficiente de Rugosidad de Manning. |
"""

TRANSPARENT_PIXEL = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="

def build_topologia_view(page: ft.Page):
    
    # --- ESTADO LOCAL & REHIDRATACIÓN DESDE EL PROYECTO (FASE 2) ---
    capas_memoria = {"cuenca": None, "cauces": None, "suelo": None, "thiessen": None}
    
    # Rehidratar capas espaciales existentes desde el GeoPackage activo
    if pm.db_path and os.path.exists(pm.db_path):
        try:
            capas_existentes = pm.listar_capas_espaciales()
            if "shp_cuenca_limite" in capas_existentes:
                capas_memoria["cuenca"] = pm.cargar_capa_espacial("shp_cuenca_limite")
            if "shp_cauces" in capas_existentes:
                capas_memoria["cauces"] = pm.cargar_capa_espacial("shp_cauces")
            if "shp_uso_suelo" in capas_existentes:
                capas_memoria["suelo"] = pm.cargar_capa_espacial("shp_uso_suelo")
            if "shp_thiessen" in capas_existentes:
                capas_memoria["thiessen"] = pm.cargar_capa_espacial("shp_thiessen")
        except Exception as e:
            print(f"[WARN] No se pudieron rehidratar capas espaciales previas: {e}")
    
    # --- UI COMPONENTES ---
    img_visor = ft.Image(src_base64=TRANSPARENT_PIXEL, expand=True, fit=ft.ImageFit.CONTAIN)
    chk_cuenca = ft.Checkbox(label="Límite Cuenca", value=True, on_change=lambda e: renderizar_mapa())
    chk_cauces = ft.Checkbox(label="Red Hídrica", value=True, on_change=lambda e: renderizar_mapa())
    chk_suelo = ft.Checkbox(label="Uso de Suelo", value=True, on_change=lambda e: renderizar_mapa())
    chk_thiessen = ft.Checkbox(label="Polígonos Thiessen", value=True, on_change=lambda e: renderizar_mapa())
    
    txt_log = ft.Text("Listo.", color="#00ff41", size=12, font_family="Roboto Mono")
    loading_ring = ft.ProgressRing(color="#00f0ff", visible=False)

    def log(msg, error=False):
        txt_log.value = msg
        txt_log.color = "#cc0000" if error else "#00ff41"
        page.update()

    def renderizar_mapa():
        def _task():
            loading_ring.visible = True; page.update()
            kwargs = {
                'mostrar_cuenca': chk_cuenca.value,
                'mostrar_cauces': chk_cauces.value,
                'mostrar_suelo': chk_suelo.value,
                'mostrar_thiessen': chk_thiessen.value
            }
            b64 = VisorEspacial.renderizar_mapa_base64(capas_memoria, kwargs)
            if b64: img_visor.src_base64 = b64
            loading_ring.visible = False; page.update()
        threading.Thread(target=_task, daemon=True).start()

    # Si hay capas rehidratadas, renderizar de inmediato
    if any(capas_memoria.values()):
        renderizar_mapa()

    # --- LÓGICA: GENERAR PLANTILLAS INTERNAS (GeoPackage) ---
    def generar_plantillas_base(e):
        def _task():
            log("Generando Contratos OGC internos..."); loading_ring.visible = True; page.update()
            try:
                gdf_cuenca = MotorEspacial.generar_plantilla(SubcuencaSchema, "Polygon")
                gdf_suelo = MotorEspacial.generar_plantilla(UsoSueloSchema, "Polygon")
                gdf_cauces = MotorEspacial.generar_plantilla(CaucesSchema, "LineString")
                
                pm.guardar_capa_espacial("shp_cuenca_limite", gdf_cuenca)
                pm.guardar_capa_espacial("shp_uso_suelo", gdf_suelo)
                pm.guardar_capa_espacial("shp_cauces", gdf_cauces)
                
                log("✅ Plantillas guardadas internamente en el proyecto.")
                page.snack_bar = ft.SnackBar(ft.Text("✅ Plantillas guardadas en GeoPackage."), bgcolor="green", open=True)
            except Exception as ex:
                traceback.print_exc()
                log(f"Error: {str(ex)}", error=True)
            finally:
                loading_ring.visible = False; page.update()
        threading.Thread(target=_task, daemon=True).start()

    # =========================================================================
    # [PARCHE DEVSECOPS: EXPORTACIÓN EXTERNA]
    # Aislamiento de capas base para su edición segura en QGIS.
    # =========================================================================
    dir_picker_export = page.session.get("topologia_dir_picker_export")
    if not dir_picker_export:
        dir_picker_export = ft.FilePicker()
        page.session.set("topologia_dir_picker_export", dir_picker_export)
        page.overlay.append(dir_picker_export)

    def on_export_dir_picked(e: ft.FilePickerResultEvent):
        if not e.path: return 
        
        def _task():
            log("Forzando cabeceras OGC y exportando Shapefiles..."); loading_ring.visible = True; page.update()
            try:
                # Directriz 6 y 7: Uso del motor estricto de Fiona para evitar corrupción de geometría.
                MotorEspacial.exportar_plantilla_shp(SubcuencaSchema, "Polygon", os.path.join(e.path, "Plantilla_Cuenca.shp"))
                MotorEspacial.exportar_plantilla_shp(UsoSueloSchema, "Polygon", os.path.join(e.path, "Plantilla_Uso_Suelo.shp"))
                MotorEspacial.exportar_plantilla_shp(CaucesSchema, "LineString", os.path.join(e.path, "Plantilla_Cauces.shp"))
                
                log(f"✅ SHPs generados (Topología Estricta) en: {e.path}")
                page.snack_bar = ft.SnackBar(
                    ft.Text(f"📦 Shapefiles (Polígonos y Líneas) exportados con éxito.", color="#050505", weight="bold"), 
                    bgcolor="#00ff41", open=True
                )
            except Exception as ex:
                import traceback
                traceback.print_exc()
                log(f"Error de Exportación: {str(ex)}", error=True)
                page.snack_bar = ft.SnackBar(ft.Text(f"❌ Error: {str(ex)}", color="white"), bgcolor="#cc0000", open=True)
            finally:
                loading_ring.visible = False
                page.update()
                
                # Directriz 1: Recolección explícita de basura para proteger el Hilo de Flet
                import gc
                gc.collect()
                
        threading.Thread(target=_task, daemon=True).start()

    dir_picker_export.on_result = on_export_dir_picked
    # =========================================================================

    # Componentes Fase 3: DEM
    inp_umbral = ft.TextField(label="Umbral Acum. (px)", value="500", width=150, text_size=12, tooltip="A mayor número, menos afluentes pequeños.")

    def procesar_dem_ui(ruta_dem: str):
        def _task():
            log("Iniciando Motor Físico RichDEM (RAM pura)..."); loading_ring.visible = True; page.update()
            try:
                umbral = int(inp_umbral.value)
                
                print("DEBUG: Iniciando procesar_dem...")
                # Ejecutar Núcleo Físico (inyectamos el callback para la UI)
                resultados = MotorFisicoRichDEM.procesar_dem(ruta_dem, umbral, callback=log)
                
                print("DEBUG: procesar_dem finalizó con éxito. Actualizando memoria...")
                log("Guardando resultados en memoria...")
                # Memoria de sesión
                capas_memoria['cuenca'] = resultados['cuencas']
                capas_memoria['cauces'] = resultados['cauces']
                
                print("DEBUG: Iniciando pm.guardar_capa_espacial para cuenca...")
                log("Escribiendo al GeoPackage (Persistencia)...")
                # Guardado OGC Persistente
                pm.guardar_capa_espacial("shp_cuenca_limite", resultados['cuencas'])
                
                print("DEBUG: Iniciando pm.guardar_capa_espacial para cauces...")
                pm.guardar_capa_espacial("shp_cauces", resultados['cauces'])
                
                print("DEBUG: pm.guardar_capa_espacial terminó. Actualizando checkboxes...")
                chk_cuenca.value = True
                chk_cauces.value = True
                
                log(f"✅ DEM procesado en memoria. Geometrías extraídas exitosamente.")
                page.snack_bar = ft.SnackBar(ft.Text("✅ Física extraída a máxima velocidad en RAM."), bgcolor="green", open=True)
                
                print("DEBUG: Llamando a renderizar_mapa()...")
                renderizar_mapa()
                print("DEBUG: renderizar_mapa() lanzado (corre en hilo secundario).")
            except Exception as ex:
                traceback.print_exc()
                log(f"Error procesando DEM: {str(ex)}", error=True)
                page.snack_bar = ft.SnackBar(ft.Text(f"❌ Error DEM: {str(ex)}", color="white"), bgcolor="#cc0000", open=True)
            finally:
                loading_ring.visible = False; page.update()
        page.run_thread(_task)

    file_picker_dem = page.session.get("topologia_file_picker_dem")
    if not file_picker_dem:
        file_picker_dem = ft.FilePicker()
        page.session.set("topologia_file_picker_dem", file_picker_dem)
        page.overlay.append(file_picker_dem)
    file_picker_dem.on_result = lambda e: procesar_dem_ui(e.files[0].path) if e.files else None

    # --- LÓGICA: INGESTA DE SHP EDITADOS (Aduana) ---
    file_picker = page.session.get("topologia_file_picker")
    if not file_picker:
        file_picker = ft.FilePicker()
        page.session.set("topologia_file_picker", file_picker)
        page.overlay.append(file_picker)
    tipo_capa_actual = None

    def on_file_picked(e: ft.FilePickerResultEvent):
        nonlocal tipo_capa_actual
        if not e.files: return
        ruta = e.files[0].path
        
        def _task():
            log(f"Validando e ingestando {tipo_capa_actual}..."); loading_ring.visible = True; page.update()
            try:
                schema = SubcuencaSchema if tipo_capa_actual == "cuenca" else (
                         UsoSueloSchema if tipo_capa_actual == "suelo" else CaucesSchema)
                         
                gdf_validado = MotorEspacial.ingestar_capa_externa(ruta, schema)
                
                capas_memoria[tipo_capa_actual] = gdf_validado
                pm.guardar_capa_espacial(f"shp_{tipo_capa_actual}", gdf_validado)
                
                log(f"✅ Capa {tipo_capa_actual} importada y validada exitosamente.")
                renderizar_mapa()
            except Exception as ex:
                traceback.print_exc()
                log(f"Error de Ingesta: {str(ex)}", error=True)
                page.snack_bar = ft.SnackBar(ft.Text(f"❌ {str(ex)}", color="white", weight="bold"), bgcolor="#cc0000", open=True)
            finally:
                loading_ring.visible = False; page.update()
                
        threading.Thread(target=_task, daemon=True).start()

    file_picker.on_result = on_file_picked

    def solicitar_archivo(tipo: str):
        nonlocal tipo_capa_actual
        tipo_capa_actual = tipo
        file_picker.pick_files(allowed_extensions=["shp", "gpkg"])

    def on_cerrar_manual(e):
        # Al cerrar, devolvemos la ventana a su comportamiento normal
        page.window.always_on_top = False
        page.update()

    dlg_manual = ft.AlertDialog(
        title=ft.Row([ft.Icon(ft.Icons.LIBRARY_BOOKS, color="#00f0ff"), ft.Text("Manual de Atributos", color="#00f0ff")]),
        content=ft.Container(
            content=ft.Column([
                ft.Markdown(
                    MANUAL_MARKDOWN, 
                    selectable=True, 
                    extension_set=ft.MarkdownExtensionSet.GITHUB_WEB
                )
            ], scroll=ft.ScrollMode.AUTO),
            width=600,
            height=500, # Altura cómoda para leer sin abarcar toda la pantalla
            padding=10
        ),
        bgcolor="#111111", # Fondo oscuro para descanso visual
        shape=ft.RoundedRectangleBorder(radius=10),
        on_dismiss=on_cerrar_manual,
        actions=[
            ft.TextButton("Cerrar", on_click=lambda _: page.close(dlg_manual), style=ft.ButtonStyle(color="#cc0000"))
        ]
    )

    def abrir_manual_flotante():
        # Magia ergonómica: Forzamos la ventana de HidroSistem a quedarse flotando 
        # sobre todas las demás aplicaciones (como QGIS) mientras el manual esté abierto.
        page.window.always_on_top = True
        page.update()
        page.open(dlg_manual)

    # --- LAYOUT PRINCIPAL ---
    panel_izquierdo = ft.Container(
        width=320, padding=15, bgcolor="#111111", border=ft.border.all(1, "#333333"), border_radius=5,
        content=ft.Column([
            ft.Text("1. EXPORTACIÓN OGC", weight="bold", color="#00ff41"),
            ft.Text("Crea plantillas con las columnas obligatorias.", size=11, color="gray"),
            # Botón 1: Interno (Para usuarios avanzados)
            ft.ElevatedButton("Inyectar Capas al Proyecto (.hds)", icon=ft.Icons.DATA_SAVER_ON, on_click=generar_plantillas_base, style=ft.ButtonStyle(color="white")),
            # Botón 2: Externo (Modo Seguro - SHP sueltos)
            ft.ElevatedButton(
                text="Exportar a SHP (Carpeta Externa)", 
                icon=ft.Icons.FOLDER_SHARED, 
                on_click=lambda _: dir_picker_export.get_directory_path(dialog_title="Seleccione dónde guardar los Shapefiles"), 
                bgcolor="#333333", color="#00ff41"
            ),
            # =================================================================
            # [PARCHE DEVSECOPS] MANUAL FLOTANTE (ALWAYS ON TOP)
            # =================================================================
            ft.ElevatedButton(
                text="📖 Ver Manual de Atributos", 
                icon=ft.Icons.MENU_BOOK, 
                bgcolor="#111111", 
                color="#00f0ff", # Azul Cyan para diferenciarlo
                on_click=lambda _: abrir_manual_flotante()
            ),
            # =================================================================
            ft.Divider(color="#333333"),
            
            ft.Text("2. INGESTA Y VALIDACIÓN", weight="bold", color="#00ff41"),
            ft.Text("Cargue los archivos ya digitalizados en QGIS.", size=11, color="gray"),
            ft.ElevatedButton("Importar Cuenca", icon=ft.Icons.UPLOAD, on_click=lambda _: solicitar_archivo("cuenca")),
            ft.ElevatedButton("Importar Cauces", icon=ft.Icons.UPLOAD, on_click=lambda _: solicitar_archivo("cauces")),
            ft.ElevatedButton("Importar Uso de Suelo", icon=ft.Icons.UPLOAD, on_click=lambda _: solicitar_archivo("suelo")),
            ft.Divider(color="#333333"),
            
            ft.Text("3. GENERACIÓN INTERNA", weight="bold", color="#00ff41"),
            ft.ElevatedButton("Calcular Polígonos Thiessen", icon=ft.Icons.POLYLINE, disabled=True, tooltip="Requiere estaciones activas en Fase 1"),
            ft.Divider(color="#333333"),
            ft.Text("4. EXTRACCIÓN FÍSICA (DEM)", weight="bold", color="#00ff41"),
            ft.Text("Motor RichDEM (C++ en RAM).", size=11, color="gray"),
            inp_umbral,
            ft.ElevatedButton("Seleccionar DEM (.tif) y Procesar", icon=ft.Icons.TERRAIN, 
                              on_click=lambda _: file_picker_dem.pick_files(allowed_extensions=["tif", "TIF"]),
                              bgcolor="#333333", color="#00f0ff")
        ], scroll=ft.ScrollMode.AUTO)
    )

    panel_central = ft.Container(
        expand=True, padding=10, border=ft.border.all(1, "#333333"), border_radius=5,
        content=ft.Column([
            ft.Row([
                ft.Icon(ft.Icons.MAP, color="#00ff41"), ft.Text("VISOR ESPACIAL DE INTEGRACIÓN", weight="bold"),
                loading_ring
            ]),
            ft.Row([chk_cuenca, chk_cauces, chk_suelo, chk_thiessen], wrap=True),
            ft.Container(content=img_visor, expand=True, bgcolor="#ffffff", border_radius=5),
            txt_log
        ])
    )

    return ft.Row([panel_izquierdo, panel_central], expand=True, spacing=10)
