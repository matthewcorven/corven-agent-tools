using ChangeLearn.Host.Components;
using ChangeLearn.Host.Services;
using Squad.Agents.AI;

// Dual transport:
//   default  -> Blazor Server UI + MCP over streamable HTTP at /mcp (port 5190)
//   --stdio  -> headless MCP over stdio, launched on demand by AI harnesses
//               (e.g. via plugins/change-learn/scripts/mcp-stdio.sh)
if (args.Contains("--stdio"))
{
    var stdioBuilder = Host.CreateApplicationBuilder(new HostApplicationBuilderSettings
    {
        Args = args,
        // Load appsettings.json from the host's output directory rather than
        // the launch directory (the harness starts us in the user's repo).
        ContentRootPath = AppContext.BaseDirectory,
    });

    // stdout carries the MCP protocol; every log line must go to stderr.
    stdioBuilder.Logging.ClearProviders();
    stdioBuilder.Logging.AddConsole(o => o.LogToStandardErrorThreshold = LogLevel.Trace);

    AddChangeLearnServices(stdioBuilder.Services, stdioBuilder.Configuration);

    stdioBuilder.Services.AddMcpServer()
        .WithStdioServerTransport()
        .WithToolsFromAssembly();

    stdioBuilder.Build().Run();
    return;
}

var builder = WebApplication.CreateBuilder(args);

// Blazor Server UI
builder.Services.AddRazorComponents()
    .AddInteractiveServerComponents();

AddChangeLearnServices(builder.Services, builder.Configuration);

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

// Shared registrations for both transports.
static void AddChangeLearnServices(IServiceCollection services, IConfiguration configuration)
{
    // Deterministic services (always available, no AI runtime required)
    services.AddSingleton<GitLineageService>();
    services.AddSingleton<DeckService>();
    services.AddSingleton<DecisionLedger>();
    services.AddSingleton<ChangeSummarizer>();

    // Squad agent team over the GitHub Copilot SDK (coordinator + lineage-analyst,
    // diagrammer, deck-author from team/.squad). Registration is declarative; the
    // Copilot CLI is only contacted when an agent session starts, and
    // ChangeSummarizer degrades to its deterministic fallback if that fails.
    var squadFolder = configuration["ChangeLearn:SquadFolderPath"];
    if (string.IsNullOrWhiteSpace(squadFolder))
        squadFolder = Path.Combine(AppContext.BaseDirectory, "team", ".squad");
    if (Directory.Exists(squadFolder))
    {
        var repoCwd = configuration["ChangeLearn:RepoPath"];
        services.AddSquadAgent(options =>
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
}
