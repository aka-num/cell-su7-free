# Publishing to GitHub

This source archive is prepared for publishing as `aka-num/cell-su7-free`. It deliberately does not contain the uploaded biomedical source images or API credentials.

**One-time prerequisite:** Install [GitHub CLI](https://cli.github.com/) (`winget install --id GitHub.cli`) and [Git](https://git-scm.com/), then run `gh auth login` on your own computer. GitHub login happens locally, never by sharing credentials in chat.

From PowerShell in this directory:

```powershell
.\publish-to-github.ps1
```

The script creates the **public** repository on GitHub using your authenticated GitHub CLI session, commits this sanitised source tree, and pushes to `main`. It does not overwrite or force-push to an existing remote repository.

Before publishing, run `python -m pytest -q` if the test dependencies are installed. Review the `LICENSE`, example images, and README. External service use is optional and may be charged.
