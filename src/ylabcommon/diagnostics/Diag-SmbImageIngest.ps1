#Requires -Version 5.1
<#
================================================================================
 Diag-SmbImageIngest.ps1
 SMB 共有上の TIFF 読み込みが極端に遅い原因を切り分けるための「計測専用」スクリプト
 (measure-only: システム設定は一切変更しません / no system setting is modified)
================================================================================

 背景 (measured facts):
   - 3001 枚 / 1024x1024 uint16 / 約 2 MB / 1 ディレクトリ
   - Path.resolve() x3001 + os.path.getsize() x3001 (約 6000 メタデータ操作) は 29 秒以内に完了
   - その直後の「たった 1 回の open()」が 91 秒以上戻ってこない
   - 同じコードがローカル SSD では 1 ファイル 2.5 ms、TIFF ヘッダしか読まない

 このスクリプトが答える問い:
   Q1. 遅いのは CreateFile (open) か、それともデータ転送 (read) か?
   Q2. open が遅いなら、それは「ハンドルを開くこと自体」か「読み取り権限を要求すること」か?
       -> dwDesiredAccess=0 の open と GENERIC_READ の open を別々に計測して区別する。
          これが AV スキャン / HSM リコール を他の原因から切り離す決定的な測定。
   Q3. 1 ファイル目だけ遅いのか (一回限りのコスト)、全ファイルが遅いのか (系統的)?
   Q4. メタデータが速いのは本当に「メタデータが安い」からか、
       それとも SMB リダイレクタのディレクトリキャッシュに載っているだけか?
       -> 列挙 (enumeration) の前後で同じファイルの stat を測って比較する。

 重要な前提 (正直に書きます):
   「メタデータが速い / open が遅い」だけでは AV の証拠になりません。
   Windows の SMB リダイレクタはディレクトリ列挙後にメタデータをキャッシュするため、
   getsize() はネットワーク往復ゼロで返ることがあります。一方 CreateFile は必ず往復します。
   HSM リコール・oplock ブレーク・冷えた NAS ティア・回線飽和も全く同じ症状を出します。

 使い方 (パイプラインを動かしている Windows 機で実行):
     powershell -NoProfile -ExecutionPolicy Bypass -File .\Diag-SmbImageIngest.ps1 `
         -Path '\\yg-storage4\Storage-4\2PM_raw\<実際のパス>\img01'
   管理者 PowerShell で実行すると、パフォーマンスカウンタと fltmc (ミニフィルタ一覧) も取得できます。
   管理者でなくても主要な測定はすべて動きます。

 注意: 1 回の open が数十秒ブロックすることがあります。途中で止めたい場合は Ctrl+C。
================================================================================
#>

[CmdletBinding()]
param(
    # 計測対象ディレクトリ (img01)。UNC 推奨。V: などのドライブレターでも可。
    [string] $Path = '\\yg-storage4\Storage-4\2PM_raw\img01',

    # 何ファイル計測するか
    [int]    $Count = 20,

    # 1 番目のファイル名。ここから連番を算術生成する (Get-ChildItem を使わないため)
    [string] $FirstFileName = 'ChanA_001_001_001_001.tif',

    # 全体の打ち切り時間 (秒)。ループの各ファイルの直前でチェックする
    [int]    $MaxTotalSeconds = 900,

    # 比較用の別ディレクトリ (任意)。「このディレクトリ固有の問題か」を見るのに使う
    [string] $ControlPath = '',

    # 全バイト読み取りを省略したい場合 (open だけ見たいとき)
    [switch] $SkipFullRead,

    # 3001 件のディレクトリ列挙も計測する (既定で実施)。巨大ディレクトリで時間がかかる場合は -SkipEnumeration
    [switch] $SkipEnumeration,

    # ログ出力先
    [string] $LogPath = (Join-Path $env:TEMP ('SmbDiag_{0:yyyyMMdd_HHmmss}.txt' -f (Get-Date)))
)

$ErrorActionPreference = 'Continue'
$ProgressPreference    = 'SilentlyContinue'

# ==============================================================================
# 0. 共通ヘルパー
# ==============================================================================

function Write-Section {
    param([string]$Title)
    Write-Host ''
    Write-Host ('=' * 78) -ForegroundColor DarkCyan
    Write-Host ("  " + $Title) -ForegroundColor Cyan
    Write-Host ('=' * 78) -ForegroundColor DarkCyan
}

function Write-Sub {
    param([string]$Title)
    Write-Host ''
    Write-Host ("--- " + $Title + " " + ('-' * [Math]::Max(0, 70 - $Title.Length))) -ForegroundColor DarkGray
}

function Write-Note {
    param([string]$Text)
    Write-Host ("    " + $Text) -ForegroundColor DarkGray
}

function Write-Warn2 {
    param([string]$Text)
    Write-Host ("  ! " + $Text) -ForegroundColor Yellow
}

function Write-Bad {
    param([string]$Text)
    Write-Host ("  X " + $Text) -ForegroundColor Red
}

function Write-Good {
    param([string]$Text)
    Write-Host ("  o " + $Text) -ForegroundColor Green
}

# 中央値 (median)
function Get-Median {
    param([double[]]$Values)
    if (-not $Values -or $Values.Count -eq 0) { return $null }
    $s = @($Values | Sort-Object)
    $n = $s.Count
    if ($n % 2 -eq 1) { return [double]$s[[int](($n - 1) / 2)] }
    return ([double]$s[$n / 2 - 1] + [double]$s[$n / 2]) / 2.0
}

# min / median / max / p90 をまとめて返す
function Get-Stat {
    param([double[]]$Values)
    if (-not $Values -or $Values.Count -eq 0) {
        return [pscustomobject]@{ N = 0; Min = $null; Median = $null; Max = $null; P90 = $null; Sum = $null }
    }
    $s = @($Values | Sort-Object)
    $idx = [int][Math]::Floor(0.9 * ($s.Count - 1))
    [pscustomobject]@{
        N      = $s.Count
        Min    = [double]$s[0]
        Median = Get-Median $Values
        Max    = [double]$s[$s.Count - 1]
        P90    = [double]$s[$idx]
        Sum    = ($Values | Measure-Object -Sum).Sum
    }
}

function Fmt {
    param($v, [int]$d = 1)
    if ($null -eq $v) { return 'n/a' }
    return ('{0:N' + $d + '}') -f [double]$v
}

# オブジェクトのうち「存在するプロパティだけ」を安全に表示する
function Show-Props {
    param($Object, [string[]]$Names)
    if ($null -eq $Object) { Write-Note '(取得できませんでした / not available)'; return }
    foreach ($n in $Names) {
        $p = $Object.PSObject.Properties[$n]
        if ($null -ne $p) {
            $val = $p.Value
            if ($val -is [System.Array]) { $val = ($val -join ', ') }
            if ($null -eq $val -or "$val" -eq '') { $val = '(empty)' }
            Write-Host ('    {0,-46} : {1}' -f $n, $val)
        }
    }
}

function Test-IsAdmin {
    try {
        $id = [Security.Principal.WindowsIdentity]::GetCurrent()
        $pr = New-Object Security.Principal.WindowsPrincipal($id)
        return $pr.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    } catch { return $false }
}

function Test-TcpPort {
    param([string]$HostName, [int]$Port, [int]$TimeoutMs = 900)
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $ar = $client.BeginConnect($HostName, $Port, $null, $null)
        $ok = $ar.AsyncWaitHandle.WaitOne($TimeoutMs, $false)
        if (-not $ok) { return $false }
        $client.EndConnect($ar)
        return $true
    } catch {
        return $false
    } finally {
        try { $client.Close() } catch { }
    }
}

# Win32 エラーコードを人間語に
function Get-Win32Text {
    param([int]$Code)
    switch ($Code) {
        0     { 'ERROR_SUCCESS' }
        2     { 'ERROR_FILE_NOT_FOUND (ファイルが存在しない)' }
        3     { 'ERROR_PATH_NOT_FOUND (パスが存在しない)' }
        5     { 'ERROR_ACCESS_DENIED (アクセス拒否)' }
        32    { 'ERROR_SHARING_VIOLATION (他プロセスが排他で開いている)' }
        33    { 'ERROR_LOCK_VIOLATION' }
        53    { 'ERROR_BAD_NETPATH (サーバ/共有に到達できない)' }
        59    { 'ERROR_UNEXP_NET_ERR (予期しないネットワークエラー)' }
        64    { 'ERROR_NETNAME_DELETED (SMB セッションが切れた)' }
        67    { 'ERROR_BAD_NET_NAME' }
        121   { 'ERROR_SEM_TIMEOUT (セマフォタイムアウト = SMB 応答待ちタイムアウト)' }
        1231  { 'ERROR_NETWORK_UNREACHABLE' }
        1450  { 'ERROR_NO_SYSTEM_RESOURCES' }
        default { "Win32Error=$Code" }
    }
}

# ==============================================================================
# 0b. ネイティブ CreateFileW (これが本スクリプトの核心)
#     .NET の File.Open では dwDesiredAccess=0 の「メタデータだけのハンドル」が作れない。
#     アクセス権を変えて open 時間を比較することで、
#       - CreateFile 往復そのものが遅い    (サーバ/回線/oplock)
#       - 読み取り権を要求した瞬間に遅い    (AV スキャン / HSM リコール)
#     を分離する。
# ==============================================================================
$script:HasNative = $false
if (-not ('SmbDiag.NativeOpen' -as [type])) {
    $csharp = @'
using System;
using System.Diagnostics;
using System.Runtime.InteropServices;
using Microsoft.Win32.SafeHandles;

namespace SmbDiag {
    public class OpenResult {
        public double Ms;
        public bool   Ok;
        public int    Err;
    }
    public static class NativeOpen {
        [DllImport("kernel32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
        private static extern SafeFileHandle CreateFileW(
            string lpFileName,
            uint   dwDesiredAccess,
            uint   dwShareMode,
            IntPtr lpSecurityAttributes,
            uint   dwCreationDisposition,
            uint   dwFlagsAndAttributes,
            IntPtr hTemplateFile);

        // OPEN_EXISTING = 3
        public static OpenResult TimedOpen(string path, uint access, uint share, uint flags) {
            OpenResult r = new OpenResult();
            Stopwatch sw = Stopwatch.StartNew();
            SafeFileHandle h = CreateFileW(path, access, share, IntPtr.Zero, 3, flags, IntPtr.Zero);
            int err = Marshal.GetLastWin32Error();
            sw.Stop();
            r.Ms  = sw.Elapsed.TotalMilliseconds;
            r.Err = err;
            r.Ok  = (h != null && !h.IsInvalid);
            if (r.Ok) { h.Close(); }
            return r;
        }
    }
}
'@
    try {
        Add-Type -TypeDefinition $csharp -ErrorAction Stop
        $script:HasNative = $true
    } catch {
        $script:HasNative = $false
    }
} else {
    $script:HasNative = $true
}

# CreateFileW 定数
$GENERIC_READ            = [uint32]2147483648    # 0x80000000
$FILE_READ_ATTRIBUTES    = [uint32]128           # 0x00000080
$ACCESS_NONE             = [uint32]0             # メタデータ専用ハンドル (Python の Path.resolve() 相当)
$SHARE_ALL               = [uint32]7             # READ|WRITE|DELETE
$SHARE_NONE              = [uint32]0
$FLAG_NONE               = [uint32]0
$FILE_FLAG_OPEN_NO_RECALL= [uint32]1048576       # 0x00100000  HSM から復元させずに開く

function Invoke-NativeOpen {
    param([string]$FilePath, [uint32]$Access, [uint32]$Share = 7, [uint32]$Flags = 0)
    if (-not $script:HasNative) { return $null }
    try { return [SmbDiag.NativeOpen]::TimedOpen($FilePath, $Access, $Share, $Flags) }
    catch { return $null }
}

# ==============================================================================
# 0c. 1 ファイル分の (a) stat / (b) open+close / (c) 全バイト読み取り 計測
# ==============================================================================
function Measure-OneFile {
    param(
        [string]$FilePath,
        [switch]$DoFullRead,
        [switch]$Deep      # ネイティブ open マトリクスも取る
    )

    $o = [pscustomobject]@{
        Name          = [System.IO.Path]::GetFileName($FilePath)
        StatMs        = $null   # (a) .NET FileInfo.Length  (低ノイズな stat)
        GetItemMs     = $null   # (a') Get-Item             (仕様どおりの stat)
        OpenMs        = $null   # (b) FileStream open (GENERIC_READ, share=ReadWrite) — read せず
        CloseMs       = $null   # (b) close だけの時間 (oplock ブレークは close 側に出ることがある)
        Open2Ms       = $null   # (c) 直後にもう一度 open した時間 (= 2 回目 / warm)
        ReadMs        = $null   # (c) 全バイト読み取りのみの時間 (open を含まない)
        Bytes         = $null
        MBps          = $null   # 読み取りのみの実効速度
        E2EMs         = $null   # open+read+close の合計
        Attributes    = ''
        Error         = ''
        # Deep 用
        NatAccess0Ms  = $null; NatAccess0Err = $null
        NatAttrMs     = $null; NatAttrErr    = $null
        NatReadMs     = $null; NatReadErr    = $null
        NatNoRecallMs = $null; NatNoRecallErr= $null
        NatShareNoneMs= $null; NatShareNoneErr = $null
    }

    $sw = New-Object System.Diagnostics.Stopwatch

    # ---- (a) stat: .NET FileInfo (PowerShell プロバイダのオーバーヘッドを避ける) ----
    try {
        $sw.Restart()
        $fi  = New-Object System.IO.FileInfo($FilePath)
        $len = $fi.Length          # ここで初めて実際のメタデータ取得が走る
        $att = $fi.Attributes
        $sw.Stop()
        $o.StatMs     = $sw.Elapsed.TotalMilliseconds
        $o.Bytes      = $len
        $o.Attributes = "$att"
    } catch {
        $sw.Stop()
        $o.StatMs = $sw.Elapsed.TotalMilliseconds
        $o.Error  = "stat: " + $_.Exception.Message
        return $o
    }

    # ---- (a') stat: Get-Item (仕様で明示されているもの) ----
    try {
        $sw.Restart()
        $null = Get-Item -LiteralPath $FilePath -Force -ErrorAction Stop
        $sw.Stop()
        $o.GetItemMs = $sw.Elapsed.TotalMilliseconds
    } catch {
        $sw.Stop(); $o.GetItemMs = $sw.Elapsed.TotalMilliseconds
    }

    # ---- Deep: ネイティブ open マトリクス (アクセス権を変えて比較) ----
    if ($Deep -and $script:HasNative) {
        $r = Invoke-NativeOpen -FilePath $FilePath -Access $ACCESS_NONE          -Share $SHARE_ALL
        if ($r) { $o.NatAccess0Ms = $r.Ms; $o.NatAccess0Err = $r.Err }
        $r = Invoke-NativeOpen -FilePath $FilePath -Access $FILE_READ_ATTRIBUTES -Share $SHARE_ALL
        if ($r) { $o.NatAttrMs = $r.Ms; $o.NatAttrErr = $r.Err }
        $r = Invoke-NativeOpen -FilePath $FilePath -Access $GENERIC_READ         -Share $SHARE_ALL
        if ($r) { $o.NatReadMs = $r.Ms; $o.NatReadErr = $r.Err }
        $r = Invoke-NativeOpen -FilePath $FilePath -Access $GENERIC_READ         -Share $SHARE_ALL -Flags $FILE_FLAG_OPEN_NO_RECALL
        if ($r) { $o.NatNoRecallMs = $r.Ms; $o.NatNoRecallErr = $r.Err }
        $r = Invoke-NativeOpen -FilePath $FilePath -Access $GENERIC_READ         -Share $SHARE_NONE
        if ($r) { $o.NatShareNoneMs = $r.Ms; $o.NatShareNoneErr = $r.Err }
    }

    # ---- (b) open して即 close (READ は一切しない) ----
    #      Python の open(path,'rb') と同じ共有モード (ReadWrite) を要求する
    $fs = $null
    try {
        $sw.Restart()
        $fs = [System.IO.File]::Open($FilePath,
                                     [System.IO.FileMode]::Open,
                                     [System.IO.FileAccess]::Read,
                                     [System.IO.FileShare]::ReadWrite)
        $sw.Stop()
        $o.OpenMs = $sw.Elapsed.TotalMilliseconds
        $sw.Restart()
        $fs.Dispose(); $fs = $null
        $sw.Stop()
        $o.CloseMs = $sw.Elapsed.TotalMilliseconds
    } catch {
        $sw.Stop()
        $o.OpenMs = $sw.Elapsed.TotalMilliseconds
        $o.Error  = "open: " + $_.Exception.Message
        if ($fs) { try { $fs.Dispose() } catch { } }
        return $o
    }

    # ---- (c) もう一度 open して全バイト読み取り ----
    if ($DoFullRead) {
        $fs = $null
        try {
            $sw.Restart()
            $fs = [System.IO.File]::Open($FilePath,
                                         [System.IO.FileMode]::Open,
                                         [System.IO.FileAccess]::Read,
                                         [System.IO.FileShare]::ReadWrite)
            $sw.Stop()
            $o.Open2Ms = $sw.Elapsed.TotalMilliseconds

            $buf   = New-Object byte[] (1MB)
            $total = 0L
            $sw.Restart()
            while ($true) {
                $n = $fs.Read($buf, 0, $buf.Length)
                if ($n -le 0) { break }
                $total += $n
            }
            $sw.Stop()
            $o.ReadMs = $sw.Elapsed.TotalMilliseconds
            $o.Bytes  = $total
            if ($o.ReadMs -gt 0) {
                $o.MBps = ($total / 1MB) / ($o.ReadMs / 1000.0)
            }
            $fs.Dispose(); $fs = $null
        } catch {
            $sw.Stop()
            $o.Error = "read: " + $_.Exception.Message
            if ($fs) { try { $fs.Dispose() } catch { } }
        }
    }

    $parts = @($o.OpenMs, $o.CloseMs, $o.Open2Ms, $o.ReadMs) | Where-Object { $null -ne $_ }
    if ($parts.Count -gt 0) { $o.E2EMs = ($parts | Measure-Object -Sum).Sum }
    return $o
}

# ==============================================================================
# 1. 開始 / 環境 / パス解決
# ==============================================================================
try { Start-Transcript -Path $LogPath -Force | Out-Null; $script:Transcript = $true }
catch { $script:Transcript = $false }

$isAdmin  = Test-IsAdmin
$globalSw = [System.Diagnostics.Stopwatch]::StartNew()

Write-Section 'SMB image-ingest 診断 / diagnostic (MEASURE ONLY - 設定は変更しません)'
Write-Host ('  Date            : {0}' -f (Get-Date))
Write-Host ('  Computer        : {0}' -f $env:COMPUTERNAME)
Write-Host ('  User            : {0}\{1}' -f $env:USERDOMAIN, $env:USERNAME)
Write-Host ('  PowerShell      : {0} ({1})' -f $PSVersionTable.PSVersion, $PSVersionTable.PSEdition)
Write-Host ('  Elevated (admin): {0}' -f $isAdmin)
Write-Host ('  Native CreateFile probe : {0}' -f $script:HasNative)
Write-Host ('  Log file        : {0}' -f $LogPath)
if (-not $isAdmin) {
    Write-Warn2 '管理者ではありません。パフォーマンスカウンタと fltmc (ミニフィルタ一覧) はスキップされる可能性があります。'
    Write-Warn2 'Not elevated: SMB performance counters and fltmc may be unavailable. Everything else still works.'
}
if (-not $script:HasNative) {
    Write-Warn2 'C# コンパイルに失敗したため、ネイティブ open マトリクス (最重要テスト) は使えません。'
}

Write-Sub 'パス解決 / path resolution'
Write-Host ('    Requested Path : {0}' -f $Path)

if ($Path -match '\.\.\.') {
    Write-Bad 'パスに "..." が含まれています。実際の img01 のフルパスを -Path で指定してください。'
    Write-Bad 'The default path contains a placeholder. Re-run with the real path, e.g.:'
    Write-Host "      .\Diag-SmbImageIngest.ps1 -Path '\\yg-storage4\Storage-4\2PM_raw\<project>\img01'"
}

# ドライブレター (V: など) が渡された場合は UNC に直す
$uncPath = $Path
try {
    $qualifier = $null
    if ($Path -match '^[A-Za-z]:') { $qualifier = $Path.Substring(0, 2) }
    if ($qualifier) {
        $map = Get-SmbMapping -LocalPath $qualifier -ErrorAction SilentlyContinue
        if ($map -and $map.RemotePath) {
            $uncPath = $map.RemotePath.TrimEnd('\') + $Path.Substring(2)
            Write-Host ('    Mapped drive   : {0} -> {1}' -f $qualifier, $map.RemotePath)
        }
    }
} catch { }
Write-Host ('    UNC path used  : {0}' -f $uncPath)

$serverName = $null; $shareName = $null
if ($uncPath -match '^\\\\([^\\]+)\\([^\\]+)') {
    $serverName = $Matches[1]
    $shareName  = $Matches[2]
    Write-Host ('    Server / Share : {0} / {1}' -f $serverName, $shareName)
} else {
    Write-Warn2 'UNC 形式として解釈できませんでした (ローカルパス?)。SMB 固有の項目はスキップされます。'
}

# 共有への最初のアクセス。SMB セッション確立・認証・ツリー接続のコストはここで吸収される。
# (この時間を PHASE 1 のファイル計測に混ぜないために、あえてここで計測して切り離す)
$pathExists  = $false
$sessionMs   = $null
try {
    $sw0 = [System.Diagnostics.Stopwatch]::StartNew()
    $pathExists = [System.IO.Directory]::Exists($uncPath)
    $sw0.Stop()
    $sessionMs = $sw0.Elapsed.TotalMilliseconds
    Write-Host ('    共有への初回アクセス / first touch of the share : {0} ms' -f (Fmt $sessionMs 1))
    Write-Note '(SMB セッション確立・認証・ツリー接続を含みます。以降のファイル計測からはこのコストは除かれます)'
} catch { }
if (-not $pathExists) {
    Write-Bad ('ディレクトリが見つかりません / directory not found: {0}' -f $uncPath)
    Write-Sub '現在のドライブマッピング / current SMB mappings'
    try { Get-SmbMapping -ErrorAction Stop | Format-Table LocalPath, RemotePath, Status -AutoSize | Out-String | Write-Host }
    catch { Write-Note 'Get-SmbMapping を実行できませんでした。' }
    Write-Host ''
    Write-Bad '正しい img01 のパスを -Path で指定して再実行してください。ここで終了します。'
    if ($script:Transcript) { try { Stop-Transcript | Out-Null } catch { } }
    return
}

# 連番ファイル名を算術生成する。
# ★ Get-ChildItem を先に走らせると SMB リダイレクタのディレクトリキャッシュが温まり、
#    「メタデータが速い」という測定が無意味になるため、絶対に列挙しない。
$genOk   = $false
$fileNames = @()
if ($FirstFileName -match '^(.*?)(\d+)(\.[^.]+)$') {
    $prefix = $Matches[1]
    $digits = $Matches[2]
    $ext    = $Matches[3]
    $width  = $digits.Length
    $start  = [int]$digits
    $fileNames = @(0..($Count) | ForEach-Object { '{0}{1}{2}' -f $prefix, ($start + $_).ToString('D' + $width), $ext })
    $genOk = $true
    Write-Host ('    File pattern   : {0}[{1}]{2}  ({3} 桁ゼロ埋め, {4} から)' -f $prefix, ('#' * $width), $ext, $width, $start)
} else {
    Write-Warn2 ('-FirstFileName "{0}" から連番パターンを推定できませんでした。列挙にフォールバックします。' -f $FirstFileName)
}

# 生成した先頭ファイルが本当に存在するか、1 回だけ軽く確認する。
# 存在しなければゼロ埋め桁数を 3-6 桁で総当たりする (3001 枚なら 4 桁のことが多い)。
# ここでも列挙はしない — 列挙するとディレクトリキャッシュが温まり、stat の測定が無意味になるため。
$enumerationWarmedCache = $false
if ($genOk) {
    $exists = $false
    try { $exists = [System.IO.File]::Exists((Join-Path $uncPath $fileNames[0])) } catch { }
    if (-not $exists) {
        Write-Warn2 ('生成したファイル名が存在しません: {0}  -> ゼロ埋め桁数を推定します' -f $fileNames[0])
        foreach ($w in 3, 4, 5, 6, 2, 1) {
            if ($w -eq $width) { continue }
            $cand = '{0}{1}{2}' -f $prefix, $start.ToString('D' + $w), $ext
            $hit = $false
            try { $hit = [System.IO.File]::Exists((Join-Path $uncPath $cand)) } catch { }
            if ($hit) {
                $width = $w
                $fileNames = @(0..($Count) | ForEach-Object { '{0}{1}{2}' -f $prefix, ($start + $_).ToString('D' + $width), $ext })
                $exists = $true
                Write-Good ('{0} 桁ゼロ埋めで見つかりました: {1}' -f $w, $cand)
                break
            }
        }
    }
    if (-not $exists) {
        Write-Warn2 'ディレクトリ列挙にフォールバックします (この時点でメタデータキャッシュが温まります)。'
        $genOk = $false
    }
}
if (-not $genOk) {
    try {
        $listed = Get-ChildItem -LiteralPath $uncPath -File -ErrorAction Stop | Select-Object -First ($Count + 1)
        $fileNames = @($listed | ForEach-Object { $_.Name })
        $enumerationWarmedCache = $true
        Write-Note ('列挙で {0} 件取得しました。先頭: {1}' -f $fileNames.Count, ($fileNames | Select-Object -First 1))
    } catch {
        Write-Bad ('ディレクトリ列挙にも失敗しました: {0}' -f $_.Exception.Message)
        if ($script:Transcript) { try { Stop-Transcript | Out-Null } catch { } }
        return
    }
}
if ($fileNames.Count -lt 1) {
    Write-Bad 'ファイルが 1 件も見つかりませんでした。'
    if ($script:Transcript) { try { Stop-Transcript | Out-Null } catch { } }
    return
}

# ==============================================================================
# 2. SMB パフォーマンスカウンタのバックグラウンド収集を開始
#    (計測ループの裏で 1 秒ごとにサンプリングする)
# ==============================================================================
$counterJob   = $null
$counterPaths = @()
$counterSetName = $null

Write-Sub 'SMB クライアントカウンタの準備 / SMB client counters'
try {
    $set = $null
    try { $set = Get-Counter -ListSet 'SMB Client Shares' -ErrorAction Stop } catch { }
    if (-not $set) {
        # 日本語版 Windows などではカウンタセット名がローカライズされている
        Write-Note 'English のカウンタセット名で見つかりません。全カウンタセットを検索します (10-30 秒かかることがあります)...'
        $all = Get-Counter -ListSet * -ErrorAction SilentlyContinue
        $set = $all | Where-Object { $_.CounterSetName -match 'SMB' -and $_.CounterSetName -match '(Share|共有)' } | Select-Object -First 1
        if (-not $set) { $set = $all | Where-Object { $_.CounterSetName -match 'SMB' } | Select-Object -First 1 }
    }
    if ($set) {
        $counterSetName = $set.CounterSetName
        $counterPaths = @($set.PathsWithInstances | Where-Object {
            (-not $serverName) -or ($_ -like ('*' + $serverName + '*'))
        })
        if ($counterPaths.Count -eq 0) {
            Write-Note ('カウンタセット "{0}" は存在しますが、{1} のインスタンスがまだありません (接続が確立していない?)。' -f $counterSetName, $serverName)
            $counterPaths = @($set.PathsWithInstances)
        }
        Write-Host ('    Counter set    : {0}  ({1} paths matched)' -f $counterSetName, $counterPaths.Count)
    } else {
        Write-Note 'SMB クライアントカウンタセットが見つかりませんでした (Windows 8 / Server 2012 以降で利用可)。'
    }
} catch {
    Write-Note ('カウンタ列挙に失敗: {0}' -f $_.Exception.Message)
}

if ($counterPaths.Count -gt 0) {
    $samples = [Math]::Min([Math]::Max($MaxTotalSeconds, 30), 900)
    try {
        $counterJob = Start-Job -ScriptBlock {
            param($paths, $maxSamples)
            try {
                Get-Counter -Counter $paths -SampleInterval 1 -MaxSamples $maxSamples -ErrorAction Stop |
                    ForEach-Object {
                        $ts = $_.Timestamp
                        foreach ($cs in $_.CounterSamples) {
                            [pscustomobject]@{ Time = $ts; Path = $cs.Path; Value = [double]$cs.CookedValue }
                        }
                    }
            } catch {
                [pscustomobject]@{ Time = $null; Path = 'COUNTER-ERROR'; Value = 0; Err = $_.Exception.Message }
            }
        } -ArgumentList (,$counterPaths), $samples
        Write-Good ('バックグラウンドでカウンタ収集を開始しました (最大 {0} 秒)。' -f $samples)
    } catch {
        Write-Note ('カウンタ収集ジョブを開始できませんでした: {0}' -f $_.Exception.Message)
        $counterJob = $null
    }
}

# ==============================================================================
# 3. PHASE 1 — 1 ファイルの徹底計測 (cold)
#    ここで使うのはループ (PHASE 2) では使わないファイル。
#    このファイルが「この実行での最初のアクセス」なので、
#    SMB セッション確立・認証・ツリー接続のコストを全部ここで吸収する。
# ==============================================================================
Write-Section 'PHASE 1: 1 ファイル徹底計測 (この実行での最初のアクセス / cold)'

$deepName = $fileNames[[Math]::Min($Count, $fileNames.Count - 1)]
$deepPath = Join-Path $uncPath $deepName
Write-Host ('  Target: {0}' -f $deepPath)
Write-Note 'このファイルは PHASE 2 のループでは使いません (二重計測を避けるため)。'
Write-Note 'SMB セッション確立のコストは既に上の「共有への初回アクセス」で吸収済みなので、ここには含まれません。'
Write-Note '測定順序: stat -> access=0 -> READ_ATTRIBUTES -> GENERIC_READ -> GENERIC_READ+NO_RECALL -> share=NONE -> FileStream open -> read。'
Write-Note '読み取り権を要求するのは GENERIC_READ が最初なので、AV スキャン / HSM リコールのコストはそこに現れます。'
Write-Host ''

$deep = Measure-OneFile -FilePath $deepPath -DoFullRead:(-not $SkipFullRead) -Deep

Write-Host '  (a) stat / メタデータのみ:'
Write-Host ('        FileInfo.Length          : {0,12} ms' -f (Fmt $deep.StatMs 2))
Write-Host ('        Get-Item                 : {0,12} ms' -f (Fmt $deep.GetItemMs 2))
Write-Host ('        Attributes               : {0}' -f $deep.Attributes)
Write-Host ('        Size                     : {0,12} bytes' -f $deep.Bytes)
Write-Host ''
if ($script:HasNative) {
    Write-Host '  (a2) CreateFile アクセス権マトリクス  <<< 最重要 / THE decisive test >>>'
    Write-Host ('        access=0 (metadata handle only)      : {0,12} ms   [{1}]' -f (Fmt $deep.NatAccess0Ms 2), (Get-Win32Text ([int]$deep.NatAccess0Err)))
    Write-Host ('        access=FILE_READ_ATTRIBUTES          : {0,12} ms   [{1}]' -f (Fmt $deep.NatAttrMs 2),     (Get-Win32Text ([int]$deep.NatAttrErr)))
    Write-Host ('        access=GENERIC_READ                  : {0,12} ms   [{1}]' -f (Fmt $deep.NatReadMs 2),     (Get-Win32Text ([int]$deep.NatReadErr)))
    Write-Host ('        access=GENERIC_READ + NO_RECALL      : {0,12} ms   [{1}]' -f (Fmt $deep.NatNoRecallMs 2), (Get-Win32Text ([int]$deep.NatNoRecallErr)))
    Write-Host ('        access=GENERIC_READ, share=NONE      : {0,12} ms   [{1}]' -f (Fmt $deep.NatShareNoneMs 2),(Get-Win32Text ([int]$deep.NatShareNoneErr)))
    Write-Note 'access=0 は Python の Path.resolve() (GetFinalPathNameByHandle) と同じ「読み取り権を要求しない open」です。'
    Write-Note 'access=0 が速く GENERIC_READ が遅い => 読み取り権要求で起動するフィルタ (AV スキャン / HSM リコール)。'
    Write-Note 'share=NONE が ERROR_SHARING_VIOLATION => 他プロセス (顕微鏡の取得ソフト) がハンドルを保持している。'
    Write-Host ''
}
Write-Host '  (b) open して即 close (READ なし):'
Write-Host ('        open                     : {0,12} ms' -f (Fmt $deep.OpenMs 2))
Write-Host ('        close                    : {0,12} ms' -f (Fmt $deep.CloseMs 2))
Write-Host ''
if (-not $SkipFullRead) {
    Write-Host '  (c) 全バイト読み取り:'
    Write-Host ('        open (2 回目 / warm)     : {0,12} ms' -f (Fmt $deep.Open2Ms 2))
    Write-Host ('        read all bytes           : {0,12} ms' -f (Fmt $deep.ReadMs 2))
    Write-Host ('        bytes                    : {0,12}' -f $deep.Bytes)
    Write-Host ('        effective throughput     : {0,12} MB/s' -f (Fmt $deep.MBps 2))
}
if ($deep.Error) { Write-Bad ('エラー: {0}' -f $deep.Error) }

# 同じファイルをもう一度フルで測る (2 回目 = warm)。「一回限りのコスト」かどうかを見る。
Write-Sub '同じファイルの 2 回目 (warm) — 一回限りのコストかどうか / same file, second pass'
$deep2 = Measure-OneFile -FilePath $deepPath -DoFullRead:(-not $SkipFullRead) -Deep
Write-Host ('        stat     : 1st {0,10} ms   2nd {1,10} ms' -f (Fmt $deep.StatMs 2),  (Fmt $deep2.StatMs 2))
Write-Host ('        open     : 1st {0,10} ms   2nd {1,10} ms' -f (Fmt $deep.OpenMs 2),  (Fmt $deep2.OpenMs 2))
Write-Host ('        read     : 1st {0,10} ms   2nd {1,10} ms' -f (Fmt $deep.ReadMs 2),  (Fmt $deep2.ReadMs 2))
if ($script:HasNative) {
    Write-Host ('        GENERIC_READ open : 1st {0,10} ms   2nd {1,10} ms' -f (Fmt $deep.NatReadMs 2), (Fmt $deep2.NatReadMs 2))
}
Write-Note '2 回目が劇的に速い => ファイルごとの一回限りコスト (AV の初回スキャン / HSM リコール / ティア復元)。'
Write-Note '2 回目も同じく遅い => 毎回発生するコスト (回線・サーバ・oplock・署名/暗号化オーバーヘッド)。'
Write-Note '注意: 2 回目の read はクライアント側キャッシュ (SMB2 read lease) から返る可能性があります。open は必ず往復します。'

# ==============================================================================
# 4. PHASE 2 — 先頭 N ファイルのループ計測
# ==============================================================================
Write-Section ('PHASE 2: 先頭 {0} ファイルの計測 / per-file measurement' -f $Count)
Write-Note 'ファイル名は算術生成しています (列挙でキャッシュを温めないため)。'
if ($enumerationWarmedCache) {
    Write-Warn2 '※ 今回は列挙にフォールバックしたため、メタデータキャッシュが既に温まっています。stat の値は「下限」として読んでください。'
}
Write-Host ''
Write-Host ('  {0,-34} {1,10} {2,10} {3,10} {4,10} {5,9}' -f 'File', 'stat(ms)', 'open(ms)', 'close(ms)', 'read(ms)', 'MB/s')
Write-Host ('  ' + ('-' * 88))

$rows = New-Object System.Collections.ArrayList
$stopped = $false
$n = [Math]::Min($Count, $fileNames.Count)
for ($i = 0; $i -lt $n; $i++) {
    if ($globalSw.Elapsed.TotalSeconds -gt $MaxTotalSeconds) {
        Write-Warn2 ('全体の打ち切り時間 {0} 秒を超えたのでループを中断しました ({1}/{2} 完了)。' -f $MaxTotalSeconds, $i, $n)
        $stopped = $true
        break
    }
    $fp = Join-Path $uncPath $fileNames[$i]
    $r  = Measure-OneFile -FilePath $fp -DoFullRead:(-not $SkipFullRead)
    [void]$rows.Add($r)
    Write-Host ('  {0,-34} {1,10} {2,10} {3,10} {4,10} {5,9}' -f `
        $r.Name, (Fmt $r.StatMs 2), (Fmt $r.OpenMs 1), (Fmt $r.CloseMs 1), (Fmt $r.ReadMs 1), (Fmt $r.MBps 1))
    if ($r.Error) { Write-Bad ('      -> {0}' -f $r.Error) }
}

$ok = @($rows | Where-Object { $null -ne $_.OpenMs -and -not $_.Error })

$statStat  = Get-Stat @($ok | ForEach-Object { [double]$_.StatMs })
$openStat  = Get-Stat @($ok | ForEach-Object { [double]$_.OpenMs })
$closeStat = Get-Stat @($ok | ForEach-Object { [double]$_.CloseMs })
$open2Stat = Get-Stat @($ok | Where-Object { $null -ne $_.Open2Ms } | ForEach-Object { [double]$_.Open2Ms })
$readStat  = Get-Stat @($ok | Where-Object { $null -ne $_.ReadMs }  | ForEach-Object { [double]$_.ReadMs })
$e2eStat   = Get-Stat @($ok | Where-Object { $null -ne $_.E2EMs }   | ForEach-Object { [double]$_.E2EMs })

Write-Sub '集計 / summary (min / median / max)'
Write-Host ('  {0,-26} {1,12} {2,12} {3,12} {4,12}' -f 'metric', 'min', 'median', 'max', 'p90')
Write-Host ('  ' + ('-' * 78))
Write-Host ('  {0,-26} {1,12} {2,12} {3,12} {4,12}' -f 'stat (ms)',          (Fmt $statStat.Min 2), (Fmt $statStat.Median 2), (Fmt $statStat.Max 2), (Fmt $statStat.P90 2))
Write-Host ('  {0,-26} {1,12} {2,12} {3,12} {4,12}' -f 'open, no read (ms)', (Fmt $openStat.Min 1), (Fmt $openStat.Median 1), (Fmt $openStat.Max 1), (Fmt $openStat.P90 1))
Write-Host ('  {0,-26} {1,12} {2,12} {3,12} {4,12}' -f 'close (ms)',         (Fmt $closeStat.Min 2),(Fmt $closeStat.Median 2),(Fmt $closeStat.Max 2),(Fmt $closeStat.P90 2))
Write-Host ('  {0,-26} {1,12} {2,12} {3,12} {4,12}' -f 'open 2nd/warm (ms)', (Fmt $open2Stat.Min 1),(Fmt $open2Stat.Median 1),(Fmt $open2Stat.Max 1),(Fmt $open2Stat.P90 1))
Write-Host ('  {0,-26} {1,12} {2,12} {3,12} {4,12}' -f 'read all bytes (ms)',(Fmt $readStat.Min 1), (Fmt $readStat.Median 1), (Fmt $readStat.Max 1), (Fmt $readStat.P90 1))
Write-Host ('  {0,-26} {1,12} {2,12} {3,12} {4,12}' -f 'end-to-end (ms)',    (Fmt $e2eStat.Min 1),  (Fmt $e2eStat.Median 1),  (Fmt $e2eStat.Max 1),  (Fmt $e2eStat.P90 1))

# ---- 実効スループット ----
$totalBytes = ($ok | Where-Object { $null -ne $_.ReadMs } | Measure-Object -Property Bytes -Sum).Sum
$aggMBps = $null; $e2eMBps = $null
if ($readStat.Sum -and $readStat.Sum -gt 0 -and $totalBytes) {
    $aggMBps = ($totalBytes / 1MB) / ($readStat.Sum / 1000.0)
}
if ($e2eStat.Sum -and $e2eStat.Sum -gt 0 -and $totalBytes) {
    $e2eMBps = ($totalBytes / 1MB) / ($e2eStat.Sum / 1000.0)
}
Write-Host ''
Write-Host ('  実効スループット / effective throughput')
Write-Host ('    read only (open を除く)      : {0} MB/s' -f (Fmt $aggMBps 2))
Write-Host ('    end-to-end (open+read+close) : {0} MB/s' -f (Fmt $e2eMBps 2))
Write-Host ('    total bytes read             : {0:N0} bytes ({1} MB)' -f ([long]$totalBytes), (Fmt ($totalBytes / 1MB) 1))

# ---- 1 ファイル目は外れ値か? ----
$file1Outlier = $false
$file1Ratio   = $null
if ($ok.Count -ge 3) {
    $first = [double]$ok[0].OpenMs
    $rest  = @($ok | Select-Object -Skip 1 | ForEach-Object { [double]$_.OpenMs })
    $restMed = Get-Median $rest
    if ($restMed -and $restMed -gt 0) { $file1Ratio = $first / $restMed }
    if ($file1Ratio -and $file1Ratio -ge 5 -and $first -ge 500) { $file1Outlier = $true }
    Write-Host ''
    Write-Host ('  1 ファイル目の open : {0} ms  /  2 番目以降の中央値 : {1} ms  (比 {2}x)' -f (Fmt $first 1), (Fmt $restMed 1), (Fmt $file1Ratio 1))
    if ($file1Outlier) {
        Write-Warn2 '=> 1 ファイル目だけが外れ値です。一回限りのコスト (接続確立 / 初回スキャン / 初回リコール) です。'
    } else {
        Write-Host '  => 1 ファイル目は外れ値ではありません。コストは全ファイルに共通 (systemic) です。' -ForegroundColor Yellow
    }
}

# ---- 3001 枚に外挿 ----
$projSec = $null
if ($e2eStat.Median) {
    $projSec = ($e2eStat.Median / 1000.0) * 3001
    Write-Host ''
    Write-Host ('  外挿 / extrapolation: 中央値 {0} ms/file x 3001 files = {1} 秒 = {2} 分' -f `
        (Fmt $e2eStat.Median 1), (Fmt $projSec 0), (Fmt ($projSec / 60.0) 1)) -ForegroundColor Magenta
}

# ==============================================================================
# 5. PHASE 3 — ディレクトリ列挙のコスト (3001 エントリ)
# ==============================================================================
$enumMs = $null; $enumCount = 0; $enumBytes = 0L
$offlineCount = 0; $reparseCount = 0; $recallCount = 0; $sparseCount = 0
if (-not $SkipEnumeration) {
    Write-Section 'PHASE 3: ディレクトリ列挙のコスト / directory enumeration (3001 entries)'
    if ($enumerationWarmedCache) {
        Write-Warn2 '既に列挙済みのため、ここでの時間は「2 回目 (warm)」です。cold の列挙コストではありません。'
    }
    try {
        $sw = [System.Diagnostics.Stopwatch]::StartNew()
        $all = @(Get-ChildItem -LiteralPath $uncPath -File -Force -ErrorAction Stop)
        $sw.Stop()
        $enumMs    = $sw.Elapsed.TotalMilliseconds
        $enumCount = $all.Count
        $enumBytes = ($all | Measure-Object -Property Length -Sum).Sum
        Write-Host ('  entries        : {0:N0}' -f $enumCount)
        Write-Host ('  total size     : {0} GB' -f (Fmt ($enumBytes / 1GB) 2))
        Write-Host ('  enumeration    : {0} ms  ({1} ms/entry)' -f (Fmt $enumMs 1), (Fmt ($enumMs / [Math]::Max(1, $enumCount)) 3))

        # ---- OFFLINE / REPARSE / RECALL 属性 (HSM・階層化ストレージの決定的な証拠) ----
        # FILE_ATTRIBUTE_OFFLINE              = 0x1000    (4096)
        # FILE_ATTRIBUTE_REPARSE_POINT        = 0x400     (1024)
        # FILE_ATTRIBUTE_SPARSE_FILE          = 0x200     (512)
        # FILE_ATTRIBUTE_RECALL_ON_OPEN       = 0x40000   (262144)
        # FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS= 0x400000  (4194304)
        foreach ($f in $all) {
            $a = [int]$f.Attributes
            if ($a -band 0x1000)   { $offlineCount++ }
            if ($a -band 0x400)    { $reparseCount++ }
            if ($a -band 0x200)    { $sparseCount++ }
            if (($a -band 0x40000) -or ($a -band 0x400000)) { $recallCount++ }
        }
        Write-Host ''
        Write-Host ('  OFFLINE                 (0x1000)   : {0} / {1}' -f $offlineCount, $enumCount)
        Write-Host ('  REPARSE_POINT           (0x400)    : {0} / {1}' -f $reparseCount, $enumCount)
        Write-Host ('  SPARSE_FILE             (0x200)    : {0} / {1}' -f $sparseCount, $enumCount)
        Write-Host ('  RECALL_ON_OPEN/DATA_ACC (0x40000/0x400000) : {0} / {1}' -f $recallCount, $enumCount)
        if ($offlineCount -gt 0 -or $recallCount -gt 0 -or $reparseCount -gt 0) {
            Write-Bad '階層化/アーカイブ属性が付いています。HSM・Azure File Sync・NAS コールドティアの可能性が非常に高い。'
        } else {
            Write-Good '階層化/アーカイブ属性は付いていません (HSM リコール説は弱い)。'
        }
    } catch {
        Write-Bad ('列挙に失敗: {0}' -f $_.Exception.Message)
    }
}

# ==============================================================================
# 6. PHASE 4 — 列挙後に同じファイルを再 stat (キャッシュ効果の定量化)
#    「メタデータが速いのはキャッシュのおかげか?」に正面から答える
# ==============================================================================
Write-Section 'PHASE 4: 列挙後の warm stat / is metadata cheap, or just cached?'
$warmStats = @()
foreach ($r in $ok) {
    $fp = Join-Path $uncPath $r.Name
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    try {
        $fi = New-Object System.IO.FileInfo($fp)
        $null = $fi.Length
    } catch { }
    $sw.Stop()
    $warmStats += [double]$sw.Elapsed.TotalMilliseconds
}
$warmStat = Get-Stat $warmStats
Write-Host ('  cold stat (列挙前) median : {0} ms' -f (Fmt $statStat.Median 3))
Write-Host ('  warm stat (列挙後) median : {0} ms' -f (Fmt $warmStat.Median 3))
Write-Note 'SMB クライアントは既定で DirectoryCacheLifetime=10 秒 / FileInfoCacheLifetime=10 秒 のキャッシュを持ちます。'
Write-Note '両者がほぼ同じで、かつ 1 ms 未満なら、6000 回のメタデータ操作は「そもそも安い」のではなく'
Write-Note 'ネットワーク往復ゼロで返っていた可能性が高い、と読むべきです。'

# ==============================================================================
# 7. 環境ファクト
# ==============================================================================
Write-Section '環境ファクト / environment facts'

# ---- 7.1 セキュリティセンター登録の AV 製品 ----
Write-Sub '7.1 インストール済み AV 製品 / registered antivirus products (root/SecurityCenter2)'
$avProducts = @()
try {
    $avProducts = @(Get-CimInstance -Namespace 'root/SecurityCenter2' -ClassName AntiVirusProduct -ErrorAction Stop)
    if ($avProducts.Count -eq 0) {
        Write-Note '登録された AV 製品はありません。'
    }
    foreach ($av in $avProducts) {
        # productState のビット解釈 (公式ドキュメントなし・経験則。生の値も併記します)
        $hex = '{0:X6}' -f [int]$av.productState
        $rtByte = $hex.Substring(2, 2)
        $rtOn   = ($rtByte -eq '10' -or $rtByte -eq '11')
        $upByte = $hex.Substring(4, 2)
        Write-Host ('    displayName    : {0}' -f $av.displayName)
        Write-Host ('      pathToSignedProductExe : {0}' -f $av.pathToSignedProductExe)
        Write-Host ('      productState  : {0} (0x{1})' -f $av.productState, $hex)
        Write-Host ('      -> real-time (推定): {0}   signatures: {1}' -f `
            $(if ($rtOn) { 'ON' } else { 'OFF/unknown' }), $(if ($upByte -eq '00') { 'up to date' } else { 'outdated?' }))
    }
} catch {
    Write-Note ('root/SecurityCenter2 を照会できませんでした (Windows Server では存在しません): {0}' -f $_.Exception.Message)
}

# ---- 7.2 Microsoft Defender ----
Write-Sub '7.2 Microsoft Defender'
$mpStatus = $null; $mpPref = $null
try { $mpStatus = Get-MpComputerStatus -ErrorAction Stop } catch { Write-Note ('Get-MpComputerStatus 不可: {0}' -f $_.Exception.Message) }
try { $mpPref   = Get-MpPreference     -ErrorAction Stop } catch { Write-Note ('Get-MpPreference 不可: {0}' -f $_.Exception.Message) }

if ($mpStatus) {
    Show-Props $mpStatus @(
        'AMRunningMode','AMProductVersion','AMEngineVersion','AntivirusEnabled',
        'RealTimeProtectionEnabled','OnAccessProtectionEnabled','IoavProtectionEnabled',
        'BehaviorMonitorEnabled','AntispywareEnabled','IsTamperProtected'
    )
}
if ($mpPref) {
    Show-Props $mpPref @(
        'DisableRealtimeMonitoring','DisableScanningNetworkFiles','DisableScanningMappedNetworkDrivesForFullScan',
        'RealTimeScanDirection','DisableIOAVProtection','DisableBehaviorMonitoring','DisableScriptScanning',
        'ScanAvgCPULoadFactor','ExclusionPath','ExclusionProcess','ExclusionExtension'
    )
}

# GPO / ポリシーで管理されているか (レジストリ読み取りのみ)
$polKey = 'HKLM:\SOFTWARE\Policies\Microsoft\Windows Defender\Scan'
try {
    $pol = Get-ItemProperty -Path $polKey -ErrorAction Stop
    Write-Host ('    [Policy] {0}' -f $polKey)
    Show-Props $pol @('DisableScanningNetworkFiles','DisableScanningMappedNetworkDrivesForFullScan')
} catch {
    Write-Note 'GPO ポリシー (Windows Defender\Scan) は設定されていません = 既定値が有効。'
}

Write-Host ''
Write-Host '    [既定値についての事実 / what the documented defaults actually are]' -ForegroundColor White
Write-Note 'Defender CSP の DDF: Defender/AllowScanningNetworkFiles の DefaultValue = 0 (= ネットワークファイルをスキャンしない)。'
Write-Note 'ADMX "Scan network files": 「無効または未構成ならネットワークファイルはスキャンされない」。GP 既定 = Disabled。'
Write-Note '=> 未管理のマシンでは Get-MpPreference の DisableScanningNetworkFiles は True になるのが正常です。'
Write-Note '注意: Set-MpPreference のリファレンスページはこれと正反対のことを書いていますが、それは同ページ全体に'
Write-Note '      共通するテンプレートの誤記です (-DisableScriptScanning の説明も同様に破綻しています)。ドキュメントではなく'
Write-Note '      実機の値 (上の出力) を読んでください。'
Write-Host ''
Write-Warn2 'ただし「既定でネットワークファイルをスキャンしない」= Defender 説が消える、ではありません。'
Write-Note 'DisableScanningNetworkFiles は Scan ノード配下 = スキャンエンジンの走査対象の話です。'
Write-Note 'リアルタイム/オンアクセス経路は別設定で、いずれも既定 ON:'
Write-Note '  AllowRealtimeMonitoring=1 / AllowOnAccessProtection=1 / AllowIOAVProtection=1 / RealTimeScanDirection=0 (双方向・全ファイル)。'
Write-Note 'Microsoft も「リアルタイム保護/オンアクセス保護が有効なら、スキャンはネットワーク共有も含む」と明記しています。'
Write-Note '=> 既定構成でも UNC パスの CreateFile でオンアクセススキャンは起こり得ます。上の (a2) の実測で判断してください。'

if ($mpStatus -and $mpStatus.PSObject.Properties['AMRunningMode']) {
    $mode = "$($mpStatus.AMRunningMode)"
    Write-Host ''
    if ($mode -match 'Passive|EDR|SxS') {
        Write-Warn2 ('Defender は {0} モードです。オンアクセススキャンをしているのは Defender ではなく サードパーティ AV です。' -f $mode)
        Write-Warn2 'この場合 Defender Performance Analyzer を回してもレポートはほぼ空になり、それは「AV が無実」の証拠ではありません。'
    } elseif ($mode -match 'Normal') {
        Write-Note 'Defender は Normal (アクティブ) です。=> Defender Performance Analyzer が有効な追試になります (下の NEXT STEPS 参照)。'
    }
}

# ---- 7.3 ファイルシステムミニフィルタ (サードパーティ AV の実体) ----
Write-Sub '7.3 ファイルシステムミニフィルタ / minifilter drivers (fltmc)'
$avFilters = @()
$fltOk = $false
try {
    $flt = & fltmc.exe filters 2>&1
    if ($LASTEXITCODE -eq 0 -and $flt) {
        $fltOk = $true
        foreach ($line in $flt) {
            $t = "$line".Trim()
            if ($t -match '^(\S+)\s+(\d+)\s+(\d+)\s+(\d+)\s*$') {
                $fname = $Matches[1]; $alt = [int]$Matches[3]
                $isAv  = ($alt -ge 320000 -and $alt -le 329999)
                $avFilters += [pscustomobject]@{ Filter = $fname; Altitude = $alt; AntiVirusRange = $isAv }
            }
        }
        $avFilters | Sort-Object Altitude | Format-Table Filter, Altitude, AntiVirusRange -AutoSize | Out-String | Write-Host
        Write-Note 'アルティチュード 320000-329999 は FSFilter Anti-Virus 帯です。ここに載るドライバがオンアクセススキャンの実行主体です。'
    } else {
        Write-Note 'fltmc を実行できませんでした (管理者権限が必要です)。'
    }
} catch {
    Write-Note ('fltmc を実行できませんでした: {0}' -f $_.Exception.Message)
}

# fltmc が使えない場合のフォールバック: 既知の AV ドライバ名を検索 (非管理者でも可)
Write-Note '既知の AV ドライバの検出 (非管理者でも可 / Win32_SystemDriver):'
$knownAv = @{
    'WdFilter'       = 'Microsoft Defender'
    'TmXPflt'        = 'Trend Micro'
    'TmPreFlt'       = 'Trend Micro'
    'tmevtmgr'       = 'Trend Micro'
    'TmFilter'       = 'Trend Micro'
    'eamonm'         = 'ESET'
    'ehdrv'          = 'ESET'
    'epfwwfp'        = 'ESET'
    'SophosED'       = 'Sophos'
    'savonaccess'    = 'Sophos'
    'sophosntpaccess'= 'Sophos'
    'mfeaskm'        = 'McAfee'
    'mfehidk'        = 'McAfee'
    'mfencfilter'    = 'McAfee'
    'SymEFA'         = 'Symantec'
    'eeCtrl'         = 'Symantec'
    'BHDrvx64'       = 'Symantec'
    'SISIPSFileFilter'= 'Symantec'
    'csagent'        = 'CrowdStrike'
    'SentinelMonitor'= 'SentinelOne'
    'CarbonBlackK'   = 'Carbon Black'
    'klif'           = 'Kaspersky'
    'klam'           = 'Kaspersky'
    'avgntflt'       = 'Avira'
    'atrsdfw'        = 'Altiris/Symantec'
    'CyOptics'       = 'Cylance'
    'FeKern'         = 'FireEye'
    'HevFileFilter'  = 'HitmanPro'
    'PSINPROC'       = 'Panda'
    'V3Flt'          = 'AhnLab'
    'FSFilter'       = 'F-Secure'
}
$foundAvDrivers = @()
try {
    $drv = Get-CimInstance -ClassName Win32_SystemDriver -ErrorAction Stop | Where-Object { $_.State -eq 'Running' }
    foreach ($d in $drv) {
        foreach ($k in $knownAv.Keys) {
            if ($d.Name -like ("*" + $k + "*")) {
                $foundAvDrivers += [pscustomobject]@{ Driver = $d.Name; Vendor = $knownAv[$k]; DisplayName = $d.DisplayName }
                break
            }
        }
    }
    if ($foundAvDrivers.Count -gt 0) {
        $foundAvDrivers | Sort-Object Vendor, Driver -Unique | Format-Table Vendor, Driver, DisplayName -AutoSize | Out-String | Write-Host
    } else {
        Write-Note '既知の AV ドライバは検出されませんでした。'
    }
} catch {
    Write-Note ('Win32_SystemDriver を照会できませんでした: {0}' -f $_.Exception.Message)
}

$thirdPartyAv = @($foundAvDrivers | Where-Object { $_.Vendor -ne 'Microsoft Defender' })
$avInAvRange  = @($avFilters | Where-Object { $_.AntiVirusRange -and $_.Filter -ne 'WdFilter' })

# ---- 7.4 SMB 接続 / 構成 ----
Write-Sub '7.4 SMB 接続 / SMB connection (dialect, signing, encryption)'
$smbConn = $null
try {
    if ($serverName) { $smbConn = @(Get-SmbConnection -ServerName $serverName -ErrorAction Stop) }
    else             { $smbConn = @(Get-SmbConnection -ErrorAction Stop) }
    if ($smbConn.Count -eq 0) {
        Write-Note 'この時点でアクティブな SMB 接続がありません (アイドルで切断された?)。'
    }
    foreach ($c in $smbConn) {
        Write-Host ('    -- {0}\{1}' -f $c.ServerName, $c.ShareName)
        Show-Props $c @('Dialect','NumOpens','Encrypted','Signed','ContinuouslyAvailable','Compression','UserName','Credential')
    }
} catch {
    Write-Note ('Get-SmbConnection 不可: {0}' -f $_.Exception.Message)
}
if ($smbConn -and $serverName) {
    $otherServers = @($smbConn | Where-Object { $_.ServerName -and ($_.ServerName -ne $serverName) })
    if ($otherServers.Count -gt 0) {
        Write-Warn2 ('要求したサーバ名 ({0}) と実際の接続先が違います => DFS 参照が挟まっている可能性: {1}' -f `
            $serverName, (($otherServers | ForEach-Object { $_.ServerName }) -join ', '))
    }
}

Write-Sub '7.5 SMB クライアント構成 / Get-SmbClientConfiguration'
$smbCfg = $null
try {
    $smbCfg = Get-SmbClientConfiguration -ErrorAction Stop
    Show-Props $smbCfg @(
        'EnableSecuritySignature','RequireSecuritySignature','EnableMultiChannel',
        'EnableLargeMtu','EnableBandwidthThrottling','UseOpportunisticLocking','OplocksDisabled',
        'DirectoryCacheLifetime','DirectoryCacheEntriesMax','FileInfoCacheLifetime','FileInfoCacheEntriesMax',
        'FileNotFoundCacheLifetime','SessionTimeout','ExtendedSessionTimeout','MaxCmds',
        'MaximumConnectionCountPerServer','ConnectionCountPerRssNetworkInterface','WindowSizeThreshold',
        'EnableCompressedTraffic','DormantFileLimit','KeepConn'
    )
    Write-Note '既定値: DirectoryCacheLifetime=10s, FileInfoCacheLifetime=10s, FileNotFoundCacheLifetime=5s, SessionTimeout=60s。'
    Write-Note 'UseOpportunisticLocking=False / OplocksDisabled=True になっていると、キャッシュが効かず毎回往復します。'
} catch {
    Write-Note ('Get-SmbClientConfiguration 不可: {0}' -f $_.Exception.Message)
}

Write-Sub '7.6 マルチチャネル / NIC'
try {
    $mc = @(Get-SmbMultichannelConnection -ErrorAction Stop)
    if ($mc.Count -gt 0) {
        $mc | Format-Table Server*, ClientIpAddress, ServerIpAddress, ClientRss*, ClientRdma*, CurrentChannels, MaxChannels -AutoSize | Out-String | Write-Host
    } else {
        Write-Note 'マルチチャネル接続はありません (NAS が対応していない / 1 NIC のみ)。それ自体は異常ではありません。'
    }
} catch { Write-Note ('Get-SmbMultichannelConnection 不可: {0}' -f $_.Exception.Message) }

$linkSpeed = $null
try {
    $ni = @(Get-SmbClientNetworkInterface -ErrorAction Stop)
    if ($ni.Count -gt 0) {
        $ni | Format-Table InterfaceIndex, FriendlyName, IpAddresses, RssCapable, RdmaCapable, LinkSpeed -AutoSize | Out-String | Write-Host
        $linkSpeed = ($ni | Where-Object { $_.LinkSpeed } | Sort-Object LinkSpeed -Descending | Select-Object -First 1).LinkSpeed
    }
} catch { Write-Note ('Get-SmbClientNetworkInterface 不可: {0}' -f $_.Exception.Message) }

# ---- 7.7 サーバの正体 (NAS アプライアンスか Windows Server か) ----
Write-Sub '7.7 サーバの正体 / is the server a NAS appliance or a Windows Server?'
$serverGuess = 'undetermined'
$pingMs = $null
$ports = @{}
if ($serverName) {
    # DNS
    try {
        $addrs = [System.Net.Dns]::GetHostAddresses($serverName)
        Write-Host ('    DNS            : {0}' -f (($addrs | ForEach-Object { $_.IPAddressToString }) -join ', '))
    } catch { Write-Note ('DNS 解決失敗: {0}' -f $_.Exception.Message) }

    # ICMP RTT
    try {
        $p = New-Object System.Net.NetworkInformation.Ping
        $rtts = @()
        for ($i = 0; $i -lt 4; $i++) {
            $rep = $p.Send($serverName, 2000)
            if ($rep.Status -eq 'Success') { $rtts += [double]$rep.RoundtripTime }
        }
        if ($rtts.Count -gt 0) {
            $pingMs = (Get-Median $rtts)
            Write-Host ('    ICMP RTT       : median {0} ms  ({1} replies)' -f (Fmt $pingMs 1), $rtts.Count)
        } else {
            Write-Note 'ICMP に応答しません (ファイアウォールでブロックされている可能性)。'
        }
    } catch { Write-Note 'ICMP 測定に失敗しました。' }

    # ポートスキャン (読み取りのみ・短いタイムアウト)
    $probe = @(
        @{ Port = 445;  Name = 'SMB' }
        @{ Port = 139;  Name = 'NetBIOS' }
        @{ Port = 135;  Name = 'MSRPC (Windows の強い指標)' }
        @{ Port = 3389; Name = 'RDP (Windows)' }
        @{ Port = 5985; Name = 'WinRM (Windows)' }
        @{ Port = 22;   Name = 'SSH (Linux/NAS)' }
        @{ Port = 80;   Name = 'HTTP (NAS 管理 UI)' }
        @{ Port = 443;  Name = 'HTTPS (NAS 管理 UI)' }
        @{ Port = 111;  Name = 'rpcbind/NFS (NAS)' }
        @{ Port = 2049; Name = 'NFS (NAS)' }
        @{ Port = 548;  Name = 'AFP (NAS)' }
        @{ Port = 5000; Name = 'Synology DSM (NAS)' }
        @{ Port = 8080; Name = 'QNAP/other admin UI (NAS)' }
    )
    foreach ($pp in $probe) {
        $open = Test-TcpPort -HostName $serverName -Port $pp.Port -TimeoutMs 700
        $ports[[int]$pp.Port] = $open
        Write-Host ('    tcp/{0,-5} {1,-32} : {2}' -f $pp.Port, $pp.Name, $(if ($open) { 'OPEN' } else { 'closed/filtered' }))
    }

    # 管理共有 (ADMIN$ / C$) の有無 = Windows の強い指標
    $adminShares = $false
    try {
        $nv = & net.exe view ("\\" + $serverName) /all 2>&1
        $nvText = ($nv | Out-String)
        if ($nvText -match 'ADMIN\$' -or $nvText -match '\bC\$') { $adminShares = $true }
        Write-Host ('    net view       : admin shares (ADMIN$/C$) = {0}' -f $adminShares)
    } catch { Write-Note 'net view を実行できませんでした。' }

    # WMI/CIM でリモート OS を取りに行く (Windows なら通ることがある)
    # WinRM ポートが閉じているときは試さない (WSMan の接続タイムアウトで数十秒待たされるため)
    $remoteOs = $null
    if ($ports[5985]) {
        try {
            $remoteOs = Get-CimInstance -ClassName Win32_OperatingSystem -ComputerName $serverName -OperationTimeoutSec 5 -ErrorAction Stop
            Write-Good ('リモート WMI 応答: {0} (=> Windows Server です)' -f $remoteOs.Caption)
        } catch {
            Write-Note ('WinRM は開いていますが CIM 照会に失敗しました: {0}' -f $_.Exception.Message)
        }
    } else {
        Write-Note 'WinRM (5985) が閉じているためリモート WMI 照会はスキップしました (NAS でも、ファイアウォールのある Windows でもこうなります。単独では決め手になりません)。'
    }

    # ヒューリスティック判定
    $winScore = 0; $nasScore = 0
    if ($ports[135])  { $winScore += 2 }
    if ($ports[3389]) { $winScore += 2 }
    if ($ports[5985]) { $winScore += 2 }
    if ($adminShares) { $winScore += 3 }
    if ($remoteOs)    { $winScore += 4 }
    if ($ports[22])   { $nasScore += 2 }
    if ($ports[5000]) { $nasScore += 3 }
    if ($ports[548])  { $nasScore += 2 }
    if ($ports[111] -or $ports[2049]) { $nasScore += 2 }
    if (($ports[80] -or $ports[443]) -and -not $ports[135]) { $nasScore += 2 }
    if ($winScore -gt $nasScore + 1)      { $serverGuess = 'Windows Server の可能性が高い / likely Windows Server' }
    elseif ($nasScore -gt $winScore + 1)  { $serverGuess = 'NAS アプライアンス (Linux/Samba) の可能性が高い / likely NAS appliance' }
    else                                  { $serverGuess = '判定不能 / undetermined' }
    Write-Host ''
    Write-Host ('    => 推定: {0}   (windows score {1} / nas score {2})' -f $serverGuess, $winScore, $nasScore) -ForegroundColor Magenta
    Write-Note '"Storage-4" のような名前は NAS を示唆しますが、名前だけでは決まりません。上のポート/管理共有/WMI の結果で判断してください。'
}

# ---- 7.8 カウンタ収集の回収 ----
Write-Sub '7.8 SMB クライアント共有カウンタ / per-share counters (計測中のサンプル)'
if ($counterJob) {
    try {
        Start-Sleep -Milliseconds 1200
        Stop-Job -Job $counterJob -ErrorAction SilentlyContinue
        $samplesOut = @(Receive-Job -Job $counterJob -ErrorAction SilentlyContinue)
        Remove-Job -Job $counterJob -Force -ErrorAction SilentlyContinue
        $err = @($samplesOut | Where-Object { $_.Path -eq 'COUNTER-ERROR' })
        if ($err.Count -gt 0) {
            Write-Note ('カウンタ収集エラー: {0}' -f $err[0].Err)
            Write-Note '非管理者の場合は、ローカル "Performance Monitor Users" グループに入っている必要があります。'
        }
        $good = @($samplesOut | Where-Object { $_.Path -ne 'COUNTER-ERROR' -and $_.Path })
        if ($good.Count -gt 0) {
            Write-Host ('    samples: {0}' -f $good.Count)
            Write-Host ''
            Write-Host ('    {0,-62} {1,10} {2,12} {3,12}' -f 'counter', 'min', 'median', 'max')
            Write-Host ('    ' + ('-' * 100))
            $grp = $good | Group-Object Path
            foreach ($g in ($grp | Sort-Object Name)) {
                $vals = @($g.Group | ForEach-Object { [double]$_.Value })
                $st = Get-Stat $vals
                $short = $g.Name
                if ($short.Length -gt 62) { $short = '...' + $short.Substring($short.Length - 59) }
                Write-Host ('    {0,-62} {1,10} {2,12} {3,12}' -f $short, (Fmt $st.Min 4), (Fmt $st.Median 4), (Fmt $st.Max 4))
            }
            $script:CounterSamples = $good
        } else {
            Write-Note 'カウンタサンプルが取得できませんでした。'
            $script:CounterSamples = @()
        }
    } catch {
        Write-Note ('カウンタ回収に失敗: {0}' -f $_.Exception.Message)
        $script:CounterSamples = @()
    }
} else {
    Write-Note 'カウンタ収集は行われませんでした (管理者権限なし / カウンタセットなし)。'
    Write-Note '手動で見る場合 (管理者):'
    Write-Note "  Get-Counter -ListSet 'SMB Client Shares' | Select-Object -ExpandProperty PathsWithInstances"
    Write-Note '  typeperf "\SMB Client Shares(*)\Avg. sec/Read" "\SMB Client Shares(*)\Data Bytes/sec" -si 1 -sc 180 -f CSV -o C:\temp\smb.csv'
    $script:CounterSamples = @()
}
Write-Note '重要: SMB Client Shares の Avg. sec/Read などは「データ面」の指標で、CreateFile の時間は測れません。'
Write-Note '決定的なのは停止中の Data Bytes/sec です。ほぼ 0 なら 1 バイトも流れていない = 「回線が遅い」説は否定されます。'

# ---- 7.9 コントロールディレクトリ (任意) ----
if ($ControlPath -and (Test-Path -LiteralPath $ControlPath -PathType Container)) {
    Write-Sub '7.9 コントロールディレクトリとの比較 / control directory'
    try {
        $cf = Get-ChildItem -LiteralPath $ControlPath -File -ErrorAction Stop | Select-Object -First 3
        foreach ($c in $cf) {
            $cr = Measure-OneFile -FilePath $c.FullName -DoFullRead:(-not $SkipFullRead)
            Write-Host ('    {0,-40} stat {1,8} ms  open {2,8} ms  read {3,8} ms  {4,7} MB/s' -f `
                $cr.Name, (Fmt $cr.StatMs 2), (Fmt $cr.OpenMs 1), (Fmt $cr.ReadMs 1), (Fmt $cr.MBps 1))
        }
        Write-Note 'ここが速くて img01 が遅ければ、原因は「このディレクトリ/これらのファイル固有」です。'
    } catch { Write-Note ('コントロール測定に失敗: {0}' -f $_.Exception.Message) }
}

# ==============================================================================
# 8. VERDICT
# ==============================================================================
Write-Section 'VERDICT — 測定値から読み取れること / what the numbers say'

$medOpen  = $openStat.Median
$medStat  = $statStat.Median
$medRead  = $readStat.Median
$maxOpen  = $openStat.Max
$nat0     = $deep.NatAccess0Ms
$natRd    = $deep.NatReadMs
$natNR    = $deep.NatNoRecallMs
$shareViolation = ($deep.NatShareNoneErr -eq 32)

Write-Host ''
Write-Host '  【測定サマリ / measured summary】' -ForegroundColor White
Write-Host ('    stat  中央値 : {0} ms' -f (Fmt $medStat 3))
Write-Host ('    open  中央値 : {0} ms   (最大 {1} ms)' -f (Fmt $medOpen 1), (Fmt $maxOpen 1))
Write-Host ('    read  中央値 : {0} ms   ({1} MB/s)' -f (Fmt $medRead 1), (Fmt $aggMBps 2))
if ($script:HasNative) {
    Write-Host ('    CreateFile access=0          : {0} ms' -f (Fmt $nat0 2))
    Write-Host ('    CreateFile GENERIC_READ      : {0} ms' -f (Fmt $natRd 2))
    Write-Host ('    CreateFile GR + NO_RECALL    : {0} ms' -f (Fmt $natNR 2))
}
Write-Host ''

$findings = New-Object System.Collections.ArrayList
function Add-Finding { param([string]$Rank, [string]$Text) [void]$findings.Add([pscustomobject]@{ Rank = $Rank; Text = $Text }) }

$SLOW = 1000.0    # これ以上なら「遅い」
$FAST = 50.0      # これ以下なら「正常」

# open 経路が実際にブロックされているか。AV が入っているという「状況証拠」を
# 主犯扱いするか参考情報扱いするかを、この実測フラグで切り替える。
$openPathBlocked = $false
if ($medOpen -ne $null -and $medOpen -gt 200.0) { $openPathBlocked = $true }
if ($natRd   -ne $null -and $natRd   -gt 100.0) { $openPathBlocked = $true }

# --- 再現しなかったケース ---
if ($medOpen -ne $null -and $medOpen -lt $FAST -and ($medRead -eq $null -or $medRead -lt 500)) {
    Add-Finding 'A' ("いま計測した限りでは共有は正常です (open 中央値 {0} ms)。症状が再現していません。" -f (Fmt $medOpen 1))
    Add-Finding 'A' "=> 停止が「間欠的」または「状態依存」であることを意味します。ハングしている最中に、別ウィンドウでこのスクリプトを再実行してください。"
    Add-Finding 'A' "=> あわせて、取得ソフトが動作中/直後かどうか、NAS 側でスクラブ・RAID リビルド・バックアップ・ウイルススキャンが走っていないかを確認してください。"
}

# --- 決定的分岐: access=0 は速いが GENERIC_READ が遅い ---
# 判定は「絶対値」ではなく「比」で行う。1 回 300 ms でも 3001 枚なら 15 分になるため、
# 1000 ms のしきい値だけで切ると重要なケースを取りこぼす。
if ($script:HasNative -and $nat0 -ne $null -and $natRd -ne $null) {
    $accessRatio = $null
    if ($nat0 -gt 0.01) { $accessRatio = $natRd / $nat0 }
    $readOpenIsSlow = ($natRd -gt 100.0)
    $baseOpenIsFast = ($nat0 -lt 50.0)

    if ($readOpenIsSlow -and $baseOpenIsFast -and $accessRatio -ne $null -and $accessRatio -ge 5.0) {
        Add-Finding '1' ("★ CreateFile 往復そのものは速い ({0} ms) のに、読み取り権 (GENERIC_READ) を要求した瞬間に {1} ms かかっています (比 {2}x)。" -f (Fmt $nat0 1), (Fmt $natRd 1), (Fmt $accessRatio 0))
        Add-Finding '1' "=> これは『読み取りをトリガにして起動する何か』が犯人であることのほぼ決定的な証拠です。候補は 2 つだけ: (i) オンアクセス AV スキャン、(ii) HSM/階層化ストレージのリコール。"
        if ($natNR -ne $null -and $natNR -lt ($natRd / 5.0)) {
            Add-Finding '1' ("=> さらに FILE_FLAG_OPEN_NO_RECALL を付けると {0} ms に落ちます。これは HSM / 階層化ストレージのリコールです (原因 3)。" -f (Fmt $natNR 1))
        } elseif ($offlineCount -gt 0 -or $recallCount -gt 0) {
            Add-Finding '1' ("=> {0} 個のファイルに OFFLINE/RECALL 属性が付いています。HSM / コールドティアからの復元です (原因 3)。" -f ([Math]::Max($offlineCount, $recallCount)))
        } else {
            Add-Finding '1' "=> NO_RECALL でも速くならず、OFFLINE/RECALL 属性も無い => HSM ではなくオンアクセス AV スキャンです (原因 1)。"
        }
    }
    elseif ($readOpenIsSlow -and $nat0 -gt 100.0) {
        Add-Finding '1' ("★ アクセス権を要求しない CreateFile (access=0) でさえ {0} ms かかっています (GENERIC_READ は {1} ms)。" -f (Fmt $nat0 1), (Fmt $natRd 1))
        Add-Finding '1' "=> コストは『読み取り』ではなく『ハンドルを開くこと自体』にあります。AV スキャンや HSM リコールでは説明できません。"
        Add-Finding '1' "=> 候補: oplock / lease ブレーク待ち、サーバ側のメタデータ処理の詰まり、DFS 参照、認証、NAS の過負荷。"
        Add-Finding '1' "   Windows の oplock / lease ブレーク応答タイムアウトは 35 秒です。2 回連続で待つと約 70 秒 — 観測された 91 秒に近い値になります。"
    }
    elseif (-not $readOpenIsSlow) {
        Add-Finding '-' ("CreateFile は access=0 で {0} ms、GENERIC_READ で {1} ms でした。open 経路にブロックはありません。" -f (Fmt $nat0 2), (Fmt $natRd 2))
        Add-Finding '-' "=> 少なくともこの計測時点では、オンアクセス AV スキャン (原因 1) と HSM リコール (原因 3) は否定されます。"
    }
}
if (-not $script:HasNative) {
    Add-Finding '*' "ネイティブ CreateFile 計測 (access=0 vs GENERIC_READ) が使えませんでした。この比較なしでは、AV / HSM / oplock / 低速回線を厳密に切り分けることはできません。Process Monitor (NEXT STEPS 2) で代替してください。"
}

# --- 共有違反 (取得ソフトがファイルを掴んでいる) ---
if ($shareViolation) {
    Add-Finding '2' "share=NONE の open が ERROR_SHARING_VIOLATION でした => 他プロセス (顕微鏡の取得ソフトなど) が同じファイルのハンドルを保持しています (原因 2)。"
    if ($medOpen -ne $null -and $medOpen -gt $SLOW) {
        Add-Finding '2' "=> かつ通常の open も遅いので、oplock/lease ブレーク待ちが強く疑われます。書き手側にサーバがブレークを要求し、応答があるまで open がブロックします。"
    } else {
        Add-Finding '2' "=> ただし共有違反は『即エラー』を返すだけで、それ自体は 91 秒のハングを説明しません。open が速いなら、これは主原因ではありません。"
    }
} elseif ($script:HasNative -and $deep.NatShareNoneErr -eq 0) {
    Add-Finding '-' "share=NONE で open できました => 他プロセスはこのファイルを掴んでいません。共有違反 / oplock 説 (原因 2) は否定されます。"
}

# --- 階層化ストレージ ---
if ($offlineCount -gt 0 -or $recallCount -gt 0 -or $reparseCount -gt 0) {
    Add-Finding '3' ("OFFLINE={0} / RECALL={1} / REPARSE={2} 個のファイルに階層化属性があります => HSM・Azure File Sync・NAS コールドティア (原因 3)。" -f $offlineCount, $recallCount, $reparseCount)
} elseif ($enumCount -gt 0) {
    Add-Finding '-' ("{0} 個のファイルすべてに OFFLINE/RECALL/REPARSE 属性がありません => 階層化ストレージ説 (原因 3) は弱い、あるいはクライアントから見えない NAS 内部のティアリングです。" -f $enumCount)
}

# --- 転送が遅い / 回線が遅い ---
if ($medOpen -ne $null -and $medRead -ne $null) {
    if ($medOpen -lt 200 -and $aggMBps -ne $null -and $aggMBps -lt 20) {
        Add-Finding '4' ("open は速い ({0} ms) のに読み取りが {1} MB/s しか出ていません => データ経路が律速です (原因 4: 回線が遅い/飽和、NAS が過負荷/リビルド中)。" -f (Fmt $medOpen 1), (Fmt $aggMBps 1))
        if ($linkSpeed) { Add-Finding '4' ("   NIC のリンク速度: {0} bps。実効 {1} MB/s がこれに対して妥当かを確認してください。" -f $linkSpeed, (Fmt $aggMBps 1)) }
    }
    if ($aggMBps -ne $null -and $aggMBps -ge 20 -and $medOpen -gt $SLOW) {
        Add-Finding '4' ("読み取りは {0} MB/s 出ています => 帯域は問題ありません。『回線が遅い』説 (原因 4) は否定されます。" -f (Fmt $aggMBps 1))
    }
}

# --- 単純に共有が遅い (これも答えのひとつ) ---
if ($medOpen -ne $null -and $medOpen -ge 100 -and $medOpen -le $SLOW -and -not $file1Outlier) {
    Add-Finding '4' ("open が全ファイルで {0} ms 前後、外れ値なし => 『この共有はそもそも遅い』が答えである可能性があります。" -f (Fmt $medOpen 1))
    if ($projSec) {
        Add-Finding '4' ("   この速度だと 3001 枚で約 {0} 分かかります。パイプライン側で並列化 (ThreadPool で 8-16 並列) するのが最も現実的な対策です。" -f (Fmt ($projSec / 60.0) 1))
    }
}

# --- 1 ファイル目だけ遅い ---
if ($file1Outlier) {
    Add-Finding '5' ("1 ファイル目の open だけが中央値の {0} 倍でした => 一回限りのコストです (SMB セッション確立 / 認証 / 初回スキャン / 初回ティア復元)。" -f (Fmt $file1Ratio 1))
    Add-Finding '5' "=> 観測された『1/3001 で 91 秒』は、まさにこの一回限りのコストである可能性があります。2 枚目以降が速いなら、全体としては問題になりません。"
}

# --- SMB 署名/暗号化 ---
if ($smbConn) {
    foreach ($c in $smbConn) {
        if ($c.PSObject.Properties['Encrypted'] -and $c.Encrypted) {
            Add-Finding '5' ("SMB 暗号化が有効です ({0}\{1})。CPU コストは増えますが、これ単体で 1 回の open が数十秒かかることはありません。" -f $c.ServerName, $c.ShareName)
        }
        if ($c.PSObject.Properties['Dialect'] -and $c.Dialect -and ("$($c.Dialect)" -like '1.*' -or "$($c.Dialect)" -like '2.0*')) {
            Add-Finding '5' ("SMB ダイアレクトが {0} に落ちています。SMB1/2.0 ではリース・大容量 MTU・パイプライン化が効かず、全体的に遅くなります。" -f $c.Dialect)
        }
    }
}
if ($smbCfg) {
    if ($smbCfg.PSObject.Properties['UseOpportunisticLocking'] -and ($smbCfg.UseOpportunisticLocking -eq $false)) {
        Add-Finding '5' "クライアントで日和見ロック (oplock) が無効化されています => キャッシュが効かず、全操作がサーバ往復になります。"
    }
    if ($smbCfg.PSObject.Properties['EnableBandwidthThrottling'] -and ($smbCfg.EnableBandwidthThrottling -eq $true)) {
        Add-Finding '5' "EnableBandwidthThrottling が有効です (既定 True)。高遅延リンクでスループットを制限することがあります。"
    }
}

# --- AV の存在 (これはあくまで状況証拠。実測でランクを変える) ---
$avRank = $(if ($openPathBlocked) { '1' } else { '-' })
if ($thirdPartyAv.Count -gt 0 -or $avInAvRange.Count -gt 0) {
    $vendors = (@($thirdPartyAv | ForEach-Object { $_.Vendor }) + @($avInAvRange | ForEach-Object { $_.Filter })) | Sort-Object -Unique
    Add-Finding $avRank ("サードパーティ AV のカーネルフィルタが動作しています: {0}" -f ($vendors -join ', '))
    if ($openPathBlocked) {
        Add-Finding '1' "=> open 経路が実際にブロックされているので、これが最有力候補です。日本の大学/研究機関環境では特に。これらの製品は Defender と違い、ネットワークファイルを既定でスキャンすることがよくあります。"
        Add-Finding '1' "=> Defender Performance Analyzer では測れません (Microsoft-Antimalware-Engine のイベントしか拾わないため)。ベンダのログ/管理コンソールで、この UNC パスへのオンアクセススキャンが記録されているかを確認してください。"
    } else {
        Add-Finding '-' "=> ただし今回の計測では open 経路にブロックが見られませんでした。AV が入っていること自体は、この症状の証拠にはなりません。"
    }
}
if ($mpStatus -and $mpStatus.PSObject.Properties['RealTimeProtectionEnabled'] -and $mpStatus.RealTimeProtectionEnabled -and `
    $mpStatus.PSObject.Properties['AMRunningMode'] -and "$($mpStatus.AMRunningMode)" -match 'Normal') {
    Add-Finding $avRank "Defender が Normal モード + リアルタイム保護 ON です。既定でも UNC の CreateFile でオンアクセススキャンが起こり得ます (Scan ノードの DisableScanningNetworkFiles とは別経路のため)。"
}

# --- サーバが NAS の場合の注意 ---
if ($serverGuess -match 'NAS') {
    Add-Finding '6' "サーバは NAS アプライアンスの可能性が高いです。=> サーバ側のウイルススキャン (ClamAV / Trend Micro NAS 版など)、スケジュールスキャン、RAID リビルド、スクラブ、重複排除、スナップショット、コールドティアはクライアントからは一切見えません。"
    Add-Finding '6' "=> NAS の管理画面で以下を確認してください: (1) アンチウイルスパッケージの有無と実行中スキャン、(2) ストレージプールの健全性 / リビルド・スクラブの進行状況、(3) 階層化/クラウド同期の設定、(4) SMB ログの警告。"
} elseif ($serverGuess -match 'Windows') {
    Add-Finding '6' "サーバは Windows Server の可能性が高いです。=> サーバ側で 'openfiles /query'、Defender の状態、FSRM、バックアップジョブ、DFS 名前空間を確認できます。"
}

# --- ディレクトリ固有 ---
if ($enumMs -ne $null -and $enumCount -gt 0) {
    $perEntry = $enumMs / $enumCount
    if ($enumMs -gt 10000) {
        Add-Finding '6' ("ディレクトリ列挙に {0} 秒 ({1} 件, {2} ms/件) かかりました => 大きなディレクトリ自体もコストになっています (原因 6)。" -f (Fmt ($enumMs/1000.0) 1), $enumCount, (Fmt $perEntry 2))
    } else {
        Add-Finding '-' ("ディレクトリ列挙は {0} ms / {1} 件 = 正常です。ディレクトリサイズ (原因 6) は主因ではありません。" -f (Fmt $enumMs 0), $enumCount)
    }
}

# --- メタデータキャッシュについての正直な注記 ---
if ($medStat -ne $null -and $medStat -lt 1.0) {
    Add-Finding '*' ("stat の中央値が {0} ms = ネットワーク往復としてはあり得ない速さです。" -f (Fmt $medStat 3))
    Add-Finding '*' "=> つまり『6000 回のメタデータ操作が 29 秒で終わった』ことは『メタデータが安い』証拠ではなく、SMB リダイレクタのディレクトリ/ファイル情報キャッシュから返っていた証拠です。"
    Add-Finding '*' "=> したがって『メタデータは速いのに open が遅い』という事実だけでは AV は立証できません。上の access=0 vs GENERIC_READ の比較が唯一の切り分けです。"
}

# --- カウンタからの所見 ---
if ($script:CounterSamples -and $script:CounterSamples.Count -gt 0) {
    $bytesSamples = @($script:CounterSamples | Where-Object { $_.Path -match 'data bytes/sec|bytes/sec' } | ForEach-Object { [double]$_.Value })
    if ($bytesSamples.Count -gt 0) {
        $bmax = ($bytesSamples | Measure-Object -Maximum).Maximum
        if ($bmax -lt 1024) {
            Add-Finding '4' "計測中、SMB の Data Bytes/sec が事実上 0 のままでした => バイトが 1 つも流れていない時間帯があります。『回線が遅い』では説明できません (遅い回線でもバイトは流れます)。"
        } else {
            Add-Finding '-' ("計測中の SMB Data Bytes/sec の最大は {0} MB/s でした。" -f (Fmt ($bmax / 1MB) 2))
        }
    }
    $readLat = @($script:CounterSamples | Where-Object { $_.Path -match 'sec/read' } | ForEach-Object { [double]$_.Value })
    if ($readLat.Count -gt 0) {
        $rmax = ($readLat | Measure-Object -Maximum).Maximum
        $rmed = Get-Median $readLat
        Add-Finding '-' ("SMB Avg. sec/Read: 中央値 {0} s / 最大 {1} s (LAN の正常値は 0.0005-0.005 s、0.020 s 超が続くなら経路が本当に遅い)。" -f (Fmt $rmed 4), (Fmt $rmax 4))
    }
}

# --- 出力 ---
$order = @('1','2','3','4','5','6','A','*','-')
foreach ($rank in $order) {
    $items = @($findings | Where-Object { $_.Rank -eq $rank })
    if ($items.Count -eq 0) { continue }
    switch ($rank) {
        '1' { Write-Host '  ▼ 最有力 / PRIMARY' -ForegroundColor Red }
        '2' { Write-Host '  ▼ 共有違反・oplock / SHARING & OPLOCKS' -ForegroundColor Yellow }
        '3' { Write-Host '  ▼ 階層化ストレージ / TIERED STORAGE' -ForegroundColor Yellow }
        '4' { Write-Host '  ▼ 回線・スループット / LINK & THROUGHPUT' -ForegroundColor Yellow }
        '5' { Write-Host '  ▼ SMB 設定・一回限りのコスト / SMB CONFIG & ONE-TIME COSTS' -ForegroundColor DarkYellow }
        '6' { Write-Host '  ▼ サーバ側・ディレクトリ / SERVER-SIDE & DIRECTORY' -ForegroundColor DarkYellow }
        'A' { Write-Host '  ▼ 再現しませんでした / NOT REPRODUCED' -ForegroundColor Green }
        '*' { Write-Host '  ▼ 解釈上の注意 / HONEST CAVEAT' -ForegroundColor Magenta }
        '-' { Write-Host '  ▼ 否定された仮説 / RULED OUT' -ForegroundColor Green }
    }
    foreach ($it in $items) { Write-Host ('     - ' + $it.Text) }
    Write-Host ''
}

if ($findings.Count -eq 0) {
    Write-Host '  判定に足る特徴が出ませんでした。上の生の数値を見てください。' -ForegroundColor Yellow
}

# ==============================================================================
# 9. NEXT STEPS
# ==============================================================================
Write-Section 'NEXT STEPS — 次にやること'
Write-Host '  1) ハング中に再実行する (これが一番情報量が多い)。'
Write-Host '     パイプラインが 1/3001 で固まっている、まさにその最中に、別の PowerShell でこのスクリプトを走らせてください。'
Write-Host ''
Write-Host '  2) Process Monitor (Sysinternals) で犯人のドライバを特定する:'
Write-Host '       - procmon64.exe を管理者で起動'
Write-Host '       - Options > Enable Advanced Output   (IRP_MJ_CREATE の詳細が見える)'
Write-Host '       - 列ヘッダ右クリック > Select Columns... > Duration をチェック'
Write-Host '       - Filter: Process Name is python.exe / Path begins with \\yg-storage4'
Write-Host '       - 再現させて停止 > Filter: Duration more than 1'
Write-Host '       - 長い IRP_MJ_CREATE が 1 本だけ見えるなら、その待ちはカーネル内 (AV/HSM) かサーバ応答待ち。'
Write-Host ''
if ($mpStatus -and $mpStatus.PSObject.Properties['AMRunningMode'] -and "$($mpStatus.AMRunningMode)" -match 'Normal') {
    Write-Host '  3) Defender Performance Analyzer (Defender が Normal モードなので有効):'
    Write-Host '       New-MpPerformanceRecording -RecordTo C:\temp\defender.etl     # 記録中にパイプラインを動かす'
    Write-Host '       Get-MpPerformanceReport -Path C:\temp\defender.etl -TopFiles 20 -TopPaths 20 -TopExtensions 10'
    Write-Host '       (要 Defender プラットフォーム 4.18.2108.7 以降。SkipReason 列は 4.18.2206 以降)'
} else {
    Write-Host '  3) Defender Performance Analyzer は今回 役に立ちません。'
    Write-Host '     Defender が Passive/無効、またはサードパーティ AV が実働中だからです。Performance Analyzer は'
    Write-Host '     Microsoft-Antimalware-Engine のイベントしか記録しないため、レポートが空でも「AV は無実」の証拠にはなりません。'
    Write-Host '     サードパーティ製品は、ベンダ自身の管理コンソール/ログで、この UNC パスのオンアクセススキャンを確認してください。'
}
Write-Host ''
Write-Host '  4) もし AV が原因だと判明した場合の除外設定について (適用は管理者判断で):'
Write-Warn2 '     マップドライブ V: を除外しても意味がありません。Microsoft は「マップされたネットワークドライブを除外しないこと。'
Write-Warn2 '     実際のネットワークパスを指定すること」と明記しています。また「ドライブレターの代わりにワイルドカードは使えません」。'
Write-Host '     => 除外するなら UNC 形式 (\\yg-storage4\Storage-4\...) を指定してください。'
Write-Note '     補足: ユーザーセッションでマップされたドライブ (V: がまさにそれ) はそもそもスキャン対象外です。'
Write-Note '           これも「除外は UNC で書く」べき理由になります。'
Write-Host ''
Write-Host '  5) パイプライン側でできること (原因に関わらず効く):'
Write-Host '     - concurrent.futures.ThreadPoolExecutor で 8-16 並列に open/read する (SMB は遅延が支配的なので並列化がよく効く)'
Write-Host '     - Path.resolve() をループ内で呼ばない (Windows では CreateFile を発行します)'
Write-Host '     - 一度ローカル SSD にまとめてコピー (robocopy /MT:16) してから処理する'
Write-Host ''

# ==============================================================================
# 10. コピペ用サマリ
# ==============================================================================
Write-Section 'SUMMARY — この塊をそのまま貼って共有してください / paste this back'
$summary = [ordered]@{
    timestamp          = (Get-Date).ToString('s')
    computer           = $env:COMPUTERNAME
    psVersion          = "$($PSVersionTable.PSVersion)"
    elevated           = $isAdmin
    path               = $uncPath
    server             = $serverName
    share              = $shareName
    serverGuess        = $serverGuess
    pingMedianMs       = $(if ($pingMs -ne $null) { [math]::Round($pingMs, 1) } else { $null })
    shareFirstTouchMs  = $(if ($sessionMs -ne $null) { [math]::Round($sessionMs, 1) } else { $null })
    filesMeasured      = $ok.Count
    statMedianMs       = $(if ($medStat -ne $null) { [math]::Round($medStat, 3) } else { $null })
    statWarmMedianMs   = $(if ($warmStat.Median -ne $null) { [math]::Round($warmStat.Median, 3) } else { $null })
    openMinMs          = $(if ($openStat.Min -ne $null) { [math]::Round($openStat.Min, 1) } else { $null })
    openMedianMs       = $(if ($medOpen -ne $null) { [math]::Round($medOpen, 1) } else { $null })
    openMaxMs          = $(if ($maxOpen -ne $null) { [math]::Round($maxOpen, 1) } else { $null })
    closeMedianMs      = $(if ($closeStat.Median -ne $null) { [math]::Round($closeStat.Median, 2) } else { $null })
    readMedianMs       = $(if ($medRead -ne $null) { [math]::Round($medRead, 1) } else { $null })
    readMBps           = $(if ($aggMBps -ne $null) { [math]::Round($aggMBps, 2) } else { $null })
    e2eMBps            = $(if ($e2eMBps -ne $null) { [math]::Round($e2eMBps, 2) } else { $null })
    file1OutlierRatio  = $(if ($file1Ratio -ne $null) { [math]::Round($file1Ratio, 2) } else { $null })
    file1IsOutlier     = $file1Outlier
    projected3001Sec   = $(if ($projSec -ne $null) { [math]::Round($projSec, 0) } else { $null })
    natOpenAccess0Ms   = $(if ($nat0 -ne $null) { [math]::Round($nat0, 2) } else { $null })
    natOpenGenericMs   = $(if ($natRd -ne $null) { [math]::Round($natRd, 2) } else { $null })
    natOpenNoRecallMs  = $(if ($natNR -ne $null) { [math]::Round($natNR, 2) } else { $null })
    natShareNoneErr    = $deep.NatShareNoneErr
    enumMs             = $(if ($enumMs -ne $null) { [math]::Round($enumMs, 0) } else { $null })
    enumCount          = $enumCount
    offlineCount       = $offlineCount
    reparseCount       = $reparseCount
    recallCount        = $recallCount
    smbDialect         = $(if ($smbConn -and $smbConn.Count -gt 0) { "$($smbConn[0].Dialect)" } else { $null })
    smbEncrypted       = $(if ($smbConn -and $smbConn.Count -gt 0 -and $smbConn[0].PSObject.Properties['Encrypted']) { $smbConn[0].Encrypted } else { $null })
    smbSigningEnabled  = $(if ($smbCfg) { $smbCfg.EnableSecuritySignature } else { $null })
    smbSigningRequired = $(if ($smbCfg) { $smbCfg.RequireSecuritySignature } else { $null })
    defenderMode       = $(if ($mpStatus -and $mpStatus.PSObject.Properties['AMRunningMode']) { "$($mpStatus.AMRunningMode)" } else { $null })
    defenderRtpEnabled = $(if ($mpStatus -and $mpStatus.PSObject.Properties['RealTimeProtectionEnabled']) { $mpStatus.RealTimeProtectionEnabled } else { $null })
    defenderDisableScanningNetworkFiles = $(if ($mpPref -and $mpPref.PSObject.Properties['DisableScanningNetworkFiles']) { $mpPref.DisableScanningNetworkFiles } else { $null })
    registeredAvProducts = (@($avProducts | ForEach-Object { $_.displayName }) -join '; ')
    thirdPartyAvDrivers  = ((@($foundAvDrivers | Where-Object { $_.Vendor -ne 'Microsoft Defender' } | ForEach-Object { $_.Vendor }) | Sort-Object -Unique) -join '; ')
    avAltitudeFilters    = ((@($avFilters | Where-Object { $_.AntiVirusRange } | ForEach-Object { $_.Filter }) | Sort-Object -Unique) -join '; ')
}
foreach ($k in $summary.Keys) {
    Write-Host ('  {0,-36} = {1}' -f $k, $summary[$k])
}
Write-Host ''
Write-Host ('  ログファイル / log file: {0}' -f $LogPath) -ForegroundColor Cyan
Write-Host ('  総所要時間 / total elapsed: {0} 秒' -f (Fmt $globalSw.Elapsed.TotalSeconds 1)) -ForegroundColor Cyan
if ($stopped) { Write-Warn2 '打ち切り時間に達したため、一部の計測はスキップされています。' }

if ($script:Transcript) { try { Stop-Transcript | Out-Null } catch { } }
