# ui/views/analisis_view.py
import flet as ft
import pandas as pd
import numpy as np
import os 
import traceback
import asyncio
import tempfile
import uuid

from core import Analisis
from core import lluvias_logic as lluvias  
from core import analisis_cuenca 

TRANSPARENT_PIXEL = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="

def build_analisis_view(page: ft.Page):
    
    keys_temp = [
        "b64_hist", "b64_series", "b64_violin", "b64_max_annual", "b64_acf", 
        "b64_weibull", "b64_dist_comp", "cuenca_plot1_b64", "cuenca_plot2_b64", 
        "cuenca_plot3_b64", "cuenca_plot4_b64"
    ]
    
    keys_data = [
        "df_procesado", "df_filtrado", "df_estadisticas", "df_maximos_mensuales", 
        "df_weibull", "best_fit_name", "df_homogeneidad", "df_acf", "df_ajustes", 
        "df_altura", "df_intensidad", "df_parametros", 
        "metodo_r_seleccionado", "p1_10_manual", "p1_25_manual", "p1_50_manual", 
        "r_regional_manual", "descriptores_chen"
    ]
    
    loading_ring = ft.ProgressRing(color="#00f0ff", stroke_width=5)
    loading_text = ft.Text("Calculando...", color="#00f0ff", weight="bold", font_family="Roboto Mono")
    bloqueo_ui = ft.Container(
        content=ft.Column([loading_ring, loading_text], alignment=ft.MainAxisAlignment.CENTER, horizontal_alignment=ft.CrossAxisAlignment.CENTER),
        bgcolor="#d9050505", expand=True, alignment=ft.alignment.center, visible=False
    )
    page.overlay.append(bloqueo_ui)

    def _guardar_descriptores_sqlite(sid: str, descriptores: dict, ruta_gpkg: str):
        """Persistencia atómica transaccional en GeoPackage con migración DDL dinámica (EAFP)."""
        if not ruta_gpkg or not os.path.exists(ruta_gpkg):
            return
        import sqlite3
        conn = None
        try:
            # Timeout defensivo de 10s para mitigar bloqueos concurrentes de archivo
            conn = sqlite3.connect(ruta_gpkg, timeout=10.0)
            cur = conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL;")
            cur.execute("PRAGMA busy_timeout=5000;")
            
            # 1. Asegurar la existencia de la tabla base
            cur.execute("""
                CREATE TABLE IF NOT EXISTS estacion_descriptores_chen (
                    station_id TEXT PRIMARY KEY,
                    distribucion TEXT NOT NULL,
                    metodo_r TEXT NOT NULL,
                    p1_10 REAL NOT NULL,
                    p1_25 REAL NOT NULL,
                    p1_50 REAL NOT NULL,
                    p24_fijo_10 REAL NOT NULL,
                    p24_fijo_25 REAL NOT NULL,
                    p24_fijo_50 REAL NOT NULL,
                    p24_fijo_100 REAL NOT NULL,
                    factor_weiss REAL NOT NULL,
                    p24_movil_10 REAL NOT NULL,
                    p24_movil_25 REAL NOT NULL,
                    p24_movil_50 REAL NOT NULL,
                    p24_movil_100 REAL NOT NULL,
                    r_10 REAL NOT NULL,
                    r_25 REAL NOT NULL,
                    r_50 REAL NOT NULL,
                    r_promedio REAL NOT NULL,
                    factor_f REAL NOT NULL,
                    param_a REAL NOT NULL,
                    param_b REAL NOT NULL,
                    param_c REAL NOT NULL,
                    fecha_calculo TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            
            # 2. Migración Dinámica en Caliente: Validar si existen todas las columnas requeridas
            cur.execute("PRAGMA table_info(estacion_descriptores_chen);")
            columnas_existentes = {col[1] for col in cur.fetchall()}
            
            columnas_requeridas = {
                "distribucion": "TEXT DEFAULT ''",
                "metodo_r": "TEXT DEFAULT 'r_regional'",
                "p1_10": "REAL DEFAULT 0.0",
                "p1_25": "REAL DEFAULT 0.0",
                "p1_50": "REAL DEFAULT 0.0",
                "p24_fijo_10": "REAL DEFAULT 0.0",
                "p24_fijo_25": "REAL DEFAULT 0.0",
                "p24_fijo_50": "REAL DEFAULT 0.0",
                "p24_fijo_100": "REAL DEFAULT 0.0",
                "factor_weiss": "REAL DEFAULT 1.13",
                "p24_movil_10": "REAL DEFAULT 0.0",
                "p24_movil_25": "REAL DEFAULT 0.0",
                "p24_movil_50": "REAL DEFAULT 0.0",
                "p24_movil_100": "REAL DEFAULT 0.0",
                "r_10": "REAL DEFAULT 0.0",
                "r_25": "REAL DEFAULT 0.0",
                "r_50": "REAL DEFAULT 0.0",
                "r_promedio": "REAL DEFAULT 0.0",
                "factor_f": "REAL DEFAULT 1.0",
                "param_a": "REAL DEFAULT 0.0",
                "param_b": "REAL DEFAULT 0.0",
                "param_c": "REAL DEFAULT 0.0",
                "fecha_calculo": "TIMESTAMP DEFAULT CURRENT_TIMESTAMP"
            }
            
            for col_nombre, col_tipo in columnas_requeridas.items():
                if col_nombre not in columnas_existentes:
                    try:
                        cur.execute(f"ALTER TABLE estacion_descriptores_chen ADD COLUMN {col_nombre} {col_tipo};")
                    except Exception as e_col:
                        print(f"Aviso migración columna {col_nombre}: {e_col}")
            
            # 3. Inserción / Upsert Seguro
            cur.execute("""
                INSERT INTO estacion_descriptores_chen (
                    station_id, distribucion, metodo_r, p1_10, p1_25, p1_50,
                    p24_fijo_10, p24_fijo_25, p24_fijo_50, p24_fijo_100,
                    factor_weiss, p24_movil_10, p24_movil_25, p24_movil_50, p24_movil_100,
                    r_10, r_25, r_50, r_promedio, factor_f, param_a, param_b, param_c
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(station_id) DO UPDATE SET
                    distribucion=excluded.distribucion,
                    metodo_r=excluded.metodo_r,
                    p1_10=excluded.p1_10,
                    p1_25=excluded.p1_25,
                    p1_50=excluded.p1_50,
                    p24_fijo_10=excluded.p24_fijo_10,
                    p24_fijo_25=excluded.p24_fijo_25,
                    p24_fijo_50=excluded.p24_fijo_50,
                    p24_fijo_100=excluded.p24_fijo_100,
                    factor_weiss=excluded.factor_weiss,
                    p24_movil_10=excluded.p24_movil_10,
                    p24_movil_25=excluded.p24_movil_25,
                    p24_movil_50=excluded.p24_movil_50,
                    p24_movil_100=excluded.p24_movil_100,
                    r_10=excluded.r_10,
                    r_25=excluded.r_25,
                    r_50=excluded.r_50,
                    r_promedio=excluded.r_promedio,
                    factor_f=excluded.factor_f,
                    param_a=excluded.param_a,
                    param_b=excluded.param_b,
                    param_c=excluded.param_c,
                    fecha_calculo=CURRENT_TIMESTAMP;
            """, (
                sid, descriptores.get("distribucion_aplicada", ""), descriptores.get("metodo_r_utilizado", "r_regional"),
                descriptores.get("p1_10_mm", 0.0), descriptores.get("p1_25_mm", 0.0), descriptores.get("p1_50_mm", 0.0),
                descriptores.get("p24_fijo_10_mm", 0.0), descriptores.get("p24_fijo_25_mm", 0.0),
                descriptores.get("p24_fijo_50_mm", 0.0), descriptores.get("p24_fijo_100_mm", 0.0),
                descriptores.get("factor_weiss", 1.13),
                descriptores.get("p24_movil_10_mm", 0.0), descriptores.get("p24_movil_25_mm", 0.0),
                descriptores.get("p24_movil_50_mm", 0.0), descriptores.get("p24_movil_100_mm", 0.0),
                descriptores.get("r_10", 0.0), descriptores.get("r_25", 0.0), descriptores.get("r_50", 0.0),
                descriptores.get("r_acotado", 0.3882), descriptores.get("f_calculado", 1.0),
                descriptores.get("param_a", 0.0), descriptores.get("param_b", 0.0), descriptores.get("param_c", 0.0)
            ))
            conn.commit()
        except Exception as ex:
            print(f"Error persistiendo descriptores en SQLite: {ex}")
            traceback.print_exc()
        finally:
            if conn:
                conn.close()

    def _es_directorio_escribible(dir_path: str) -> bool:
        """Comprueba si una ruta existe, es directorio y permite lectura y escritura reales."""
        if not dir_path or not isinstance(dir_path, str) or not os.path.exists(dir_path) or not os.path.isdir(dir_path):
            return False
        test_file = os.path.join(dir_path, f".chk_{os.getpid()}_{uuid.uuid4().hex[:4]}")
        try:
            with open(test_file, "w", encoding="utf-8") as f:
                f.write("1")
            os.remove(test_file)
            return True
        except Exception:
            return False

    def _crear_cache_seguro() -> str:
        """Crea un directorio de caché validado contra errores de permisos WinError 5."""
        # 1. Intentar con tempfile estándar del sistema operativo
        try:
            cand = tempfile.mkdtemp(prefix="hidro_cache_")
            if _es_directorio_escribible(cand):
                return cand
        except Exception:
            pass
            
        # 2. Fallback a directorio en el perfil de usuario (.hydas_cache)
        try:
            cand_user = os.path.join(os.path.expanduser("~"), ".hydas_cache", f"sess_{os.getpid()}_{uuid.uuid4().hex[:4]}")
            os.makedirs(cand_user, exist_ok=True)
            if _es_directorio_escribible(cand_user):
                return cand_user
        except Exception:
            pass
            
        # 3. Fallback a carpeta local en el directorio de trabajo
        try:
            cand_local = os.path.join(os.getcwd(), ".temp_cache", f"sess_{os.getpid()}_{uuid.uuid4().hex[:4]}")
            os.makedirs(cand_local, exist_ok=True)
            if _es_directorio_escribible(cand_local):
                return cand_local
        except Exception:
            pass
            
        return tempfile.gettempdir()

    def persistir_estacion_actual():
        """Persiste estructuras en parquet bajo una jerarquía de carpetas protegida contra colisiones y denegación de permisos en Windows."""
        try:
            sid = page.session.get("target_station_id")
            if not sid:
                return

            desc = page.session.get("descriptores_chen")
            if desc:
                ruta_gpkg = page.session.get("current_project_path") or page.session.get("ruta_bd_activa")
                if ruta_gpkg:
                    from core.imputacion_logic import resolver_ruta_hds
                    ruta_limpia = resolver_ruta_hds(ruta_gpkg)
                    _guardar_descriptores_sqlite(sid, desc, ruta_limpia)

            db_curvas = page.session.get("db_curvas_procesadas") or {}
            cache_dir = page.session.get("project_cache_dir")
            
            # Validación defensiva del directorio de caché (Windows Safe)
            if not _es_directorio_escribible(cache_dir):
                cache_dir = _crear_cache_seguro()
                page.session.set("project_cache_dir", cache_dir)
            
            # Resolver directorio para la estación
            station_cache = None
            can_persist_disk = False
            
            target_station_cache = os.path.join(cache_dir, str(sid).strip())
            try:
                if os.path.exists(target_station_cache) and not os.path.isdir(target_station_cache):
                    os.remove(target_station_cache)
                os.makedirs(target_station_cache, exist_ok=True)
                station_cache = target_station_cache
                can_persist_disk = True
            except Exception:
                # Reintento con UUID si el nombre de la estación choca con un archivo
                try:
                    target_station_cache = os.path.join(cache_dir, f"{sid}_{uuid.uuid4().hex[:6]}")
                    os.makedirs(target_station_cache, exist_ok=True)
                    station_cache = target_station_cache
                    can_persist_disk = True
                except Exception:
                    # Si falló porque cache_dir se volvió inaccesible, crear un nuevo cache_dir seguro
                    try:
                        cache_dir = _crear_cache_seguro()
                        page.session.set("project_cache_dir", cache_dir)
                        target_station_cache = os.path.join(cache_dir, str(sid).strip())
                        os.makedirs(target_station_cache, exist_ok=True)
                        station_cache = target_station_cache
                        can_persist_disk = True
                    except Exception as ex_dir:
                        print(f"⚠️ No se pudo crear directorio en disco para estación {sid}: {ex_dir}. Se persistirá en RAM.")
                        can_persist_disk = False
            
            refs = {}
            for k in keys_data + keys_temp:
                val = page.session.get(k)
                if val is None:
                    continue
                if can_persist_disk and station_cache:
                    try:
                        if isinstance(val, pd.DataFrame) and not val.empty:
                            df_safe = val.copy(deep=True)
                            # Forzar compatibilidad de tipos sin mutar el índice numérico
                            if not isinstance(df_safe.index, pd.RangeIndex):
                                try:
                                    df_safe.index = df_safe.index.astype(np.float64)
                                except Exception:
                                    df_safe.index = df_safe.index.astype(str)
                            df_safe.columns = [str(col) for col in df_safe.columns]
                            
                            path = os.path.join(station_cache, f"{k}.parquet")
                            df_safe.to_parquet(path, engine="pyarrow")
                            refs[k] = {"type": "df", "path": path}
                        elif isinstance(val, str) and len(val) > 100:
                            path = os.path.join(station_cache, f"{k}.txt")
                            with open(path, "w", encoding="utf-8") as f:
                                f.write(val)
                            refs[k] = {"type": "b64", "path": path}
                        else:
                            refs[k] = {"type": "raw", "value": val}
                    except Exception:
                        refs[k] = {"type": "raw", "value": val}
                else:
                    refs[k] = {"type": "raw", "value": val}
                    
            db_curvas[sid] = refs
            page.session.set("db_curvas_procesadas", db_curvas)
        except Exception as ex_persist:
            print(f"⚠️ Aviso no fatal en persistir_estacion_actual: {ex_persist}")
            traceback.print_exc()

    if not page.session.get("db_curvas_procesadas"): 
        page.session.set("db_curvas_procesadas", {})

    dd_estacion_maestra = ft.Dropdown(
        label="Estación de Trabajo Activa", hint_text="Seleccione una estación...",
        width=350, border_color="#00ff41",
        on_change=lambda e: sincronizar_estacion_activa(e.control.value)
    )

    def actualizar_dropdown_maestro():
        db_crudas = page.session.get("db_series_crudas") or {}
        dd_estacion_maestra.options = [ft.dropdown.Option(k) for k in db_crudas.keys()]
        page.update()

    def limpiar_tablas_ui():
        tbl_mx.controls = [ft.Text("Esperando cálculo...", italic=True)]
        tbl_alt.controls = [ft.Text("Esperando cálculo...", italic=True)]
        data_table_filt.controls = [ft.Text("Esperando aplicación de filtros...", italic=True)]
        data_table_stats.controls = [ft.Text("Esperando cálculo...", italic=True)]
        tbl_homo.controls = [ft.Text("Esperando cálculo...", italic=True)]
        tbl_wei.controls = [ft.Text("Esperando cálculo...", italic=True)]
        tbl_ajustes.controls = [ft.Text("Esperando cálculo...", italic=True)]
        tbl_int.controls = [ft.Text("Esperando cálculo...", italic=True)]
        tbl_parametros.controls = [ft.Text("Esperando cálculo...", italic=True)]
        log_cuenca.value = ""

    def sincronizar_estacion_activa(sid):
        db_curvas = page.session.get("db_curvas_procesadas") or {}
        if sid in db_curvas:
            data = db_curvas[sid]
            for k in keys_data + keys_temp: 
                val = data.get(k)
                if isinstance(val, dict) and "type" in val:
                    if val["type"] == "df" and os.path.exists(val["path"]):
                        try: page.session.set(k, pd.read_parquet(val["path"]))
                        except: page.session.set(k, None)
                    elif val["type"] == "b64" and os.path.exists(val["path"]):
                        try:
                            with open(val["path"], "r") as f: page.session.set(k, f.read())
                        except: page.session.set(k, None)
                    else: page.session.set(k, val.get("value"))
                else: page.session.set(k, val)
            rehidratar_interfaz() 
            page.snack_bar = ft.SnackBar(ft.Text(f"✅ {sid}: Análisis recuperado desde disco/memoria."), bgcolor="green", open=True)
        else:
            ruta_bd = page.session.get("current_project_path") or page.session.get("ruta_bd_activa")
            df_procesado = None
            saved_p1 = None
            if ruta_bd:
                from core.imputacion_logic import resolver_ruta_hds
                ruta_hds = resolver_ruta_hds(ruta_bd)
                    
                if os.path.exists(ruta_hds):
                    import sqlite3
                    conn = sqlite3.connect(ruta_hds, timeout=10.0)
                    try:
                        query = f"SELECT * FROM serie_imputada_{sid}"
                        df_estacion = pd.read_sql_query(query, conn, parse_dates=['FECHA'])
                        if not df_estacion.empty:
                            df_estacion.set_index('FECHA', inplace=True)
                            mapa_nombres = {}
                            if 'PRECIP' in df_estacion.columns: mapa_nombres['PRECIP'] = 'PRECIP_imputado'
                            if 'PRECIP_ORIGINAL' in df_estacion.columns: mapa_nombres['PRECIP_ORIGINAL'] = 'PRECIP_original'
                            df_estacion.rename(columns=mapa_nombres, inplace=True)
                            if 'PRECIP_original' not in df_estacion.columns and 'PRECIP_imputado' in df_estacion.columns:
                                df_estacion['PRECIP_original'] = df_estacion['PRECIP_imputado']
                            df_estacion.drop(columns=[c for c in ['lat', 'lon', 'station_id'] if c in df_estacion.columns], inplace=True)
                            df_procesado = df_estacion

                        cur_desc = conn.cursor()
                        cur_desc.execute("SELECT metodo_r, p1_10, p1_25, p1_50 FROM estacion_descriptores_chen WHERE station_id = ?", (sid,))
                        row_desc = cur_desc.fetchone()
                        if row_desc:
                            saved_p1 = (str(row_desc[0]), float(row_desc[1]), float(row_desc[2]), float(row_desc[3]))
                    except Exception:
                        pass
                    finally:
                        conn.close()

            for k in keys_data + keys_temp: page.session.set(k, None)

            if saved_p1 is not None:
                rg_metodo_r.value = saved_p1[0]
                inp_p1_10.value = str(saved_p1[1])
                inp_p1_25.value = str(saved_p1[2])
                inp_p1_50.value = str(saved_p1[3])
                on_cambio_metodo_r(None)
            else:
                rg_metodo_r.value = "r_regional"
                inp_p1_10.value = ""
                inp_p1_25.value = ""
                inp_p1_50.value = ""
                on_cambio_metodo_r(None)

            page.session.set("df_procesado", df_procesado)
            page.session.set("target_station_id", sid)
            csv_path_txt.value = f"Datos Crudos: {sid}"
            data_table_raw.controls = [ft.Text(f"Previsualización {sid}:"), dataframe_to_datatable(df_procesado)]
            
            for img in [img_h, img_s, img_v, img_max_an, img_acf, img_wei, img_dist, img_c1, img_c2, img_c3, img_c4]:
                img.content.src_base64 = TRANSPARENT_PIXEL
            
            limpiar_tablas_ui()
            btn_nav_filt.disabled = False 
            tabs.selected_index = 0
            page.snack_bar = ft.SnackBar(ft.Text(f"📥 {sid}: Datos cargados."), bgcolor="blue", open=True)
            page.update()
    
    for k in keys_temp + keys_data:
        if page.session.get(k) is None: page.session.set(k, None)

    csv_path_txt = ft.Column(expand=True, controls=[ft.Text("Esperando filtros...")])
    data_table_raw = ft.Column(expand=True, controls=[ft.Text("Esperando filtros...")])
    data_table_stats = ft.Column(expand=True, controls=[ft.Text("Calculando...")])
    
    def safe_img(session_key):
        val = page.session.get(session_key)
        src = val if (val and len(val) > 100) else TRANSPARENT_PIXEL
        return ft.Container(content=ft.Image(src_base64=src, fit=ft.ImageFit.CONTAIN), expand=True)

    def dataframe_to_datatable(df, max_filas=90, auto_expand=False):
        if df is None or (hasattr(df, 'empty') and df.empty): return ft.Text("Sin datos")
        try:
            df_view = df.head(max_filas).copy()
            if df_view.index.name is not None or not isinstance(df_view.index, pd.RangeIndex):
                df_view.reset_index(inplace=True)
            df_view.columns = df_view.columns.astype(str)
            cols = [ft.DataColumn(ft.Text(col[:15], weight="bold", color="#00ff41")) for col in df_view.columns]
            rows = [ft.DataRow([ft.DataCell(ft.Text(str(val)[:20], size=11)) for val in row]) for _, row in df_view.iterrows()]
            tabla = ft.DataTable(columns=cols, rows=rows, column_spacing=15, border=ft.border.all(1, "#333333"), vertical_lines=ft.border.BorderSide(1, "#333333"))
            inner_col = ft.Column(controls=[ft.Row([tabla], scroll=ft.ScrollMode.AUTO)], scroll=ft.ScrollMode.AUTO)
            if auto_expand:
                inner_col.expand = True
                return ft.Container(content=inner_col, expand=True, border=ft.border.all(1, "#222222"), border_radius=5)
            else:
                return ft.Container(content=inner_col, height=370, border=ft.border.all(1, "#222222"), border_radius=5)
        except: return ft.Text("Error tabla", color="red")

    def stats_to_datatable(df, auto_expand=False):
        if df is None: return ft.Text("Sin estadísticas")
        try: return dataframe_to_datatable(df.reset_index().rename(columns={'index': 'Métrica'}), auto_expand=auto_expand)
        except: return ft.Text("Error stats")

    csv_picker = ft.FilePicker()
    export_picker = ft.FilePicker()
    page.overlay.extend([csv_picker, export_picker])

    def on_export_result(e: ft.FilePickerResultEvent):
        if e.path:
            try:
                content = page.session.get("temp_export_content")
                ext = page.session.get("temp_export_ext")
                if content is None: raise ValueError("No hay datos en memoria para guardar.")

                if ext == "png":
                    import base64
                    path = f"{e.path}.png" if not e.path.endswith(".png") else e.path
                    with open(path, "wb") as f: f.write(base64.b64decode(content))
                elif ext == "csv":
                    path = f"{e.path}.csv" if not e.path.endswith(".csv") else e.path
                    if hasattr(content, 'columns') and 'TR (AÑOS)' in content.columns: 
                        content.set_index('TR (AÑOS)').to_csv(path)
                    else: 
                        content.to_csv(path, index=False, encoding='utf-8-sig')
                page.snack_bar = ft.SnackBar(ft.Text("Archivo guardado exitosamente"), bgcolor="green", open=True)
            except Exception as ex:
                page.snack_bar = ft.SnackBar(ft.Text(f"Error al guardar: {str(ex)}"), bgcolor="red", open=True)
            page.update()

    export_picker.on_result = on_export_result

    def trigger_save_image(session_key, default_name):
        b64_string = page.session.get(session_key)
        if not b64_string or b64_string == TRANSPARENT_PIXEL:
            page.snack_bar = ft.SnackBar(ft.Text("No hay gráfico generado para guardar."), bgcolor="orange", open=True)
            page.update(); return
        page.session.set("temp_export_content", b64_string)
        page.session.set("temp_export_ext", "png")
        export_picker.save_file(dialog_title="Guardar Gráfico", file_name=default_name, allowed_extensions=["png"])

    def trigger_save_csv(session_key, default_name):
        df_to_save = page.session.get(session_key)
        if df_to_save is None or (hasattr(df_to_save, 'empty') and df_to_save.empty):
            page.snack_bar = ft.SnackBar(ft.Text("No hay tabla generada para guardar."), bgcolor="orange", open=True)
            page.update(); return
        page.session.set("temp_export_content", df_to_save)
        page.session.set("temp_export_ext", "csv")
        export_picker.save_file(dialog_title="Guardar Tabla", file_name=default_name, allowed_extensions=["csv"])

    data_table_filt, stats_table_container = ft.Column(scroll=ft.ScrollMode.AUTO, height=300), ft.Column()
    chk_c1, chk_c2, chk_c3 = ft.Checkbox(label="Eliminar Nulos", value=True), ft.Checkbox(label="Eliminar < 5 Vecinos", value=True), ft.Checkbox(label="Eliminar Ruido", value=True)
    img_h, img_s, img_v = safe_img("b64_hist"), safe_img("b64_series"), safe_img("b64_violin")
    img_max_an, img_acf, img_wei, img_dist = safe_img("b64_max_annual"), safe_img("b64_acf"), safe_img("b64_weibull"), safe_img("b64_dist_comp")
    img_c1, img_c2, img_c3, img_c4 = safe_img("cuenca_plot1_b64"), safe_img("cuenca_plot2_b64"), safe_img("cuenca_plot3_b64"), safe_img("cuenca_plot4_b64")
    tbl_homo, tbl_acf, tbl_ajustes, tbl_wei, tbl_mx, tbl_alt, tbl_int, tbl_parametros = ft.Column(), ft.Column(), ft.Column(), ft.Column(), ft.Column(), ft.Column(), ft.Column(), ft.Column()
    txt_best_fit, inp_ylim_h, inp_ylim_i, log_cuenca = ft.Text("No detectado", weight="bold", color="#00ff41"), ft.TextField(label="Límite Altura", width=150), ft.TextField(label="Límite Intensidad", width=150), ft.Markdown()

    # Controles de Calibración R (Reemplazo Oficial)
    rg_metodo_r = ft.RadioGroup(
        content=ft.Row([
            ft.Radio(value="r_regional", label="Método 1: Factor R Regional Directo (Campos-Aranda / SICT)", fill_color="#00ff41"),
            ft.Radio(value="isoyetas_sct", label="Método 2: Isoyetas SCT (Láminas P1 Manuales)", fill_color="#00ff41")
        ]),
        value="r_regional"
    )

    inp_r_regional = ft.TextField(
        label="Factor R Regional (P₆₀¹⁰/P₂₄¹⁰)",
        value="0.3882",
        width=200,
        border_color="#00ff41",
        keyboard_type=ft.KeyboardType.NUMBER,
        tooltip="Por defecto 0.3882 para RH-24 (Bravo-Conchos)."
    )
    inp_p1_10 = ft.TextField(label="P₁¹⁰ SCT (Tr 10a)", hint_text="mm", width=160, border_color="#00ff41", keyboard_type=ft.KeyboardType.NUMBER)
    inp_p1_25 = ft.TextField(label="P₁²⁵ SCT (Tr 25a)", hint_text="mm", width=160, border_color="#00ff41", keyboard_type=ft.KeyboardType.NUMBER)
    inp_p1_50 = ft.TextField(label="P₁⁵⁰ SCT (Tr 50a)", hint_text="mm", width=160, border_color="#00ff41", keyboard_type=ft.KeyboardType.NUMBER)

    fila_inputs_sct = ft.Row([inp_p1_10, inp_p1_25, inp_p1_50], wrap=True, spacing=10, visible=False)
    fila_inputs_regional = ft.Row([
        inp_r_regional,
        ft.Text("Ingreso directo del cociente regional. Deduce curvas sin interpolaciones empíricas.", color="#00ff41", size=11, italic=True)
    ], visible=True)

    def on_cambio_metodo_r(e):
        es_sct = (rg_metodo_r.value == "isoyetas_sct")
        fila_inputs_sct.visible = es_sct
        fila_inputs_regional.visible = not es_sct
        page.session.set("metodo_r_seleccionado", rg_metodo_r.value)
        page.update()

    rg_metodo_r.on_change = on_cambio_metodo_r

    def advance_tab(index):
        tabs.selected_index = index
        page.update()

    def on_load(e):
        if e.files:
            csv_path_txt.value = e.files[0].name; csv_path_txt.color="green"
            try:
                df = Analisis.procesar_datos(e.files[0].path)
                if df is not None:
                    page.session.set("df_procesado", df)
                    data_table_raw.controls = [ft.Text("Datos:"), dataframe_to_datatable(df)]
                    btn_nav_filt.disabled = False
            except: pass
            page.update()

    def run_filter(e=None):
        try:
            df_procesado = page.session.get("df_procesado")
            if df_procesado is None: return
            df = page.session.get("df_filtrado") if (e is None and page.session.get("df_filtrado") is not None) else Analisis.filtrar_datos(df_procesado, chk_c1.value, chk_c2.value, chk_c3.value)
            if df is not None:
                page.session.set("df_filtrado", df)
                data_table_filt.controls = [ft.Text(f"Datos Filtrados: {len(df)} registros"), dataframe_to_datatable(df)]
                btn_nav_stats.disabled = False
                if e: advance_tab(1)
            if e: page.update()
        except Exception as ex:
            page.snack_bar = ft.SnackBar(ft.Text(f"❌ Error en Filtrado: {str(ex)}"), bgcolor="red", open=True)
            page.update()

    def run_stats(e=None):
        df_filtrado = page.session.get("df_filtrado")
        if df_filtrado is None: return
        st = page.session.get("df_estadisticas") if (e is None and page.session.get("df_estadisticas") is not None) else Analisis.analizar_estadisticas(df_filtrado)
        if st is not None:
            page.session.set("df_estadisticas", st)
            stats_table_container.controls = [ft.Text("Estadísticas:", color="#00ff41"), stats_to_datatable(st)]
            btn_nav_graph.disabled = False
            if e: advance_tab(2)
        if e: page.update()

    def run_graphs(e=None):
        df_filtrado = page.session.get("df_filtrado")
        if df_filtrado is None: return
        sid = page.session.get("target_station_id") or ""
        imgs = Analisis.generar_graficos(df_filtrado, page.session.get("df_estadisticas"), sid)
        if imgs:
            img_h.content.src_base64, img_s.content.src_base64, img_v.content.src_base64 = imgs['hist'], imgs['series'], imgs['violin']
            page.session.set("b64_hist", imgs['hist'])
            page.session.set("b64_series", imgs['series'])
            page.session.set("b64_violin", imgs['violin'])
            btn_nav_lluvias.disabled = False
            if e: advance_tab(3)
        if e: page.update()

    def run_lluvias(e=None):
        df_filtrado = page.session.get("df_filtrado")
        if df_filtrado is None: return
        sid = page.session.get("target_station_id") or ""
        res = lluvias.analizar_eventos_lluvia(df_filtrado, sid)
        if res:
            page.session.set("df_homogeneidad", res["df_homogeneidad"])
            page.session.set("df_maximos_mensuales", res["df_maximos_mensuales"])
            page.session.set("best_fit_name", res["best_fit_name"])
            page.session.set("df_acf", res["df_acf"])
            page.session.set("df_ajustes", res["df_ajustes"])
            page.session.set("df_weibull", res["df_weibull"])
            
            page.session.set("b64_max_annual", res["max_annual_series_b64"])
            page.session.set("b64_acf", res["acf_plot_b64"])
            page.session.set("b64_weibull", res["weibull_plot_b64"])
            page.session.set("b64_dist_comp", res["dist_comparison_b64"])
            
            tbl_homo.controls = [dataframe_to_datatable(res["df_homogeneidad"])]
            tbl_acf.controls = [dataframe_to_datatable(res["df_acf"])]
            tbl_ajustes.controls = [dataframe_to_datatable(res["df_ajustes"])]
            tbl_wei.controls = [dataframe_to_datatable(res["df_weibull"])]
            tbl_mx.controls = [dataframe_to_datatable(res["df_maximos_mensuales"])]
            
            img_max_an.content.src_base64 = res["max_annual_series_b64"]
            img_acf.content.src_base64 = res["acf_plot_b64"]
            img_wei.content.src_base64 = res["weibull_plot_b64"]
            img_dist.content.src_base64 = res["dist_comparison_b64"]
            
            txt_best_fit.value = f"Mejor Ajuste: {res['best_fit_name']}"
            btn_nav_cuenca.disabled = False
            if e: advance_tab(4)
        if e: page.update()

    def run_cuenca(e=None):
        metodo = rg_metodo_r.value or "r_regional"
        val_10, val_25, val_50 = None, None, None
        r_reg = 0.3882

        if metodo == "isoyetas_sct":
            v10 = inp_p1_10.value.strip() if inp_p1_10.value else ""
            v25 = inp_p1_25.value.strip() if inp_p1_25.value else ""
            v50 = inp_p1_50.value.strip() if inp_p1_50.value else ""
            if not v10 or not v25 or not v50:
                page.open(ft.SnackBar(content=ft.Text("⚠️ Ingrese las 3 láminas SCT (Tr 10, 25 y 50 años).", color="white", weight="bold"), bgcolor="#cc0000"))
                page.update(); return
            try:
                val_10, val_25, val_50 = float(v10.replace(",", ".")), float(v25.replace(",", ".")), float(v50.replace(",", "."))
                if val_10 <= 0 or val_25 <= 0 or val_50 <= 0: raise ValueError()
            except Exception:
                page.open(ft.SnackBar(content=ft.Text("⚠️ Las láminas SCT deben ser números positivos.", color="white", weight="bold"), bgcolor="#cc0000"))
                page.update(); return
        else:
            try:
                r_reg = float(inp_r_regional.value.replace(",", ".")) if inp_r_regional.value else 0.3882
                if not (0.10 <= r_reg <= 0.699): raise ValueError()
            except Exception:
                page.open(ft.SnackBar(content=ft.Text("⚠️ Factor R debe estar entre 0.10 y 0.699.", color="white", weight="bold"), bgcolor="#cc0000"))
                page.update(); return

        def _task():
            sid = page.session.get("target_station_id")
            if not sid: return
            bloqueo_ui.visible = True
            loading_text.value = f"Calculando Curvas Chen ({sid})..."
            page.update()
            try:
                df_m = page.session.get("df_maximos_mensuales")
                best_fit = page.session.get("best_fit_name")
                if df_m is None or df_m.empty or not best_fit:
                    raise ValueError("Faltan datos de probabilidad. Ejecute la Pestaña 5 primero.")

                res = analisis_cuenca.run_cuenca_analysis(
                    best_fit_name=best_fit,
                    df_maximos_mensuales=df_m,
                    metodo_r=metodo,
                    r_regional_manual=r_reg,
                    p1_10_manual=val_10,
                    p1_25_manual=val_25,
                    p1_50_manual=val_50,
                    ylim_altura=float(inp_ylim_h.value) if inp_ylim_h.value else None,
                    ylim_intensidad=float(inp_ylim_i.value) if inp_ylim_i.value else None,
                    station_id=sid,
                    aplicar_weiss=True
                )

                if res:
                    page.session.set("log_cuenca", res["log_text"])
                    page.session.set("df_altura", res["df_altura"])
                    page.session.set("df_intensidad", res["df_intensidad"])
                    page.session.set("df_parametros", res.get("df_parametros"))
                    page.session.set("descriptores_chen", res.get("descriptores_chen"))

                    page.session.set("cuenca_plot1_b64", res["plot_1_b64"])
                    page.session.set("cuenca_plot2_b64", res["plot_2_b64"])
                    page.session.set("cuenca_plot3_b64", res["plot_3_b64"])
                    page.session.set("cuenca_plot4_b64", res["plot_4_b64"])

                    try:
                        persistir_estacion_actual()
                    except Exception as ex_p:
                        print(f"Aviso persistencia estación {sid}: {ex_p}")

                    log_cuenca.value = f"```text\n{res['log_text']}\n```"
                    img_c1.content.src_base64 = res["plot_1_b64"]
                    img_c2.content.src_base64 = res["plot_2_b64"]
                    img_c3.content.src_base64 = res["plot_3_b64"]
                    img_c4.content.src_base64 = res["plot_4_b64"]

                    # Limpieza de reset_index() redundante
                    tbl_alt.controls = [dataframe_to_datatable(res["df_altura"], auto_expand=True)]
                    tbl_int.controls = [dataframe_to_datatable(res["df_intensidad"], auto_expand=True)]
                    tbl_parametros.controls = [dataframe_to_datatable(res["df_parametros"], auto_expand=True)]

            except Exception as ex:
                traceback.print_exc()
                page.open(ft.SnackBar(content=ft.Text(f"⚠️ {str(ex)}", color="white", weight="bold"), bgcolor="#cc0000"))
            finally:
                bloqueo_ui.visible = False
                page.update()

        page.run_thread(_task)

    csv_picker.on_result = on_load

    btn_nav_filt = ft.ElevatedButton("Siguiente: Filtros", icon=ft.Icons.ARROW_FORWARD, on_click=lambda e: advance_tab(1), disabled=True)
    btn_nav_stats = ft.ElevatedButton("Ejecutar Estadísticas", icon=ft.Icons.ARROW_FORWARD, on_click=run_stats, disabled=True)
    btn_nav_graph = ft.ElevatedButton("Generar Gráficos", icon=ft.Icons.ARROW_FORWARD, on_click=run_graphs, disabled=True)
    btn_nav_lluvias = ft.ElevatedButton("Iniciar Probabilidades", icon=ft.Icons.ARROW_FORWARD, on_click=run_lluvias, disabled=True)
    btn_nav_cuenca = ft.ElevatedButton("Calcular PDR / IDF", icon=ft.Icons.ARROW_FORWARD, on_click=run_cuenca, disabled=True)

    tab_carga = ft.Tab(text="1. Carga", icon=ft.Icons.INPUT, content=ft.Container(ft.Column([
        ft.Text("Paso 1: Serie Temporal Imputada", color="#00ff41", size=20), 
        ft.Row([ft.ElevatedButton("Cargar CSV Manual", icon=ft.Icons.UPLOAD, on_click=lambda _: csv_picker.pick_files()), csv_path_txt]), 
        data_table_raw, btn_nav_filt
    ]), padding=20))

    tab_filtros = ft.Tab(text="2. Filtros", icon=ft.Icons.FILTER_LIST, content=ft.Container(ft.Column([
        ft.Text("Paso 2: Limpieza Analítica", color="#00ff41", size=20), 
        ft.Row([chk_c1, chk_c2, chk_c3]),
        ft.ElevatedButton("Aplicar Filtros", icon=ft.Icons.FILTER_ALT, on_click=run_filter), 
        data_table_filt, btn_nav_stats
    ]), padding=20))

    tab_stats = ft.Tab(text="3. Stats", icon=ft.Icons.ANALYTICS, content=ft.Container(ft.Column([
        ft.Text("Paso 3: Resumen Estadístico", color="#00ff41", size=20), 
        stats_table_container, btn_nav_graph
    ]), padding=20))

    tab_graficos = ft.Tab(text="4. Gráficos", icon=ft.Icons.IMAGE, content=ft.Container(ft.Column([
        ft.Text("Paso 4: Comportamiento Visual", color="#00ff41", size=20), 
        ft.Text("Histograma"), img_h, ft.ElevatedButton("Guardar", on_click=lambda _: trigger_save_image("b64_hist", "histograma.png")), 
        ft.Text("Series"), img_s, ft.ElevatedButton("Guardar", on_click=lambda _: trigger_save_image("b64_series", "series.png")), 
        ft.Text("Violin"), img_v, ft.ElevatedButton("Guardar", on_click=lambda _: trigger_save_image("b64_violin", "violin.png")), 
        btn_nav_lluvias
    ], scroll=ft.ScrollMode.AUTO), padding=20))

    tab_lluvias = ft.Tab(text="5. Probabilidad", icon=ft.Icons.WATER_DROP, content=ft.Container(ft.Column([
        ft.Text("Paso 5: Ajuste de Funciones", color="#00ff41", size=20), 
        ft.Container(
            content=ft.Column([
                ft.Row([ft.Icon(ft.Icons.CALCULATE, color="#00ff41", size=22), ft.Text("Metodología de Calibración del Cociente R (Chen / Campos-Aranda):", weight="bold", color="white")]),
                rg_metodo_r,
                fila_inputs_regional,
                fila_inputs_sct
            ], spacing=10),
            padding=12, bgcolor="#112211", border_radius=8, border=ft.border.all(1, "#00ff41")
        ),
        ft.Divider(),
        ft.Text("Homogeneidad:", weight="bold"), tbl_homo, ft.ElevatedButton("CSV", on_click=lambda _: trigger_save_csv("df_homogeneidad", "homogeneidad.csv")), ft.Divider(), 
        ft.Text("Series de Valores Máximos Anuales:", weight="bold"), img_max_an, ft.ElevatedButton("IMG", on_click=lambda _: trigger_save_image("b64_max_annual", "max_anuales.png")), ft.Divider(), 
        ft.Text("Autocorrelación (ACF):", weight="bold"), img_acf, ft.ElevatedButton("IMG", on_click=lambda _: trigger_save_image("b64_acf", "acf.png")), tbl_acf, ft.ElevatedButton("CSV", on_click=lambda _: trigger_save_csv("df_acf", "acf.csv")), ft.Divider(), 
        ft.Text("Weibull:", weight="bold"), img_wei, ft.ElevatedButton("IMG", on_click=lambda _: trigger_save_image("b64_weibull", "weibull.png")), tbl_wei, ft.ElevatedButton("CSV", on_click=lambda _: trigger_save_csv("df_weibull", "weibull.csv")), ft.Divider(), 
        ft.Text("Ajuste de Distribuciones:", weight="bold"), img_dist, ft.ElevatedButton("IMG", on_click=lambda _: trigger_save_image("b64_dist_comp", "distribuciones.png")), tbl_ajustes, ft.ElevatedButton("CSV", on_click=lambda _: trigger_save_csv("df_ajustes", "ajustes.csv")), ft.Divider(), 
        ft.Text("Máximos Mensuales:", weight="bold"), tbl_mx, ft.ElevatedButton("CSV", on_click=lambda _: trigger_save_csv("df_maximos_mensuales", "maximos_mensuales.csv")), 
        btn_nav_cuenca
    ], scroll=ft.ScrollMode.AUTO), padding=20))

    tab_cuenca = ft.Tab(text="6. A-D-TR/I-D-TR", icon=ft.Icons.MAP, content=ft.Container(ft.Column([
        ft.Text("Paso 6: Curvas Hidrológicas", color="#00ff41", size=20), txt_best_fit, 
        ft.Row([inp_ylim_h, inp_ylim_i, ft.ElevatedButton("Recalcular Ejes", on_click=run_cuenca)]), 
        ft.Text("PDR:"), img_c1, ft.ElevatedButton("Guardar", on_click=lambda _: trigger_save_image("cuenca_plot1_b64", "pdr.png")), 
        img_c2, ft.ElevatedButton("Guardar Zoom", on_click=lambda _: trigger_save_image("cuenca_plot2_b64", "pdr_zoom.png")), tbl_alt, ft.ElevatedButton("CSV", on_click=lambda _: trigger_save_csv("df_altura", "alturas.csv")), 
        ft.Text("IDF:"), img_c3, ft.ElevatedButton("Guardar", on_click=lambda _: trigger_save_image("cuenca_plot3_b64", "idf.png")), 
        img_c4, ft.ElevatedButton("Guardar Zoom", on_click=lambda _: trigger_save_image("cuenca_plot4_b64", "idf_zoom.png")), tbl_int, ft.ElevatedButton("CSV", on_click=lambda _: trigger_save_csv("df_intensidad", "I-D-TR.csv")), 
        ft.Divider(),
        ft.Text("Parámetros del Modelo y Ajuste Estadístico:", color="#00ff41", weight="bold"), 
        tbl_parametros, 
        ft.ElevatedButton("Exportar Parámetros (CSV)", icon=ft.Icons.DOWNLOAD, on_click=lambda _: trigger_save_csv("df_parametros", "parametros_modelo.csv")),
        ft.Text("Log Detallado:"), log_cuenca
    ], scroll=ft.ScrollMode.AUTO), padding=20))

    tabs = ft.Tabs(selected_index=0, animation_duration=300, tabs=[tab_carga, tab_filtros, tab_stats, tab_graficos, tab_lluvias, tab_cuenca], expand=True)

    def rehidratar_interfaz():
        try:
            df_proc = page.session.get("df_procesado")
            if df_proc is not None:
                csv_path_txt.value = "✅ Datos Activos en Sesión"
                data_table_raw.controls = [ft.Text("Datos Procesados:"), dataframe_to_datatable(df_proc)]
                
                img_h.content.src_base64 = page.session.get("b64_hist") or TRANSPARENT_PIXEL
                img_s.content.src_base64 = page.session.get("b64_series") or TRANSPARENT_PIXEL
                img_v.content.src_base64 = page.session.get("b64_violin") or TRANSPARENT_PIXEL
                img_max_an.content.src_base64 = page.session.get("b64_max_annual") or TRANSPARENT_PIXEL
                img_acf.content.src_base64 = page.session.get("b64_acf") or TRANSPARENT_PIXEL
                img_wei.content.src_base64 = page.session.get("b64_weibull") or TRANSPARENT_PIXEL
                img_dist.content.src_base64 = page.session.get("b64_dist_comp") or TRANSPARENT_PIXEL
                img_c1.content.src_base64 = page.session.get("cuenca_plot1_b64") or TRANSPARENT_PIXEL
                img_c2.content.src_base64 = page.session.get("cuenca_plot2_b64") or TRANSPARENT_PIXEL
                img_c3.content.src_base64 = page.session.get("cuenca_plot3_b64") or TRANSPARENT_PIXEL
                img_c4.content.src_base64 = page.session.get("cuenca_plot4_b64") or TRANSPARENT_PIXEL
                
                df_f = page.session.get("df_filtrado")
                if df_f is not None: data_table_filt.controls = [ft.Text(f"Filtrados: {len(df_f)}"), dataframe_to_datatable(df_f, auto_expand=True)]
                df_st = page.session.get("df_estadisticas")
                if df_st is not None: stats_table_container.controls = [ft.Text("Estadísticas:", color="#00ff41"), stats_to_datatable(df_st, auto_expand=True)]
                df_homo = page.session.get("df_homogeneidad")
                if df_homo is not None: tbl_homo.controls = [dataframe_to_datatable(df_homo, auto_expand=True)]
                df_ajustes = page.session.get("df_ajustes")
                if df_ajustes is not None: tbl_ajustes.controls = [dataframe_to_datatable(df_ajustes, auto_expand=True)]
                df_mx = page.session.get("df_maximos_mensuales")
                if df_mx is not None: tbl_mx.controls = [dataframe_to_datatable(df_mx, auto_expand=True)]
                
                df_alt = page.session.get("df_altura")
                if df_alt is not None: tbl_alt.controls = [dataframe_to_datatable(df_alt, auto_expand=True)]
                df_int = page.session.get("df_intensidad")
                if df_int is not None: tbl_int.controls = [dataframe_to_datatable(df_int, auto_expand=True)]
                df_param = page.session.get("df_parametros")
                if df_param is not None: tbl_parametros.controls = [dataframe_to_datatable(df_param, auto_expand=True)]
                
                metodo_guardado = page.session.get("metodo_r_seleccionado") or "r_regional"
                rg_metodo_r.value = metodo_guardado
                inp_p1_10.value = str(page.session.get("p1_10_manual") or "")
                inp_p1_25.value = str(page.session.get("p1_25_manual") or "")
                inp_p1_50.value = str(page.session.get("p1_50_manual") or "")
                inp_r_regional.value = str(page.session.get("r_regional_manual") or "0.3882")
                on_cambio_metodo_r(None)

                btn_nav_filt.disabled = False
                if df_f is not None: btn_nav_stats.disabled = False
                if df_st is not None: btn_nav_graph.disabled = False
                if page.session.get("b64_hist") is not None: btn_nav_lluvias.disabled = False
                if df_homo is not None: btn_nav_cuenca.disabled = False
                
            page.update()
        except: pass

    def eliminar_estacion_analizada(e):
        sid = dd_estacion_maestra.value
        if not sid:
            page.snack_bar = ft.SnackBar(ft.Text("⚠️ Selecciona una estación para eliminar.", color="white", weight="bold"), bgcolor="#cc0000", open=True)
            page.update(); return
            
        db_curvas = page.session.get("db_curvas_procesadas") or {}
        db_curvas.pop(sid, None)
        page.session.set("db_curvas_procesadas", db_curvas)
        page.session.set("target_station_id", None)
        
        dd_estacion_maestra.value = None
        limpiar_tablas_ui()
        actualizar_dropdown_maestro()
        
        btn_nav_filt.disabled = True
        btn_nav_stats.disabled = True
        btn_nav_graph.disabled = True
        btn_nav_lluvias.disabled = True
        btn_nav_cuenca.disabled = True
        tabs.selected_index = 0
        
        page.snack_bar = ft.SnackBar(ft.Text(f"🗑️ Análisis de la estación {sid} eliminado.", color="white", weight="bold"), bgcolor="#cc0000", open=True)
        page.update()

    async def trigger_hydration():
        await asyncio.sleep(0.1)
        actualizar_dropdown_maestro()
        rehidratar_interfaz()

    page.run_task(trigger_hydration)

    return ft.Column([
        ft.Container(
            content=ft.Row([
                ft.Icon(ft.Icons.ANALYTICS, color="#00ff41"),
                ft.Text("MÓDULO 3: ANÁLISIS CLIMÁTICO", weight="bold"),
                ft.VerticalDivider(),
                dd_estacion_maestra,
                ft.IconButton(ft.Icons.REFRESH, on_click=lambda _: actualizar_dropdown_maestro(), tooltip="Actualizar lista"),
                ft.IconButton(icon=ft.Icons.DELETE_FOREVER, icon_color="#cc0000", tooltip="Eliminar estación actual de la memoria", on_click=eliminar_estacion_analizada)
            ]),
            padding=10, bgcolor="#111111", border=ft.border.all(1, "#333333"), border_radius=5
        ),
        ft.Container(content=tabs, expand=True, padding=10)
    ], expand=True)