' 静默启动多屏直播监控台：不弹控制台窗口，日志写到 .research\run.log
Set fso = CreateObject("Scripting.FileSystemObject")
dir = fso.GetParentFolderName(WScript.ScriptFullName)
Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = dir
sh.Run """" & dir & "\start_livedeck.bat""", 0, False
