; NSIS hooks for the Pebble Companion Chat installer.
;
; The updater and the uninstaller both kill processes by name, and they only
; know the shell's own binary. The Python backend is spawned by the shell and
; can outlive it whenever the shell is terminated hard - which is exactly what
; an installer does. A surviving backend keeps its executable mapped, so
; replacing pebble-backend-x86_64-pc-windows-msvc.exe fails with
; "Error opening file for writing" and the install stops half-way.
;
; lib.rs prevents that survivor with a job object, but the hook is still needed:
; it covers builds made before that fix, and it makes a manual reinstall work
; while the app is running.
;
; nsExec ships with the NSIS distribution the bundler uses and taskkill is part
; of Windows, so nothing here needs a third-party plugin.

!macro NSIS_HOOK_PREINSTALL
  DetailPrint "Stopping any running Pebble backend..."
  nsExec::ExecToLog 'taskkill /F /T /IM pebble-backend-x86_64-pc-windows-msvc.exe'
  Pop $0
  ; taskkill returns once the kill is issued; Windows still has to unmap the
  ; image and drop the file lock before the copy can take the name.
  Sleep 800
!macroend

!macro NSIS_HOOK_PREUNINSTALL
  DetailPrint "Stopping any running Pebble backend..."
  nsExec::ExecToLog 'taskkill /F /T /IM pebble-backend-x86_64-pc-windows-msvc.exe'
  Pop $0
  Sleep 800
!macroend
