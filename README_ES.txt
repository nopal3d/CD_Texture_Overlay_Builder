CD Texture Overlay Builder v1.2.4

Herramienta local/offline para crear e instalar overlays PAZ/PAMT de texturas para Crimson Desert.

Cambios de v1.2.4:
- Se agrega BUILD_FAST_HASH_HELPER.bat como alias de compatibilidad para compilar el helper nativo rápido.

- Build modo onedir de PyInstaller, no onefile.
- No usa UPX.
- Se elimina el helper C# del flujo normal.
- Usa helper nativo C si existe.
- Usa fallback Python si no existe helper nativo.
- Oculta la ventana CMD del helper al ejecutarlo desde la UI.
- Incluye README_SECURITY.txt.
- Incluye THIRD_PARTY_LICENSES.txt.
- El build genera SHA256SUMS.txt para el exe final.

Uso recomendado:
1. Instala otros mods primero con tu manager favorito.
2. Ejecuta CD_Texture_Overlay_Builder.exe.
3. Auto detecta o selecciona la carpeta del juego.
4. Selecciona la carpeta DDS.
5. Selecciona 0000 - Object textures.
6. Presiona Build / Apply Overlay.

Si después quieres instalar más mods:
1. Presiona Smart Hold Overlays.
2. Instala más mods con otro manager.
3. Presiona Release Hold + Reapply.

Para compilar:
1. Instala Visual Studio con Desktop development with C++.
2. Ejecuta BUILD_NATIVE_C_HELPER.bat.
3. Ejecuta TEST_FAST_HASH_HELPER.bat.
4. Ejecuta BUILD_WINDOWS_EXE.bat.
