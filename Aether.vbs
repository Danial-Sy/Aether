Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
Set env = sh.Environment("Process")
sh.CurrentDirectory = fso.GetParentFolderName(WScript.ScriptFullName)
env("PYTHONIOENCODING") = "utf-8"
env("PYTHONUTF8") = "1"
If Len(env("OLLAMA_HOST")) = 0 Then env("OLLAMA_HOST") = "127.0.0.1:11434"
If Len(env("OLLAMA_KV_CACHE_TYPE")) = 0 Then env("OLLAMA_KV_CACHE_TYPE") = "q8_0"
If Len(env("OLLAMA_FLASH_ATTENTION")) = 0 Then env("OLLAMA_FLASH_ATTENTION") = "1"
If Len(env("OLLAMA_NUM_PARALLEL")) = 0 Then env("OLLAMA_NUM_PARALLEL") = "1"
If Len(env("GGML_CUDA_ENABLE_UNIFIED_MEMORY")) = 0 Then env("GGML_CUDA_ENABLE_UNIFIED_MEMORY") = "1"
pyw = sh.CurrentDirectory & "\.venv\Scripts\pythonw.exe"
desk = sh.CurrentDirectory & "\desktop.py"
If Not fso.FileExists(pyw) Or Not fso.FileExists(desk) Then
  MsgBox "Aether is not fully installed. Run Install-Aether.bat, then try the shortcut again.", 16, "Aether"
  WScript.Quit 1
End If
sh.Run """" & pyw & """ """ & desk & """", 0, False
