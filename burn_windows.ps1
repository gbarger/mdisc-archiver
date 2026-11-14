<#
  Burns discs with the Windows built-in IMAPI2 service. Called by burner.py; prints one JSON object per line.

  -Action list                                          list burner drives
  -Action burn  -Drive <id> -Manifest <tsv> -IndexDir <dir> -Label <name>
  -Action iso   -Manifest <tsv> -IndexDir <dir> -Label <name> -Out <file.iso>
  -Action eject -Drive <id>                             eject, then try to close the tray

  The manifest has one line per file: <path on disc>\t<source file path>
#>
param(
    [Parameter(Mandatory = $true)][ValidateSet('list', 'burn', 'iso', 'eject')][string]$Action,
    [string]$Drive,
    [string]$Manifest,
    [string]$IndexDir,
    [string]$Label,
    [string]$Out
)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false

function Emit($obj) {
    [Console]::Out.WriteLine(($obj | ConvertTo-Json -Compress))
    [Console]::Out.Flush()
}

Add-Type -TypeDefinition @'
using System;
using System.IO;
using System.Runtime.InteropServices;
using System.Runtime.InteropServices.ComTypes;

public static class MDiscIO {
    [DllImport("shlwapi.dll", CharSet = CharSet.Unicode, PreserveSig = false)]
    static extern void SHCreateStreamOnFileEx(string pszFile, uint grfMode, uint dwAttributes,
        bool fCreate, IStream pstmTemplate, out IStream ppstm);

    // Opens a file as a COM IStream without reading it into memory (STGM_READ | STGM_SHARE_DENY_NONE).
    public static object OpenRead(string path) {
        IStream s;
        SHCreateStreamOnFileEx(path, 0x40, 0x80, false, null, out s);
        return s;
    }

    // Copies an IMAPI image stream to a file, printing progress as JSON lines.
    public static void SaveStream(object comStream, string path) {
        IStream s = (IStream)comStream;
        System.Runtime.InteropServices.ComTypes.STATSTG st;
        s.Stat(out st, 1);
        long total = st.cbSize, done = 0;
        int lastPct = -1;
        byte[] buf = new byte[4 * 1024 * 1024];
        IntPtr pRead = Marshal.AllocHGlobal(sizeof(int));
        try {
            using (FileStream fs = File.Create(path)) {
                while (true) {
                    s.Read(buf, buf.Length, pRead);
                    int n = Marshal.ReadInt32(pRead);
                    if (n <= 0) break;
                    fs.Write(buf, 0, n);
                    done += n;
                    int pct = total > 0 ? (int)(done * 100 / total) : 0;
                    if (pct != lastPct) {
                        lastPct = pct;
                        Console.Out.WriteLine("{\"type\":\"progress\",\"pct\":" + pct + "}");
                        Console.Out.Flush();
                    }
                }
            }
        } finally {
            Marshal.FreeHGlobal(pRead);
        }
    }
}
'@

function Get-Recorder($id) {
    $r = New-Object -ComObject IMAPI2.MsftDiscRecorder2
    $r.InitializeDiscRecorder($id)
    return $r
}

function New-Image($recorder) {
    $fs = New-Object -ComObject IMAPI2FS.MsftFileSystemImage
    if ($recorder) { $fs.ChooseImageDefaults($recorder) } else { $fs.FreeMediaBlocks = 0 }  # 0 = no size limit
    $fs.FileSystemsToCreate = 4   # UDF only (handles files over 4 GB and long names)
    try { $fs.UDFRevision = 0x250 } catch { Emit @{ type = 'log'; msg = 'UDF 2.50 not available; using default UDF revision' } }
    $fs.VolumeName = $Label

    $root = $fs.Root
    $root.AddTree($IndexDir, $true)    # adds the _index folder
    $made = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
    $lines = [IO.File]::ReadAllLines($Manifest, [Text.Encoding]::UTF8)
    $total = $lines.Count
    $i = 0
    foreach ($line in $lines) {
        if (-not $line) { continue }
        $parts = $line.Split("`t")
        $dest = $parts[0]; $src = $parts[1]

        # Create parent folders top-down.
        $segs = $dest.Split('\')
        $path = ''
        for ($k = 0; $k -lt $segs.Count - 1; $k++) {
            $path = if ($path) { $path + '\' + $segs[$k] } else { $segs[$k] }
            if ($made.Add($path)) { $root.AddDirectory($path) }
        }

        try {
            $root.AddFile($dest, [MDiscIO]::OpenRead($src))
        } catch {
            throw "Could not add '$src': $($_.Exception.Message)"
        }
        $i++
        if ($i % 250 -eq 0 -or $i -eq $total) {
            Emit @{ type = 'stage'; done = $i; total = $total }
        }
    }
    return $fs
}

try {
    switch ($Action) {
        'list' {
            $master = New-Object -ComObject IMAPI2.MsftDiscMaster2
            if (-not $master.IsSupportedEnvironment) { throw 'No optical drives found (IMAPI2 reports an unsupported environment).' }
            for ($n = 0; $n -lt $master.Count; $n++) {
                $id = $master.Item($n)
                $r = Get-Recorder $id
                $vols = @($r.VolumePathNames)
                Emit @{ type = 'drive'; id = $id; volume = $(if ($vols.Count) { [string]$vols[0] } else { '' });
                        name = ("{0} {1}" -f $r.VendorId.Trim(), $r.ProductId.Trim()) }
            }
        }
        'burn' {
            $recorder = Get-Recorder $Drive
            $fmt = New-Object -ComObject IMAPI2.MsftDiscFormat2Data
            if (-not $fmt.IsRecorderSupported($recorder)) { throw 'This drive is not supported for data burning by IMAPI2.' }
            if (-not $fmt.IsCurrentMediaSupported($recorder)) { throw 'No writable disc in the drive, or IMAPI2 does not support this disc type. Try "Save ISO" and burn it with ImgBurn.' }
            $fmt.Recorder = $recorder
            $fmt.ClientName = 'MDiscArchiver'
            if (-not $fmt.MediaHeuristicallyBlank) { throw 'The disc in the drive is not blank.' }
            $free = $fmt.FreeSectorsOnMedia
            Emit @{ type = 'log'; msg = ("Media type {0}, {1:N1} GB free" -f $fmt.CurrentPhysicalMediaType, ($free * 2048 / 1e9)) }

            Emit @{ type = 'phase'; msg = 'Building disc image...' }
            $fs = New-Image $recorder
            $result = $fs.CreateResultImage()
            if ($result.TotalBlocks -gt $free) {
                throw ("Image is {0:N1} GB but the disc has {1:N1} GB free." -f ($result.TotalBlocks * 2048 / 1e9), ($free * 2048 / 1e9))
            }
            $fmt.ForceMediaToBeClosed = $true
            Emit @{ type = 'phase'; spin = $true; msg = ("Writing {0:N1} GB to disc. This can take an hour or more; do not unplug the drive." -f ($result.TotalBlocks * 2048 / 1e9)) }
            $fmt.Write($result.ImageStream)
            Emit @{ type = 'log'; msg = 'Write finished.' }
        }
        'iso' {
            Emit @{ type = 'phase'; msg = 'Building disc image...' }
            $fs = New-Image $null
            $result = $fs.CreateResultImage()
            Emit @{ type = 'phase'; msg = "Writing ISO to $Out" }
            [MDiscIO]::SaveStream($result.ImageStream, $Out)
        }
        'eject' {
            $recorder = Get-Recorder $Drive
            $recorder.EjectMedia()
            Start-Sleep -Seconds 2
            try { $recorder.CloseTray(); Emit @{ type = 'log'; msg = 'Tray closed.' } }
            catch { Emit @{ type = 'log'; msg = 'Drive cannot close its tray by itself. Please reinsert the disc.' } }
        }
    }
    Emit @{ type = 'done' }
    exit 0
} catch {
    $msg = $_.Exception.Message
    $inner = $_.Exception
    while ($inner.InnerException) { $inner = $inner.InnerException }
    if ($inner -is [System.Runtime.InteropServices.COMException]) { $msg = "{0} (IMAPI error 0x{1:X8})" -f $msg, $inner.HResult }
    Emit @{ type = 'error'; msg = $msg }
    exit 1
}
