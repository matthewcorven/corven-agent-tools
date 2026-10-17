using System.Text;
using System.Text.Json;
using Microsoft.Agents.AI;

namespace ChangeLearn.Host.Services;

/// <summary>
/// Narrates a change window high-level-first. Prefers the Squad agent team
/// (lineage-analyst → diagrammer → deck-author, orchestrated by the Squad
/// coordinator over the GitHub Copilot SDK); falls back to a deterministic
/// template when the Copilot CLI runtime is unavailable so the MCP host
/// always answers.
/// </summary>
public sealed class ChangeSummarizer(IServiceProvider services, ILogger<ChangeSummarizer> logger)
{
    public async Task<string> SummarizeAsync(string analysisJson, string? audience, CancellationToken ct = default)
    {
        var agentResult = await TrySquadAsync(analysisJson, audience, ct);
        if (agentResult.Summary is not null) return agentResult.Summary;
        return DeterministicSummary(analysisJson, audience, agentResult.Warning);
    }

    private async Task<SquadSummaryResult> TrySquadAsync(string analysisJson, string? audience, CancellationToken ct)
    {
        try
        {
            var agent = services.GetService<AIAgent>();
            if (agent is null)
                return new(null, "No AI agent is configured for the selected provider.");

            var prompt = new StringBuilder()
                .AppendLine("Summarize this source-control change window for a reader seeing it for the first time.")
                .AppendLine("Order strictly high-level first: intent, then shape (areas/flows), then per-area design, then notable file-level details.")
                .AppendLine($"Audience: {audience ?? "engineers new to the change"}.")
                .AppendLine("Analysis JSON from lineage.py follows:")
                .AppendLine(analysisJson)
                .ToString();

            var session = await agent.CreateSessionAsync(cancellationToken: ct);
            var response = await agent.RunAsync(prompt, session, cancellationToken: ct);
            var text = response.Text;
            return string.IsNullOrWhiteSpace(text)
                ? new(null, "The configured AI agent returned an empty response.")
                : new(text, null);
        }
        catch (Exception ex)
        {
            logger.LogWarning(ex, "Squad agent unavailable; using deterministic summary.");
            return new(null, SummarizationWarning(ex));
        }
    }

    private static string SummarizationWarning(Exception ex) =>
        ex.ToString().Contains("unsupported argument '--allow-all'", StringComparison.Ordinal)
            ? "The installed Copilot CLI is incompatible with the GitHub Copilot SDK " +
              "(it rejects '--allow-all'). Update the Copilot CLI, then retry for an AI-written summary."
            : "The configured AI runtime was unavailable. Inspect the Change Learn host stderr for details.";

    /// <summary>Rigid, template-driven narrative derived only from the analysis JSON.</summary>
    internal static string DeterministicSummary(string analysisJson, string? audience, string? warning = null)
    {
        using var doc = JsonDocument.Parse(analysisJson);
        var root = doc.RootElement;
        var totals = root.GetProperty("totals");
        var sb = new StringBuilder();

        sb.AppendLine("# Change summary (deterministic fallback)");
        sb.AppendLine();
        sb.AppendLine($"_Audience: {audience ?? "engineers new to the change"}. Squad agent team was unavailable; this is a template-driven narrative._");
        if (!string.IsNullOrWhiteSpace(warning))
        {
            sb.AppendLine();
            sb.AppendLine($"> **Warning:** {warning}");
        }
        sb.AppendLine();
        sb.AppendLine("## Intent (high level)");
        sb.AppendLine($"- Window `{root.GetProperty("spec").GetRawText()}` touches " +
                      $"{totals.GetProperty("files").GetInt32()} files " +
                      $"(+{totals.GetProperty("adds").GetInt32()} / -{totals.GetProperty("dels").GetInt32()}) " +
                      $"across {totals.GetProperty("commits").GetInt32()} commits.");

        if (root.TryGetProperty("commits", out var commits) && commits.GetArrayLength() > 0)
        {
            sb.AppendLine();
            sb.AppendLine("## Story told by commits");
            foreach (var c in commits.EnumerateArray().Take(10))
                sb.AppendLine($"- `{c.GetProperty("sha").GetString()}` {c.GetProperty("s").GetString()}");
        }

        if (root.TryGetProperty("areas", out var areas) && areas.GetArrayLength() > 0)
        {
            sb.AppendLine();
            sb.AppendLine("## Shape — where the change landed");
            foreach (var a in areas.EnumerateArray())
                sb.AppendLine($"- **{a.GetProperty("area").GetString()}** — {a.GetProperty("files").GetInt32()} files " +
                              $"(+{a.GetProperty("adds").GetInt32()} / -{a.GetProperty("dels").GetInt32()})");
        }

        if (root.TryGetProperty("files", out var files) && files.GetArrayLength() > 0)
        {
            sb.AppendLine();
            sb.AppendLine("## Detail — files to read first");
            foreach (var f in files.EnumerateArray().Take(8))
                sb.AppendLine($"- `{f.GetProperty("p").GetString()}` ({f.GetProperty("st").GetString()}, " +
                              $"+{f.GetProperty("a").GetInt32()} / -{f.GetProperty("d").GetInt32()})");
        }

        if (root.TryGetProperty("flags", out var flags) && flags.GetArrayLength() > 0)
        {
            sb.AppendLine();
            sb.AppendLine("## Flags worth a look");
            foreach (var fl in flags.EnumerateArray().Take(10))
                sb.AppendLine($"- `{fl.GetProperty("p").GetString()}:{fl.GetProperty("line").GetInt32()}` " +
                              $"[{fl.GetProperty("kind").GetString()}] {fl.GetProperty("txt").GetString()}");
        }

        return sb.ToString();
    }

    private sealed record SquadSummaryResult(string? Summary, string? Warning);
}
