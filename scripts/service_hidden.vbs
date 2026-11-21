' bili monitor hidden launcher (self-locating: this file lives in scripts\, project root is two levels up)
Set fso = CreateObject("Scripting.FileSystemObject")
root = fso.GetParentFolderName(fso.GetParentFolderName(WScript.ScriptFullName))
Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = root
sh.Run """" & root & "\scripts\service_start.cmd" & """", 0, False
