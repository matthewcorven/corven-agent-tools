using System.Text.Json;

namespace ChangeLearn.Host.Services;

/// <summary>
/// Model selection for the summarizer agent, read from the <c>"ai"</c> section of
/// the repo-root <c>.change-learn.json</c>. Resolved once at startup (declarative,
/// per the host's rigid-behavior charter). The API key itself never lives in the
/// file — only the name of the environment variable that holds it.
/// </summary>
public sealed record AiModelOptions
{
    public const string FileName = ".change-learn.json";

    /// <summary>
    /// "copilot" (GitHub Copilot SDK via Squad, the default) or any of
    /// "openai" / "openai-compatible" / "openrouter" for an OpenAI-compatible endpoint.
    /// </summary>
    public string Provider { get; init; } = "copilot";

    /// <summary>Model id, e.g. "gpt-6-luna" or "deepseek/deepseek-v4.1-flash". Empty = provider default.</summary>
    public string? Model { get; init; }

    /// <summary>Endpoint for OpenAI-compatible providers, e.g. "https://openrouter.ai/api/v1".</summary>
    public string? BaseUrl { get; init; }

    /// <summary>Name of the environment variable holding the API key (never the key itself).</summary>
    public string ApiKeyEnv { get; init; } = "OPENAI_API_KEY";

    public bool IsOpenAiCompatible => Provider is "openai" or "openai-compatible" or "openrouter";

    public static AiModelOptions Load(string repoPath)
    {
        var path = Path.Combine(repoPath, FileName);
        if (!File.Exists(path)) return new AiModelOptions();
        try
        {
            using var doc = JsonDocument.Parse(File.ReadAllText(path));
            if (!doc.RootElement.TryGetProperty("ai", out var ai) || ai.ValueKind != JsonValueKind.Object)
                return new AiModelOptions();

            string? Str(string name) =>
                ai.TryGetProperty(name, out var v) && v.ValueKind == JsonValueKind.String ? v.GetString() : null;

            return new AiModelOptions
            {
                Provider = Str("provider")?.Trim().ToLowerInvariant() is { Length: > 0 } p ? p : "copilot",
                Model = Str("model"),
                BaseUrl = Str("baseUrl"),
                ApiKeyEnv = Str("apiKeyEnv") is { Length: > 0 } e ? e : "OPENAI_API_KEY",
            };
        }
        catch (JsonException)
        {
            // Malformed config is surfaced to users by lineage.py as _config_error;
            // the summarizer simply stays on provider defaults.
            return new AiModelOptions();
        }
    }
}
