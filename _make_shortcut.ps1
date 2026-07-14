# Creates "Breakdown Studio.lnk" that launches the GUI directly with pythonw (no .bat needed).
$dir = Split-Path -Parent $MyInvocation.MyCommand.Path
# Pick an interpreter: installed bs_env first, then config worker_python, then ShotGrid Python.
$pyw = $null
$cand = Join-Path $dir 'bs_env\Scripts\pythonw.exe'
if (Test-Path $cand) { $pyw = $cand }
if (-not $pyw) {
  try { $cfg = Get-Content -Raw (Join-Path $dir 'config.json') | ConvertFrom-Json
        $p = $cfg.worker_python -replace 'python\.exe$','pythonw.exe'
        if (Test-Path $p) { $pyw = $p } } catch {}
}
if (-not $pyw) { $pyw = 'C:\Program Files\Shotgun\Python3\pythonw.exe' }
$ws = New-Object -ComObject WScript.Shell
$lnk = $ws.CreateShortcut((Join-Path $dir 'Breakdown Studio.lnk'))
$lnk.TargetPath = $pyw
$lnk.Arguments = '"breakdown_studio.py"'
$lnk.WorkingDirectory = $dir
$lnk.IconLocation = "$pyw,0"
$lnk.Description = 'Breakdown Studio'
$lnk.Save()
Write-Output "created: $(Join-Path $dir 'Breakdown Studio.lnk')"
Write-Output "target : $pyw"
