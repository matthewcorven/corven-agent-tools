using System.Diagnostics;
using System.Text;

namespace ChangeLearn.Host.Services;

/// <summary>
/// Options describing a change window to analyze.
/// </summary>
public sealed record ChangeWindow(
    string? FromRef = null,
    string? ToRef = null,
    bool Staged = false,
    bool Working = false,
    bool Untracked = false,
    bool NoCommits = false);

/// <summary>
/// Shells out to the skill's lineage.py so the MCP host and the standalone
/// skill share a single source of truth for token-efficient analysis.
/// </summary>
public sealed class GitLineageService(IConfiguration config, ILogger<GitLineageService> logger)
{
    private string ScriptsPath => ResolvePath(
        Setting("SkillScriptsPath", "plugins/change-learn/skills/change-learn/scripts"));

    public string RepoPath => ResolvePath(Setting("RepoPath", "."));

    private string Setting(string key, string fallback)
    {
        var value = config[$"ChangeLearn:{key}"];
        return string.IsNullOrWhiteSpace(value) ? fallback : value;
    }

    private static string ResolvePath(string path) =>
        Path.IsPathRooted(path) ? path : Path.GetFullPath(Path.Combine(FindRepoRoot(), path));

    private static string FindRepoRoot()
    {
        // Walk up from the app base dir until a .git folder/file is found.
        var dir = new DirectoryInfo(AppContext.BaseDirectory);
        while (dir is not null
               && !Directory.Exists(Path.Combine(dir.FullName, ".git"))
               && !File.Exists(Path.Combine(dir.FullName, ".git")))
        {
            dir = dir.Parent;
        }
        return dir?.FullName ?? Directory.GetCurrentDirectory();
    }

    public Task<string> SummaryAsync(ChangeWindow window, CancellationToken ct = default) =>
        RunLineageAsync(BuildArgs("summary", window), ct);

    public Task<string> ConfigAsync(CancellationToken ct = default) =>
        RunLineageAsync(["config"], ct);

    public Task<string> DiffAsync(ChangeWindow window, IEnumerable<string> paths, CancellationToken ct = default) =>
        RunLineageAsync([.. BuildArgs("diff", window), .. paths], ct);

    private static List<string> BuildArgs(string command, ChangeWindow w)
    {
        var args = new List<string> { command };
        if (!string.IsNullOrWhiteSpace(w.FromRef)) { args.Add("--from"); args.Add(w.FromRef); }
        if (!string.IsNullOrWhiteSpace(w.ToRef)) { args.Add("--to"); args.Add(w.ToRef); }
        if (w.Staged) args.Add("--staged");
        if (w.Working) args.Add("--working");
        if (w.Untracked) args.Add("--untracked");
        if (w.NoCommits) args.Add("--no-commits");
        return args;
    }

    private async Task<string> RunLineageAsync(IReadOnlyList<string> args, CancellationToken ct)
    {
        var script = Path.Combine(ScriptsPath, "lineage.py");
        if (!File.Exists(script))
            throw new FileNotFoundException($"lineage.py not found at {script}; set ChangeLearn:SkillScriptsPath.");

        var psi = new ProcessStartInfo
        {
            FileName = config["ChangeLearn:PythonPath"] ?? "python3",
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            WorkingDirectory = RepoPath,
        };
        psi.ArgumentList.Add(script);
        psi.ArgumentList.Add("--repo");
        psi.ArgumentList.Add(RepoPath);
        foreach (var a in args) psi.ArgumentList.Add(a);

        logger.LogDebug("Running lineage.py {Args}", string.Join(' ', args));
        using var proc = Process.Start(psi)
            ?? throw new InvalidOperationException("Failed to start python3.");
        var readOut = proc.StandardOutput.ReadToEndAsync(ct);
        var readErr = proc.StandardError.ReadToEndAsync(ct);
        await proc.WaitForExitAsync(ct);
        var stdout = await readOut;
        var stderr = await readErr;

        if (proc.ExitCode != 0)
            throw new InvalidOperationException($"lineage.py exited {proc.ExitCode}: {stderr}");
        return stdout.Trim();
    }
}
