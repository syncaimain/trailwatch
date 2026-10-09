# Records the "TrailWatch demo" window (http://localhost:8000/demo) while
# scripts/kill_test.py runs. Captures the screen region under the window, so
# keep it on top while recording.
#   powershell -File scripts/record_demo.ps1 -Seconds 150 -Out media/kill_test.mp4
param([int]$Seconds = 150, [string]$Out = "media/kill_test.mp4")

Add-Type @"
using System;
using System.Runtime.InteropServices;
public class Win {
  [StructLayout(LayoutKind.Sequential)] public struct RECT { public int L, T, R, B; }
  [DllImport("user32.dll")] public static extern IntPtr FindWindow(string c, string t);
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
  [DllImport("user32.dll")] public static extern bool SetWindowPos(IntPtr h, IntPtr after, int x, int y, int cx, int cy, uint f);
  [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
}
"@
[Win]::SetProcessDPIAware() | Out-Null
$h = [Win]::FindWindow([NullString]::Value, "TrailWatch demo")
if ($h -eq [IntPtr]::Zero) { throw "Open http://localhost:8000/demo in its own window first." }
# Pin on top (HWND_TOPMOST) for the recording, then release.
[Win]::SetWindowPos($h, [IntPtr](-1), 0, 0, 0, 0, 0x0003) | Out-Null
[Win]::SetForegroundWindow($h) | Out-Null
Start-Sleep -Milliseconds 500
$r = New-Object Win+RECT
[Win]::GetWindowRect($h, [ref]$r) | Out-Null
# Trim the invisible resize border Windows adds around the window.
$x = $r.L + 8; $y = $r.T; $w = ($r.R - $r.L - 16); $hh = ($r.B - $r.T - 8)
$w -= $w % 2; $hh -= $hh % 2
New-Item -ItemType Directory -Force (Split-Path $Out) | Out-Null
ffmpeg -y -loglevel error -f gdigrab -framerate 20 -offset_x $x -offset_y $y -video_size "${w}x${hh}" -draw_mouse 0 -t $Seconds -i desktop -c:v libx264 -preset veryfast -crf 23 -pix_fmt yuv420p $Out
[Win]::SetWindowPos($h, [IntPtr](-2), 0, 0, 0, 0, 0x0003) | Out-Null
Write-Output "Saved $Out ($w x $hh)"
