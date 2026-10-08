from pathlib import Path
import os
import subprocess


def test_actual_tracked_copy_excludes_only_data_and_keeps_source_modules_and_python_runtime(tmp_path):
    source, target = tmp_path / 'source', tmp_path / 'package'
    source.mkdir()
    excluded = ['runtime/logs/app.log', 'runtime/backups/manual.txt', 'runtime/..rebuild-data-recovery/snapshots/manual/data', 'runtime/server/logs/app.log', 'runtime/users/a/logs/app.log', 'runtime/users/a/..rebuild-data-recovery/snapshots/auto/data', 'runtime/shared/a/backups/data']
    kept = ['src/backend/api/access_log.py', 'src/logs/module.py', 'src/backup.py', 'runtime/python.exe', 'runtime/Lib/site-packages/module.py', 'README.md']
    for name in excluded + kept:
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('synthetic')
    subprocess.run(['git','init',str(source)],check=True,capture_output=True)
    subprocess.run(['git','-C',str(source),'add','.'],check=True,capture_output=True)
    script = Path(os.environ.get('T14_PACKAGE_SCRIPT', 'tools/build_release.ps1')).resolve()
    command = r"$ast=[System.Management.Automation.Language.Parser]::ParseFile($args[0],[ref]$null,[ref]$null); $functions=$ast.FindAll({param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -in @('Copy-GitTrackedFiles','Ensure-Directory')},$true); foreach($f in $functions){ Invoke-Expression $f.Extent.Text }; $RepoRoot=$args[1]; Copy-GitTrackedFiles -DestinationRoot $args[2]"
    harness = tmp_path / 'copy.ps1'
    harness.write_text('param($Script,$Source,$Target)\n' + command.replace('$args[0]', '$Script').replace('$args[1]', '$Source').replace('$args[2]', '$Target'))
    subprocess.run(['pwsh','-NoProfile','-File',str(harness),str(script),str(source),str(target)],check=True,capture_output=True,text=True)
    for name in excluded:
        assert not (target/name).exists(), name
    for name in kept:
        assert (target/name).read_text() == 'synthetic', name
    assert 'Copy-DirectoryIfExists -Source (Join-Path $Variant.BuildRoot "runtime") -Destination (Join-Path $Variant.PackageRoot "runtime")' in script.read_text()
