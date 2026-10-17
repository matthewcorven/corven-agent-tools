using System.Diagnostics;

namespace ChangeLearn.Host.Services;

/// <summary>
/// Wraps the skill's deck.py for scaffolding Marp decks and exporting PDFs.
/// </summary>
public sealed class DeckService(IConfiguration config, GitLineageService lineage, ILogger<DeckService> logger)
{
    private string DeckScript
    {
        get
        {
            var configured = config["ChangeLearn:SkillScriptsPath"];
            var scripts = string.IsNullOrWhiteSpace(configured)
                ? "plugins/change-learn/skills/change-learn/scripts"
                : configured;
            var path = Path.IsPathRooted(scripts)
                ? scripts
                : Path.GetFullPath(Path.Combine(lineage.RepoPath, scripts));
            return Path.Combine(path, "deck.py");
        }
    }

    public async Task<string> ScaffoldAsync(string analysisJson, string outputPath, string? title, string? theme, CancellationToken ct = default)
    {
        var analysisFile = Path.Combine(Path.GetTempPath(), $"change-learn-analysis-{Guid.NewGuid():N}.json");
        await File.WriteAllTextAsync(analysisFile, analysisJson, ct);
        try
        {
            var args = new List<string> { "scaffold", "--analysis", analysisFile, "-o", outputPath };
            if (!string.IsNullOrWhiteSpace(title)) { args.Add("--title"); args.Add(title); }
            if (!string.IsNullOrWhiteSpace(theme)) { args.Add("--theme"); args.Add(theme); }
            return await RunAsync(args, ct);
        }
        finally
        {
            File.Delete(analysisFile);
        }
    }

    public Task<string> ExportAsync(string deckPath, string? outputPath, string format = "pdf", CancellationToken ct = default)
    {
        var args = new List<string> { "export", "--deck", deckPath, "--format", format };
        if (!string.IsNullOrWhiteSpace(outputPath)) { args.Add("-o"); args.Add(outputPath); }
        return RunAsync(args, ct);
    }

    private async Task<string> RunAsync(IReadOnlyList<string> args, CancellationToken ct)
    {
        if (!File.Exists(DeckScript))
            throw new FileNotFoundException($"deck.py not found at {DeckScript}; set ChangeLearn:SkillScriptsPath.");

        var psi = new ProcessStartInfo
        {
            FileName = string.IsNullOrWhiteSpace(config["ChangeLearn:PythonPath"]) ? "python3" : config["ChangeLearn:PythonPath"]!,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            WorkingDirectory = lineage.RepoPath,
        };
        psi.ArgumentList.Add(DeckScript);
        foreach (var a in args) psi.ArgumentList.Add(a);

        logger.LogDebug("Running deck.py {Args}", string.Join(' ', args));
        using var proc = Process.Start(psi)
            ?? throw new InvalidOperationException("Failed to start python3.");
        var readOut = proc.StandardOutput.ReadToEndAsync(ct);
        var readErr = proc.StandardError.ReadToEndAsync(ct);
        await proc.WaitForExitAsync(ct);
        var stdout = await readOut;
        var stderr = await readErr;

        if (proc.ExitCode != 0)
            throw new InvalidOperationException($"deck.py exited {proc.ExitCode}: {stderr}");
        return stdout.Trim();
    }
}
