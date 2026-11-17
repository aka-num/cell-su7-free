# Publishes ONLY the reviewed, clean project directory. No GitHub tokens are hardcoded.
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
if (-not (Get-Command git -ErrorAction SilentlyContinue)) { throw 'Git is required: https://git-scm.com/downloads' }
if (-not (Get-Command gh -ErrorAction SilentlyContinue)) { throw 'GitHub CLI is required. Install: winget install --id GitHub.cli -e' }
& gh auth status *> $null
if ($LASTEXITCODE -ne 0) { throw 'GitHub login required: gh auth login' }
$account = (& gh api user --jq .login).Trim()
if ($LASTEXITCODE -ne 0 -or $account -ne 'aka-num') { throw "Authenticated as '$account' but target owner is aka-num. Change GitHub account first." }
$repo = 'aka-num/cell-su7-free'
& gh repo view $repo *> $null
if ($LASTEXITCODE -eq 0) { throw "Repository $repo already exists. Refusing to overwrite an existing repository." }
if (-not (Test-Path '.git')) { & git init -b main; if ($LASTEXITCODE -ne 0) { throw 'git init failed' } }
if (-not (& git config user.name)) { throw 'Set your git name first: git config --global user.name "Your Name"' }
if (-not (& git config user.email)) { throw 'Set your git email first: git config --global user.email "you@example.com"' }
& git add .
if ($LASTEXITCODE -ne 0) { throw 'git add failed' }
& git commit -m 'Initial public release: cell-su7-free v4.2.1'
if ($LASTEXITCODE -ne 0) { throw 'git commit failed' }
& gh repo create $repo --public --source . --remote origin --push --description 'Free local-first editable scientific figure vector reconstruction'
if ($LASTEXITCODE -ne 0) { throw 'GitHub repository creation or push failed; inspect Git state before retrying' }
Write-Host "Published: https://github.com/$repo"
