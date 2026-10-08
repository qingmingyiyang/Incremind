param([Parameter(Mandatory = $true)][string]$ImagePath)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
Add-Type -AssemblyName System.Runtime.WindowsRuntime

function Wait-WinRtOperation {
    param(
        [Parameter(Mandatory = $true)][object]$Operation,
        [Parameter(Mandatory = $true)][Type]$ResultType
    )
    $asTask = [System.WindowsRuntimeSystemExtensions].GetMethods() |
        Where-Object {
            $_.Name -eq "AsTask" -and $_.IsGenericMethod -and $_.GetParameters().Count -eq 1
        } |
        Select-Object -First 1
    if ($null -eq $asTask) { throw "Windows Runtime task bridge is unavailable" }
    $task = $asTask.MakeGenericMethod($ResultType).Invoke($null, @($Operation))
    $task.GetAwaiter().GetResult()
}

$resolvedPath = [System.IO.Path]::GetFullPath($ImagePath)
if (-not [System.IO.File]::Exists($resolvedPath)) { throw "authorized image does not exist" }

$storageFileType = [Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime]
$randomAccessStreamType = [Windows.Storage.Streams.IRandomAccessStream, Windows.Storage.Streams, ContentType = WindowsRuntime]
$bitmapDecoderType = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Foundation, ContentType = WindowsRuntime]
$softwareBitmapType = [Windows.Graphics.Imaging.SoftwareBitmap, Windows.Foundation, ContentType = WindowsRuntime]
$ocrResultType = [Windows.Media.Ocr.OcrResult, Windows.Foundation, ContentType = WindowsRuntime]
$ocrEngineType = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime]

$file = Wait-WinRtOperation ([Windows.Storage.StorageFile]::GetFileFromPathAsync($resolvedPath)) $storageFileType
$stream = Wait-WinRtOperation ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) $randomAccessStreamType
try {
    $decoder = Wait-WinRtOperation ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) $bitmapDecoderType
    $bitmap = Wait-WinRtOperation ($decoder.GetSoftwareBitmapAsync()) $softwareBitmapType
    try {
        $engine = $ocrEngineType::TryCreateFromUserProfileLanguages()
        if ($null -eq $engine) { throw "Windows OCR language pack is unavailable" }
        $result = Wait-WinRtOperation ($engine.RecognizeAsync($bitmap)) $ocrResultType
        $text = [string]$result.Text
        if ([string]::IsNullOrWhiteSpace($text)) { throw "Windows OCR returned no text" }
        [Console]::Out.WriteLine($text.Trim())
    }
    finally {
        if ($null -ne $bitmap) { $bitmap.Dispose() }
    }
}
finally {
    $stream.Dispose()
}
