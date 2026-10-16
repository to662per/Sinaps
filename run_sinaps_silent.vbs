' ===================================================================
'  Sinaps: silent autostart launcher for Windows logon.
'  ASCII-only on purpose, see run_sinaps.bat for the reason.
'
'  WHY THIS FILE EXISTS
'
'  The registry entry (HKCU\...\Run) used to point straight at
'  "pythonw.exe -m sinaps --tray". That cannot work. At logon Windows
'  runs the entry with the working directory set to C:\Windows\
'  System32, and "python -m sinaps" looks for the package in the
'  working directory. The project is not installed into site-packages,
'  so the launch ended in "No module named sinaps", exit code 1 - and
'  under pythonw there is no console, so nothing was shown. The
'  assistant simply did not start.
'
'  run_sinaps_silent.bat already does the missing part: "cd /d %~dp0"
'  puts the working directory back to the project folder. Pointing
'  the registry entry at that file is the whole fix. This wrapper
'  exists only so the .bat runs with a hidden window instead of a
'  console flashing on every logon.
'
'  The project folder is taken from this script's own location, so
'  moving the whole folder does not break the entry.
' ===================================================================

Option Explicit

Dim shell, fso, root, launcher

Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

' Path derived from this script's own location: moving the whole folder
' must not break the entry.
root = Left(WScript.ScriptFullName, InStrRev(WScript.ScriptFullName, "\"))
launcher = root & "run_sinaps_silent.bat"

If Not fso.FileExists(launcher) Then
    WScript.Echo "Sinaps autostart: launcher not found: " & launcher
    WScript.Quit 1
End If

' 0 = hidden window, False = do not wait for it to finish.
shell.Run """" & launcher & """", 0, False