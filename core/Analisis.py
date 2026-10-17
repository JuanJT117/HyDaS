import matplotlib
matplotlib.use("agg") 

import pandas as pd
import matplotlib.pyplot as plt
import seaborn as sns
import os
import traceback 
from tabulate import tabulate
import io
import base64

# --- Funciones de Datos ---

def procesar_datos(ruta_csv: str):
    try:
        df = pd.read_csv(ruta_csv, parse_dates=['FECHA'])
        df.set_index('FECHA', inplace=True)
        
        mapa_nombres = {}
        if 'PRECIP' in df.columns: mapa_nombres['PRECIP'] = 'PRECIP_imputado'
        if 'PRECIP_ORIGINAL' in df.columns: mapa_nombres['PRECIP_ORIGINAL'] = 'PRECIP_original'
        df.rename(columns=mapa_nombres, inplace=True)
        
        if 'PRECIP_original' not in df.columns and 'PRECIP_imputado' in df.columns:
            df['PRECIP_original'] = df['PRECIP_imputado']
            
        columnas_a_eliminar = ['lat', 'lon', 'station_id']
        df.drop(columns=[c for c in columnas_a_eliminar if c in df.columns], inplace=True)
        return df
    except Exception as e:
        return None

def filtrar_datos(df: pd.DataFrame, usar_c1=True, usar_c2=True, usar_c3=True):
    if df is None or df.empty: return pd.DataFrame() 
    try:
        df_proc = df.copy()
        cols_precip_n = [col for col in df_proc.columns if (col.startswith('PRECIP_') or col.startswith('N_')) and col not in ['PRECIP_imputado', 'PRECIP_original']]
        
        if usar_c3:
            if 'Año' not in df_proc.columns: df_proc['Año'] = df_proc.index.year
            if 'Semana' not in df_proc.columns: df_proc['Semana'] = df_proc.index.isocalendar().week
            if cols_precip_n:
                maximos_por_año_semana = df_proc.groupby(['Año', 'Semana'])[cols_precip_n].max().max(axis=1)
            else:
                maximos_por_año_semana = pd.Series(dtype=float)

        mask_eliminar = pd.Series(False, index=df_proc.index)
        
        if usar_c1:
            if cols_precip_n:
                condicion_1 = df_proc['PRECIP_original'].isnull() & df_proc[cols_precip_n].isnull().all(axis=1)
                mask_eliminar = mask_eliminar | condicion_1
            else:
                mask_eliminar = mask_eliminar | df_proc['PRECIP_original'].isnull()

        if usar_c2 and cols_precip_n:
            condicion_2 = (df_proc['PRECIP_original'].isnull()) & (df_proc[cols_precip_n].notnull().sum(axis=1) < 5)
            mask_eliminar = mask_eliminar | condicion_2

        if usar_c3:
            def get_max(row): return maximos_por_año_semana.get((row['Año'], row['Semana']), 0)
            map_max = df_proc.apply(get_max, axis=1)
            condicion_3 = df_proc['PRECIP_original'].isnull() & (df_proc['PRECIP_imputado'] > map_max)
            mask_eliminar = mask_eliminar | condicion_3

        return df_proc[~mask_eliminar]
    except Exception as e:
        return None

def analizar_estadisticas(df_filtrado: pd.DataFrame):
    if df_filtrado is None or df_filtrado.empty: return None
    try:
        stats_imputado = df_filtrado['PRECIP_imputado'].describe()
        stats_original = df_filtrado['PRECIP_original'].describe()
        stats_imputado['skewness'] = df_filtrado['PRECIP_imputado'].skew()
        stats_imputado['kurtosis'] = df_filtrado['PRECIP_imputado'].kurtosis()
        stats_original['skewness'] = df_filtrado['PRECIP_original'].skew()
        stats_original['kurtosis'] = df_filtrado['PRECIP_original'].kurtosis()
        
        return pd.DataFrame({'Imputado': stats_imputado, 'Original': stats_original}).round(2) 
    except: return None

# --- Funciones de Gráficos (ESTILO ACADÉMICO) ---

def _crear_texto_stats(stats_table, col):
    try:
        s = stats_table[col]
        return f"Media: {s['mean']:.2f}\nStd: {s['std']:.2f}\nMax: {s['max']:.2f}\nSkew: {s['skewness']:.2f}"
    except: return ""

def crear_grafico_histogramas(df_filtrado, stats_table, station_id=""):
    try:
        # FORZAR ESTILO ACADÉMICO (Blanco/Negro)
        with plt.style.context('default'):
            fig, axes = plt.subplots(1, 2, figsize=(12, 6), sharey=True)
            
            # Imputado
            sns.histplot(df_filtrado['PRECIP_imputado'].dropna(), bins=50, ax=axes[0], kde=False, color='#4682B4', edgecolor='black', linewidth=0.5)
            titulo_imp = f'Precipitación Imputada - {station_id}' if station_id else 'Precipitación Imputada'
            axes[0].set_title(titulo_imp, fontweight='bold', fontsize=12)
            axes[0].set_yscale('log')
            axes[0].set_xlabel('Precipitación (mm)')
            axes[0].set_ylabel('Frecuencia (Log)')
            axes[0].grid(True, which='both', linestyle='--', alpha=0.5)
            
            txt_imp = _crear_texto_stats(stats_table, 'Imputado')
            axes[0].text(0.95, 0.95, txt_imp, transform=axes[0].transAxes, va='top', ha='right', 
                         bbox=dict(boxstyle='round', facecolor='white', edgecolor='black', alpha=0.9))

            # Original
            sns.histplot(df_filtrado['PRECIP_original'].dropna(), bins=50, ax=axes[1], kde=False, color='#FA8072', edgecolor='black', linewidth=0.5)
            titulo_ori = f'Precipitación Original - {station_id}' if station_id else 'Precipitación Original'
            axes[1].set_title(titulo_ori, fontweight='bold', fontsize=12)
            axes[1].set_yscale('log')
            axes[1].set_xlabel('Precipitación (mm)')
            axes[1].grid(True, which='both', linestyle='--', alpha=0.5)
            
            txt_orig = _crear_texto_stats(stats_table, 'Original')
            axes[1].text(0.95, 0.95, txt_orig, transform=axes[1].transAxes, va='top', ha='right', 
                         bbox=dict(boxstyle='round', facecolor='white', edgecolor='black', alpha=0.9))

            plt.tight_layout()
            buf = io.BytesIO(); fig.savefig(buf, format='png', dpi=200, bbox_inches='tight'); plt.close(fig)
            return base64.b64encode(buf.getvalue()).decode('utf-8')
    except: return None

def crear_grafico_series_temporales(df_filtrado, station_id=""):
    try:
        with plt.style.context('default'):
            fig, ax = plt.subplots(figsize=(12, 6))
            # Líneas finas y colores sobrios
            df_filtrado['PRECIP_imputado'].plot(ax=ax, label='Imputado', color='#1f77b4', alpha=0.6, linewidth=0.8)
            df_filtrado['PRECIP_original'].plot(ax=ax, label='Original', color='black', alpha=0.8, linewidth=0.8, linestyle='-')
            
            titulo_serie = f'Serie Temporal de Precipitación - {station_id}' if station_id else 'Serie Temporal de Precipitación'
            ax.set_title(titulo_serie, fontweight='bold', fontsize=14)
            ax.set_xlabel('Fecha')
            ax.set_ylabel('Precipitación (mm)')
            ax.legend(frameon=True, fancybox=False, edgecolor='black')
            ax.grid(True, linestyle='--', alpha=0.5)
            
            plt.tight_layout()
            buf = io.BytesIO(); fig.savefig(buf, format='png', dpi=200, bbox_inches='tight'); plt.close(fig)
            return base64.b64encode(buf.getvalue()).decode('utf-8')
    except: return None

def crear_grafico_violin(df_filtrado, station_id=""):
    try:
        df_p = df_filtrado.copy()
        if 'Mes' not in df_p.columns: df_p['Mes'] = df_p.index.month
        df_m = df_p.reset_index().melt(id_vars=['Mes'], value_vars=['PRECIP_imputado', 'PRECIP_original'], var_name='Tipo', value_name='PRECIP')
        
        with plt.style.context('default'):
            fig, ax = plt.subplots(figsize=(12, 6))
            sns.violinplot(x='Mes', y='PRECIP', hue='Tipo', data=df_m, split=True, inner='quartile', palette=['#4682B4', '#FA8072'], ax=ax, linewidth=0.8)
            
            ax.set_yscale('symlog', linthresh=0.1)
            ax.set_ylim(bottom=0.01)
            titulo_violin = f'Distribución Mensual (Violin Plot) - {station_id}' if station_id else 'Distribución Mensual (Violin Plot)'
            ax.set_title(titulo_violin, fontweight='bold', fontsize=14)
            ax.set_ylabel('Precipitación (Escala Log)')
            ax.grid(True, linestyle='--', alpha=0.5)
            ax.legend(frameon=True, fancybox=False, edgecolor='black')
            
            plt.tight_layout()
            buf = io.BytesIO(); fig.savefig(buf, format='png', dpi=200, bbox_inches='tight'); plt.close(fig)
            return base64.b64encode(buf.getvalue()).decode('utf-8')
    except: return None
            
def generar_graficos(df_filtrado, stats_table, station_id=""):
    if df_filtrado is None: return None
    return {
        "hist": crear_grafico_histogramas(df_filtrado, stats_table, station_id),
        "series": crear_grafico_series_temporales(df_filtrado, station_id),
        "violin": crear_grafico_violin(df_filtrado, station_id)
    }