# Puts a "GastroAGI Chunk Uploader" icon on the desktop. setup.bat runs this.
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$desktop = [Environment]::GetFolderPath('Desktop')
$shell = New-Object -ComObject WScript.Shell
$link = $shell.CreateShortcut((Join-Path $desktop 'GastroAGI Chunk Uploader.lnk'))
$link.TargetPath = Join-Path $root 'start.bat'
$link.WorkingDirectory = $root
$link.IconLocation = Join-Path $root 'program\app.ico'
$link.Description = 'Start the GastroAGI Chunk Uploader'
$link.Save()
Write-Output "Desktop icon created in $desktop"
