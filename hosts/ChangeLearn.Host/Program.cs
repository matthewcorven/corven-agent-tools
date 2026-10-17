using ChangeLearn.Host.Components;
using ChangeLearn.Host.Services;
using Squad.Agents.AI;

var builder = WebApplication.CreateBuilder(args);

// Blazor Server UI
builder.Services.AddRazorComponents()
    .AddInteractiveServerComponents();

// Deterministic services (always available, no AI runtime required)
builder.Services.AddSingleton<GitLineageService>();
builder.Services.AddSingleton<DeckService>();
builder.Services.AddSingleton<DecisionLedger>();
builder.Services.AddSingleton<ChangeSummarizer>();

// Squad agent team over the GitHub Copilot SDK (coordinator + lineage-analyst,
// diagrammer, deck-author from team/.squad). Registration is declarative; the
// Copilot CLI is only contacted when an agent session starts, and
// ChangeSummarizer degrades to its deterministic fallback if that fails.
var squadFolder = builder.Configuration["ChangeLearn:SquadFolderPath"];
if (string.IsNullOrWhiteSpace(squadFolder))
    squadFolder = Path.Combine(AppContext.BaseDirectory, "team", ".squad");
if (Directory.Exists(squadFolder))
{
    var repoCwd = builder.Configuration["ChangeLearn:RepoPath"];
    builder.Services.AddSquadAgent(options =>
    {
        options.SquadFolderPath = squadFolder;
        options.Cwd = string.IsNullOrWhiteSpace(repoCwd)
            ? Directory.GetCurrentDirectory()
            : repoCwd;
        options.Instructions =
            "You are the change-learn team coordinator. Route lineage analysis to " +
            "lineage-analyst, diagram authoring to diagrammer, and slide narration to " +
            "deck-author. Always narrate high-level first, then progressively lower. " +
            "Record notable routing choices in decisions.md.";
    });
}

// MCP server over streamable HTTP at /mcp
builder.Services.AddMcpServer()
    .WithHttpTransport()
    .WithToolsFromAssembly();

var app = builder.Build();

app.UseStaticFiles();
app.UseAntiforgery();

app.MapMcp("/mcp");
app.MapRazorComponents<App>()
    .AddInteractiveServerRenderMode();

app.Run();
