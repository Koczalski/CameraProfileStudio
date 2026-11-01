param([Parameter(Mandatory=$true)][string]$Executable,
      [Parameter(Mandatory=$true)][string]$EvidenceDirectory,
      [ValidateRange(2,7)][int]$NavigationCount = 2,
      [string]$RequiredWorkspaceLabel = '')
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Drawing
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
if (!('CpsWindowCapture' -as [type])) {
    Add-Type @'
using System;
using System.Runtime.InteropServices;
public static class CpsWindowCapture {
    [StructLayout(LayoutKind.Sequential)] public struct Rect { public int Left, Top, Right, Bottom; }
    [StructLayout(LayoutKind.Sequential)] public struct Point { public int X, Y; }
    [DllImport("user32.dll")] public static extern bool ScreenToClient(IntPtr h, ref Point p);
    [DllImport("user32.dll")] public static extern bool GetClientRect(IntPtr h, out Rect r);
    [DllImport("user32.dll")] public static extern IntPtr SendMessageTimeout(IntPtr h, uint msg, UIntPtr w, IntPtr l, uint flags, uint timeout, out UIntPtr result);
    [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out Rect r);
    [DllImport("user32.dll")] public static extern bool PrintWindow(IntPtr h, IntPtr dc, uint flags);
    [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
}
'@
}
New-Item -ItemType Directory -Force $EvidenceDirectory | Out-Null
$oldPlatform = $env:QT_QPA_PLATFORM
$env:QT_QPA_PLATFORM = 'windows'
$p = $null
$result = [ordered]@{platform='windows'; window_only_capture=$true; desktop_capture=$false;
    interaction='NOT VERIFIED'; screenshot='NOT CAPTURED'; clean_vm=$false}
try {
    $p = Start-Process $Executable -WorkingDirectory $EvidenceDirectory -PassThru -RedirectStandardOutput "$EvidenceDirectory/native-gui.stdout.txt" -RedirectStandardError "$EvidenceDirectory/native-gui.stderr.txt"
    $deadline = [DateTime]::UtcNow.AddSeconds(30)
    do {
        Start-Sleep -Milliseconds 250
        $p.Refresh()
        if ($p.HasExited) { throw "Native GUI exited: $($p.ExitCode)" }
    } while ($p.MainWindowHandle -eq [IntPtr]::Zero -and [DateTime]::UtcNow -lt $deadline)
    Start-Sleep -Seconds 2
    $p.Refresh()
    $handle = $p.MainWindowHandle
    if ($handle -eq [IntPtr]::Zero) { throw 'No native application window within 30 seconds' }
    [uint32]$owner = 0
    [CpsWindowCapture]::GetWindowThreadProcessId($handle, [ref]$owner) | Out-Null
    if ($owner -ne $p.Id) { throw 'Refusing to capture a window belonging to another process' }
    $result.window_title = $p.MainWindowTitle
    $result.process_id = $p.Id
    # UI Automation targets this application's tree only; never sends desktop-global keys.
    $element = [System.Windows.Automation.AutomationElement]::FromHandle($handle)
    function Find-VisiblePageText([string]$Name) {
        $condition = New-Object System.Windows.Automation.PropertyCondition(
            [System.Windows.Automation.AutomationElement]::NameProperty, $Name)
        foreach ($candidate in $element.FindAll([System.Windows.Automation.TreeScope]::Descendants, $condition)) {
            $current = $candidate.Current
            if ($current.ProcessId -eq $p.Id -and !$current.IsOffscreen -and
                $current.BoundingRectangle.Width -gt 0 -and $current.BoundingRectangle.Height -gt 0 -and
                $current.ControlType -in @([System.Windows.Automation.ControlType]::Text,
                    [System.Windows.Automation.ControlType]::Group, [System.Windows.Automation.ControlType]::Button,
                    [System.Windows.Automation.ControlType]::Table, [System.Windows.Automation.ControlType]::DataGrid)) {
                return $true
            }
        }
        return $false
    }
    function Invoke-WorkspaceMenu([string]$Name) {
        $condition = New-Object System.Windows.Automation.PropertyCondition(
            [System.Windows.Automation.AutomationElement]::NameProperty, 'View')
        $expanded = $false
        foreach ($view in $element.FindAll([System.Windows.Automation.TreeScope]::Descendants, $condition)) {
            if ($view.Current.ProcessId -ne $p.Id -or $view.Current.ControlType -ne [System.Windows.Automation.ControlType]::MenuItem) { continue }
            $pattern = $null
            if ($view.TryGetCurrentPattern([System.Windows.Automation.ExpandCollapsePattern]::Pattern, [ref]$pattern)) {
                if ($pattern.Current.ExpandCollapseState -ne [System.Windows.Automation.ExpandCollapseState]::Collapsed) {
                    $pattern.Collapse()
                    Start-Sleep -Milliseconds 200
                }
                $pattern.Expand(); $expanded=$true; break
            }
            if ($view.TryGetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern, [ref]$pattern)) {
                $pattern.Invoke(); $expanded=$true; break
            }
        }
        if (!$expanded) { throw 'Process-scoped View menu cannot be expanded' }
        Start-Sleep -Milliseconds 250
        $processCondition = New-Object System.Windows.Automation.PropertyCondition(
            [System.Windows.Automation.AutomationElement]::ProcessIdProperty, $p.Id)
        $nameCondition = New-Object System.Windows.Automation.PropertyCondition(
            [System.Windows.Automation.AutomationElement]::NameProperty, $Name)
        # Qt popup menus can be separate windows; wait for their asynchronous UIA publication.
        $menuDeadline=[DateTime]::UtcNow.AddSeconds(5)
        do {
        foreach ($window in [System.Windows.Automation.AutomationElement]::RootElement.FindAll(
                [System.Windows.Automation.TreeScope]::Children, $processCondition)) {
            foreach ($action in $window.FindAll([System.Windows.Automation.TreeScope]::Descendants, $nameCondition)) {
                if ($action.Current.ProcessId -ne $p.Id -or $action.Current.ControlType -ne [System.Windows.Automation.ControlType]::MenuItem) { continue }
                $invoke=$null
                if ($action.TryGetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern, [ref]$invoke)) {
                    $invoke.Invoke()
                    $collapse=$null
                    if ($view.TryGetCurrentPattern([System.Windows.Automation.ExpandCollapsePattern]::Pattern, [ref]$collapse)) {
                        $collapse.Collapse()
                        Start-Sleep -Milliseconds 200
                    }
                    return
                }
            }
        }
        Start-Sleep -Milliseconds 100
        } while ([DateTime]::UtcNow -lt $menuDeadline)
        $debug=@()
        foreach ($window in [System.Windows.Automation.AutomationElement]::RootElement.FindAll([System.Windows.Automation.TreeScope]::Children, $processCondition)) {
            foreach ($node in $window.FindAll([System.Windows.Automation.TreeScope]::Descendants, [System.Windows.Automation.Condition]::TrueCondition)) {
                if ($node.Current.ControlType -eq [System.Windows.Automation.ControlType]::MenuItem) {
                    $debug += @{name=$node.Current.Name;pid=$node.Current.ProcessId;offscreen=$node.Current.IsOffscreen;patterns=@($node.GetSupportedPatterns() | ForEach-Object { $_.ProgrammaticName })}
                }
            }
        }
        $debug | ConvertTo-Json -Depth 5 | Set-Content "$EvidenceDirectory/menu-debug.json"
        throw "Process-scoped workspace QAction unavailable: $Name"
    }
    function Invoke-WorkspaceLocalClick($Item) {
        [uint32]$targetOwner=0
        [CpsWindowCapture]::GetWindowThreadProcessId($handle, [ref]$targetOwner) | Out-Null
        if ($targetOwner -ne $p.Id -or $Item.Current.ProcessId -ne $p.Id) { throw 'Refusing cross-process navigation click' }
        $bounds=$Item.Current.BoundingRectangle
        if ($Item.Current.IsOffscreen -or $bounds.Width -le 0 -or $bounds.Height -le 0) { throw 'Navigation item is not visibly clickable' }
        $point=New-Object CpsWindowCapture+Point
        $point.X=[int]($bounds.X+$bounds.Width/2); $point.Y=[int]($bounds.Y+$bounds.Height/2)
        if (![CpsWindowCapture]::ScreenToClient($handle,[ref]$point)) { throw 'ScreenToClient failed' }
        $client=New-Object CpsWindowCapture+Rect
        if (![CpsWindowCapture]::GetClientRect($handle,[ref]$client) -or $point.X -lt 0 -or $point.Y -lt 0 -or
            $point.X -ge $client.Right -or $point.Y -ge $client.Bottom) { throw 'Navigation click is outside the verified client window' }
        $position=[IntPtr](($point.Y -shl 16) -bor ($point.X -band 65535))
        [UIntPtr]$messageResult=[UIntPtr]::Zero
        # Parent-authorized fallback: only this PID-owned original HWND; no global input API.
        foreach ($message in @(0x001F,0x0201,0x0202)) {
            $button=[UIntPtr]::Zero
            if ($message -eq 0x0201) { $button=[UIntPtr]::new([uint32]1) }
            if ([CpsWindowCapture]::SendMessageTimeout($handle,[uint32]$message,$button,$position,2,2000,[ref]$messageResult) -eq [IntPtr]::Zero) {
                throw 'Window-local navigation message timed out'
            }
        }
        return @{hwnd=$handle.ToInt64();pid=$targetOwner;client_x=$point.X;client_y=$point.Y;messages=@('WM_CANCELMODE','WM_LBUTTONDOWN','WM_LBUTTONUP')}
    }
    $navigationCondition = New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::NameProperty, 'Workspaces')
    $navigation=$null
    foreach ($candidate in $element.FindAll([System.Windows.Automation.TreeScope]::Descendants, $navigationCondition)) {
        if ($candidate.Current.ProcessId -eq $p.Id -and $candidate.Current.ControlType -eq [System.Windows.Automation.ControlType]::List) {
            $navigation=$candidate; break
        }
    }
    if (!$navigation) { throw 'English workspace navigation list unavailable' }
    $condition = New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
        [System.Windows.Automation.ControlType]::ListItem)
    $items=$navigation.FindAll([System.Windows.Automation.TreeScope]::Descendants, $condition)
    $result.accessible_navigation_candidates=$items.Count
    if ($items.Count -lt $NavigationCount) { throw 'Workspace navigation item count is too small' }
    $selected=@(); $activation=@()
    foreach ($item in $items) {
        $name=$item.Current.Name
        if ($item.Current.ProcessId -ne $p.Id) { throw 'Navigation element belongs to another process' }
        $selection=$null
        if (!$item.TryGetCurrentPattern([System.Windows.Automation.SelectionItemPattern]::Pattern, [ref]$selection)) {
            throw "Workspace selection pattern missing: $name"
        }
        # Qt currentIndex (focus), not selection highlighting alone, activates the page.
        $item.SetFocus()
        $selection.Select()
        Start-Sleep -Milliseconds 500
        $method='UIA SetFocus + Select'
        $menuFailure=$null; $localClick=$null
        if (!(Find-VisiblePageText $name)) {
            try { Invoke-WorkspaceMenu $name }
            catch { $menuFailure=$_.Exception.Message }
            Start-Sleep -Milliseconds 500
            $method='process-scoped View QAction Invoke'
        }
        if (!(Find-VisiblePageText $name)) {
            $localClick=Invoke-WorkspaceLocalClick $item
            Start-Sleep -Milliseconds 500
            $method='verified HWND-local navigation click'
        }
        if (!(Find-VisiblePageText $name)) { throw "Workspace content did not activate: $name" }
        $selected += $name
        $activation += @{workspace=$name;method=$method;visible_page_title=$true;uia_menu_failure=$menuFailure;window_local_click=$localClick}
        if ($selected.Count -ge $NavigationCount) { break }
    }
    $result.selected_items=$selected
    $result.activation=$activation
    if ($RequiredWorkspaceLabel) {
        if ($selected[-1] -ne $RequiredWorkspaceLabel) { throw 'Required seventh workspace was not the final activated page' }
        $requiredContent=@('Reference images (analysed)', '1. Look amounts', 'Analyse references (F5)')
        foreach ($name in $requiredContent) {
            if (!(Find-VisiblePageText $name)) { throw "Image Look Creator content missing or hidden: $name" }
        }
        $result.required_workspace=$RequiredWorkspaceLabel
        $result.visible_content_assertions=$requiredContent
        $result.packaged_image_workflow='NOT VERIFIED: activated workspace only; no packaged GUI reference analysis/export workflow'
    }
    $result.interaction="PASS: activated $NavigationCount actual workspace pages and verified visible content"
    # Keep the original main-window handle: a Qt menu popup can temporarily become MainWindowHandle.
    $p.Refresh()
    [CpsWindowCapture]::GetWindowThreadProcessId($handle, [ref]$owner) | Out-Null
    if ($owner -ne $p.Id -or $handle -eq [IntPtr]::Zero) { throw 'Native application window disappeared before capture' }
    $rect = New-Object CpsWindowCapture+Rect
    if (![CpsWindowCapture]::GetWindowRect($handle,[ref]$rect)) { throw 'Cannot determine target window dimensions' }
    $width=$rect.Right-$rect.Left; $height=$rect.Bottom-$rect.Top
    if ($width -lt 50 -or $height -lt 50) { throw 'Invalid target window dimensions' }
    $bitmap = New-Object System.Drawing.Bitmap($width,$height)
    $graphics = [System.Drawing.Graphics]::FromImage($bitmap)
    $dc = $graphics.GetHdc()
    try { $captured=[CpsWindowCapture]::PrintWindow($handle,$dc,2) }
    finally { $graphics.ReleaseHdc($dc); $graphics.Dispose() }
    try {
        if (!$captured) { throw 'PrintWindow failed; no desktop-capture fallback is permitted' }
        $imagePath=Join-Path $EvidenceDirectory 'native-app-window.png'
        $bitmap.Save($imagePath,[System.Drawing.Imaging.ImageFormat]::Png)
        $result.screenshot=$imagePath
        $result.visual_review='REQUIRED: inspect PNG for rendering/content correctness; PrintWindow success alone is insufficient'
    } finally { $bitmap.Dispose() }
} finally {
    if ($p -and !$p.HasExited) { Stop-Process -Id $p.Id }
    $env:QT_QPA_PLATFORM=$oldPlatform
    $result | ConvertTo-Json -Depth 5 | Set-Content -Encoding utf8 "$EvidenceDirectory/native-gui-results.json"
}
