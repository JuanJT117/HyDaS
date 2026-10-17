##############################################################################################################
##############################################################################################################
#
#   HyDaS/
import logging

# Evitar doble inicialización del logger
if not logging.getLogger().handlers:
    logging.basicConfig(level=logging.DEBUG)

# Silenciar mensajes excesivamente verbosos de bibliotecas auxiliares
logging.getLogger("matplotlib").setLevel(logging.WARNING)
logging.getLogger("matplotlib.font_manager").setLevel(logging.WARNING)
logging.getLogger("flet").setLevel(logging.INFO)
logging.getLogger("flet_core").setLevel(logging.INFO)
logging.getLogger("PIL").setLevel(logging.WARNING)
##
#   ├── main.py                     # Punto de entrada estricto (App Shell)
#   ├── core/                       # BACKEND: Lógica pura, matemáticas y algoritmos
#   │   ├── __init__.py
#   │   ├── descarga_logic.py        # Motor de descarga masiva de datos de CONAGUA
#   │   ├── hidrologia_mx.py        # Constantes y diccionarios de dominio
#   │   ├── imputacion_logic.py     # Motor de interpolación y ML
#   │   ├── lluvias_logic.py        # (Fusión/Limpieza de Analisis.py, lluvias.py y analisis_cuenca.py)
#   │   └── gastos_logic.py         # (Renombrado de gastos.py)
#   ├── ui/                         # FRONTEND: Vistas y componentes Flet
#   │   ├── __init__.py
#   │   ├── components.py           # Reutilizables: MatrixButton, TerminalHeader, etc.
#   │   └── views/
#   │       ├── welcome_view.py     # NUEVA: Pantalla inicial (Nuevo/Abrir Proyecto) + Easter Egg
#   │       ├── descarga_view.py    # (Modulo 1)
#   │       ├── imputacion_view.py  # (Modulo 2)
#   │       ├── analisis_view.py    # (Modulo 3)
#   │       └── gastos_view.py      # (Modulo 4)
#   ├── infrastructure/             # PUERTOS Y ADAPTADORES: Persistencia de datos
#   │   ├── __init__.py    
#   │   └── project_manager.py      # NUEVO: Motor de base de datos comprimida (.hds via .xz)
#   └── assets/                     # Recursos estáticos
#       ├── cuencas.cpg
#       ├── cuencas.dbf
#       ├── cuencas.prj
#       ├── cuencas.qmd
#       ├── cuencas.shp
#       ├── cuencas.shx
#       ├── environment.yml  
#       ├── estados.cpg
#       ├── estados.dbf
#       ├── estados.prj
#       ├── estados.qmd
#       ├── estados.shp
#       ├── estados.shx
#       ├── tlaloc_egg.gif
#       ├── icon.ico
#       └── path19.jpg
#
##############################################################################################################
##############################################################################################################

import flet as ft
import os
import sys
import traceback

# --- FIX PARA PROJ EN ENTORNOS CONDA (Evita ERROR 1: PROJ: Cannot find proj.db) ---
_proj_win = os.path.join(sys.prefix, "Library", "share", "proj")
_proj_unix = os.path.join(sys.prefix, "share", "proj")
if os.path.exists(os.path.join(_proj_win, "proj.db")):
    os.environ["PROJ_LIB"] = _proj_win
    os.environ["PROJ_DATA"] = _proj_win
elif os.path.exists(os.path.join(_proj_unix, "proj.db")):
    os.environ["PROJ_LIB"] = _proj_unix
    os.environ["PROJ_DATA"] = _proj_unix

# --- IMPORTACIÓN DE INFRAESTRUCTURA Y COMPONENTES ---
from infrastructure.project_manager import project_manager_instance
from ui.components import COLOR_FONDO, COLOR_ACENTO, COLOR_SUPERFICIE, FUENTE_PRINCIPAL, add_opacity
import threading
import time
import gc
import traceback
from ui.views.welcome_view import build_welcome_view

# --- IMPORTACIÓN DE MÓDULOS (FRONTEND) ---
from ui.views.descarga_view import build_descarga_view
import ui.views.imputacion_view as imputacion_app
import ui.views.analisis_view as analisis_app
# --- NUEVA INYECCIÓN: Módulo ARF ---
from ui.views.arf_view import build_arf_view
import ui.views.gastos_view as gastos_app
from ui.views.climatologia_view import build_climatologia_view
from ui.views.topologia_view import build_topologia_view

def main(page: ft.Page):
    # --- CONFIGURACIÓN DE PÁGINA ---
    page.title = "HyDaS v11"
    page.theme_mode = ft.ThemeMode.DARK
    page.bgcolor = COLOR_FONDO
    page.window_width = 1200
    page.window_height = 1000
    page.fonts = {"Roboto Mono": "https://github.com/google/fonts/raw/main/apache/robotomono/RobotoMono%5Bwght%5D.ttf"}
    page.theme = ft.Theme(
        font_family=FUENTE_PRINCIPAL,
        scrollbar_theme=ft.ScrollbarTheme(
            track_visibility=True,
            thumb_visibility=True,
            thickness=14,
            radius=8,
            thumb_color=COLOR_ACENTO,
            track_color="#222222",
        )
    )

    # ==========================================
    # BLINDAJE CONTRA CIERRE FORZADO DEL OS (GUILLOTINA)
    # ==========================================
    page.window.prevent_close = True

    def _interceptor_cierre_os(e):
        if e.data == "close":
            # Si el sistema ya está guardando o procesando, IGNORAMOS el clic a la 'X' para evitar corrupción
            if is_navigating[0]:
                return 
            
            # Si hay un proyecto activo y la función de cierre seguro está enlazada, la invocamos
            funcion_cierre_seguro = page.session.get("trigger_exit_dialog")
            if funcion_cierre_seguro and page.session.get("current_project_path"):
                funcion_cierre_seguro(None)
            else:
                # Si estamos en la pantalla de bienvenida, cerramos la app limpiamente
                page.window.destroy()

    page.window.on_event = _interceptor_cierre_os

    # --- ESTADO DEL PROYECTO ---
    page.session.set("current_project_path", None)

    # --- CONTENEDOR PRINCIPAL (ROUTER) ---

    # ==========================================
    # --- SISTEMA GLOBAL DE PROTECCIÓN DE NAVEGACIÓN (ANTI-CRASH) ---
    # ==========================================
    # Variable bandera para evitar clicks dobles
    is_navigating = [False] 

    # Capa visual de carga (Cyberpunk)
    loading_overlay = ft.Container(
        content=ft.Column(
            [
                ft.ProgressRing(color=COLOR_ACENTO, stroke_width=5),
                ft.Text("CARGANDO MÓDULO...", color=COLOR_ACENTO, weight="bold", font_family=FUENTE_PRINCIPAL),
                ft.Text("Descomprimiendo estructuras de memoria", color="grey", size=11, font_family=FUENTE_PRINCIPAL)
            ],
            alignment=ft.MainAxisAlignment.CENTER,
            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
        ),
        alignment=ft.alignment.center,
        # POR QUÉ: Forzamos el uso de nuestro adaptador EAFP para inyectar el canal Alpha 
        # directamente en el string HEX, evadiendo la API inestable de colores nativa de Flet.
        bgcolor=add_opacity(COLOR_FONDO, 0.85), 
        expand=True,
        visible=False,
    )
    
    # Lo agregamos al overlay superior de la página para que cubra TODO
    page.overlay.append(loading_overlay)
    
    # --- LISTA GLOBAL DE BOTONES PARA BLOQUEO ---
    # Esta lista guardará las referencias a los botones de Matrix que queremos controlar
    menu_buttons = [] 
    
    # --- MOTOR DE EXTRACCIÓN Y AUTOGUARDADO CONTINUO (FASE 3) ---
    def _extract_serializable_session():
        """Extrae de page.session los tensores y datos serializables filtrando controles Flet."""
        session_dict = {}
        BLACKLIST_KEYS = ["catalogo_conagua", "df_estaciones_nacionales", "db_climatologica_masiva"]
        for k in page.session.get_keys():
            val = page.session.get(k)
            if callable(val) or isinstance(val, ft.Control) or "flet" in type(val).__module__:
                continue
            if k in BLACKLIST_KEYS:
                continue
            session_dict[k] = val
        return session_dict

    def trigger_autosave(silent=True, modulo_origen=None):
        """Dispara el autoguardado en segundo plano sin congelar la UI de Flet."""
        path = page.session.get("current_project_path")
        if not path:
            return
        def _bg_autosave():
            try:
                s_dict = _extract_serializable_session()
                project_manager_instance.save_project(path, s_dict)
                del s_dict
                if modulo_origen:
                    project_manager_instance.registrar_auditoria("AUTOSAVE", "CHECKPOINT", f"Checkpoint silencioso desde {modulo_origen}")
            except Exception as e:
                if not silent:
                    print(f"[WARN] Autosave error: {e}", file=sys.stderr)
        threading.Thread(target=_bg_autosave, daemon=True).start()

    page.session.set("trigger_autosave", trigger_autosave)

    # --- LÓGICA DE NAVEGACIÓN Y CARGA ---
    
    def show_dashboard():
        """Cambia la vista al menú principal de herramientas una vez cargado el proyecto."""
        page.clean()
        
        # --- LÓGICA DE RETORNO A INICIO CORREGIDA ---
        def return_to_welcome_screen():
            project_manager_instance.set_session_activa(False)
            # Forzamos la reactivación de todo el sistema de navegación
            main_rail = page.session.get("main_rail")
            if main_rail:
                main_rail.disabled = False
                if main_rail.trailing:
                    for ctrl in main_rail.trailing.controls:
                        ctrl.disabled = False
            # 1. Limpiamos la ruta del proyecto
            page.session.set("current_project_path", None)
            
            # 3. Limpiamos la pantalla
            page.clean()
            
            # 4. Re-inyectamos la vista de bienvenida
            # IMPORTANTE: Asegúrate de usar los nombres exactos de tus pickers (file_picker_open/save)
            page.add(
                build_welcome_view(
                    page, 
                    on_new_project=lambda _: file_picker_save.save_file(file_name="ProyectoNuevo.hds", allowed_extensions=["hds"]),
                    on_open_project=lambda _: file_picker_open.pick_files(allowed_extensions=["hds"])
                )
            )

        def prompt_exit_dialog(e):
            def exit_without_saving(_):
                page.close(exit_dialog)
                # --- CIERRE SEGURO ---
                project_manager_instance.close_project()
                return_to_welcome_screen()

            def save_and_exit(_):
                # 1. Cerramos el diálogo primero para limpiar la pantalla
                page.close(exit_dialog)
                
                if is_navigating[0]: return
                is_navigating[0] = True
                
                main_rail = page.session.get("main_rail")
                if main_rail: main_rail.disabled = True
                
                # Modificamos el Overlay para el Cierre
                loading_overlay.content.controls[1].value = "GUARDANDO Y CERRANDO..."
                loading_overlay.content.controls[2].value = "Escribiendo persistencia final en disco duro antes del cierre seguro"
                loading_overlay.visible = True
                page.update()
                
                def _save_and_close_task():
                    try:
                        import gc, time
                        path = page.session.get("current_project_path")
                        
                        # Proceso de Guardado
                        # Proceso de Guardado
                        if path:
                            gc.collect()
                            time.sleep(0.2)
                            session_dict = _extract_serializable_session()
                            exito = project_manager_instance.save_project(path, session_dict)
                            del session_dict
                            gc.collect()

                            if not exito:
                                raise RuntimeError("El motor de persistencia abortó la escritura. Revisa la terminal para ver el traceback JSON.")
                        
                        time.sleep(0.5)
                        
                        # --- LIMPIEZA FINAL SESION ---
                        page.session.clear()
                        project_manager_instance.close_project()
                        gc.collect()
                        
                        # Retorno a pantalla inicial de forma segura
                        def _ui_reset():
                            return_to_welcome_screen()
                            page.snack_bar = ft.SnackBar(ft.Text("📦 Proyecto guardado y cerrado correctamente."), bgcolor="#00ff41", open=True)
                            page.update()
                            
                        page.run_thread(_ui_reset)
                        
                    except Exception as ex:
                        import traceback
                        traceback.print_exc()
                        def _show_fatal_error():
                            page.snack_bar = ft.SnackBar(ft.Text(f"❌ Error crítico al cerrar: {str(ex)}"), bgcolor="red", open=True)
                            page.update()
                        page.run_thread(_show_fatal_error)
                        
                    finally:
                        # Limpieza final del Overlay
                        time.sleep(0.3)
                        def _finalize_close():
                            loading_overlay.visible = False
                            if main_rail: main_rail.disabled = False
                            is_navigating[0] = False
                            
                            loading_overlay.content.controls[1].value = "CARGANDO MÓDULO..."
                            loading_overlay.content.controls[2].value = "Descomprimiendo estructuras de memoria"
                            page.update()
                            
                        page.run_thread(_finalize_close)
                        
                # Mandamos a hilo demonio para no congelar Flet
                threading.Thread(target=_save_and_close_task, daemon=True).start()

            exit_dialog = ft.AlertDialog(
                modal=True,
                title=ft.Text("⚠️ CERRAR PROYECTO", color="#ffdd00", font_family=FUENTE_PRINCIPAL),
                content=ft.Text("¿Deseas guardar los cambios antes de volver a la pantalla de inicio?"),
                actions=[
                    ft.TextButton("Cancelar", on_click=lambda _: page.close(exit_dialog), style=ft.ButtonStyle(color="white")),
                    ft.TextButton("Cerrar sin Guardar", on_click=exit_without_saving, style=ft.ButtonStyle(color="red")),
                    ft.ElevatedButton("Guardar y Cerrar", on_click=save_and_exit, bgcolor=COLOR_ACENTO, color="black"),
                ],
                actions_alignment=ft.MainAxisAlignment.END,
                bgcolor=COLOR_SUPERFICIE,
            )
            page.open(exit_dialog)
            
        # --- EXPOSICIÓN DE SEGURIDAD PARA EL INTERCEPTOR DEL OS ---
        page.session.set("trigger_exit_dialog", prompt_exit_dialog)
        # -----------------------------------------------

        # Definimos el NavigationRail con los 4 módulos solicitados
        rail = ft.NavigationRail(
            selected_index=0,
            label_type=ft.NavigationRailLabelType.ALL,
            min_width=100,
            min_extended_width=200,
            group_alignment=-0.9,
            destinations=[
                ft.NavigationRailDestination(icon=ft.Icons.CLOUD_DOWNLOAD, label="1. EXTRACCIÓN"),
                ft.NavigationRailDestination(icon=ft.Icons.MEMORY, label="2. IMPUTACIÓN"),
                ft.NavigationRailDestination(icon=ft.Icons.SHOW_CHART, label="3. ANÁLISIS"),
                ft.NavigationRailDestination(icon=ft.Icons.MAP, label="4. ARF"), # <--- NUEVO MÓDULO INYECTADO
                ft.NavigationRailDestination(icon=ft.Icons.CALCULATE, label="5. GASTOS"),
                ft.NavigationRailDestination(icon=ft.Icons.THERMOSTAT_OUTLINED, selected_icon=ft.Icons.THERMOSTAT, label="6. CLIMATOLOGÍA"),
                ft.NavigationRailDestination(icon=ft.Icons.MAP_OUTLINED, selected_icon=ft.Icons.MAP, label="7. GIS / MODELO FÍSICO"),
            ],
            on_change=lambda e: change_module(e.control.selected_index),
            trailing=ft.Column([
                ft.Divider(),
                ft.IconButton(ft.Icons.SAVE, tooltip="GUARDAR PROYECTO", icon_color=COLOR_ACENTO, on_click=save_current_project),
                ft.IconButton(ft.Icons.LOGOUT, tooltip="CERRAR PROYECTO", icon_color="red", on_click=prompt_exit_dialog), # <--- AQUÍ ENLAZAMOS LA FUNCIÓN
            ], alignment=ft.MainAxisAlignment.END, spacing=10)
        )
        page.session.set("main_rail", rail)
        content_area = ft.Container(expand=True, padding=10)

        def change_module(index):
            if is_navigating[0]: 
                rail.selected_index = page.session.get("last_nav_index") or 0
                page.update()
                return

            # Autoguardado preventivo en background del módulo saliente
            last_idx = page.session.get("last_nav_index")
            if last_idx is not None and last_idx != index:
                trigger_autosave(silent=True, modulo_origen=f"Modulo_{last_idx}")

            is_navigating[0] = True
            page.session.set("last_nav_index", index)
            # Exponemos la función segura para que otras vistas puedan saltar
            page.session.set("navigate_to_module", change_module)
            
            # 1. Bloqueo Visual y Menú
            rail.disabled = True
            loading_overlay.visible = True
            page.update()

            def _loader_task():
                try:
                    # 2. Forzamos una recolección de basura agresiva antes de cargar el nuevo módulo
                    # Esto previene el OOM (Out Of Memory) al destruir las gráficas de Matplotlib de la vista anterior
                    import gc
                    gc.collect()

                    # 3. Asignación del Módulo
                    # Simulación de un retraso mínimo de UI para que el loader sea visible 
                    # y el DOM limpie los nodos viejos de Flutter
                    import time
                    time.sleep(0.3) 

                    if index == 0:
                        nueva_vista = build_descarga_view(page)
                    elif index == 1:
                        nueva_vista = imputacion_app.build_imputacion_view(page)
                    elif index == 2:
                        nueva_vista = analisis_app.build_analisis_view(page)
                    elif index == 3:
                        nueva_vista = build_arf_view(page) 
                    elif index == 4:
                        nueva_vista = gastos_app.build_gastos_view(page) 
                    elif index == 5: 
                        nueva_vista = build_climatologia_view(page) 
                    elif index == 6:
                        nueva_vista = build_topologia_view(page)
                    else:
                        nueva_vista = ft.Container()
                        
                    content_area.content = nueva_vista

                except Exception as e:
                    import traceback
                    traceback.print_exc()
                    page.snack_bar = ft.SnackBar(ft.Text(f"Error cargando módulo: {str(e)}"), bgcolor="red", open=True)
                finally:
                    # 4. Desbloqueo y limpieza post-hidratación
                    time.sleep(0.3) 
                    def _finalize():
                        loading_overlay.visible = False
                        rail.disabled = False
                        is_navigating[0] = False
                        page.update()
                    page.run_thread(_finalize)

            # Ejecutamos el cambio en un hilo separado para que el overlay gire fluidamente
            import threading
            threading.Thread(target=_loader_task, daemon=True).start()

        # Inicializar en el primer módulo
        change_module(0)

        page.add(
            ft.Row([
                rail,
                ft.VerticalDivider(width=1),
                content_area
            ], expand=True)
        )

    # --- HANDLERS DE PROYECTO ---

    # --- PANTALLA DE CARGA DE PROYECTO (VISUAL) ---
    txt_porcentaje_carga = ft.Text("0%", color="#00f0ff", weight="bold", size=50, font_family="Roboto Mono")
    pb_carga_proyecto = ft.ProgressBar(width=600, color="#00f0ff", bgcolor="#111111", value=0)
    terminal_carga = ft.ListView(expand=True, spacing=5, auto_scroll=True)
    
    overlay_carga_proyecto = ft.Container(
        content=ft.Column([
            txt_porcentaje_carga,
            pb_carga_proyecto,
            ft.Divider(color="transparent", height=10),
            ft.Text("SISTEMA DE CARGA HDS", color="#00f0ff", weight="bold", size=22, font_family="Roboto Mono"),
            ft.Text("Descomprimiendo tensores LZMA, preparando entorno... Xinechchiyoti in ika ma motlachihual...", color="grey", size=12),
            ft.Container(
                content=terminal_carga, height=250, width=700,
                bgcolor="#050505", border=ft.border.all(1, "#00f0ff"), border_radius=5, padding=10
            )
        ], alignment=ft.MainAxisAlignment.CENTER, horizontal_alignment=ft.CrossAxisAlignment.CENTER),
        bgcolor="#d9050505", # <--- HEX NATIVO SEGURO (85% Opacidad, negro profundo)
        expand=True, alignment=ft.alignment.center, visible=False
    )
    page.overlay.append(overlay_carga_proyecto)

    def handle_open_project(e: ft.FilePickerResultEvent):
        if e.files:
            path = e.files[0].path
            
            # 1. Bloqueo Inmediato de la Interfaz
            overlay_carga_proyecto.visible = True
            terminal_carga.controls.clear()
            pb_carga_proyecto.value = 0
            txt_porcentaje_carga.value = "0%"
            page.update()
            
            # Enlaces seguros para comunicar el Hilo con Flet
            def _log(msg):
                def update_log():
                    terminal_carga.controls.append(ft.Text(f"> {msg}", color="#00f0ff", font_family="monospace", size=11))
                    page.update()
                page.run_thread(update_log)
                
            def _prog(val):
                def update_prog():
                    pb_carga_proyecto.value = val
                    txt_porcentaje_carga.value = f"{int(val * 100)}%"
                    page.update()
                page.run_thread(update_prog)
            
            # 2. Hilo Desacoplado para Evitar el Freeze del Software
            def _task():
                try:
                    _log(f"Iniciando apertura segura del archivo: {os.path.basename(path)}")
                    
                    # Llamamos al motor con los callbacks inyectados
                    data = project_manager_instance.load_project(path, progress_callback=_prog, log_callback=_log)
                    
                    _log("Realizando purga de memoria antigua...")
                    page.session.clear() 
                    
                    _log("Inyectando matrices climáticas en la sesión activa...")
                    for key, value in data.items():
                        page.session.set(key, value)
                    
                    backup = page.session.get("txt_backup")
                    if backup and isinstance(backup, dict) and "path" in backup:
                        imput_dir = backup["path"]
                        page.session.set("imput_folder_path", imput_dir)
                        # Re-vincular los paths en imput_station_files a la nueva carpeta descomprimida
                        station_files = page.session.get("imput_station_files") or {}
                        if os.path.exists(imput_dir):
                            nuevos_station_files = {}
                            archivos_en_disco = {f.upper(): f for f in os.listdir(imput_dir) if f.endswith(".txt")}
                            for sid, old_filepath in station_files.items():
                                fname = os.path.basename(old_filepath) if old_filepath else f"{sid}.txt"
                                if fname.upper() in archivos_en_disco:
                                    nuevos_station_files[sid] = os.path.join(imput_dir, archivos_en_disco[fname.upper()])
                                else:
                                    posibles = [f for f in os.listdir(imput_dir) if f.startswith(str(sid))]
                                    if posibles:
                                        nuevos_station_files[sid] = os.path.join(imput_dir, posibles[0])
                                    else:
                                        nuevos_station_files[sid] = os.path.join(imput_dir, fname)
                            page.session.set("imput_station_files", nuevos_station_files)
                    
                    page.session.set("current_project_path", path)
                    
                    # Verificación de crash recovery previo
                    recuperado_de_crash = project_manager_instance.verificar_recuperacion_sesion()
                    project_manager_instance.set_session_activa(True)

                    _prog(1.0)
                    _log("✅ Inicialización completa. Lanzando entorno de trabajo...")
                    time.sleep(0.6) # Pausa estética para que el usuario aprecie el "100%"
                    
                    def _finalize():
                        overlay_carga_proyecto.visible = False
                        show_dashboard()
                        if recuperado_de_crash:
                            page.snack_bar = ft.SnackBar(
                                ft.Text("⚠️ El proyecto fue restaurado tras un cierre inesperado previo. Estado recuperado íntegro.", color="white", weight="bold"),
                                bgcolor="orange", open=True
                            )
                            page.update()
                    page.run_thread(_finalize)
                    
                except Exception as ex:
                    _log(f"❌ Error crítico de infraestructura: {str(ex)}")
                    traceback.print_exc()
                    
                    # 1. Mostramos el error en la UI de inmediato (Sin bloquear el GIL de Flet)
                    def _show_err():
                        page.snack_bar = ft.SnackBar(ft.Text(f"Error fatal al cargar: {str(ex)}"), bgcolor="red")
                        page.snack_bar.open = True
                        page.update()
                    page.run_thread(_show_err)
                    
                    # 2. El hilo en background (Demonio) asume la pausa térmica de 4 segundos. 
                    # La UI (Flet) queda totalmente libre y responsiva.
                    time.sleep(4) 
                    
                    # 3. Restauramos la UI purgando la memoria corrupta para permitir un nuevo intento
                    def _reset_after_error():
                        overlay_carga_proyecto.visible = False
                        # Limpiar punteros para no dejar el sistema en un estado "zombie"
                        page.session.set("current_project_path", None)
                        terminal_carga.controls.clear()
                        pb_carga_proyecto.value = 0
                        txt_porcentaje_carga.value = "0%"
                        page.update()
                    page.run_thread(_reset_after_error)

            # Detonamos el proceso en el Background
            threading.Thread(target=_task, daemon=True).start()

    def handle_new_project(e: ft.FilePickerResultEvent):
        if e.path:
            # Forzar la extensión .hds si el usuario la omitió en el cuadro de diálogo
            path = e.path if e.path.endswith(".hds") else e.path + ".hds"
            try:
                page.session.clear()
                project_manager_instance.save_project(path, {"status": "new_project"})
                page.session.set("current_project_path", path)
                project_manager_instance.set_session_activa(True)
                show_dashboard()
            except Exception as ex:
                page.snack_bar = ft.SnackBar(ft.Text(f"Error al crear: {str(ex)}"), bgcolor="red")
                page.snack_bar.open = True
                page.update()

    def save_current_project(e=None):
        if is_navigating[0]: return # Evitamos spam-clicks
        
        path = page.session.get("current_project_path")
        if not path:
            page.snack_bar = ft.SnackBar(ft.Text("⚠️ No hay un proyecto activo para guardar."), bgcolor="orange", open=True)
            page.update()
            return

        # 1. Bloqueo de UI y Activación de la Animación Cyberpunk
        is_navigating[0] = True
        main_rail = page.session.get("main_rail")
        if main_rail: main_rail.disabled = True
        
        # Modificamos dinámicamente los textos del overlay
        loading_overlay.content.controls[1].value = "GUARDANDO PROYECTO..."
        loading_overlay.content.controls[2].value = "Serializando matrices, escenarios y climatología histórica"
        loading_overlay.visible = True
        page.update()

        def _save_task():
            try:
                import gc, time
                gc.collect() # Limpieza de memoria previa
                time.sleep(0.2) # Estabilización del buffer visual
                
                session_dict = _extract_serializable_session()
                exito = project_manager_instance.save_project(path, session_dict)
                del session_dict
                gc.collect()

                if not exito:
                    raise IOError("Fallo en la compresión LZMA. El archivo no se guardó correctamente.")
                
                # Función segura para actualizar Flet desde otro hilo
                def _show_success():
                    page.snack_bar = ft.SnackBar(ft.Text("✅ Proyecto guardado exitosamente."), bgcolor="#00ff41", open=True)
                    page.update()
                page.run_thread(_show_success)
                
            except Exception as ex:
                import traceback
                traceback.print_exc()
                
                # 1. Capturamos el texto del error de inmediato en una variable segura
                mensaje_error = str(ex)
                
                # 2. Inyectamos el mensaje como parámetro (Thread-Safe)
                def _show_error(msg=mensaje_error):
                    page.snack_bar = ft.SnackBar(ft.Text(f"❌ Error al guardar: {msg}"), bgcolor="red", open=True)
                    page.update()
                    
                page.run_thread(_show_error)
                
            finally:
                # 2. Restauración del Sistema
                time.sleep(0.3)
                def _finalize_save():
                    loading_overlay.visible = False
                    if main_rail: main_rail.disabled = False
                    is_navigating[0] = False
                    
                    loading_overlay.content.controls[1].value = "CARGANDO MÓDULO..."
                    loading_overlay.content.controls[2].value = "Descomprimiendo estructuras de memoria"
                    page.update()
                    
                page.run_thread(_finalize_save)

        # Desacoplamos del hilo de UI para liberar el renderizador gráfico
        threading.Thread(target=_save_task, daemon=True).start()

    # --- PICKERS ---
    file_picker_open = ft.FilePicker(on_result=handle_open_project)
    file_picker_save = ft.FilePicker(on_result=handle_new_project)
    page.overlay.extend([file_picker_open, file_picker_save])

    # --- ARRANQUE ---
    # Cargamos la pantalla de bienvenida pasándole los disparadores de los pickers
    page.add(
        build_welcome_view(
            page, 
            on_new_project=lambda _: file_picker_save.save_file(file_name="ProyectoNuevo.hds", allowed_extensions=["hds"]),
            on_open_project=lambda _: file_picker_open.pick_files(allowed_extensions=["hds"])
        )
    )

if __name__ == "__main__":
    ft.app(target=main, assets_dir="assets")