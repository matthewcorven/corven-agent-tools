using System.Text;

namespace ChangeLearn.Host.Services;

public sealed record Decision(DateTimeOffset At, string Author, string Title, string Detail);

/// <summary>
/// Append-only decision ledger persisted to the Squad team's decisions.md,
/// mirroring Squad's own convention so humans and agents share one record.
/// </summary>
public sealed class DecisionLedger(IConfiguration config)
{
    private readonly SemaphoreSlim _gate = new(1, 1);

    private string LedgerPath
    {
        get
        {
            var squadDir = config["ChangeLearn:SquadFolderPath"];
            if (string.IsNullOrWhiteSpace(squadDir))
                squadDir = Path.Combine(AppContext.BaseDirectory, "team", ".squad");
            return Path.Combine(squadDir, "decisions.md");
        }
    }

    public async Task<Decision> RecordAsync(string author, string title, string detail, CancellationToken ct = default)
    {
        var decision = new Decision(DateTimeOffset.UtcNow, author, title, detail);
        await _gate.WaitAsync(ct);
        try
        {
            Directory.CreateDirectory(Path.GetDirectoryName(LedgerPath)!);
            var entry = new StringBuilder()
                .AppendLine()
                .AppendLine($"## {decision.At:yyyy-MM-dd HH:mm:ss}Z — {title}")
                .AppendLine()
                .AppendLine($"- **By** {author}")
                .AppendLine($"- {detail.ReplaceLineEndings(" ")}")
                .ToString();
            await File.AppendAllTextAsync(LedgerPath, entry, ct);
        }
        finally
        {
            _gate.Release();
        }
        return decision;
    }

    public async Task<IReadOnlyList<string>> ListAsync(int max = 20, CancellationToken ct = default)
    {
        if (!File.Exists(LedgerPath)) return [];
        var lines = await File.ReadAllLinesAsync(LedgerPath, ct);
        return lines.Where(l => l.StartsWith("## ", StringComparison.Ordinal))
                    .TakeLast(max)
                    .ToList();
    }
}
