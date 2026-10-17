import os
import sys
import subprocess

print("=========================================================")
print("🚀 Iniciando Motor de Empaquetado Cross-Platform (HyDaS)")
print("=========================================================")

# 1. Configuración de Hooks y Binarios Dinámicos
hidden_imports = [
    "--hidden-import=rasterio",
    "--hidden-import=richdem",
    "--hidden-import=fiona",
    "--hidden-import=shapely",
    "--hidden-import=skimage"
]

add_binaries = []

if sys.platform == "win32":
    print("🖥️  Plataforma detectada: WINDOWS")
    # En Windows, empaquetamos dinámicamente las .dll del entorno activo
    rasterio_dll_path = os.path.join(sys.prefix, "Lib", "site-packages", "rasterio", "*.dll")
    add_binaries.append(f"--add-binary={rasterio_dll_path};rasterio")
elif sys.platform.startswith("linux"):
    print("🐧 Plataforma detectada: LINUX")
    print("⚠️  ADVERTENCIA GLIBC: Para máxima compatibilidad hacia atrás (forward compatibility),")
    print("   este script debe ejecutarse en un contenedor 'manylinux' o Ubuntu 20.04 LTS.")
    # En Linux, forzamos el empaquetado de librerías dinámicas .so
    add_binaries.append("--add-binary=/usr/local/lib/python3.11/site-packages/rasterio/*.so:rasterio")
    add_binaries.append("--add-binary=/usr/local/lib/python3.11/site-packages/richdem/*.so:richdem")

print("\n⚙️ Comando recomendado para flet pack / pyinstaller:")
flet_command = f"flet pack main.py --name HyDaS_v11 {' '.join(hidden_imports)} {' '.join(add_binaries)}"
print(f"{flet_command}\n")


# 2. Generación del Instalador Final (Dependiente del OS)
if sys.platform == "win32":
    print("📦 Generando Instalador NSIS para Windows...")
    nsi_script = """
!define APPNAME "HyDaS"
!define APPVERSION "11"
!define EXECUTABLE "HyDaS_v11.exe"

Name "${APPNAME} v${APPVERSION}"
OutFile "HyDaS_11.exe"
InstallDir "$PROGRAMFILES64\\${APPNAME}"

RequestExecutionLevel admin

Section "Instalar"
    SetOutPath "$INSTDIR"
    File "dist\\${EXECUTABLE}"
    WriteUninstaller "$INSTDIR\\uninstall.exe"
    CreateShortcut "$DESKTOP\\${APPNAME}.lnk" "$INSTDIR\\${EXECUTABLE}" "" "$INSTDIR\\${EXECUTABLE}" 0
    CreateDirectory "$SMPROGRAMS\\${APPNAME}"
    CreateShortcut "$SMPROGRAMS\\${APPNAME}\\${APPNAME}.lnk" "$INSTDIR\\${EXECUTABLE}" "" "$INSTDIR\\${EXECUTABLE}" 0
    CreateShortcut "$SMPROGRAMS\\${APPNAME}\\Desinstalar.lnk" "$INSTDIR\\uninstall.exe"
    WriteRegStr HKLM "Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\${APPNAME}" "DisplayName" "${APPNAME}"
    WriteRegStr HKLM "Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\${APPNAME}" "UninstallString" '"$INSTDIR\\uninstall.exe"'
    WriteRegStr HKLM "Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\${APPNAME}" "DisplayIcon" '"$INSTDIR\\${EXECUTABLE}"'
SectionEnd

Section "Uninstall"
    Delete "$INSTDIR\\${EXECUTABLE}"
    Delete "$INSTDIR\\uninstall.exe"
    RMDir "$INSTDIR"
    Delete "$DESKTOP\\${APPNAME}.lnk"
    Delete "$SMPROGRAMS\\${APPNAME}\\${APPNAME}.lnk"
    Delete "$SMPROGRAMS\\${APPNAME}\\Desinstalar.lnk"
    RMDir "$SMPROGRAMS\\${APPNAME}"
    DeleteRegKey HKLM "Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\${APPNAME}"
SectionEnd
"""
    with open("setup.nsi", "w", encoding="utf-8") as f:
        f.write(nsi_script)

    nsis_path = r"C:\Program Files (x86)\NSIS\makensis.exe"
    try:
        subprocess.run([nsis_path, "setup.nsi"], check=True)
        print("✅ ¡Instalador 'HyDaS_11.exe' creado con éxito!")
    except Exception as e:
        print(f"❌ Error al compilar NSIS: {e}")

elif sys.platform.startswith("linux"):
    print("📦 Para Linux, distribuye la carpeta 'dist/HyDaS_v11' generada por flet pack.")
    print("   Puedes comprimirla con: tar -czvf HyDaS_v11_linux.tar.gz -C dist HyDaS_v11")