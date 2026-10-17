using System.ComponentModel;
using ChangeLearn.Host.Services;
using ModelContextProtocol.Server;

namespace ChangeLearn.Host.Tools;

[McpServerToolType]
public sealed class ChangeLearnTools(
    GitLineageService lineage,
    DeckService deck,
    ChangeSummarizer summarizer,
    DecisionLedger ledger)
{
    [McpServerTool(Name = "analyze_changes")]
    [Description("Token-efficient analysis of a change window (commits, staged, working, untracked). Returns compact JSON: totals, commits, per-area churn, ranked files, flags.")]
    public Task<string> AnalyzeChanges(
        [Description("Base ref (default: merge-base with the default branch)")] string? fromRef = null,
        [Description("Target ref (default HEAD)")] string? toRef = null,
        [Description("Include staged changes")] bool staged = false,
        [Description("Include unstaged working-tree changes")] bool working = false,
        [Description("Include untracked files")] bool untracked = false,
        [Description("Exclude the commit range entirely")] bool noCommits = false,
        CancellationToken ct = default) =>
        lineage.SummaryAsync(new ChangeWindow(fromRef, toRef, staged, working, untracked, noCommits), ct);

    [McpServerTool(Name = "get_change_config")]
    [Description("Resolve the repo-root .change-learn.json config (focus, ignore, areas, highlight, context_files, deck).")]
    public Task<string> GetChangeConfig(CancellationToken ct = default) => lineage.ConfigAsync(ct);

    [McpServerTool(Name = "get_file_diffs")]
    [Description("Write per-file patches for specific paths to temp files and return a manifest. Use after analyze_changes for the few files that need line-level reading.")]
    public Task<string> GetFileDiffs(
        [Description("Repo-relative paths to diff")] string[] paths,
        [Description("Base ref (default: merge-base with the default branch)")] string? fromRef = null,
        [Description("Target ref (default HEAD)")] string? toRef = null,
        [Description("Include staged changes")] bool staged = false,
        [Description("Include unstaged working-tree changes")] bool working = false,
        CancellationToken ct = default) =>
        lineage.DiffAsync(new ChangeWindow(fromRef, toRef, staged, working, false, false), paths, ct);

    [McpServerTool(Name = "summarize_changes")]
    [Description("AI-narrated summary of a change window, high-level first then progressively lower. Uses the Squad agent team when available, with a deterministic fallback.")]
    public async Task<string> SummarizeChanges(
        [Description("Base ref (default: merge-base with the default branch)")] string? fromRef = null,
        [Description("Target ref (default HEAD)")] string? toRef = null,
        [Description("Include staged changes")] bool staged = false,
        [Description("Include unstaged working-tree changes")] bool working = false,
        [Description("Include untracked files")] bool untracked = false,
        [Description("Who the summary is for, e.g. 'new team member'")] string? audience = null,
        CancellationToken ct = default)
    {
        var analysis = await lineage.SummaryAsync(
            new ChangeWindow(fromRef, toRef, staged, working, untracked), ct);
        return await summarizer.SummarizeAsync(analysis, audience, ct);
    }

    [McpServerTool(Name = "generate_deck")]
    [Description("Scaffold a Marp slide deck (with Mermaid diagrams and ai-fill markers) for a change window. Returns JSON with the output path and slide count.")]
    public async Task<string> GenerateDeck(
        [Description("Absolute output path for the deck markdown")] string outputPath,
        [Description("Deck title")] string? title = null,
        [Description("Marp theme name")] string? theme = null,
        [Description("Base ref (default: merge-base with the default branch)")] string? fromRef = null,
        [Description("Target ref (default HEAD)")] string? toRef = null,
        [Description("Include staged changes")] bool staged = false,
        [Description("Include unstaged working-tree changes")] bool working = false,
        [Description("Include untracked files")] bool untracked = false,
        CancellationToken ct = default)
    {
        var analysis = await lineage.SummaryAsync(
            new ChangeWindow(fromRef, toRef, staged, working, untracked), ct);
        return await deck.ScaffoldAsync(analysis, outputPath, title, theme, ct);
    }

    [McpServerTool(Name = "export_deck_pdf")]
    [Description("Export a Marp deck to PDF (pre-renders Mermaid blocks to SVG). Requires Node.js/npx on the host.")]
    public Task<string> ExportDeckPdf(
        [Description("Path to the deck markdown")] string deckPath,
        [Description("Output path (default: deck path with .pdf)")] string? outputPath = null,
        CancellationToken ct = default) =>
        deck.ExportAsync(deckPath, outputPath, "pdf", ct);

    [McpServerTool(Name = "record_decision")]
    [Description("Append a decision to the Squad team's append-only decisions.md ledger.")]
    public async Task<string> RecordDecision(
        [Description("Short decision title")] string title,
        [Description("Decision detail and rationale")] string detail,
        [Description("Who made the decision")] string author = "change-learn",
        CancellationToken ct = default)
    {
        var d = await ledger.RecordAsync(author, title, detail, ct);
        return $"Recorded at {d.At:O}: {d.Title}";
    }

    [McpServerTool(Name = "list_decisions")]
    [Description("List recent decision titles from the Squad team's decisions.md ledger.")]
    public async Task<string> ListDecisions(
        [Description("Maximum entries to return")] int max = 20,
        CancellationToken ct = default)
    {
        var entries = await ledger.ListAsync(max, ct);
        return entries.Count == 0 ? "No decisions recorded yet." : string.Join('\n', entries);
    }
}
